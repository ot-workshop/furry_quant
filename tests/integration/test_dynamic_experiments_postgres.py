"""Migrated, isolated PostgreSQL queue/results for all dynamic split pool kinds."""

import os

import pytest
from fastapi.testclient import TestClient
from test_dynamic_experiments import pool
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


@pytest.mark.parametrize("mode", ["index_warehouse", "market_warehouse"])
def test_dynamic_split_queue_frozen_dataset_and_readonly_analysis(engine, tmp_path, mode):
    data, root, request = pool(tmp_path, mode, 5)
    runs = tmp_path / "runs"
    repository = MarketRepository(engine)
    queue = PgTaskStore(runs, engine=engine)
    service = TaskService(runs, data, task_store=queue)
    store = PgResultStore(runs, engine=engine, storage_root=tmp_path)
    client = TestClient(create_app(result_store=store, task_service=service), base_url="http://127.0.0.1")
    source = (root / "manifest.json").read_bytes()
    response = client.post(
        "/api/v1/research/experiments", json=request, headers={"Idempotency-Key": "pg-dynamic-split-flow"}
    )
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    assert queue.claim() == identifier
    run_task(
        runs,
        data,
        identifier,
        task_store=queue,
        result_store_factory=lambda dataset_id: PgResultStore(
            runs, engine=engine, storage_root=tmp_path, dataset_id=dataset_id
        ),
        market_repository=repository,
        storage_root=tmp_path,
    )
    finished = queue.get(identifier)
    assert finished["status"] == "succeeded", finished
    dataset_id = finished["result"]["dataset_id"]
    frozen = tmp_path / "datasets" / dataset_id
    assert (frozen / "manifest.json").read_bytes() == source == (root / "manifest.json").read_bytes()
    result = client.get("/api/v1/experiments/" + finished["result"]["experiment_run_id"]).json()
    assert len(result["rows"]) == 5
    assert result["metadata"]["dataset_id"] == dataset_id
    for row in result["rows"]:
        for category, field in (
            ("factors", "factor_run_id"),
            ("backtests", "backtest_run_id"),
            ("evaluations", "evaluation_run_id"),
        ):
            saved = store.get(category, row[field], hydrate=False)
            assert saved["metadata"]["dataset_id"] == dataset_id
        before = store.digest("evaluations", row["evaluation_run_id"])
        response = client.get(
            "/api/v1/evaluations/" + row["evaluation_run_id"] + "/analysis",
            params={"horizon": 1, "window": 3, "min_periods": 2},
        )
        assert response.status_code == 200, response.text
        assert response.json()["series"]
        assert store.digest("evaluations", row["evaluation_run_id"]) == before
