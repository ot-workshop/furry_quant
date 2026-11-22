import json
import pathlib
import shutil
from dataclasses import replace
from unittest.mock import Mock

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from test_managed_data import Provider

from finance_quant.application import experiment_tasks, research_requests, task_context, task_worker
from finance_quant.application import experiment_workflow as workflow
from finance_quant.configuration import BacktestConfig, DataConfig, ResearchConfig
from finance_quant.data.financials import financial_root
from finance_quant.persistence.market import MarketRepository
from finance_quant.research.evaluation import forward_open_labels
from finance_quant.research.experiments import (
    ExperimentPlan,
    experiment_markdown,
    load_experiment_plan,
    select_candidate,
)
from finance_quant.research.fundamentals import fundamentals_for
from finance_quant.research.market_factors import market_factors
from finance_quant.research.registry import EvaluationConfig
from finance_quant.research.universe import UniverseFilters
from finance_quant.storage import ResultStore
from finance_quant.task_contracts import TaskConflict
from support.fakes import make_app, make_service, run_claimed
from support.financial import seed_earnings
from support.fixed_pool import download_dataset, publish_fixture_snapshot


def plan_document():
    return {
        "name": "工程分段测试",
        "selection_metric": "validation_excess_return",
        "periods": {
            "development": {"start": "2025-01-02", "end": "2025-01-17"},
            "validation": {"start": "2025-01-20", "end": "2025-02-04"},
            "test": {"start": "2025-02-05", "end": "2025-02-28"},
        },
        "candidates": [
            {"id": "value", "name": "估值", "factors": [{"name": "value", "weight": 1}]},
            {"id": "momentum", "name": "动量", "factors": [{"name": "momentum", "weight": 1, "window": 5}]},
        ],
    }


@pytest.fixture
def setup(tmp_path):
    config = ResearchConfig(
        DataConfig(tmp_path / "data", "20241202", "20250228", ("600000.SH", "000001.SZ", "600519.SH")),
        BacktestConfig(
            "2025-01-02", "2025-02-28", topk=1, initial_cash=150000, open_cost=0.002, close_cost=0.003, min_cost=8
        ),
        tmp_path / "runs",
        evaluation=EvaluationConfig(horizons=(1, 3)),
    )
    download_dataset(config.data, provider=Provider())
    config = replace(config, financials=seed_earnings(financial_root(tmp_path / "data"), config.data.codes))
    return config, ExperimentPlan.from_dict(plan_document())


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(unexpected=True),
        lambda d: d.update(selection_metric="test_return"),
        lambda d: d["periods"]["validation"].update(start="2025-01-17"),
        lambda d: d["periods"]["test"].update(end="2025-02-30"),
        lambda d: d["periods"]["test"].update(start="20250205"),
        lambda d: d["candidates"][0].update(id="../bad"),
        lambda d: d["candidates"][0].update(id="momentum"),
        lambda d: d["candidates"][0].update(factors=d["candidates"][1]["factors"]),
        lambda d: d["candidates"][0]["factors"][0].update(weight=True),
        lambda d: d.update(candidates=d["candidates"][:1]),
        lambda d: d["candidates"][0]["factors"][0].update(training_end="2025-02-28"),
    ],
)
def test_invalid_plans_fail_before_running(change):
    document = plan_document()
    change(document)
    with pytest.raises(ValueError):
        ExperimentPlan.from_dict(document)


