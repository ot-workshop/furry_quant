"""Common eligibility rules across index, market and user-selected research."""

import copy
import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from test_historical_requests import setup as index_setup
from test_joint_data import Secondary
from test_shared_market import spec
from test_shared_market import workspace as workspace

from finance_quant.application import market_download
from finance_quant.application.research_requests import research_options, validate_request
from finance_quant.checksums import file_sha256
from finance_quant.data.historical_warehouse import load_historical_input
from finance_quant.data.market_universe import market_option, market_roots
from finance_quant.data.pool_inputs import filter_input
from finance_quant.research.universe import UniverseFilters
from finance_quant.storage import ResultStore
from support.fakes import make_app, make_service, run_claimed


def execute(data, runs, request, key):
    service = make_service(runs, data)
    client = TestClient(make_app(runs, data_root=data, service=service), base_url="http://127.0.0.1")
    response = client.post("/api/v1/research/tasks", json=request, headers={"Idempotency-Key": key})
    assert response.status_code == 202, response.text
    task = response.json()
    assert service.store.claim() == task["id"]
    done = run_claimed(service, task["id"])
    assert done["status"] == "succeeded", done.get("error_details")
    result = client.get("/api/v1/backtests/" + done["result"]["backtest_run_id"])
    assert result.status_code == 200, result.text
    curve = client.get("/api/v1/backtests/" + done["result"]["backtest_run_id"] + "/equity")
    assert curve.status_code == 200, curve.text
    return done, {**result.json(), "equity": curve.json()["items"]}


@pytest.mark.parametrize("frequency", ["daily", "weekly", "monthly"])
@pytest.mark.parametrize("mode", ["snapshot", "custom", "market"])
def test_shared_data_filters_submit_execute_restore_without_changing_sources(workspace, mode, frequency):
    data, root, _, provider = workspace
    plan = market_download.prepare(root, spec("all_market", "2025-02-28", codes=[]), provider=provider)
    result = market_download.run(root, plan["id"], provider=provider, secondary=Secondary())
    assert result["status"] == "complete", result
    option = market_option(market_roots(data)[0])
    options = research_options(data, data.parent / "runs")
    assert option["listing_coverage"] == {"covered": 3, "total": 3}
    filters = {"min_listing_days": 0, "min_avg_amount": 1, "amount_window": 4}
    if mode == "snapshot":
        snapshot = options["snapshots"][0]
        assert snapshot["st_coverage"]["covered"] == 3
        selected = {"mode": "snapshot", "snapshot_id": snapshot["id"], "codes": option["codes"][:2], "filters": filters}
    else:
        selected = {"mode": "market_warehouse", "warehouse_id": option["id"], "filters": filters}
        if mode == "custom":
            selected["codes"] = option["codes"][:2]
    request = {
        "name": "共用筛选验收",
        "data": selected,
        "backtest": {"start": "2025-01-10", "end": "2025-02-28", "topk": 1, "rebalance_frequency": frequency},
        "factors": [{"name": "momentum", "weight": 1, "window": 2}],
        "evaluation": {"horizons": [1], "groups": 2, "min_cross_section": 3},
    }
    immutable = {path: file_sha256(path) for path in data.rglob("*.parquet")}
    api_calls = len(provider.calls)
    done, detail = execute(data, data.parent / "runs", request, f"common-filter-{mode}-{frequency}")
    assert detail["universe_kind"] == ("market" if mode == "market" else "custom")
    assert detail["universe_filters"]["amount_window"] == 4
    assert detail["universe_filters"]["exclude_st"]
    assert detail["pool_coverage_by_date"]
    assert detail["equity"]
    factors = ResultStore(data.parent / "runs").get("factors", done["result"]["factor_run_id"])
    expected_codes = set(selected.get("codes", option["codes"]))
    assert {row["instrument"] for row in factors["history"]} == {code[-2:] + code[:6] for code in expected_codes}
    assert all(file_sha256(path) == digest for path, digest in immutable.items())
    assert len(provider.calls) == api_calls
    normalized, _ = validate_request(done["request"], data, data.parent / "runs")
    assert normalized == done["request"]
    changed = copy.deepcopy(request)
    changed["data"]["filters"]["amount_window"] = 60
    with pytest.raises(ValueError, match="预热|回测前交易日"):
        validate_request(changed, data, data.parent / "runs")


@pytest.mark.parametrize("frequency", ["daily", "weekly", "monthly"])
def test_index_filters_intersect_historical_members_and_change_actual_backtest(tmp_path, monkeypatch, frequency):
    root, request = index_setup(tmp_path, monkeypatch)
    request["backtest"]["rebalance_frequency"] = frequency
    original, original_result = execute(tmp_path / "data", tmp_path / "runs", request, "index-original-12345")
    old_file = ResultStore(tmp_path / "runs").get("backtests", original["result"]["backtest_run_id"])
    request["data"]["filters"] = {"exclude_st": False, "min_listing_days": 0, "boards": ["main"]}
    done, filtered = execute(tmp_path / "data", tmp_path / "runs", request, "index-filtered-12345")
    assert filtered["universe_kind"] == "index"
    assert done["result"]["backtest_run_id"] != original["result"]["backtest_run_id"]
    factors = ResultStore(tmp_path / "runs").get("factors", done["result"]["factor_run_id"])
    assert "SH688001" not in {row["instrument"] for row in factors["history"]}
    if frequency != "monthly":
        assert filtered["equity"] != original_result["equity"]
    assert any(row["excluded"]["board"] for row in filtered["pool_coverage_by_date"].values())
    assert ResultStore(tmp_path / "runs").get("backtests", original["result"]["backtest_run_id"]) == old_file
    request["data"].pop("filters")
    again, _ = execute(tmp_path / "data", tmp_path / "runs", request, "index-unfiltered-12345")
    assert again["result"]["backtest_run_id"] == original["result"]["backtest_run_id"]


