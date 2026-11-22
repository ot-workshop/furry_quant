"""Selection API, immutable handoff and native comparison regressions."""

import copy
import json

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from test_dynamic_experiments import pool, refresh
from test_factor_selection import parameters, sample
from test_industry_neutralization import history
from test_managed_data import Provider

from finance_quant.application import experiment_tasks, research_requests, selection_context, selection_experiments
from finance_quant.application.selection_context import selection_result, source_context
from finance_quant.application.selection_experiments import verify_queued_selection
from finance_quant.data import historical_warehouse, market_universe, pool_inputs
from finance_quant.data.financials import financial_root
from finance_quant.data.historical_warehouse import warehouse_option
from finance_quant.data.industries import publish_industries
from finance_quant.data.market_universe import market_option
from finance_quant.research.factor_selection import SelectionParameters
from finance_quant.storage import ResultStore
from support.fakes import make_app, make_service, run_claimed
from support.financial import balance, cashflow, income, publish
from support.fixed_pool import publish_fixture_snapshot


def source(tmp_path, mode="index_warehouse", size=5, *, runtime=None, execute=run_claimed):
    data, root, document = pool(tmp_path, mode, size)
    manifest = json.loads((root / "manifest.json").read_bytes())
    for index, asset in enumerate(manifest["assets"].values()):
        frame = pd.read_parquet(root / asset["file"])
        t = np.arange(len(frame))
        close = 10 + 0.07 * t + 0.3 * np.sin(t * 0.9 + index)
        frame["open"], frame["close"] = close, close
        frame["high"], frame["low"] = close + 0.1, close - 0.1
        frame["pre_close"] = frame.close.shift(1).fillna(10)
        frame["up_limit"], frame["down_limit"] = frame.pre_close * 1.1, frame.pre_close * 0.9
        frame.to_parquet(root / asset["file"], index=False)
    refresh(root, manifest)
    document["data"]["warehouse_id"] = (warehouse_option(root) if mode == "index_warehouse" else market_option(root))[
        "id"
    ]
    runs = tmp_path / "runs"
    if runtime:
        service, client, store = runtime(runs, data)
    else:
        service = make_service(runs, data)
        client = TestClient(make_app(runs, data, service=service), base_url="http://127.0.0.1")
        store = ResultStore(runs)
    research = {
        "name": "筛选来源",
        "data": document["data"],
        "backtest": {**document["backtest"], "start": "2024-01-08", "end": "2024-02-02"},
        "factors": [
            {"name": "momentum", "window": 2, "weight": 0.5},
            {"name": "reversal", "window": 2, "skip": 0, "weight": 0.3},
            {"name": "value", "weight": 0.2},
        ],
        "evaluation": document["evaluation"],
    }
    response = client.post(
        "/api/v1/research/tasks", json=research, headers={"Idempotency-Key": "selection-source-fixture"}
    )
    assert response.status_code == 202, response.text
    task_id = response.json()["id"]
    assert service.store.claim() == task_id
    task = execute(service, task_id)
    assert task["status"] == "succeeded", task["error"]
    evaluation_id = task["result"]["evaluation_run_id"]
    params = {"horizon": 1, "cutoff": "2024-01-23", "factor_count": 1, "min_dates": 2}
    response = client.get(f"/api/v1/evaluations/{evaluation_id}/selection", params=params)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "optimal", response.text
    reference = response.json()["reference"]
    # Match JSON.stringify in the real browser: integral numeric values lose .0.
    for key in ("min_ic", "min_ir", "max_correlation"):
        if float(reference["parameters"][key]).is_integer():
            reference["parameters"][key] = int(reference["parameters"][key])
    response = client.get(
        f"/api/v1/evaluations/{evaluation_id}/selection/draft",
        params={**params, "selection_id": reference["selection_id"]},
    )
    assert response.status_code == 200, response.text
    draft = response.json()
    request = {
        "name": draft["research"]["name"],
        "data": draft["research"]["data"],
        "backtest": {k: v for k, v in draft["research"]["backtest"].items() if k not in {"start", "end"}},
        "evaluation": draft["research"]["evaluation"],
        "neutralization": draft["research"]["neutralization"],
        "plan": {"periods": draft["periods"], "candidates": draft["candidates"]},
        "factor_selection": reference,
    }
    return client, service, store, evaluation_id, request, root