def test_plan_json_duplicate_keys_bom_and_round_trip(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan_document()), encoding="utf-8-sig")
    plan = load_experiment_plan(path)
    assert ExperimentPlan.from_dict(plan.as_dict()) == plan
    path.write_text('{"name":"a","name":"b"}', encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        load_experiment_plan(path)


def metric_row(candidate, period, total, benchmark=0.01):
    return {
        "candidate_id": candidate,
        "period": period,
        "metrics": {"total_return": total, "benchmark_return": benchmark, "excess_return": 999},
    }


def test_selection_uses_only_validation_returns_and_deterministic_tie_rule():
    rows = [
        metric_row("b", "validation", 0.08),
        metric_row("a", "validation", 0.08),
        metric_row("b", "test", 100),
        metric_row("b", "development", 100),
    ]
    result = select_candidate(rows, ["b", "a"])
    assert result["candidate_id"] == "a" and result["score"] == pytest.approx(0.07)
    rows[-2]["metrics"] = {"total_return": float("nan")}
    rows[-1]["metrics"] = {}
    assert select_candidate(rows, ["a", "b"]) == result
    with pytest.raises(ValueError, match="恰好"):
        select_candidate(rows + [rows[0]], ["a", "b"])
    with pytest.raises(ValueError, match="恰好"):
        select_candidate(rows, ["a", "b", "c"])
    for invalid in (float("nan"), float("inf"), True, "0.2"):
        rows[0]["metrics"]["total_return"] = invalid
        with pytest.raises(ValueError, match="有限"):
            select_candidate(rows, ["a", "b"])


def fake_engines(monkeypatch, config, *, fail_test=False):
    store = ResultStore(config.runs_dir)
    calls = []
    state = {"fail_test": fail_test}

    def backtest(research, **kwargs):
        is_test = research.backtest.start == "2025-02-05"
        if is_test:
            checkpoints = list((config.runs_dir / "experiments").glob("*/selection.json"))
            assert len(checkpoints) == 1  # selection must be on disk before test entry
            locked = json.loads(checkpoints[0].read_text(encoding="utf-8"))
            assert locked["decision"]["candidate_id"] == "value"
            assert research.factors.specs[0].name == "value"
            if state["fail_test"]:
                state["fail_test"] = False
                raise RuntimeError("Simulated interrupted test")
        calls.append(research)
        fid = "factor-" + research.factors.scheme
        bid = "backtest-" + research.factors.scheme + research.backtest.start
        store.publish("factors", fid, {"history": []})
        total = 0.05 if research.factors.specs[0].name == "value" else -0.02
        store.publish(
            "backtests",
            bid,
            {
                "summary": {
                    "metrics": {
                        "total_return": total,
                        "benchmark_return": 0.01,
                        "max_drawdown": -0.03,
                        "total_cost_amount": 80.0,
                        "avg_turnover": 0.1,
                    }
                }
            },
        )
        return {"run_id": bid, "factor_run_id": fid}

    def evaluation(research, **kwargs):
        fid = "factor-" + research.factors.scheme
        eid = "eval-" + research.factors.scheme + research.backtest.start
        store.publish("evaluations", eid, {"metadata": {"factor_run_id": fid}})
        return {"run_id": eid, "factor_run_id": fid}

    monkeypatch.setattr(workflow, "run_configured", backtest)
    monkeypatch.setattr(workflow, "evaluate_configured", evaluation)
    return calls


def test_orchestration_freezes_selection_preserves_common_parameters_and_reuses(setup, monkeypatch):
    config, plan = setup
    calls = fake_engines(monkeypatch, config)
    active = config.runs_dir / "active-research.json"
    active.parent.mkdir(parents=True, exist_ok=True)
    active.write_text('"keep existing active configuration"', encoding="utf-8")
    completed = workflow.run_experiment(config, plan)
    assert len(calls) == 5  # two candidates x dev/validation + selected test
    for research in calls:
        assert replace(research.backtest, start=config.backtest.start, end=config.backtest.end) == config.backtest
        assert replace(research.data, root=config.data.root) == config.data
        assert research.data.root.parent.name == "snapshots"
        assert research.evaluation == config.evaluation
    store = ResultStore(config.runs_dir)
    result = store.get("experiments", completed["run_id"])
    assert result["selection"]["candidate_id"] == "value"
    assert [r["candidate_id"] for r in result["rows"] if r["period"] == "test"] == ["value"]
    assert active.read_text(encoding="utf-8") == '"keep existing active configuration"'
    assert workflow.run_experiment(config, plan)["reused"]
    assert len(calls) == 5
    row = result["rows"][0]
    store.path("backtests", row["backtest_run_id"]).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="已被改动"):
        workflow.run_experiment(config, plan)