def test_index_filters_keep_membership_timing_and_unknown_state_explicit(tmp_path, monkeypatch):
    root, request = index_setup(tmp_path, monkeypatch)
    inputs = load_historical_input(root, "2024-01-08", "2024-01-31", 2)
    manifest = json.loads((root / "manifest.json").read_text())
    _, _, _, meta = filter_input(
        inputs, root, manifest, UniverseFilters(exclude_st=False, min_listing_days=0), "2024-01-08", "2024-01-31"
    )
    assert "SH688001" not in meta["membership_by_execution_date"]["2024-01-11"]
    assert "SH688001" in meta["membership_by_execution_date"]["2024-01-12"]
    request["data"]["filters"] = {"min_listing_days": 0}
    with pytest.raises(ValueError, match="历史名称"):
        validate_request(request, tmp_path / "data", tmp_path / "runs")


def test_custom_codes_are_validated_without_expanding_to_all_market(workspace):
    data, root, _, provider = workspace
    plan = market_download.prepare(root, spec("all_market", codes=[]), provider=provider)
    assert market_download.run(root, plan["id"], provider=provider, secondary=Secondary())["status"] == "complete"
    option = market_option(market_roots(data)[0])
    request = {
        "name": "自选范围",
        "data": {"mode": "market_warehouse", "warehouse_id": option["id"], "codes": ["000001.SZ", "999999.SH"]},
        "backtest": {"start": "2025-01-10", "end": "2025-02-28", "topk": 1},
        "factors": [{"name": "momentum", "weight": 1, "window": 2}],
        "evaluation": {"horizons": [1], "groups": 2, "min_cross_section": 3},
    }
    with pytest.raises(ValueError, match="自选股票"):
        validate_request(request, data, data.parent / "runs")


def test_index_historical_st_forces_exit_on_a_non_rebalance_day(tmp_path, monkeypatch):
    root, request = index_setup(tmp_path, monkeypatch)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    names = pd.DataFrame(
        [
            {
                "instrument": symbol,
                "name": "正常股票",
                "start_date": pd.Timestamp("2010-01-01"),
                "end_date": pd.NaT,
                "ann_date": pd.Timestamp("2010-01-01"),
            }
            for symbol in ["SH600000", "SZ000001", "SH688001"]
        ]
        + [
            {
                "instrument": "SH688001",
                "name": "ST股票",
                "start_date": pd.Timestamp("2024-01-15"),
                "end_date": pd.NaT,
                "ann_date": pd.Timestamp("2024-01-15"),
            }
        ]
    )
    path = root / "reference/name_history.parquet"
    names.to_parquet(path, index=False)
    manifest["reference_files"][path.name] = file_sha256(path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    request["data"]["warehouse_id"] = "csi300-" + file_sha256(manifest_path)[:20]
    request["data"]["filters"] = {"exclude_st": True, "min_listing_days": 0}
    request["backtest"]["rebalance_frequency"] = "weekly"
    done, result = execute(tmp_path / "data", tmp_path / "runs", request, "index-st-history-12345")
    saved = ResultStore(tmp_path / "runs").get("backtests", done["result"]["backtest_run_id"])
    assert any(
        row["date"] == "2024-01-15" and row["instrument"] == "SH688001" and row["side"] == "buy"
        for row in saved["trades"]
    )
    assert any(
        row["date"] == "2024-01-17" and row["instrument"] == "SH688001" and row["side"] == "sell"
        for row in saved["trades"]
    )
    assert result["pool_coverage_by_date"]["2024-01-16"]["excluded"]["st"] == 1


def test_snapshot_permissive_filters_match_legacy_and_old_results_stay_readable(workspace):
    data, root, _, provider = workspace
    plan = market_download.prepare(root, spec("custom", codes=["600000.SH", "000001.SZ"]), provider=provider)
    assert market_download.run(root, plan["id"], provider=provider, secondary=Secondary())["status"] == "complete"
    snapshot = research_options(data, data.parent / "runs")["snapshots"][0]
    request = {
        "name": "自选池一致性",
        "data": {
            "mode": "snapshot",
            "snapshot_id": snapshot["id"],
            "codes": [item["code"] for item in snapshot["codes"]],
        },
        "backtest": {"start": "2025-01-10", "end": "2025-02-28", "topk": 1},
        "factors": [{"name": "momentum", "weight": 1, "window": 2}],
        "evaluation": {"horizons": [1], "groups": 2, "min_cross_section": 3},
    }
    original, before = execute(data, data.parent / "runs", request, "snapshot-original-12345")
    request["data"]["filters"] = {"exclude_st": False, "min_listing_days": 0}
    filtered, after = execute(data, data.parent / "runs", request, "snapshot-permissive-12345")
    assert before["equity"] == after["equity"]
    request["data"].pop("filters")
    again, restored = execute(data, data.parent / "runs", request, "snapshot-restored-12345")
    assert again["result"]["backtest_run_id"] == original["result"]["backtest_run_id"]
    assert restored["equity"] == before["equity"]
    assert filtered["result"]["backtest_run_id"] != original["result"]["backtest_run_id"]
