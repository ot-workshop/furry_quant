"""Gross-margin policies through different pools and an entire native research task."""

import copy
import json

import pytest
from fastapi.testclient import TestClient
from test_dynamic_experiments import pool

from finance_quant.data.financials import financial_root
from finance_quant.storage import ResultStore
from support.fakes import make_app, make_service, run_claimed
from support.financial import income, publish


def seed_financials(data, codes):
    variants = (
        {"comp_type": "2", "oper_cost": None},
        {"revenue": 100, "total_revenue": 200, "oper_cost": 60},
        {"oper_cost": None},
        {"oper_cost": 120},
    )
    return publish(
        financial_root(data),
        [income(code, period="20231231", announce="20240103", **variants[i % 4]) for i, code in enumerate(codes)],
        seen_on="2024-01-03",
    )


@pytest.mark.parametrize("mode", ["snapshot", "index_warehouse", "market_warehouse"])
def test_gross_policies_survive_queue_scoring_execution_and_evaluation(tmp_path, workspace, mode):
    if mode == "snapshot":
        data, runs, source = workspace
        document = copy.deepcopy(source)
        document["data"]["codes"] = ["600000.SH", "000001.SZ", "600519.SH", "600036.SH"]
        document["backtest"].update(start="2025-01-02", end="2025-01-17", topk=1, n_drop=0)
        codes = document["data"]["codes"]
    else:
        data, root, plan = pool(tmp_path, mode, 6)
        runs = tmp_path / "runs"
        document = {
            "name": "毛利率对照",
            "data": plan["data"],
            "backtest": {**plan["backtest"], "start": "2024-01-08", "end": "2024-02-02"},
            "evaluation": plan["evaluation"],
        }
        codes = list(json.loads((root / "manifest.json").read_bytes())["assets"])
    document["factors"] = [
        {"name": "momentum", "weight": 0.5, "window": 2},
        {"name": "gross_margin_ttm", "weight": 0.5},
    ]
    seed_financials(data, codes)
    before = {path: path.read_bytes() for path in data.rglob("*") if path.is_file()}
    service = make_service(runs, data)
    client = TestClient(make_app(runs, data, service=service), base_url="http://127.0.0.1")
    store, saved = ResultStore(runs), {}
    for policy in ("zero", "neutral", "exclude"):
        document["factors"][1]["missing_policy"] = policy
        preview = client.post(
            "/api/v1/research/factor-coverage",
            json={
                "data": document["data"],
                "start": document["backtest"]["start"],
                "factor": {"name": "gross_margin_ttm", "missing_policy": policy},
            },
        )
        assert preview.status_code == 200, preview.text
        counts = preview.json()
        assert counts["available"] + counts["not_applicable"] + counts["unavailable"] == counts["total"]
        assert counts["not_applicable"] > 0
        submitted = client.post("/api/v1/research/tasks", json=document, headers={"Idempotency-Key": "gross-" + policy})
        assert submitted.status_code == 202, submitted.text
        identifier = submitted.json()["id"]
        assert service.store.claim() == identifier
        task = run_claimed(service, identifier)
        assert task["status"] == "succeeded", task.get("error")
        result = task["result"]
        factor_id = result["factor_run_id"]
        factors = store.get("factors", factor_id)
        bank = [
            row for row in factors["history"] if row["factor_details"]["gross_margin_ttm"]["status"] == "not_applicable"
        ]
        assert bank and all(
            row["scores"]["gross_margin_ttm"] == 50 and row["raw"]["gross_margin_ttm"] is None for row in bank
        )
        missing = [
            row for row in factors["history"] if row["factor_details"]["gross_margin_ttm"]["status"] == "missing"
        ]
        assert (
            not missing
            if policy == "exclude"
            else missing
            and all(row["scores"]["gross_margin_ttm"] == (0 if policy == "zero" else 50) for row in missing)
        )
        assert any(
            row["raw"]["gross_margin_ttm"] == pytest.approx(0.4)
            for row in factors["history"]
            if row["raw"]["gross_margin_ttm"] is not None
        )
        scores = client.get(f"/api/v1/factor-runs/{factor_id}/scores").json()
        assert scores["metadata"]["coverage"]["factors"]["gross_margin_ttm"]["missing_policy"] == policy
        for category, key in (
            ("factors", "factor_run_id"),
            ("backtests", "backtest_run_id"),
            ("evaluations", "evaluation_run_id"),
        ):
            run_id = result[key]
            if category != "factors":
                assert client.get(f"/api/v1/{category}/{run_id}").status_code == 200
            saved[category, run_id] = store.digest(category, run_id)
    assert all(store.digest(category, identifier) == digest for (category, identifier), digest in saved.items())
    assert all(path.read_bytes() == content for path, content in before.items())
