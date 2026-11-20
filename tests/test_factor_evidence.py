"""Read-only factor evidence diagnostics, Newey-West intervals, and boundary regressions."""

from __future__ import annotations

import copy
import json
import math

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from finance_quant.provenance import code_sha256
from finance_quant.storage import ResultStore
from finance_quant.web.factor_evidence import _newey_west_bartlett, evaluation_evidence
from support.fakes import make_app


def _make_evaluation(
    values: list[float | None],
    *,
    factor: str = "momentum",
    horizon: int = 10,
    end: str | None = None,
    with_exit_dates: bool = True,
    with_calendar: bool = True,
    factor_run_id: str | None = None,
):
    dates = pd.bdate_range("2025-01-02", periods=len(values) + horizon + 2).strftime("%Y-%m-%d").tolist()
    daily = []
    signal_coverage = []
    label_coverage = []
    for idx, val in enumerate(values):
        sig = dates[idx]
        entry = dates[idx + 1]
        exit_d = dates[idx + 1 + horizon] if with_exit_dates else None
        daily.append(
            {
                "date": sig,
                "entry_date": entry,
                "exit_date": exit_d,
                "observations": 120 if val is not None else 0,
                "ic": val,
                "rank_ic": val,
                "ic_exclusion": None if val is not None else "insufficient_cross_section",
            }
        )
        signal_coverage.append(
            {
                "signal_date": sig,
                "entry_date": entry,
                "scored_instruments": 120,
                "universe": 150,
            }
        )
        label_coverage.append(
            {
                "date": sig,
                "scored": 120,
                "valid_labels": 115 if val is not None else 0,
                "exclusions": {"non_trading_entry_or_exit": 5} if val is not None else {"outside_evaluation_end": 120},
            }
        )
    resolved_end = end if end is not None else (dates[len(values) - 1] if values else "2025-01-02")
    doc = {
        "metadata": {
            "id": "eval-fixture",
            "name": "因子证据夹具",
            "is_synthetic": True,
            "created_at": "2026-11-20T00:00:00Z",
            "start": dates[0],
            "end": resolved_end,
            "sample_size": 150,
            "data_version": "data-v1",
            "factor_run_id": factor_run_id or "",
            "evaluation": {"horizons": [horizon], "groups": 5, "min_cross_section": 10},
            "provenance": {},
        },
        "factors": {
            factor: {"horizons": {str(horizon): {"daily": daily}}},
            "composite": {"horizons": {str(horizon): {"daily": copy.deepcopy(daily)}}},
        },
        "labels": {
            str(horizon): {
                "coverage": label_coverage,
                "valid_observations": sum(r["valid_labels"] for r in label_coverage),
                "excluded_observations": 0,
            }
        },
        "correlations": {"method": "daily Spearman", "pairs": []},
    }
    if with_calendar:
        doc["signal_coverage"] = signal_coverage
    return doc, dates


def test_newey_west_middle_gap_exact_7_over_24():
    # Low-order math fixture: [1, 0.5, None, -0.5, -1], L=1 -> n=4, S=3.5, var=3.5/12=7/24.
    values = [1.0, 0.5, None, -0.5, -1.0]
    n = 4
    mean = 0.0
    z = [1.0, 0.5, 0.0, -0.5, -1.0]
    expected_s = sum(v * v for v in z) + 2.0 * 0.5 * sum(z[t] * z[t - 1] for t in range(1, len(z)))
    assert expected_s / (n * (n - 1)) == pytest.approx(7 / 24)
    # Verify on full evaluation_evidence when threshold is met (tile fixture or repeat with gap)
    res = _newey_west_bartlett(values, lag=1)
    assert res["n"] == 4
    assert res["mean"] == pytest.approx(mean)
    assert res["lag_pairs"] == 2
    assert res["variance_status"] == "insufficient_observations"

    # Repeat 6 times (n=24 >= 20) with explicit middle gaps so interval is computable and exact.
    tiled = values * 6
    z_tiled = [0.0 if v is None else v for v in tiled]
    m = len(z_tiled)
    s_tiled = sum(v * v for v in z_tiled) + 2.0 * 0.5 * sum(z_tiled[t] * z_tiled[t - 1] for t in range(1, m))
    n_tiled = 24
    expected_var = s_tiled / (n_tiled * (n_tiled - 1))
    res_tiled = _newey_west_bartlett(tiled, lag=1)
    assert res_tiled["variance_status"] == "ready"
    assert res_tiled["variance"] == pytest.approx(expected_var)
    assert res_tiled["se"] == pytest.approx(math.sqrt(expected_var))


