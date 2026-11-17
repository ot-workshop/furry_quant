"""Research submission, native Qlib execution and score details across stock pools."""

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
    profits = (20_000_000.0, 0.0, -10_000_000.0, None)
    return publish(
        financial_root(data),
        [
            income(code, period="20231231", announce="20240103", n_income_attr_p=profits[i % 4])
            for i, code in enumerate(codes)
        ],
        seen_on="2024-01-03",
    )


@pytest.mark.parametrize("mode", ["snapshot", "index_warehouse", "market_warehouse"])
def test_policies_flow_from_web_to_scores_backtest_and_evaluation_without_overwriting(tmp_path, workspace, mode):
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
            "name": "盈利收益率对照",
            "data": plan["data"],
            "backtest": {**plan["backtest"], "start": "2024-01-08", "end": "2024-02-02"},
            "evaluation": plan["evaluation"],
        }
        codes = list(json.loads((root / "manifest.json").read_bytes())["assets"])
    document["factors"] = [{"name": "momentum", "weight": 0.5, "window": 2}, {"name": "value", "weight": 0.5}]
    seed_financials(data, codes)
    before = {path: path.read_bytes() for path in data.rglob("*") if path.is_file()}
    service = make_service(runs, data)
    client = TestClient(make_app(runs, data, service=service), base_url="http://127.0.0.1")
    store = ResultStore(runs)
    saved, ids = {}, set()
    for policy in ("zero", "neutral", "exclude"):
        document["factors"][1]["missing_policy"] = policy
        response = client.post(
            "/api/v1/research/tasks", json=document, headers={"Idempotency-Key": f"earnings-workflow-{mode}-{policy}"}
        )
        assert response.status_code == 202, response.text
        identifier = response.json()["id"]
        assert service.store.claim() == identifier
        task = run_claimed(service, identifier)
        assert task["status"] == "succeeded", task.get("error")
        result = task["result"]
        factor_id = result["factor_run_id"]
        ids.add(factor_id)
        factors = store.get("factors", factor_id)
        assert (
            next(item for item in factors["metadata"]["factor_config"] if item["name"] == "value")["missing_policy"]
            == policy
        )
        assert any(row["raw"]["value"] == 0 for row in factors["history"])
        assert any(row["raw"]["value"] is not None and row["raw"]["value"] < 0 for row in factors["history"])
        if policy != "exclude":
            filled = [row for row in factors["history"] if row["raw"]["value"] is None]
            assert filled and all(row["scores"]["value"] == (0 if policy == "zero" else 50) for row in filled)
            assert all(row["factor_details"]["value"]["imputed"] for row in filled)
        score = client.get(f"/api/v1/factor-runs/{factor_id}/scores")
        assert (
            score.status_code == 200
            and score.json()["metadata"]["coverage"]["factors"]["value"]["missing_policy"] == policy
        )
        assert client.get(f"/api/v1/backtests/{result['backtest_run_id']}").status_code == 200
        assert client.get(f"/api/v1/evaluations/{result['evaluation_run_id']}").status_code == 200
        saved.update(
            {
                (category, run_id): store.digest(category, run_id)
                for category, run_id in (
                    ("factors", factor_id),
                    ("backtests", result["backtest_run_id"]),
                    ("evaluations", result["evaluation_run_id"]),
                )
            }
        )
    assert len(ids) == 3
    assert all(store.digest(category, identifier) == digest for (category, identifier), digest in saved.items())
    assert all(path.read_bytes() == content for path, content in before.items())
