"""Dynamic pool split experiments through submission, native Qlib and result APIs."""

import copy
import json

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from test_historical_workflow import warehouse_fixture

from finance_quant.application import experiment_workflow
from finance_quant.application.experiment_tasks import dynamic_data, validate_experiment_request
from finance_quant.application.research_requests import request_config
from finance_quant.checksums import file_sha256
from finance_quant.data.historical_warehouse import warehouse_option
from finance_quant.data.market_universe import SCHEMA, market_option
from finance_quant.research.experiments import ExperimentPlan
from finance_quant.research.universe import UniverseFilters
from finance_quant.storage import ResultStore
from support.fakes import make_app, make_service, run_claimed


def refresh(root, manifest):
    for asset in manifest["assets"].values():
        frame = pd.read_parquet(root / asset["file"])
        asset.update(
            sha256=file_sha256(root / asset["file"]),
            rows=len(frame),
            start=frame.date.min().strftime("%Y%m%d"),
            end=frame.date.max().strftime("%Y%m%d"),
        )
    manifest["reference_files"] = {path.name: file_sha256(path) for path in (root / "reference").glob("*.parquet")}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def pool(tmp_path, mode, size=5):
    data = tmp_path / "data"
    root = data / "shared-market/versions/fixture" / ("index-view" if mode == "index_warehouse" else "market-view")
    root.parent.mkdir(parents=True)
    manifest = warehouse_fixture(root)
    (root.parent / "version.json").write_text("{}", encoding="utf-8")
    instruments = pd.read_parquet(root / "reference/instruments.parquet")
    instruments["delist_date"] = None
    members = pd.read_parquet(root / "reference/memberships.parquet")
    members.loc[members.code.eq("600000.SH"), "effective_until"] = pd.Timestamp("2024-01-18")
    members.loc[members.code.eq("688001.SH"), ["snapshot_date", "effective_from"]] = [
        pd.Timestamp("2024-01-17"),
        pd.Timestamp("2024-01-18"),
    ]
    template = pd.read_parquet(root / "assets/600000.SH/market.parquet")
    for index in range(size - 3):
        code = f"{600100 + index}.SH"
        frame = template.copy()
        frame["instrument"], frame["name"] = "SH" + code[:6], code
        frame["close"] = 10 + np.arange(len(frame)) * (0.01 + index / 10000)
        frame["open"] = frame.close
        frame["high"], frame["low"] = frame.close + 0.1, frame.close - 0.1
        frame["pre_close"] = frame.close.shift(1).fillna(10)
        frame["up_limit"], frame["down_limit"] = frame.pre_close * 1.1, frame.pre_close * 0.9
        frame["change"] = frame.close / frame.pre_close - 1
        listed = "20240119" if index == size - 4 else "20100101"
        if listed == "20240119":
            frame = frame.loc[frame.date.ge("2024-01-19")].copy()
            frame["history_sessions_before"] = np.arange(len(frame))
        directory = root / "assets" / code
        directory.mkdir()
        frame.to_parquet(directory / "market.parquet", index=False)
        manifest["assets"][code] = {
            **manifest["assets"]["600000.SH"],
            "code": code,
            "file": f"assets/{code}/market.parquet",
        }
        instruments.loc[len(instruments)] = [code, listed, None]
        members.loc[len(members)] = [code, pd.Timestamp("2023-12-29"), pd.Timestamp("2024-01-02"), pd.NaT, 1]
    # One incumbent is temporarily impossible to sell on the scheduled exit.
    frame = template.copy()
    halted = frame.date.eq("2024-01-18")
    frame.loc[halted, ["volume", "amount"]] = 0
    frame.loc[halted, "is_suspended"] = True
    down = frame.date.eq("2024-01-19")
    for field in ("open", "close", "high", "low"):
        frame.loc[down, field] = frame.loc[down, "down_limit"]
    frame.to_parquet(root / "assets/600000.SH/market.parquet", index=False)
    instruments.to_parquet(root / "reference/instruments.parquet", index=False)
    members.to_parquet(root / "reference/memberships.parquet", index=False)
    if mode == "market_warehouse":
        manifest.update(schema_version=SCHEMA, scope="all_market", restricted=True, shared_version="fixture")
        names = pd.DataFrame(
            [
                {
                    "instrument": code[-2:] + code[:6],
                    "name": "正常股票",
                    "start_date": pd.Timestamp("2010-01-01"),
                    "end_date": pd.NaT,
                    "ann_date": pd.Timestamp("2010-01-01"),
                }
                for code in manifest["assets"]
            ]
        )
        # ST enters only in the final test segment, never retroactively in validation.
        names.loc[len(names)] = ["SH600000", "ST测试", pd.Timestamp("2024-01-25"), pd.NaT, pd.Timestamp("2024-01-25")]
        names.to_parquet(root / "reference/name_history.parquet", index=False)
    refresh(root, manifest)
    option = warehouse_option(root) if mode == "index_warehouse" else market_option(root)
    document = {
        "name": "动态分段比较",
        "data": {"mode": mode, "warehouse_id": option["id"]},
        "backtest": {"topk": 1, "n_drop": 0, "initial_cash": 100000},
        "evaluation": {"horizons": [1, 2], "groups": 2, "min_cross_section": 3},
        "plan": {
            "periods": {
                "development": {"start": "2024-01-08", "end": "2024-01-12"},
                "validation": {"start": "2024-01-15", "end": "2024-01-23"},
                "test": {"start": "2024-01-24", "end": "2024-02-02"},
            },
            "candidates": [
                {"id": "momentum", "name": "动量", "factors": [{"name": "momentum", "weight": 1, "window": 2}]},
                {
                    "id": "reversal",
                    "name": "反转",
                    "factors": [{"name": "reversal", "weight": 1, "window": 2, "skip": 0}],
                },
            ],
        },
    }
    if mode == "market_warehouse":
        document["data"]["filters"] = UniverseFilters(exclude_st=True, min_listing_days=0).as_dict()
    return data, root, document