def test_selection_api_readonly_and_thresholds_change_results(tmp_path):
    result, dates = sample()
    store = ResultStore(tmp_path / "runs")
    result["metadata"]["id"] = "selector-fixture"
    store.publish("evaluations", "selector-fixture", result)
    original = store.path("evaluations", "selector-fixture").read_bytes()
    client = TestClient(make_app(tmp_path / "runs", tmp_path / "data"))
    params = parameters(dates).as_dict()
    response = client.get(
        "/api/v1/evaluations/selector-fixture/selection", params={k: v for k, v in params.items() if v is not None}
    )
    assert response.status_code == 200, response.text
    assert response.json()["selected_keys"] == ["A", "C", "D"]
    params["min_ic"] = 0.07
    strict = client.get(
        "/api/v1/evaluations/selector-fixture/selection", params={k: v for k, v in params.items() if v is not None}
    ).json()
    assert strict["status"] == "infeasible" and strict["max_feasible_count"] == 0
    assert store.path("evaluations", "selector-fixture").read_bytes() == original
    for bad in ({"cutoff": "tomorrow"}, {"min_ir": "nan"}, {"factor_count": 0}):
        response = client.get(
            "/api/v1/evaluations/selector-fixture/selection",
            params={**{k: v for k, v in params.items() if v is not None}, **bad},
        )
        assert response.status_code == 422, response.text


@pytest.mark.parametrize("mode,size", [("index_warehouse", 5), ("market_warehouse", 17)])
def test_selection_handoff_runs_post_cutoff_native_comparison_without_modifying_old_results(tmp_path, mode, size):
    client, service, store, evaluation_id, request, root = source(tmp_path, mode, size)
    originals = {path: path.read_bytes() for path in store.root.glob("*/*/result.json")}
    manifest = (root / "manifest.json").read_bytes()
    response = client.post(
        "/api/v1/research/experiments", json=request, headers={"Idempotency-Key": "milp-native-comparison"}
    )
    assert response.status_code == 202, response.text
    assert service.store.claim() == response.json()["id"]
    task = run_claimed(service, response.json()["id"])
    assert task["status"] == "succeeded", task["error"]
    result = store.get("experiments", task["result"]["experiment_run_id"])
    assert result["metadata"]["factor_selection"]["evaluation_id"] == evaluation_id
    assert result["metadata"]["factor_selection"]["source_sha256"] == store.digest("evaluations", evaluation_id)
    assert len(result["rows"]) == 5
    assert all(period["start"] > "2024-01-23" for period in result["plan"]["periods"].values())
    assert all(row["factors"][0]["weight"] == 1 for row in result["plan"]["candidates"] if row["id"] == "milp")
    assert all(path.read_bytes() == original for path, original in originals.items())
    assert (root / "manifest.json").read_bytes() == manifest
    report = client.get(f"/api/v1/experiments/{result['metadata']['id']}/report")
    assert report.status_code == 200 and "推荐截止日：2024-01-23" in report.text


def test_handoff_rejects_references_the_request_settles_alone(tmp_path):
    """Each early rule keeps its own reason, and none of them queues a task."""

    client, service, store, evaluation_id, request, _ = source(tmp_path)

    def pre_cutoff_start(changed):
        changed["plan"]["periods"]["development"]["start"] = "2024-01-23"

    def malformed_pool(changed):
        changed["data"]["warehouse_id"] = "another-version"

    def malformed_cutoff(changed):
        changed["factor_selection"]["parameters"]["cutoff"] = "20240123"

    def invalid_horizon(changed):
        changed["factor_selection"]["parameters"]["horizon"] = True

    def malformed_reference(changed):
        changed["factor_selection"]["selection_id"] = "forged"

    def missing_candidate(changed):
        changed["plan"]["candidates"][0]["id"] = "not-milp"

    def lightened_weight(changed):
        # The selected scheme has one factor, so its weight can only be 1: the document shape
        # rejects this before the worker. A tampered two-factor candidate set is below.
        changed["plan"]["candidates"][0]["factors"][0]["weight"] = 0.5

    cases = [
        ("date", pre_cutoff_start, "必须全部晚于推荐截止日"),
        ("pool", malformed_pool, "仓库版本标识格式无效"),
        ("cutoff", malformed_cutoff, "筛选日期须为 YYYY-MM-DD"),
        ("horizon", invalid_horizon, "horizon 须为"),
        ("ref", malformed_reference, "标识格式无效"),
        ("missing", missing_candidate, "缺少来源筛选的 MILP 候选方案"),
        ("weight", lightened_weight, "factor weights must sum to 1"),
    ]
    for name, change, message in cases:
        changed = copy.deepcopy(request)
        change(changed)
        response = client.post(
            "/api/v1/research/experiments", json=changed, headers={"Idempotency-Key": f"early-{name}"}
        )
        assert response.status_code == 422, (name, response.text)
        assert message in response.text, (name, response.text)
    assert service.store.list()["total"] == 1

    params = request["factor_selection"]["parameters"]
    response = client.get(
        f"/api/v1/evaluations/{evaluation_id}/selection/draft",
        params={**{k: v for k, v in params.items() if v is not None}, "selection_id": "forged"},
    )
    assert response.status_code == 422
    late = {**params, "cutoff": "2024-02-02"}
    selected = selection_result(store, evaluation_id, SelectionParameters(**late))
    response = client.get(
        f"/api/v1/evaluations/{evaluation_id}/selection/draft",
        params={**{k: v for k, v in late.items() if v is not None}, "selection_id": selected["selection_id"]},
    )
    assert response.status_code == 422 and "交易日" in response.text


