"""Paired daily samples through API, native Qlib, result links and immutable reuse."""

import copy
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from test_dynamic_experiments import pool
from test_industry_neutralization import history

from finance_quant.application.comparison_inputs import prepare_comparison
from finance_quant.application.experiment_tasks import _plan, dynamic_data, validate_experiment_request
from finance_quant.application.research_requests import request_config
from finance_quant.data.financials import FinancialBinding, financial_root
from finance_quant.data.industries import publish_industries
from finance_quant.research.experiments import ExperimentPlan, experiment_markdown
from finance_quant.storage import ResultStore
from support.fakes import make_app, make_service, run_claimed
from support.financial import income, publish


def setup_comparison(tmp_path, workspace, mode, monkeypatch):
    if mode == "snapshot":
        data, runs, research = workspace
        document = {key: copy.deepcopy(research[key]) for key in ("name", "data", "evaluation")}
        document["data"]["codes"] = ["600000.SH", "000001.SZ", "600519.SH", "600036.SH"]
        codes = document["data"]["codes"]
        start, end = "2025-01-02", "2025-01-17"
    else:
        data, root, source = pool(tmp_path, mode, 6)
        runs = tmp_path / "runs"
        codes = sorted(json.loads((root / "manifest.json").read_bytes())["assets"])
        document = {key: source[key] for key in ("name", "data", "evaluation")}
        start, end = "2024-01-08", "2024-02-02"
    publish(
        financial_root(data),
        [
            income(code, period="20231231", announce="20240103", oper_cost=None if code == "600000.SH" else 40 + i * 4)
            for i, code in enumerate(codes)
        ],
        seen_on="2024-01-03",
    )
    industry_root = data / "industries"
    monkeypatch.setenv("FINANCE_QUANT_INDUSTRY_ROOT", str(industry_root))
    publish_industries(history([code[-2:] + code[:6] for code in codes]).frame, root=industry_root)
    factors = [{"name": "momentum", "window": 2, "weight": 0.5}, {"name": "gross_margin_ttm", "weight": 0.5}]
    document.update(
        name="收益来源对照",
        backtest={"topk": 1, "n_drop": 0, "initial_cash": 100000},
        plan={
            "periods": {"analysis": {"start": start, "end": end}},
            "comparison": {"baseline_id": "base", "sample_mode": "common"},
            "candidates": [
                {"id": "base", "name": "缺失零分", "factors": factors, "neutralization": {"mode": "off"}},
                {
                    "id": "strict",
                    "name": "缺失剔除",
                    "factors": [
                        {**f, **({"missing_policy": "exclude"} if f["name"] == "gross_margin_ttm" else {})}
                        for f in factors
                    ],
                    "neutralization": {"mode": "off"},
                },
            ],
        },
    )
    return data, runs, document


def execute(client, service, document, key):
    submitted = client.post(
        "/api/v1/research/experiments", json=document, headers={"Idempotency-Key": "return-comparison-" + key}
    )
    assert submitted.status_code == 202, submitted.text
    identifier = submitted.json()["id"]
    assert service.store.claim() == identifier
    task = run_claimed(service, identifier)
    assert task["status"] == "succeeded", task.get("error_details")
    response = client.get(f"/api/v1/experiments/{task['result']['experiment_run_id']}")
    assert response.status_code == 200
    return task, response.json()


