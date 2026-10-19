import json
from dataclasses import replace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from finance_quant.api import create_app
from finance_quant.application import experiment_workflow as workflow
from finance_quant.configuration import BacktestConfig, DataConfig, ResearchConfig
from finance_quant.data.managed import download_dataset
from finance_quant.research.evaluation import forward_open_labels
from finance_quant.research.experiments import ExperimentPlan, experiment_markdown, load_experiment_plan, select_candidate
from finance_quant.research.market_factors import market_factors
from finance_quant.research.registry import EvaluationConfig
from finance_quant.storage import ResultStore
from test_managed_data import Provider


def plan_document():
    return {"name": "工程分段测试", "selection_metric": "validation_excess_return",
            "periods": {"development": {"start": "2025-01-02", "end": "2025-01-17"},
                        "validation": {"start": "2025-01-20", "end": "2025-02-04"},
                        "test": {"start": "2025-02-05", "end": "2025-02-28"}},
            "candidates": [{"id": "value", "name": "估值", "factors": [{"name": "value", "weight": 1}]},
                           {"id": "momentum", "name": "动量", "factors": [{"name": "momentum", "weight": 1, "window": 5}]}]}


@pytest.fixture
def setup(tmp_path):
    config = ResearchConfig(DataConfig(tmp_path / "data", "20241202", "20250228",
                                       ("600000.SH", "000001.SZ", "600519.SH")),
                            BacktestConfig("2025-01-02", "2025-02-28", topk=1, initial_cash=150000,
                                           open_cost=.002, close_cost=.003, min_cost=8),
                            tmp_path / "runs", evaluation=EvaluationConfig(horizons=(1, 3)))
    download_dataset(config.data, provider=Provider())
    return config, ExperimentPlan.from_dict(plan_document())


@pytest.mark.parametrize("change", [
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
])
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
    with pytest.raises(ValueError, match="duplicate"):
        load_experiment_plan(path)


def metric_row(candidate, period, total, benchmark=.01):
    return {"candidate_id": candidate, "period": period,
            "metrics": {"total_return": total, "benchmark_return": benchmark, "excess_return": 999}}


def test_selection_uses_only_validation_returns_and_deterministic_tie_rule():
    rows = [metric_row("b", "validation", .08), metric_row("a", "validation", .08),
            metric_row("b", "test", 100), metric_row("b", "development", 100)]
    result = select_candidate(rows, ["b", "a"])
    assert result["candidate_id"] == "a" and result["score"] == pytest.approx(.07)
    rows[-2]["metrics"] = {"total_return": float("nan")}
    rows[-1]["metrics"] = {}
    assert select_candidate(rows, ["a", "b"]) == result
    with pytest.raises(ValueError, match="exactly one"):
        select_candidate(rows + [rows[0]], ["a", "b"])
    with pytest.raises(ValueError, match="exactly one"):
        select_candidate(rows, ["a", "b", "c"])
    for invalid in (float("nan"), float("inf"), True, "0.2"):
        rows[0]["metrics"]["total_return"] = invalid
        with pytest.raises(ValueError, match="finite"):
            select_candidate(rows, ["a", "b"])


def fake_engines(monkeypatch, config, *, fail_test=False):
    store = ResultStore(config.runs_dir)
    calls = []
    state = {"fail_test": fail_test}

    def backtest(research):
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
        total = .05 if research.factors.specs[0].name == "value" else -.02
        store.publish("backtests", bid, {"summary": {"metrics": {"total_return": total, "benchmark_return": .01,
                      "max_drawdown": -.03, "total_cost_amount": 80.0, "avg_turnover": .1}}})
        return {"run_id": bid, "factor_run_id": fid}

    def evaluation(research):
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
    with pytest.raises(ValueError, match="artifact has changed"):
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


@pytest.mark.parametrize("change,message", [
    (lambda d: d["periods"]["validation"].update(start="2025-01-21"), "without gaps"),
    (lambda d: d["periods"]["validation"].update(start="2025-01-19"), "actual trading"),
    (lambda d: d["periods"]["test"].update(end="2025-03-03"), "configured backtest"),
    (lambda d: d["periods"]["test"].update(end="2025-02-07"), "longest evaluation"),
    (lambda d: d["candidates"][1]["factors"][0].update(window=252), "lacks scores"),
])
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
        before = market_factors(market, "fixture", candidate.factors)
        changed = market.copy()
        future = changed.date > pd.Timestamp("2025-02-04")
        changed.loc[future, ["open", "close", "adj_factor", "pe_ttm"]] *= 100
        after = market_factors(changed, "fixture", candidate.factors)
        assert [r for r in before["history"] if r["date"] <= "2025-02-04"] == [r for r in after["history"] if r["date"] <= "2025-02-04"]
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
        assert backtest["metadata"]["provenance"]["open_cost"] == .002
        evaluation = store.get("evaluations", row["evaluation_run_id"])
        period = result["plan"]["periods"][row["period"]]
        for label in evaluation["labels"].values():
            for observation in label["observations"]:
                assert period["start"] <= observation["entry_date"] <= observation["exit_date"] <= period["end"]
    client = TestClient(create_app(config.runs_dir))
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
