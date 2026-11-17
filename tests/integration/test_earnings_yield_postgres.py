"""Real PostgreSQL queue/results with independent missing policies and immutable old results."""

import copy
import os

import pytest
from fastapi.testclient import TestClient
from test_earnings_yield_workflow import seed_financials
from test_postgres_runtime import engine as engine

from finance_quant.api import create_app
from finance_quant.application.task_worker import run_task
from finance_quant.application.tasks import TaskService
from finance_quant.persistence.market import MarketRepository
from finance_quant.persistence.results import PgResultStore
from finance_quant.persistence.tasks import PgTaskStore

pytestmark = pytest.mark.skipif(
    os.environ.get("FINANCE_QUANT_TEST_POSTGRES") != "1", reason="requires isolated PostgreSQL"
)


def test_earnings_policy_survives_queue_native_execution_and_result_apis(engine, tmp_path, workspace):
    data, runs, source = workspace
    document = copy.deepcopy(source)
    codes = ["600000.SH", "000001.SZ", "600519.SH", "600036.SH"]
    document["data"]["codes"] = codes
    document["backtest"].update(start="2025-01-02", end="2025-01-17", topk=2, n_drop=1)
    document["factors"] = [{"name": "momentum", "weight": 0.5, "window": 2}, {"name": "value", "weight": 0.5}]
    seed_financials(data, codes)
    queue = PgTaskStore(runs, engine=engine)
    service = TaskService(runs, data, task_store=queue)
    store = PgResultStore(runs, engine=engine, storage_root=tmp_path)
    client = TestClient(create_app(result_store=store, task_service=service), base_url="http://127.0.0.1")
    saved = {}
    for policy in ("zero", "neutral", "exclude"):
        document["factors"][1]["missing_policy"] = policy
        submitted = client.post(
            "/api/v1/research/tasks", json=document, headers={"Idempotency-Key": "pg-earnings-" + policy}
        )
        assert submitted.status_code == 202, submitted.text
        identifier = submitted.json()["id"]
        assert queue.claim() == identifier
        run_task(
            runs,
            data,
            identifier,
            task_store=queue,
            result_store_factory=lambda dataset_id: PgResultStore(
                runs, engine=engine, storage_root=tmp_path, dataset_id=dataset_id
            ),
            market_repository=MarketRepository(engine),
            storage_root=tmp_path,
        )
        task = queue.get(identifier)
        assert task["status"] == "succeeded", task["error"]
        factor_id = task["result"]["factor_run_id"]
        response = client.get(f"/api/v1/factor-runs/{factor_id}/scores?date=2025-01-08")
        assert response.status_code == 200
        result = response.json()
        assert result["total"] == (3 if policy == "exclude" else 4)
        assert any(row["raw"]["value"] == 0 for row in result["items"])
        assert any(row["raw"]["value"] is not None and row["raw"]["value"] < 0 for row in result["items"])
        assert result["metadata"]["coverage"]["factors"]["value"]["missing_policy"] == policy
        if policy != "exclude":
            row = next(row for row in result["items"] if row["raw"]["value"] is None)
            assert row["scores"]["value"] == (0 if policy == "zero" else 50)
            assert row["factor_details"]["value"]["imputed"]
        for category, key in (("backtests", "backtest_run_id"), ("evaluations", "evaluation_run_id")):
            run_id = task["result"][key]
            assert client.get(f"/api/v1/{category}/{run_id}").status_code == 200
            saved[category, run_id] = store.digest(category, run_id)
    assert len(store.list_metadata("backtests")[0]) == 3
    assert all(store.digest(category, run_id) == digest for (category, run_id), digest in saved.items())