@pytest.mark.parametrize("mode", ["snapshot", "index_warehouse", "market_warehouse"])
def test_missing_policy_paired_controls_use_identical_causal_samples(tmp_path, workspace, monkeypatch, mode):
    data, runs, document = setup_comparison(tmp_path, workspace, mode, monkeypatch)
    before = {p: p.read_bytes() for p in data.rglob("*") if p.is_file()}
    service = make_service(runs, data)
    client = TestClient(make_app(runs, data, service=service), base_url="http://127.0.0.1")
    task, result = execute(client, service, document, "paired")
    assert task["progress"]["completed"] == task["progress"]["total"]
    assert result["metadata"]["validation_kind"] == "return_comparison"
    assert result["selection"] is None and len(result["rows"]) == 4
    assert any(day["candidates"]["base"] > day["candidates"]["strict"] for day in result["sample_days"])
    store = ResultStore(runs)
    common = [row for row in result["rows"] if row["sample"] == "common"]
    factors = [store.get("factors", row["factor_run_id"]) for row in common]
    observations = [{(row["date"], row["instrument"]) for row in batch["history"]} for batch in factors]
    assert observations[0] == observations[1]
    assert all(row["instrument"] != "SH600000" for batch in factors for row in batch["history"])
    for date, stock in observations[0]:
        scores = [
            next(row for row in batch["history"] if row["date"] == date and row["instrument"] == stock)
            for batch in factors
        ]
        assert scores[0]["scores"] == scores[1]["scores"]
    assert common[0]["metrics"] == common[1]["metrics"]
    independent_base = next(
        row for row in result["rows"] if row["candidate_id"] == "base" and row["sample"] == "independent"
    )
    independent = {
        (row["date"], row["instrument"]): row
        for row in store.get("factors", independent_base["factor_run_id"])["history"]
    }
    assert any(row["scores"] != independent[row["date"], row["instrument"]]["scores"] for row in factors[0]["history"])
    assert result["comparisons"][1]["common_difference"] == 0
    assert "收益来源对照" in experiment_markdown(result)
    for row in result["rows"]:
        for path, key in (
            ("backtests", "backtest_run_id"),
            ("evaluations", "evaluation_run_id"),
            ("factor-runs", "factor_run_id"),
        ):
            assert (
                client.get(f"/api/v1/{path}/{row[key]}" + ("/scores" if path == "factor-runs" else "")).status_code
                == 200
            )
    digest = store.digest("experiments", result["metadata"]["id"])
    again, repeated = execute(client, service, document, "paired-again")
    assert again["result"]["reused"] and repeated == result
    assert store.digest("experiments", result["metadata"]["id"]) == digest
    assert all(path.read_bytes() == content for path, content in before.items())


def test_industry_and_factor_controls_and_independent_mode_are_effective(tmp_path, workspace, monkeypatch):
    data, runs, document = setup_comparison(tmp_path, workspace, "market_warehouse", monkeypatch)
    root = data / "shared-market/versions/fixture/market-view"
    codes = sorted(json.loads((root / "manifest.json").read_bytes())["assets"])
    industries = history([c[-2:] + c[:6] for c in codes]).frame.assign(industry_code="I0", industry_name="同行业")
    publish_industries(industries, root=data / "industries")
    base = document["plan"]["candidates"][0]
    document["plan"]["candidates"] = [
        base,
        {**base, "id": "neutral", "name": "行业处理", "neutralization": {"mode": "industry"}},
        {**base, "id": "price", "name": "移除毛利率", "factors": [{"name": "momentum", "weight": 1, "window": 2}]},
    ]
    service = make_service(runs, data)
    client = TestClient(make_app(runs, data, service=service), base_url="http://127.0.0.1")
    _, result = execute(client, service, document, "industry-and-factors")
    store = ResultStore(runs)
    batches = {
        row["candidate_id"]: store.get("factors", row["factor_run_id"])
        for row in result["rows"]
        if row["sample"] == "common"
    }
    assert all("processed" in row for row in batches["neutral"]["history"])
    assert batches["base"]["history"] != batches["neutral"]["history"]
    assert set(batches["price"]["metadata"]["weights"]) == {"momentum"}
    independent_rows = {r["candidate_id"]: r for r in result["rows"] if r["sample"] == "independent"}
    document["plan"]["comparison"]["sample_mode"] = "independent"
    _, separate = execute(client, service, document, "independent-only")
    assert len(separate["rows"]) == 3
    for row in separate["rows"]:
        assert row["backtest_run_id"] == independent_rows[row["candidate_id"]]["backtest_run_id"]
        assert row["metrics"] == independent_rows[row["candidate_id"]]["metrics"]
    assert all(c["sample_difference"] is None and c["common_difference"] is None for c in separate["comparisons"])


def test_common_sample_does_not_use_future_reports_or_require_full_period_survivors(tmp_path, workspace, monkeypatch):
    data, runs, document = setup_comparison(tmp_path, workspace, "market_warehouse", monkeypatch)
    normalized, execution = validate_experiment_request(document, data, runs)
    root = execution["warehouse_dir"]
    config = request_config(
        {
            **normalized,
            "backtest": {**normalized["backtest"], **normalized["plan"]["periods"]["analysis"]},
            "factors": normalized["plan"]["candidates"][0]["factors"],
        },
        dynamic_data(root, normalized),
        runs,
        data,
    )
    plan = _plan(normalized)
    before = prepare_comparison(config, plan, normalized)
    codes = sorted(
        json.loads((data / "shared-market/versions/fixture/market-view/manifest.json").read_bytes())["assets"]
    )
    parts = [
        income(code, period="20231231", announce="20240103", oper_cost=None if code == "600000.SH" else 40 + i * 4)
        for i, code in enumerate(codes)
    ]
    future = publish(
        financial_root(data),
        [*parts, income("600000.SH", period="20231231", announce="20240115", oper_cost=60)],
        seen_on="2024-01-15",
    )
    after = prepare_comparison(
        replace(config, financials=FinancialBinding(financial_root(data), future["id"])), plan, normalized
    )
    assert {d: m for d, m in before["allowed"].items() if d <= "2024-01-15"} == {
        d: m for d, m in after["allowed"].items() if d <= "2024-01-15"
    }
    assert "SH600000" not in after["allowed"]["2024-01-15"] and "SH600000" in after["allowed"]["2024-01-16"]
    newcomer = "SH600102"
    assert any(newcomer in values for values in before["allowed"].values())
    assert any(newcomer not in values for values in before["allowed"].values())