def test_interrupted_test_resumes_same_locked_selection(setup, monkeypatch):
    config, plan = setup
    calls = fake_engines(monkeypatch, config, fail_test=True)
    with pytest.raises(RuntimeError, match="interrupted"):
        workflow.run_experiment(config, plan)
    checkpoint = next((config.runs_dir / "experiments").glob("*/selection.json"))
    original = checkpoint.read_bytes()
    assert not checkpoint.with_name("result.json").exists()
    assert ResultStore(config.runs_dir).list("experiments") == []
    workflow.run_experiment(config, plan)
    assert checkpoint.read_bytes() == original
    assert len(calls) == 9


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda d: d["periods"]["validation"].update(start="2025-01-21"), "首尾相接"),
        (lambda d: d["periods"]["validation"].update(start="2025-01-19"), "实际交易日"),
        (lambda d: d["periods"]["test"].update(end="2025-03-03"), "回测区间之内"),
        (lambda d: d["periods"]["test"].update(end="2025-02-07"), "最长评估持有期"),
        (lambda d: d["candidates"][1]["factors"][0].update(window=252), "评分"),
    ],
)
def test_preflight_rejects_unfair_or_unusable_intervals(setup, monkeypatch, change, message):
    config, _ = setup
    document = plan_document()
    change(document)
    calls = fake_engines(monkeypatch, config)
    with pytest.raises(ValueError, match=message):
        workflow.run_experiment(config, ExperimentPlan.from_dict(document))
    assert calls == []


def test_future_prices_do_not_change_validation_signals_and_labels_cannot_exit_period(setup):
    config, plan = setup
    market, _, calendar, _ = workflow.load_configured_snapshot(config)
    for candidate in plan.candidates:
        financials = fundamentals_for(config.financials, candidate.factors, market)
        before = market_factors(market, "fixture", candidate.factors, financials=financials)
        changed = market.copy()
        future = changed.date > pd.Timestamp("2025-02-04")
        changed.loc[future, ["open", "close", "adj_factor", "pe_ttm"]] *= 100
        after = market_factors(changed, "fixture", candidate.factors, financials=financials)
        assert [r for r in before["history"] if r["date"] <= "2025-02-04"] == [
            r for r in after["history"] if r["date"] <= "2025-02-04"
        ]
    labels = forward_open_labels(market, calendar.loc[calendar.is_open, "date"], horizon=3, end="2025-02-04")
    valid = labels.loc[labels.forward_return.notna()]
    assert (pd.to_datetime(valid.exit_date) <= pd.Timestamp("2025-02-04")).all()


def test_native_split_accounts_diagnostics_report_and_readonly_api(setup):
    config, plan = setup
    completed = workflow.run_experiment(config, plan)
    store = ResultStore(config.runs_dir)
    result = store.get("experiments", completed["run_id"])
    assert len(result["rows"]) == 5
    for row in result["rows"]:
        backtest = store.get("backtests", row["backtest_run_id"])
        assert len(backtest["equity"]) == row["trading_days"] + 1
        assert backtest["equity"][0]["equity"] == 1
        assert backtest["metadata"]["config"]["account"] == 150000
        assert backtest["metadata"]["provenance"]["open_cost"] == 0.002
        evaluation = store.get("evaluations", row["evaluation_run_id"])
        period = result["plan"]["periods"][row["period"]]
        for label in evaluation["labels"].values():
            for observation in label["observations"]:
                assert period["start"] <= observation["entry_date"] <= observation["exit_date"] <= period["end"]
    client = TestClient(make_app(config.runs_dir))
    url = "/api/v1/experiments/" + completed["run_id"]
    assert client.get("/api/v1/experiments").json()["total"] == 1
    assert client.get(url).json() == result
    assert client.get(url + "/download").json() == result
    report = client.get(url + "/report")
    assert report.text == experiment_markdown(result)
    assert "不能拼接净值" in report.text and "不代表全新未见样本" in report.text
    assert client.get("/api/v1/experiments/missing").status_code == 404
    assert client.post("/api/v1/experiments").status_code == 405
    assert workflow.run_experiment(config, plan)["reused"]


def test_experiment_checkpoints_are_immutable_and_identifier_guarded(tmp_path):
    store = ResultStore(tmp_path)
    store.publish_experiment_record("split-test", "plan", {"value": 1})
    store.publish_experiment_record("split-test", "plan", {"value": 1})
    with pytest.raises(FileExistsError):
        store.publish_experiment_record("split-test", "plan", {"value": 2})
    with pytest.raises(ValueError):
        store.publish_experiment_record("split-test", "../../escape", {})
    with pytest.raises(ValueError):
        store.publish_experiment_record("../escape", "selection", {})