def test_lag_zero_matches_sample_std_over_sqrt_n_and_shorter_lag_flagged(tmp_path):
    vals = [((-1) ** i) * 0.02 + 0.05 for i in range(30)]
    doc, dates = _make_evaluation(vals, factor="momentum", horizon=10)
    doc["metadata"]["end"] = dates[30 + 1 + 10]
    store = ResultStore(tmp_path / "runs")

    res_l0 = evaluation_evidence(store, doc, horizon=10, factor="momentum", lag=0)
    assert res_l0["status"] == "ready"
    assert res_l0["lag"]["is_shorter_than_default"] is True
    assert res_l0["lag"]["default_lag"] == 9
    expected_se = pd.Series(vals).std(ddof=1) / math.sqrt(len(vals))
    assert res_l0["rank_ic"]["se"] == pytest.approx(expected_se)
    assert res_l0["counts"]["lag_pairs"] == 0

    res_l9 = evaluation_evidence(store, doc, horizon=10, factor="momentum")
    assert res_l9["lag"]["lag"] == 9
    assert res_l9["lag"]["is_shorter_than_default"] is False
    assert res_l9["counts"]["lag_pairs"] > 0


def test_cutoff_boundary_inclusive_and_future_edits_do_not_change_result(tmp_path):
    vals = [0.04 + 0.01 * (i % 3) for i in range(30)]
    doc, dates = _make_evaluation(vals, factor="momentum", horizon=5)
    # Cutoff equals the 25th row's exit_date (index 24 -> exit index 24+6=30).
    cutoff = dates[30]
    store = ResultStore(tmp_path / "runs")
    before = evaluation_evidence(store, doc, horizon=5, factor="momentum", cutoff=cutoff, lag=2)
    assert before["status"] == "ready"
    assert before["counts"]["valid_rank_ic_dates"] == 25
    assert before["counts"]["unmatured_dates"] == 5
    assert before["range"]["latest_exit_date"] == cutoff

    # Mutate observations strictly after cutoff -> must have zero impact on before cutoff evidence.
    mutated = copy.deepcopy(doc)
    for row in mutated["factors"]["momentum"]["horizons"]["5"]["daily"][25:]:
        row["rank_ic"] = -0.99
        row["observations"] = 999
    after = evaluation_evidence(store, mutated, horizon=5, factor="momentum", cutoff=cutoff, lag=2)
    assert after["rank_ic"] == before["rank_ic"]
    assert after["counts"] == before["counts"]
    assert after["range"]["latest_exit_date"] == before["range"]["latest_exit_date"]