def test_comparison_plan_accepts_industry_variants_but_not_unknown_fields():
    document = {
        "name": "对照",
        "selection_metric": "none",
        "periods": {"analysis": {"start": "2024-01-01", "end": "2024-02-01"}},
        "comparison": {"baseline_id": "base", "sample_mode": "common"},
        "candidates": [
            {
                "id": name,
                "name": name,
                "factors": [{"name": "momentum", "weight": 1, "window": 2}],
                "neutralization": {"mode": mode},
            }
            for name, mode in (("base", "off"), ("neutral", "industry"))
        ],
    }
    plan = ExperimentPlan.from_dict(document)
    assert ExperimentPlan.from_dict(plan.as_dict()).as_dict() == plan.as_dict()
    for patch in (
        {"comparison": {"baseline_id": "missing", "sample_mode": "common"}},
        {"selection_metric": "validation_excess_return"},
    ):
        with pytest.raises(ValueError):
            ExperimentPlan.from_dict(document | patch)


def test_no_common_sample_fails_explicitly_without_publishing_or_relaxing_rules(tmp_path, workspace, monkeypatch):
    data, runs, document = setup_comparison(tmp_path, workspace, "snapshot", monkeypatch)
    codes = document["data"]["codes"]
    publish(
        financial_root(data),
        [
            income(
                code,
                period="20231231",
                announce="20240103",
                n_income_attr_p=10 if i < 2 else None,
                oper_cost=None if i < 2 else 60,
            )
            for i, code in enumerate(codes)
        ],
        seen_on="2024-01-03",
    )
    document["plan"]["candidates"][0]["factors"] = [{"name": "value", "weight": 1, "missing_policy": "exclude"}]
    document["plan"]["candidates"][1]["factors"] = [
        {"name": "gross_margin_ttm", "weight": 1, "missing_policy": "exclude"}
    ]
    service = make_service(runs, data)
    client = TestClient(make_app(runs, data, service=service), base_url="http://127.0.0.1")
    response = client.post(
        "/api/v1/research/experiments", json=document, headers={"Idempotency-Key": "return-no-common-sample"}
    )
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    assert service.store.claim() == identifier
    task = run_claimed(service, identifier)
    assert task["status"] == "failed"
    assert "共同" in task["error"]
    assert ResultStore(runs).list("experiments") == []


def test_identical_daily_samples_reproduce_trades_and_equity_and_factor_changes_affect_returns(
    tmp_path, workspace, monkeypatch
):
    data, runs, document = setup_comparison(tmp_path, workspace, "market_warehouse", monkeypatch)
    document["plan"]["candidates"] = [
        {"id": "base", "name": "动量", "factors": [{"name": "momentum", "weight": 1, "window": 2}]},
        {"id": "reverse", "name": "反转", "factors": [{"name": "reversal", "weight": 1, "window": 2, "skip": 0}]},
    ]
    service = make_service(runs, data)
    client = TestClient(make_app(runs, data, service=service), base_url="http://127.0.0.1")
    _, result = execute(client, service, document, "same-samples-trades")
    store = ResultStore(runs)
    for candidate in ("base", "reverse"):
        pair = [row for row in result["rows"] if row["candidate_id"] == candidate]
        artifacts = [store.get("backtests", row["backtest_run_id"]) for row in pair]
        assert artifacts[0]["trades"] == artifacts[1]["trades"]
        assert artifacts[0]["equity"] == artifacts[1]["equity"]
    reverse = next(row for row in result["comparisons"] if row["candidate_id"] == "reverse")
    assert reverse["independent_difference"] != 0
    assert reverse["independent_difference"] == reverse["common_difference"]
    assert reverse["sample_difference"] == 0