def experiment_request(snapshot_id):
    document = plan_document()
    return {
        "name": document["name"],
        "data": {"mode": "snapshot", "snapshot_id": snapshot_id, "codes": ["600000.SH", "000001.SZ", "600519.SH"]},
        "backtest": {"topk": 1, "initial_cash": 150000},
        "plan": {"periods": document["periods"], "candidates": document["candidates"]},
        "evaluation": {"horizons": [1, 3]},
    }


def test_web_experiment_with_discontiguous_periods_queues_then_fails_at_prepare(tmp_path):
    """Ranges need the trading calendar, so the check belongs to the worker's prepare stage."""

    data, runs = tmp_path / "data", tmp_path / "runs"
    manifest = publish_fixture_snapshot(
        data, ("600000.SH", "000001.SZ", "600519.SH"), "20241202", "20250228", provider=Provider()
    )
    seed_earnings(financial_root(data), ("600000.SH", "000001.SZ", "600519.SH"))
    service = make_service(runs, data)
    client = TestClient(make_app(runs, data, service=service), base_url="http://127.0.0.1")

    invalid = json.loads(json.dumps(experiment_request(manifest["id"])))
    invalid["plan"]["periods"]["validation"]["start"] = "2025-01-21"
    response = client.post(
        "/api/v1/research/experiments", json=invalid, headers={"Idempotency-Key": "experiment-bad-2"}
    )
    assert response.status_code == 202, response.text
    task = response.json()
    assert service.store.claim() == task["id"]
    finished = run_claimed(service, task["id"])
    assert finished["status"] == "failed"
    assert "首尾相接" in (finished["error"] or "")


def test_web_experiment_submit_never_touches_heavy_paths(tmp_path, monkeypatch):
    """A comparison submission must only do cheap checks: every heavy path is a sentinel."""

    data, runs = tmp_path / "data", tmp_path / "runs"
    manifest = publish_fixture_snapshot(
        data, ("600000.SH", "000001.SZ", "600519.SH"), "20241202", "20250228", provider=Provider()
    )
    seed_earnings(financial_root(data), ("600000.SH", "000001.SZ", "600519.SH"))
    service = make_service(runs, data)
    client = TestClient(make_app(runs, data, service=service), base_url="http://127.0.0.1")

    touched = []

    def sentinel(name):
        def boom(*_args, **_kwargs):
            touched.append(name)
            raise AssertionError(f"{name} must not run during submit")

        return boom

    for name in (
        "snapshot_registry",
        "validate_comparison_inputs",
        "dynamic_preflight",
        "_preflight",
        "selection_validation",
        "validate_selection_inputs",
        "_pin_candidates",
    ):
        if hasattr(experiment_tasks, name):
            monkeypatch.setattr(experiment_tasks, name, sentinel(name))

    response = client.post(
        "/api/v1/research/experiments",
        json=experiment_request(manifest["id"]),
        headers={"Idempotency-Key": "experiment-cheap-1"},
    )
    assert response.status_code == 202, response.text
    assert touched == []
    assert service.store.list()["total"] == 1