def test_missing_exit_date_zero_all_empty_and_missing_calendar_guards(tmp_path):
    store = ResultStore(tmp_path / "runs")

    # Old artifact missing exit_date -> never assumed matured
    no_exit, dates = _make_evaluation([0.05] * 25, horizon=10, with_exit_dates=False)
    res_no_exit = evaluation_evidence(store, no_exit, horizon=10, factor="momentum", cutoff=dates[-1])
    assert res_no_exit["status"] == "no_valid_observations"
    assert res_no_exit["counts"]["missing_exit_date_dates"] == 25
    assert res_no_exit["rank_ic"]["ci_low"] is None

    # Exact zero variance -> mean 0.0, interval null
    zeros, dates = _make_evaluation([0.0] * 25, horizon=1)
    res_zero = evaluation_evidence(store, zeros, horizon=1, factor="momentum", cutoff=dates[-1])
    assert res_zero["status"] == "undefined_variance"
    assert res_zero["rank_ic"]["mean"] == 0.0
    assert res_zero["rank_ic"]["se"] is None
    assert res_zero["rank_ic"]["ci_low"] is None

    # All None (e.g. unbound value) -> no_valid_observations, plus imputation projection from factor run
    factor_doc = {
        "metadata": {
            "id": "factors-1",
            "name": "因子批次",
            "is_synthetic": True,
            "created_at": "2026-11-20T00:00:00Z",
            "scheme": "equal",
            "as_of": dates[24],
            "weights": {"value": 1.0},
            "availability_by_date": {
                d: {
                    "universe": 150,
                    "eligible": 120,
                    "factors": {"value": {"valid": 0, "missing": 150, "imputed": 120, "missing_policy": "zero"}},
                }
                for d in dates[:25]
            },
        },
        "history": [],
    }
    store.publish("factors", "factors-1", factor_doc)
    empties, _ = _make_evaluation([None] * 25, factor="value", horizon=1, factor_run_id="factors-1")
    res_empty = evaluation_evidence(store, empties, horizon=1, factor="value", cutoff=dates[-1])
    assert res_empty["status"] == "no_valid_observations"
    assert res_empty["rank_ic"]["mean"] is None
    assert res_empty["rank_ic"]["ci_low"] is None
    assert res_empty["raw_availability"]["available"] is True
    assert res_empty["raw_availability"]["raw_valid_stock_days"] == 0
    assert res_empty["raw_availability"]["imputed_stock_days"] == 25 * 120
    assert res_empty["common_sample"]["available"] is False
    assert "未知" in res_empty["common_sample"]["label"]

    # Missing frozen calendar -> explicit error status, signal_dates & n unknown, never fabricates positions
    no_cal, _ = _make_evaluation([0.05] * 25, horizon=1, with_calendar=False)
    res_no_cal = evaluation_evidence(store, no_cal, horizon=1, factor="momentum", cutoff=dates[-1])
    assert res_no_cal["status"] == "missing_frozen_calendar"
    assert res_no_cal["counts"]["signal_dates"] is None
    assert res_no_cal["counts"]["valid_rank_ic_dates"] is None
    assert res_no_cal["counts"]["lag_pairs"] is None
    assert res_no_cal["raw_availability"]["total_signal_dates"] is None
    assert res_no_cal["rank_ic"]["ci_low"] is None

    # Two-day filter where day 1 has value and day 2 only has another factor -> matched 1/2, totals stay None
    two_day_eval, two_dates = _make_evaluation([None, None], factor="value", horizon=1, factor_run_id="factors-two-day")
    two_day_doc = {
        "metadata": {
            "id": "factors-two-day",
            "name": "两日部分缺失批次",
            "is_synthetic": True,
            "created_at": "2026-11-20T00:00:00Z",
            "scheme": "equal",
            "as_of": two_dates[1],
            "weights": {"value": 1.0},
            "availability_by_date": {
                two_dates[0]: {
                    "universe": 4,
                    "eligible": 4,
                    "factors": {"value": {"valid": 0, "missing": 4, "imputed": 4, "missing_policy": "zero"}},
                },
                two_dates[1]: {
                    "universe": 4,
                    "eligible": 4,
                    "factors": {"momentum": {"valid": 4, "missing": 0, "imputed": 0}},
                },
            },
        },
        "history": [],
    }
    store.publish("factors", "factors-two-day", two_day_doc)
    res_two = evaluation_evidence(
        store, two_day_eval, horizon=1, factor="value", start=two_dates[0], end=two_dates[1], cutoff=two_dates[-1]
    )
    assert res_two["raw_availability"]["coverage_status"] == "partial"
    assert res_two["raw_availability"]["matched_signal_dates"] == 1
    assert res_two["raw_availability"]["total_signal_dates"] == 2
    assert res_two["raw_availability"]["raw_valid_stock_days"] is None
    assert res_two["raw_availability"]["raw_missing_stock_days"] is None
    assert res_two["raw_availability"]["imputed_stock_days"] is None

    # Unsaved factor fields & partial dates in availability_by_date must stay None (never partial sums or zero)
    partial_factor_doc = {
        "metadata": {
            "id": "factors-partial",
            "name": "部分旧因子批次",
            "is_synthetic": True,
            "created_at": "2026-11-20T00:00:00Z",
            "scheme": "equal",
            "as_of": dates[0],
            "weights": {"value": 1.0},
            "availability_by_date": {
                dates[0]: {"universe": 4, "eligible": 4, "factors": {"value": {}}},
            },
        },
        "history": [],
    }
    store.publish("factors", "factors-partial", partial_factor_doc)
    partial_eval, _ = _make_evaluation([None] * 25, factor="value", horizon=1, factor_run_id="factors-partial")
    res_partial = evaluation_evidence(store, partial_eval, horizon=1, factor="value", cutoff=dates[-1])
    raw_p = res_partial["raw_availability"]
    assert raw_p["available"] is True
    assert raw_p["coverage_status"] == "partial"
    assert raw_p["matched_signal_dates"] == 1
    assert raw_p["total_signal_dates"] == 25
    assert raw_p["universe_stock_days"] is None
    assert raw_p["eligible_stock_days"] is None
    assert raw_p["raw_valid_stock_days"] is None
    assert raw_p["raw_missing_stock_days"] is None
    assert raw_p["imputed_stock_days"] is None

    # Invalid exit_date ("0") and reversed date ordering must be rejected, never included as matured
    bad_exit_doc, dates = _make_evaluation([0.05] * 25, horizon=5)
    bad_exit_doc["factors"]["momentum"]["horizons"]["5"]["daily"][0]["exit_date"] = "0"
    bad_exit_doc["factors"]["momentum"]["horizons"]["5"]["daily"][1]["exit_date"] = "2024-01-01"
    res_bad_exit = evaluation_evidence(store, bad_exit_doc, horizon=5, factor="momentum", cutoff=dates[-1])
    assert res_bad_exit["counts"]["invalid_exit_date_dates"] == 2
    assert res_bad_exit["counts"]["valid_rank_ic_dates"] == 23

    # Empty scoring_sample={} or unrecognized policy stays unknown; signal_date_common_v1 + 12-char sha256 accepted
    empty_cs_doc = copy.deepcopy(no_cal)
    empty_cs_doc["metadata"]["provenance"] = {"scoring_sample": {}}
    assert (
        evaluation_evidence(store, empty_cs_doc, horizon=1, factor="momentum", cutoff=dates[-1])["common_sample"][
            "available"
        ]
        is False
    )
    indep_cs_doc = copy.deepcopy(no_cal)
    indep_cs_doc["metadata"]["provenance"] = {"scoring_sample": {"policy": "independent", "sha256": "a1b2c3d4e5f6"}}
    assert (
        evaluation_evidence(store, indep_cs_doc, horizon=1, factor="momentum", cutoff=dates[-1])["common_sample"][
            "available"
        ]
        is False
    )
    valid_cs_doc = copy.deepcopy(no_cal)
    valid_cs_doc["metadata"]["provenance"] = {
        "scoring_sample": {"policy": "signal_date_common_v1", "sha256": "a1b2c3d4e5f6"}
    }
    cs_res = evaluation_evidence(store, valid_cs_doc, horizon=1, factor="momentum", cutoff=dates[-1])["common_sample"]
    assert cs_res["available"] is True
    assert cs_res["policy"] == "signal_date_common_v1"
    assert cs_res["sha256"] == "a1b2c3d4e5f6"

    # Composite keeps all expected frozen weights keys even if absent on all days, and marks partial
    missing_comp_doc = {
        "metadata": {
            "id": "factors-comp-missing-val",
            "name": "组合缺组成因子",
            "is_synthetic": True,
            "created_at": "2026-11-20T00:00:00Z",
            "scheme": "equal",
            "as_of": two_dates[1],
            "weights": {"momentum": 0.5, "value": 0.5},
            "availability_by_date": {
                d: {
                    "universe": 10,
                    "eligible": 10,
                    "factors": {
                        "momentum": {
                            "valid": 10,
                            "missing": 0,
                            "imputed": 0,
                            "not_applicable": 0,
                            "not_applicable_scored": 0,
                        }
                    },
                }
                for d in two_dates
            },
        },
        "history": [],
    }
    store.publish("factors", "factors-comp-missing-val", missing_comp_doc)
    miss_comp_eval, _ = _make_evaluation(
        [None, None], factor="momentum", horizon=1, factor_run_id="factors-comp-missing-val"
    )
    miss_comp_res = evaluation_evidence(store, miss_comp_eval, horizon=1, factor="composite", cutoff=two_dates[-1])
    assert miss_comp_res["raw_availability"]["coverage_status"] == "partial"
    comp_by_key = {row["factor"]: row for row in miss_comp_res["raw_availability"]["component_imputations"]}
    assert set(comp_by_key) == {"momentum", "value"}
    assert comp_by_key["value"]["matched_signal_dates"] == 0
    assert comp_by_key["value"]["raw_valid_stock_days"] is None

    # Partial label coverage (1/2) suppresses label exclusion breakdown; missing exclusion reasons keeps excluded=2 & complete=False
    lbl_partial_eval, lbl_dates = _make_evaluation([0.05, 0.04], factor="momentum", horizon=1)
    lbl_partial_eval["labels"]["1"]["coverage"] = lbl_partial_eval["labels"]["1"]["coverage"][:1]
    res_lbl_partial = evaluation_evidence(store, lbl_partial_eval, horizon=1, factor="momentum", cutoff=lbl_dates[-1])
    assert res_lbl_partial["label_coverage"]["scored_stock_days"] is None
    assert res_lbl_partial["label_coverage"]["exclusions_complete"] is False
    assert res_lbl_partial["exclusions"]["labels"] == []

    lbl_no_reason, lbl_dates2 = _make_evaluation([0.05, 0.04], factor="momentum", horizon=1)
    for r in lbl_no_reason["labels"]["1"]["coverage"]:
        r["scored"] = 20
        r["valid_labels"] = 18
        r["exclusions"] = {}
    res_lbl_no_reason = evaluation_evidence(store, lbl_no_reason, horizon=1, factor="momentum", cutoff=lbl_dates2[-1])
    assert res_lbl_no_reason["label_coverage"]["excluded_stock_days"] == 4
    assert res_lbl_no_reason["label_coverage"]["exclusions_complete"] is False
    assert res_lbl_no_reason["exclusions"]["labels"] == []

    # Composite keeps component_imputations per factor without scalar cross-factor sum
    full_comp_doc = {
        "metadata": {
            "id": "factors-comp",
            "name": "组合因子批次",
            "is_synthetic": True,
            "created_at": "2026-11-20T00:00:00Z",
            "scheme": "equal",
            "as_of": dates[24],
            "weights": {"momentum": 0.5, "value": 0.5},
            "availability_by_date": {
                d: {
                    "universe": 150,
                    "eligible": 120,
                    "factors": {
                        "momentum": {
                            "valid": 120,
                            "missing": 0,
                            "imputed": 0,
                            "not_applicable": 0,
                            "not_applicable_scored": 0,
                            "missing_policy": "strict",
                        },
                        "value": {
                            "valid": 0,
                            "missing": 120,
                            "imputed": 120,
                            "not_applicable": 0,
                            "not_applicable_scored": 0,
                            "missing_policy": "zero",
                        },
                    },
                }
                for d in dates[:25]
            },
        },
        "history": [],
    }
    store.publish("factors", "factors-comp", full_comp_doc)
    comp_eval, _ = _make_evaluation(
        [0.04 + 0.01 * (i % 3) for i in range(25)], factor="momentum", horizon=1, factor_run_id="factors-comp"
    )
    comp_res = evaluation_evidence(store, comp_eval, horizon=1, factor="composite", cutoff=dates[-1])
    assert comp_res["raw_availability"]["imputed_stock_days"] is None
    assert len(comp_res["raw_availability"]["component_imputations"]) == 2