@pytest.mark.parametrize("mode,size", [("index_warehouse", 5), ("market_warehouse", 17)])
def test_handoff_post_never_resolves_warehouse_or_selection(tmp_path, monkeypatch, mode, size):
    client, service, _, _, document, _ = source(tmp_path, mode, size)
    valid_id = document["data"]["warehouse_id"]
    prefix, digest = valid_id.split("-", 1)
    unknown_id = f"{prefix}-{'0' * 20}"
    assert unknown_id != valid_id
    other_prefix = "market" if mode == "index_warehouse" else "csi300"

    def forbidden(*args, **kwargs):
        pytest.fail("POST may not enumerate warehouse versions, read Parquet, compute coverage, or resolve selection")

    def submit(test_case, warehouse_id):
        request = copy.deepcopy(document)
        request["data"]["warehouse_id"] = warehouse_id
        return client.post(
            "/api/v1/research/experiments", json=request, headers={"Idempotency-Key": f"warehouse-post-{test_case}"}
        )

    with monkeypatch.context() as guard:
        for module, name in (
            (research_requests.market_download, "warehouse_roots"),
            (research_requests, "market_roots"),
            (market_universe, "market_roots"),
            (research_requests, "warehouse_option"),
            (research_requests, "market_option"),
            (research_requests, "snapshot_registry"),
            (research_requests, "security_coverage"),
            (pool_inputs, "security_coverage"),
            (historical_warehouse, "security_coverage"),
            (market_universe, "security_coverage"),
            (selection_context, "select_factors"),
            (selection_experiments, "checked_selection"),
            (experiment_tasks, "selection_validation"),
            (pd, "read_parquet"),
            (pq, "read_table"),
            (pq, "ParquetFile"),
        ):
            guard.setattr(module, name, forbidden)
        for name, identifier in (("wrong-mode", f"{other_prefix}-{digest}"), ("malformed", f"{prefix}-123")):
            rejected = submit(name, identifier)
            assert rejected.status_code == 422 and "仓库版本标识格式无效" in rejected.text, rejected.text
        assert service.store.list()["total"] == 1  # source research only
        missing = submit("missing", unknown_id)
        assert missing.status_code == 202, missing.text
        valid = submit("valid", valid_id)
        assert valid.status_code == 202, valid.text
        assert service.store.list()["total"] == 3

    identifier = missing.json()["id"]
    row = service.store.get(identifier, private=True)
    assert row["request"]["data"]["warehouse_id"] == unknown_id
    assert row["execution"]["prepare_state"] == "pending"
    artifact_paths = {
        path for root in (service.runs_root, service.data_root) for path in root.rglob("*") if path.is_file()
    }
    assert service.store.claim() == identifier
    finished = run_claimed(service, identifier)
    assert finished["status"] == "failed" and finished["error_details"]["stage"] == "prepare"
    assert "版本不存在或已变化" in finished["error"]
    assert finished["result"] is None
    assert service.store.get(identifier, private=True)["execution"] == row["execution"]
    assert not (service.runs_root / "experiments").exists()
    assert {
        path for root in (service.runs_root, service.data_root) for path in root.rglob("*") if path.is_file()
    } == artifact_paths
    assert service.store.get(valid.json()["id"])["status"] == "queued"


