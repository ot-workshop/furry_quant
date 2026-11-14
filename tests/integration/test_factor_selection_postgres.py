"""Real PostgreSQL selection references, queue and native Qlib comparison."""

import os

import pytest
from fastapi.testclient import TestClient
from test_factor_selection_flow import source
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
def test_pg_selection_handoff_and_frozen_versions(engine, tmp_path, mode):
    def runtime(runs, data):
        queue = PgTaskStore(runs, engine=engine)
        service = TaskService(runs, data, task_store=queue)
        store = PgResultStore(runs, engine=engine, storage_root=tmp_path)
        client = TestClient(create_app(result_store=store, task_service=service), base_url="http://127.0.0.1")
        return service, client, store

    def execute(service, identifier):
        run_task(
            service.runs_root,
            service.data_root,
            identifier,
            task_store=service.store,
            result_store_factory=lambda dataset_id: PgResultStore(
                service.runs_root, engine=engine, storage_root=tmp_path, dataset_id=dataset_id
            ),
            market_repository=MarketRepository(engine),
            storage_root=tmp_path,
        )
        return service.store.get(identifier)

    client, service, store, evaluation_id, request, _ = source(tmp_path, mode, runtime=runtime, execute=execute)
    original = store.digest("evaluations", evaluation_id)
    response = client.post(
        "/api/v1/research/experiments", json=request, headers={"Idempotency-Key": "pg-milp-comparison"}
    )
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    assert service.store.claim() == identifier
    task = execute(service, identifier)
    assert task["status"] == "succeeded", task
    result = store.get("experiments", task["result"]["experiment_run_id"])
    assert len(result["rows"]) == 5
    assert result["metadata"]["factor_selection"]["source_sha256"] == original
    assert store.digest("evaluations", evaluation_id) == original
    assert (tmp_path / "datasets" / task["result"]["dataset_id"] / "manifest.json").is_file()
    assert result["shared_parameters"]["universe"] == request["data"]
