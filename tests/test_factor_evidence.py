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

    # Missing frozen calendar -> explicit error status, never fabricates positions
    no_cal, _ = _make_evaluation([0.05] * 25, horizon=1, with_calendar=False)
    res_no_cal = evaluation_evidence(store, no_cal, horizon=1, factor="momentum", cutoff=dates[-1])
    assert res_no_cal["status"] == "missing_frozen_calendar"
    assert res_no_cal["rank_ic"]["ci_low"] is None


def test_http_evidence_route_read_only_and_requires_cutoff_when_end_missing(tmp_path):
    runs, data = tmp_path / "runs", tmp_path / "data"
    store = ResultStore(runs)
    vals = [0.03 + 0.005 * (i % 4) for i in range(25)]
    doc, dates = _make_evaluation(vals, factor="momentum", horizon=5)
    doc["metadata"]["end"] = dates[-1]
    store.publish("evaluations", "eval-http-1", doc)
    original_bytes = store.path("evaluations", "eval-http-1").read_bytes()

    client = TestClient(make_app(runs, data), base_url="http://127.0.0.1")
    before_sha = code_sha256()
    resp = client.get("/api/v1/evaluations/eval-http-1/evidence", params={"horizon": 5, "factor": "momentum"})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "ready"
    assert payload["counts"]["valid_rank_ic_dates"] == 25
    assert store.path("evaluations", "eval-http-1").read_bytes() == original_bytes
    assert code_sha256() == before_sha
    json.dumps(payload, allow_nan=False)

    # Missing metadata.end without explicit cutoff -> 422
    no_end = copy.deepcopy(doc)
    no_end["metadata"]["end"] = None
    store.publish("evaluations", "eval-http-no-end", no_end)
    bad = client.get("/api/v1/evaluations/eval-http-no-end/evidence", params={"horizon": 5, "factor": "momentum"})
    assert bad.status_code == 422