def test_handoff_defers_edits_that_need_the_calendar_or_the_source(tmp_path):
    """Later-start, window, factor, candidate-set, industry and forged-fingerprint edits queue.

    None of them may be settled by the request alone, so each one is accepted with 202 and
    rejected by the worker's prepare stage with its own reason, leaving no result behind.
    """

    client, service, store, evaluation_id, request, _ = source(tmp_path)

    def later_start(changed):
        # One trading day after the cutoff and still later than it: only the calendar knows
        # that this single-day development window cannot hold the horizon.
        changed["plan"]["periods"]["development"]["start"] = "2024-01-25"

    def wider_window(changed):
        changed["plan"]["candidates"][0]["factors"][0]["window"] = 3

    def other_factor(changed):
        changed["plan"]["candidates"][0]["factors"] = [{"name": "momentum", "window": 2, "weight": 1}]

    def extra_factor(changed):
        changed["plan"]["candidates"][0]["factors"] = [
            {"name": "reversal", "window": 2, "skip": 0, "weight": 0.5},
            {"name": "momentum", "window": 2, "weight": 0.5},
        ]

    def industry_neutral(changed):
        changed["neutralization"]["mode"] = "industry"

    def forged_fingerprint(changed):
        # Well-formed but not the fingerprint of this source: only the worker can recompute it.
        changed["factor_selection"]["selection_id"] = "factor-selection-000000000000"

    cases = [
        ("date", later_start, "每个区间的交易日数须超过最长评估持有期"),
        ("window", wider_window, "推荐组合的因子、窗口或等权权重已修改"),
        ("factor", other_factor, "推荐组合的因子、窗口或等权权重已修改"),
        ("candidates", extra_factor, "推荐组合已修改，请重新筛选后创建比较"),
        ("neutralization", industry_neutral, "历史行业数据尚未下载"),
        ("ref", forged_fingerprint, "来源评估或筛选算法已变化"),
    ]
    for name, change, message in cases:
        changed = copy.deepcopy(request)
        change(changed)
        response = client.post(
            "/api/v1/research/experiments", json=changed, headers={"Idempotency-Key": f"defer-{name}"}
        )
        assert response.status_code == 202, (name, response.text)
        identifier = response.json()["id"]
        execution = service.store.get(identifier, private=True)["execution"]
        assert execution["prepare_state"] == "pending"
        assert service.store.claim() == identifier
        task = run_claimed(service, identifier)
        assert task["status"] == "failed" and task["error_details"]["stage"] == "prepare", (name, task)
        assert message in (task["error"] or ""), (name, task["error"])
        assert task["result"] is None
        assert service.store.get(identifier, private=True)["execution"] == execution
    assert not (store.root / "experiments").exists()


def test_modified_source_after_queue_does_not_run_comparison(tmp_path):
    client, service, store, evaluation_id, request, _ = source(tmp_path)
    original = store.digest("evaluations", evaluation_id)
    evidence = {"evaluation_id": evaluation_id, "source_sha256": original}
    verify_queued_selection(store, evidence)
    response = client.post(
        "/api/v1/research/experiments", json=request, headers={"Idempotency-Key": "before-source-mutated"}
    )
    assert response.status_code == 202, response.text
    path = store.path("evaluations", evaluation_id)
    path.write_bytes(path.read_bytes() + b"\n")
    assert service.store.claim() == response.json()["id"]
    task = run_claimed(service, response.json()["id"])
    assert task["status"] == "failed" and not list(store.root.glob("experiments/*/result.json"))


class VaryingProvider(Provider):
    def query(self, api, **params):
        frame = super().query(api, **params)
        if api == "daily":
            dates = pd.to_datetime(frame.trade_date)
            t = (dates - pd.Timestamp("2024-12-01")).dt.days.to_numpy()
            index = int(params["ts_code"][:6]) % 7
            close = 10 + 0.01 * t + 0.3 * np.sin(t * 0.4 + index)
            frame["open"], frame["close"] = close, close
            frame["high"], frame["low"] = close + 0.1, close - 0.1
        return frame


