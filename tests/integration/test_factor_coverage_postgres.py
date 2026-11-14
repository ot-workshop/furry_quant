"""Coverage popovers use a migrated isolated PostgreSQL composition without publishing results."""

import os

import pytest
from fastapi.testclient import TestClient
from test_dynamic_experiments import pool
from test_postgres_runtime import engine as engine

from finance_quant.api import create_app
from finance_quant.application.tasks import TaskService
from finance_quant.persistence.results import PgResultStore
from finance_quant.persistence.tasks import PgTaskStore

pytestmark = pytest.mark.skipif(
    os.environ.get("FINANCE_QUANT_TEST_POSTGRES") != "1", reason="requires isolated PostgreSQL"
)


def test_coverage_query_leaves_queue_and_results_unchanged(engine, tmp_path):
    data, _, document = pool(tmp_path, "index_warehouse", 3)
    runs = tmp_path / "runs"
    queue = PgTaskStore(runs, engine=engine)
    service = TaskService(runs, data, task_store=queue)
    store = PgResultStore(runs, engine=engine, storage_root=tmp_path)
    client = TestClient(create_app(result_store=store, task_service=service), base_url="http://127.0.0.1")
    response = client.post(
        "/api/v1/research/factor-coverage",
        json={"data": document["data"], "start": "2024-01-08", "factor": {"name": "value"}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["available"] == response.json()["total"] == 2
    assert queue.list()["total"] == 0
    assert all(store.list_metadata(kind)[0] == [] for kind in ("backtests", "factors", "evaluations", "experiments"))
    assert not (tmp_path / "datasets").exists()