def test_http_evidence_route_read_only_and_requires_cutoff_when_end_missing(tmp_path):
    runs, data = tmp_path / "runs", tmp_path / "data"
    store = ResultStore(runs)
    vals = [0.03 + 0.005 * (i % 4) for i in range(25)]
    doc, dates = _make_evaluation(vals, factor="momentum", horizon=5)
    doc["metadata"]["end"] = dates[-1]
    doc["metadata"]["factor_definitions"] = {
        "momentum": {"requires_financials": False},
        "value": {"requires_financials": True},
    }
    doc["metadata"]["factor_config"] = [
        {"id": "momentum", "name": "momentum"},
        {"id": "value", "name": "value"},
    ]
    store.publish("evaluations", "eval-http-1", doc)
    original_bytes = store.path("evaluations", "eval-http-1").read_bytes()

    client = TestClient(make_app(runs, data), base_url="http://127.0.0.1")
    before_sha = code_sha256()
    resp = client.get("/api/v1/evaluations/eval-http-1/evidence", params={"horizon": 5, "factor": "momentum"})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "ready"
    assert payload["counts"]["valid_rank_ic_dates"] == 25
    assert payload["counts"]["paired_observations"]["min"] == 120
    assert payload["counts"]["paired_observations"]["max"] == 120
    assert payload["financials"]["status"] == "missing_binding"
    assert payload["financials"]["status_label"] == "未绑定"
    assert store.path("evaluations", "eval-http-1").read_bytes() == original_bytes
    assert code_sha256() == before_sha
    json.dumps(payload, allow_nan=False)

    # Missing metadata.end without explicit cutoff -> 422
    no_end = copy.deepcopy(doc)
    no_end["metadata"]["end"] = None
    store.publish("evaluations", "eval-http-no-end", no_end)
    bad = client.get("/api/v1/evaluations/eval-http-no-end/evidence", params={"horizon": 5, "factor": "momentum"})
    assert bad.status_code == 422