def test_fixed_pool_selection_keeps_original_subset_and_windows(tmp_path, monkeypatch):
    data, runs = tmp_path / "data", tmp_path / "runs"
    codes = ["600000.SH", "000001.SZ", "600036.SH", "600519.SH"]
    manifest = publish_fixture_snapshot(data, codes, "20241202", "20250228", provider=VaryingProvider())
    service = make_service(runs, data)
    client = TestClient(make_app(runs, data, service=service), base_url="http://127.0.0.1")
    request = {
        "name": "固定池筛选",
        "data": {"mode": "snapshot", "snapshot_id": manifest["id"], "codes": codes[:3]},
        "backtest": {"start": "2025-01-02", "end": "2025-02-28", "topk": 1, "n_drop": 0},
        "factors": [
            {"name": "momentum", "window": 2, "weight": 0.6},
            {"name": "reversal", "window": 2, "skip": 0, "weight": 0.4},
        ],
        "evaluation": {"horizons": [1], "groups": 2, "min_cross_section": 3},
    }
    response = client.post("/api/v1/research/tasks", json=request, headers={"Idempotency-Key": "fixed-milp-source"})
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    assert service.store.claim() == identifier
    task = run_claimed(service, identifier)
    assert task["status"] == "succeeded", task
    params = {"horizon": 1, "cutoff": "2025-01-17", "min_dates": 2, "factor_count": 1}
    url = f"/api/v1/evaluations/{task['result']['evaluation_run_id']}/selection"
    selected = client.get(url, params=params).json()
    assert selected["status"] == "optimal", selected
    draft = client.get(url + "/draft", params={**params, "selection_id": selected["selection_id"]})
    assert draft.status_code == 200, draft.text
    draft = draft.json()
    assert draft["research"]["data"]["codes"] == sorted(codes[:3])
    assert draft["candidates"][0]["factors"][0]["window"] == 2
    research = draft["research"]
    comparison = {
        "name": "固定池组合对照",
        "data": research["data"],
        "backtest": {k: v for k, v in research["backtest"].items() if k not in {"start", "end"}},
        "evaluation": research["evaluation"],
        "neutralization": research["neutralization"],
        "plan": {"periods": draft["periods"], "candidates": draft["candidates"]},
        "factor_selection": selected["reference"],
    }
    response = client.post(
        "/api/v1/research/experiments", json=comparison, headers={"Idempotency-Key": "fixed-milp-comparison"}
    )
    assert response.status_code == 202, response.text

    def no_discovery(*args, **kwargs):
        pytest.fail("queued selection must validate the source against A, without discovering other datasets")

    monkeypatch.setattr(research_requests, "snapshot_registry", no_discovery)
    monkeypatch.setattr(research_requests.market_download, "warehouse_roots", no_discovery)
    monkeypatch.setattr(research_requests, "market_roots", no_discovery)
    assert service.store.claim() == response.json()["id"]
    task = run_claimed(service, response.json()["id"])
    assert task["status"] == "succeeded", task
    result = ResultStore(runs).get("experiments", task["result"]["experiment_run_id"])
    assert len(result["rows"]) == 5 and result["shared_parameters"]["codes"] == sorted(codes[:3])


def test_source_context_uses_saved_financial_and_industry_versions_after_current_changes(tmp_path):
    _, service, store, evaluation_id, _, _ = source(tmp_path)
    root = financial_root(service.data_root)
    first = publish(root, [income(), balance(), cashflow()])
    industry = publish_industries(history(["SH600000", "SZ000001"]).frame)
    second = publish(root, [income(n_income_attr_p=20), balance(), cashflow()])
    newer_industry = publish_industries(history(["SH600000", "SZ000001"], switch=True).frame)
    assert first["id"] != second["id"] and industry.manifest["id"] != newer_industry.manifest["id"]
    evaluation = store.get("evaluations", evaluation_id)
    factor = store.get("factors", evaluation["metadata"]["factor_run_id"])
    factor["metadata"]["id"] = "pinned-selection-factors"
    factor["metadata"]["provenance"]["industry_version"] = industry.manifest["id"]
    store.publish("factors", "pinned-selection-factors", factor)
    evaluation["metadata"]["factor_run_id"] = "pinned-selection-factors"
    evaluation["metadata"]["provenance"]["financial_version"] = first["id"]
    evaluation["metadata"]["id"] = "pinned-selection-evaluation"
    store.publish("evaluations", "pinned-selection-evaluation", evaluation)
    context = source_context(store, "pinned-selection-evaluation", service.data_root, service.runs_root)
    assert context["financials"].version == context["financial_data"]["version"] == first["id"]
    assert context["neutralization"]["mode"] == "off"
    assert context["neutralization"]["industry_version"] == context["industry_data"]["id"] == industry.manifest["id"]
