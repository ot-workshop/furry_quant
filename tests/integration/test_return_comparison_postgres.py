"""Isolated PostgreSQL queue, frozen inputs and paired comparison artifacts."""

import os

import pytest
from fastapi.testclient import TestClient
from test_postgres_runtime import engine as engine
from test_return_comparison import setup_comparison

from finance_quant.api import create_app
from finance_quant.application.task_worker import run_task
from finance_quant.application.tasks import TaskService
from finance_quant.persistence.market import MarketRepository
from finance_quant.persistence.results import PgResultStore
from finance_quant.persistence.tasks import PgTaskStore

pytestmark = pytest.mark.skipif(os.environ.get("FINANCE_QUANT_TEST_POSTGRES") != "1", reason="isolated PostgreSQL only")


@pytest.mark.parametrize("mode", ["snapshot", "index_warehouse", "market_warehouse"])
def test_paired_comparison_keeps_datasets_and_old_results_immutable(engine, tmp_path, workspace, monkeypatch, mode):
    data, runs, document = setup_comparison(tmp_path, workspace, mode, monkeypatch)
    queue = PgTaskStore(runs, engine=engine)
    service = TaskService(runs, data, task_store=queue)
    store = PgResultStore(runs, engine=engine, storage_root=tmp_path)
    client = TestClient(create_app(result_store=store, task_service=service), base_url="http://127.0.0.1")
    first = None
    for attempt in range(2):
        response = client.post(
            "/api/v1/research/experiments",
            json=document,
            headers={"Idempotency-Key": f"pg-return-comparison-{attempt}"},
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
            market_repository=MarketRepository(engine),
            storage_root=tmp_path,
        )
        task = queue.get(identifier)
        assert task["status"] == "succeeded", task
        saved = client.get("/api/v1/experiments/" + task["result"]["experiment_run_id"]).json()
        assert saved["metadata"]["dataset_id"] == task["result"]["dataset_id"]
        assert saved["selection"] is None and len(saved["rows"]) == 4
        for row in saved["rows"]:
            for category, key in (
                ("factors", "factor_run_id"),
                ("backtests", "backtest_run_id"),
                ("evaluations", "evaluation_run_id"),
            ):
                artifact = store.get(category, row[key], hydrate=False)
                assert artifact["metadata"]["dataset_id"] == saved["metadata"]["dataset_id"]
        report = client.get(f"/api/v1/experiments/{saved['metadata']['id']}/report")
        assert report.status_code == 200 and "共同样本" in report.text
        if first is None:
            first = saved
        else:
            assert task["result"]["reused"] and saved == first