@pytest.mark.parametrize("mode,size", [("index_warehouse", 5), ("market_warehouse", 17), ("market_warehouse", 55)])
def test_dynamic_split_web_native_accounts_coverage_and_all_result_links(tmp_path, monkeypatch, mode, size):
    data, root, document = pool(tmp_path, mode, size)
    runs = tmp_path / "runs"
    service = make_service(runs, data)
    client = TestClient(make_app(runs, data, service=service), base_url="http://127.0.0.1")
    execute = experiment_workflow.run_historical_research
    calls = []

    def observed(request, *args, **kwargs):
        if request["backtest"]["start"] == "2024-01-24":
            assert list(runs.glob("experiments/*/selection.json")), "Selection must be locked before testing"
        calls.append(copy.deepcopy(request))
        return execute(request, *args, **kwargs)

    monkeypatch.setattr(experiment_workflow, "run_historical_research", observed)
    response = client.post(
        "/api/v1/research/experiments", json=document, headers={"Idempotency-Key": "dynamic-experiment-test"}
    )
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    assert service.store.claim() == identifier
    task = run_claimed(service, identifier)
    assert task["status"] == "succeeded", task["error"]
    result = client.get("/api/v1/experiments/" + task["result"]["experiment_run_id"]).json()
    assert len(calls) == len(result["rows"]) == 5
    assert len([item for item in result["rows"] if item["period"] == "test"]) == 1
    assert result["shared_parameters"]["universe"]["mode"] == mode
    assert len(result["shared_parameters"]["codes"]) == size
    assert task["progress"]["completed"] == task["progress"]["total"] == 6
    store = ResultStore(runs)
    for row in result["rows"]:
        period = document["plan"]["periods"][row["period"]]
        backtest = store.get("backtests", row["backtest_run_id"])
        evaluation = store.get("evaluations", row["evaluation_run_id"])
        assert backtest["equity"][0]["equity"] == 1
        assert backtest["metadata"]["config"]["account"] == 100000
        assert row["coverage"]["signal_days"] == row["trading_days"]
        assert row["coverage"]["max_pool"] <= size
        for label in evaluation["labels"].values():
            for observation in label["observations"]:
                assert period["start"] <= observation["entry_date"] <= observation["exit_date"] <= period["end"]
        assert client.get("/api/v1/backtests/" + row["backtest_run_id"]).status_code == 200
        assert (
            client.get(
                "/api/v1/evaluations/" + row["evaluation_run_id"] + "/analysis", params={"horizon": 1, "window": 3}
            ).status_code
            == 200
        )
    validation = next(
        row for row in result["rows"] if row["candidate_id"] == "momentum" and row["period"] == "validation"
    )
    backtest = store.get("backtests", validation["backtest_run_id"])
    if mode == "index_warehouse":
        sales = [row for row in backtest["trades"] if row["instrument"] == "SH600000" and row["side"] == "sell"]
        assert sales and sales[0]["date"] == "2024-01-22"
        assert not any(
            row["instrument"] == "SH688001" and row["side"] == "buy" and row["date"] < "2024-01-18"
            for row in backtest["trades"]
        )
    # Version-pinned reruns may reuse results; no task modifies the source manifest.
    original = (root / "manifest.json").read_bytes()
    normalized, execution = validate_experiment_request(document, data, runs)
    config = request_config(
        normalized
        | {
            "factors": normalized["plan"]["candidates"][0]["factors"],
            "backtest": {**normalized["backtest"], "start": "2024-01-08", "end": "2024-02-02"},
        },
        dynamic_data(root, normalized),
        runs,
        data,
    )
    plan = ExperimentPlan.from_dict(
        {"name": normalized["name"], "selection_metric": "validation_excess_return", **normalized["plan"]}
    )
    assert experiment_workflow.run_experiment(
        config, plan, dynamic_request=normalized, expected_sha=execution["manifest_sha256"]
    )["reused"]
    assert (root / "manifest.json").read_bytes() == original