@pytest.mark.parametrize("filtered", [False, True], ids=["fixed", "filtered"])
def test_frozen_snapshot_survives_a_newer_publish(tmp_path, monkeypatch, filtered):
    """B is published after the submit; the queued task must still run the frozen A."""

    data, runs = tmp_path / "data", tmp_path / "runs"
    first = publish_fixture_snapshot(
        data, ("600000.SH", "000001.SZ", "600519.SH"), "20241202", "20250228", provider=Provider()
    )
    seed_earnings(financial_root(data), ("600000.SH", "000001.SZ", "600519.SH"))
    service = make_service(runs, data)
    client = TestClient(make_app(runs, data, service=service), base_url="http://127.0.0.1")

    request = experiment_request(first["id"])
    if filtered:
        request["data"]["filters"] = UniverseFilters(exclude_st=False, min_listing_days=0).as_dict()
    response = client.post(
        "/api/v1/research/experiments", json=request, headers={"Idempotency-Key": "experiment-frozen-a"}
    )
    assert response.status_code == 202, response.text
    task = response.json()

    # Publishing a different range gives B distinguishable input, not a republish of A.
    second = publish_fixture_snapshot(
        data, ("600000.SH", "000001.SZ", "600519.SH"), "20241202", "20250214", provider=Provider(), name="second"
    )
    assert second["id"] != first["id"] and second["files"] != first["files"]
    root_a = data / "research/downloads/fixture" / first["snapshot_path"]
    root_b = data / "research/downloads/second" / second["snapshot_path"]
    market_a = pd.read_parquet(root_a / "market.parquet")
    calendar_a = pd.read_parquet(root_a / "calendar.parquet")
    parquet_read = pd.read_parquet
    preflight, dynamic_preflight = experiment_tasks._preflight, experiment_tasks.dynamic_preflight
    reads = []
    checked = []

    def no_registry(*args, **kwargs):
        pytest.fail("frozen A must bypass snapshot_registry")

    def trace_read(path, *args, **kwargs):
        if isinstance(path, (str, pathlib.Path)):
            path = pathlib.Path(path).resolve()
            assert not path.is_relative_to(root_b)
            if path.parent == root_a:
                reads.append(path.name)
        return parquet_read(path, *args, **kwargs)

    def assert_preflight(config, plan, market, calendar):
        assert config.data.root == root_a
        pd.testing.assert_frame_equal(market, market_a)
        pd.testing.assert_frame_equal(calendar, calendar_a)
        assert set(reads) == set(first["files"])
        checked.append(config.data.root)
        return preflight(config, plan, market, calendar)

    def assert_dynamic(config, plan, research, *, expected_sha=None, frozen_snapshot=None):
        assert frozen_snapshot.root == config.data.root == root_a
        pd.testing.assert_frame_equal(frozen_snapshot.inputs[0], market_a)
        pd.testing.assert_frame_equal(frozen_snapshot.inputs[2], calendar_a)
        assert set(reads) == set(first["files"])
        checked.append(config.data.root)
        metadata, counts, coverage = dynamic_preflight(
            config, plan, research, expected_sha=expected_sha, frozen_snapshot=frozen_snapshot
        )
        assert metadata["manifest_sha256"] == expected_sha == frozen_snapshot.manifest_sha256
        return metadata, counts, coverage

    monkeypatch.setattr(research_requests, "snapshot_registry", no_registry)
    monkeypatch.setattr(experiment_tasks, "snapshot_registry", no_registry)
    monkeypatch.setattr(pd, "read_parquet", trace_read)
    monkeypatch.setattr(experiment_tasks, "_preflight", assert_preflight)
    monkeypatch.setattr(experiment_tasks, "dynamic_preflight", assert_dynamic)
    assert service.store.claim() == task["id"]
    finished = run_claimed(service, task["id"])
    assert finished["status"] == "succeeded", finished["error"]
    assert checked == [root_a]
    assert finished["result"]["data_version"] == first["id"]
    saved = ResultStore(runs).get("experiments", finished["result"]["experiment_run_id"])
    assert saved["metadata"]["provenance"]["data_version"] == first["id"]
    assert saved["metadata"]["provenance"]["data_checksums"] == first["files"]


def _worker_fixture(tmp_path):
    data, runs = tmp_path / "data", tmp_path / "runs"
    codes = ("600000.SH", "000001.SZ", "600519.SH")
    manifest = publish_fixture_snapshot(data, codes, "20241202", "20250228", provider=Provider())
    seed_earnings(financial_root(data), codes)
    document = experiment_request(manifest["id"])
    # If the worker reaches projection, it must create a real subset, not reuse the whole leaf.
    document["data"]["codes"] = list(codes[:2])
    return make_service(runs, data), document, manifest


def _submitted(tmp_path, key):
    service, document, _ = _worker_fixture(tmp_path)
    client = TestClient(make_app(service.runs_root, service.data_root, service=service), base_url="http://127.0.0.1")
    response = client.post("/api/v1/research/experiments", json=document, headers={"Idempotency-Key": key})
    assert response.status_code == 202, response.text
    return service, response.json()["id"]


def _unexpected(effect):
    def fail(*args, **kwargs):
        pytest.fail(f"unexpected {effect}")

    return fail