def test_dynamic_candidate_warmup_and_changed_queued_version_are_rejected(tmp_path):
    data, root, document = pool(tmp_path, "index_warehouse")
    invalid = copy.deepcopy(document)
    invalid["plan"]["candidates"][1]["factors"][0]["window"] = 126
    with pytest.raises(ValueError, match="预热|需要|历史|评分"):
        validate_experiment_request(invalid, data, tmp_path / "runs")
    service = make_service(tmp_path / "runs", data)
    client = TestClient(make_app(tmp_path / "runs", data, service=service), base_url="http://127.0.0.1")
    response = client.post(
        "/api/v1/research/experiments", json=document, headers={"Idempotency-Key": "changed-version-check"}
    )
    assert response.status_code == 202, response.text
    manifest = json.loads((root / "manifest.json").read_bytes())
    manifest["note"] = "a later publication"
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert service.store.claim() == response.json()["id"]
    task = run_claimed(service, response.json()["id"])
    assert task["status"] == "failed" and not list((tmp_path / "runs").glob("backtests/*/result.json"))


@pytest.mark.parametrize("mode", ["index_warehouse", "market_warehouse"])
def test_later_prices_cannot_change_validation_selection_or_earlier_signals(tmp_path, mode):
    data, root, document = pool(tmp_path, mode, 5)
    runs = tmp_path / "runs"
    service = make_service(runs, data)
    client = TestClient(make_app(runs, data, service=service), base_url="http://127.0.0.1")

    def execute(key):
        response = client.post("/api/v1/research/experiments", json=document, headers={"Idempotency-Key": key})
        assert response.status_code == 202, response.text
        assert service.store.claim() == response.json()["id"]
        task = run_claimed(service, response.json()["id"])
        assert task["status"] == "succeeded", task["error"]
        return ResultStore(runs).get("experiments", task["result"]["experiment_run_id"])

    before = execute("before-future-change")
    store = ResultStore(runs)
    old_files = {path: path.read_bytes() for path in runs.glob("*/*/result.json")}
    manifest = json.loads((root / "manifest.json").read_bytes())
    for asset in manifest["assets"].values():
        path = root / asset["file"]
        frame = pd.read_parquet(path)
        future = frame.date.ge("2024-01-24")
        multipliers = 1 + np.arange(int(future.sum())) * 0.04
        for field in ("open", "close", "high", "low", "pre_close", "up_limit", "down_limit"):
            frame.loc[future, field] *= multipliers
        frame.to_parquet(path, index=False)
    refresh(root, manifest)
    option = warehouse_option(root) if mode == "index_warehouse" else market_option(root)
    document["data"]["warehouse_id"] = option["id"]
    after = execute("after-future-change")
    assert before["selection"]["candidate_id"] == after["selection"]["candidate_id"]
    assert before["selection"]["ranking"] == after["selection"]["ranking"]
    old_rows = {(row["candidate_id"], row["period"]): row for row in before["rows"]}
    for row in after["rows"]:
        if row["period"] == "test":
            continue
        old = old_rows[row["candidate_id"], row["period"]]
        assert old["metrics"] == row["metrics"]
        first = store.get("factors", old["factor_run_id"])
        second = store.get("factors", row["factor_run_id"])
        assert first["history"] == second["history"]
        backtest_before = store.get("backtests", old["backtest_run_id"])
        backtest_after = store.get("backtests", row["backtest_run_id"])
        assert backtest_before["trades"] == backtest_after["trades"]
    assert all(path.read_bytes() == original for path, original in old_files.items())