def _run_without_side_effects(service, identifier, monkeypatch):
    data, runs = service.data_root, service.runs_root
    paths_before = set(data.rglob("*"))
    assert not (data / "research/subsets").exists()
    assert not runs.exists()
    repository = Mock(spec_set=MarketRepository)
    monkeypatch.setattr(task_context, "project_snapshot", _unexpected("snapshot projection"))
    for method in ("publish", "publish_report", "publish_experiment_record"):
        monkeypatch.setattr(ResultStore, method, _unexpected(f"result write: {method}"))

    task_worker.run_task(
        runs,
        data,
        identifier,
        task_store=service.store,
        result_store_factory=lambda dataset_id=None: ResultStore(runs),
        market_repository=repository,
    )

    assert repository.mock_calls == [], "worker unexpectedly registered or froze data"
    assert set(data.rglob("*")) == paths_before, "worker changed the data tree"
    assert {path.relative_to(runs).as_posix() for path in runs.rglob("*")} <= {"tasks", "tasks/execution.lock"}
    row = service.store.get(identifier, private=True)
    assert row["result"] is None
    return row


@pytest.mark.parametrize(
    ("damage", "error"),
    [
        ("missing", "不可用"),
        ("quarantined", "已被隔离"),
        ("manifest-changed", "排队期间股票池历史状态发生变化"),
        ("market-bytes-changed", "checksum mismatch"),
    ],
)
def test_frozen_snapshot_failure_stays_pending_without_side_effects(tmp_path, monkeypatch, damage, error):
    service, identifier = _submitted(tmp_path, "frozen-worker-failure")
    execution = service.store.get(identifier, private=True)["execution"]
    assert execution["prepare_state"] == "pending"
    leaf = service.data_root / execution["snapshot_dir"]
    manifest_path = leaf / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    if damage == "missing":
        shutil.rmtree(leaf)
    elif damage == "quarantined":
        (leaf / "rejected.json").write_text("{}", encoding="utf-8")
        assert manifest_path.read_bytes() == manifest_bytes
    elif damage == "manifest-changed":
        # A semantically identical manifest with different raw bytes is still not the frozen manifest.
        manifest_path.write_bytes(manifest_bytes + b"\n")
    else:
        market_path = leaf / "market.parquet"
        market_path.write_bytes(market_path.read_bytes() + b"modified")
        assert manifest_path.read_bytes() == manifest_bytes

    monkeypatch.setattr(experiment_tasks, "validate_experiment_inputs", _unexpected("preflight with invalid data"))
    monkeypatch.setattr(service.store, "save_prepared", _unexpected("preparation write for invalid data"))
    assert service.store.claim() == identifier
    finished = _run_without_side_effects(service, identifier, monkeypatch)
    assert finished["status"] == "failed"
    assert finished["error_details"]["stage"] == "prepare"
    assert error in finished["error"]
    assert finished["execution"] == execution


def test_worker_prepared_write_conflict_leaves_terminal_row_and_no_side_effects(tmp_path, monkeypatch):
    service, identifier = _submitted(tmp_path, "frozen-worker-conflict")
    original = service.store.get(identifier, private=True)["execution"]
    save_prepared = service.store.save_prepared
    conflicts = []

    def lose_conditional_write(task_id, execution):
        assert task_id == identifier
        assert execution["prepare_state"] == "prepared"
        assert execution["manifest_sha256"] == original["manifest_sha256"]
        assert service.store.get(task_id)["status"] == "running"
        # Simulate recovery winning the race after worker preflight, before the actual store call.
        service.store.finish(task_id, "interrupted", error="recovery won")
        try:
            save_prepared(task_id, execution)
        except TaskConflict:
            conflicts.append(task_id)
            raise

    monkeypatch.setattr(service.store, "save_prepared", lose_conditional_write)
    assert service.store.claim() == identifier
    finished = _run_without_side_effects(service, identifier, monkeypatch)
    assert conflicts == [identifier]
    assert finished["status"] == "interrupted" and finished["error"] == "recovery won"
    assert finished["execution"] == original


def test_worker_rejects_already_prepared_payload_without_side_effects(tmp_path, monkeypatch):
    service, identifier = _submitted(tmp_path, "frozen-worker-already-prepared")
    row = service.store.get(identifier, private=True)
    assert service.store.claim() == identifier
    # Create the state a later dispatch would see after a previous worker wrote its preparation.
    leaf = service.data_root / row["execution"]["snapshot_dir"]
    frozen = experiment_tasks.load_frozen_snapshot(service.data_root, row["execution"])
    _, execution = experiment_tasks.validate_experiment_inputs(
        row["request"], service.data_root, service.runs_root, frozen_snapshot=frozen
    )
    assert pathlib.Path(execution["snapshot_dir"]) == leaf
    execution["prepare_state"] = "prepared"
    service.store.save_prepared(identifier, execution)

    monkeypatch.setattr(service.store, "save_prepared", _unexpected("second preparation write"))
    monkeypatch.setattr(experiment_tasks, "validate_experiment_inputs", _unexpected("preflight repeated"))
    finished = _run_without_side_effects(service, identifier, monkeypatch)
    assert finished["status"] == "failed"
    assert finished["error_details"]["stage"] == "prepare"
    assert "准备结果已记录" in finished["error"]
    assert finished["execution"] == execution


def test_worker_accepts_legacy_prevalidated_experiment_without_preparation_write(tmp_path, monkeypatch):
    service, document, manifest = _worker_fixture(tmp_path)
    # Before async submission, POST queued the fully normalized pair from this validator.
    request, execution = experiment_tasks.validate_experiment_request(document, service.data_root, service.runs_root)
    assert "prepare_state" not in execution and pathlib.Path(execution["snapshot_dir"]).is_absolute()
    task = service.store.create_experiment(request, execution, "frozen-worker-legacy", total=6)
    monkeypatch.setattr(service.store, "save_prepared", _unexpected("preparation write for legacy task"))
    monkeypatch.setattr(experiment_tasks, "load_frozen_snapshot", _unexpected("loading pending pin for legacy task"))
    monkeypatch.setattr(
        experiment_tasks, "validate_experiment_inputs", _unexpected("preflight repeated for legacy task")
    )

    assert service.store.claim() == task["id"]
    finished = run_claimed(service, task["id"])
    assert finished["status"] == "succeeded", finished["error"]
    assert finished["progress"] == {"completed": 6, "total": 6}
    assert service.store.get(task["id"], private=True)["execution"] == execution
    saved = ResultStore(service.runs_root).get("experiments", finished["result"]["experiment_run_id"], hydrate=False)
    assert len(saved["rows"]) == 5
    subsets = list((service.data_root / "research/subsets").glob("*/manifest.json"))
    assert len(subsets) == 1
    projection = json.loads(subsets[0].read_bytes())["projection"]
    assert projection["parent_data_version"] == manifest["id"]
    assert projection["codes"] == sorted(document["data"]["codes"])


def test_web_experiment_task_validates_queues_runs_and_exposes_results(tmp_path):

    data, runs = tmp_path / "data", tmp_path / "runs"
    manifest = publish_fixture_snapshot(
        data, ("600000.SH", "000001.SZ", "600519.SH"), "20241202", "20250228", provider=Provider()
    )
    seed_earnings(financial_root(data), ("600000.SH", "000001.SZ", "600519.SH"))
    service = make_service(runs, data)
    client = TestClient(make_app(runs, data, service=service), base_url="http://127.0.0.1")
    body = experiment_request(manifest["id"])
    # Structural errors are still rejected by the cheap request-path validation.
    structural = json.loads(json.dumps(body))
    structural["plan"]["candidates"] = structural["plan"]["candidates"][:1]
    response = client.post(
        "/api/v1/research/experiments", json=structural, headers={"Idempotency-Key": "experiment-bad-1"}
    )
    assert response.status_code == 422
    assert service.store.list()["total"] == 0 and not runs.exists()
    response = client.post("/api/v1/research/experiments", json=body, headers={"Idempotency-Key": "experiment-ok-1"})
    assert response.status_code == 202, response.text
    task = response.json()
    assert task["kind"] == "experiment" and task["progress"] == {"completed": 0, "total": 6}
    assert [t["id"] for t in client.get("/api/v1/research/tasks").json()["items"]] == [task["id"]]
    assert service.store.claim() == task["id"]
    finished = run_claimed(service, task["id"])
    assert finished["status"] == "succeeded", finished["error"]
    assert finished["progress"] == {"completed": 6, "total": 6}
    run_id = finished["result"]["experiment_run_id"]
    saved = client.get("/api/v1/experiments/" + run_id).json()
    assert saved["selection"]["candidate_id"] == finished["result"]["selected_candidate"]
    assert len(saved["rows"]) == 5 and client.get("/api/v1/experiments").json()["total"] == 1
    assert all(client.get("/api/v1/backtests/" + row["backtest_run_id"]).status_code == 200 for row in saved["rows"])
