"""Actual PostgreSQL publication, task execution and immutable market datasets."""

import os

import pytest
from fastapi.testclient import TestClient
from test_joint_data import Secondary
from test_postgres_runtime import engine as engine
from test_shared_market import SharedProvider, spec

from finance_quant.api import create_app
from finance_quant.application import market_download
from finance_quant.application.task_worker import run_task
from finance_quant.application.tasks import TaskService
from finance_quant.data import postgres_market, shared_market
from finance_quant.data.market_universe import market_option, market_roots
from finance_quant.persistence.market import MarketRepository
from finance_quant.persistence.results import PgResultStore
from finance_quant.persistence.tasks import PgTaskStore

pytestmark = pytest.mark.skipif(
    os.environ.get("FINANCE_QUANT_TEST_POSTGRES") != "1", reason="requires isolated PostgreSQL"
)


def test_market_download_web_task_pins_names_lifecycle_and_results(engine, tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    data, runs = storage / "data", storage / "runs"
    repository = MarketRepository(engine)
    monkeypatch.setattr(postgres_market, "repository", lambda: repository)
    shared_market.configure_store(postgres_market.PostgresMarketStore())
    provider = SharedProvider()
    root = market_download.shared_root(data)
    plan = market_download.prepare(root, spec("all_market", "2025-03-31", codes=[]), provider=provider)
    result = market_download.run(root, plan["id"], provider=provider, secondary=Secondary())
    assert result["status"] == "complete", result
    option = market_option(market_roots(data)[0])
    queue = PgTaskStore(runs, engine=engine)
    service = TaskService(runs, data, task_store=queue)
    store = PgResultStore(runs, engine=engine, storage_root=storage)
    client = TestClient(create_app(result_store=store, task_service=service), base_url="http://127.0.0.1")
    request = {
        "name": "数据库动态池",
        "data": {"mode": "market_warehouse", "warehouse_id": option["id"], "filters": {"min_listing_days": 0}},
        "backtest": {"start": "2025-01-10", "end": "2025-03-31", "topk": 1},
        "factors": [{"name": "momentum", "window": 2, "weight": 1}],
        "evaluation": {"horizons": [1], "groups": 2, "min_cross_section": 3},
    }
    response = client.post(
        "/api/v1/research/tasks", json=request, headers={"Idempotency-Key": "pg-market-universe-request"}
    )
    assert response.status_code == 202, response.text
    task = response.json()
    assert queue.claim() == task["id"]
    run_task(
        runs,
        data,
        task["id"],
        task_store=queue,
        result_store_factory=lambda dataset_id: PgResultStore(
            runs, engine=engine, storage_root=storage, dataset_id=dataset_id
        ),
        market_repository=repository,
        storage_root=storage,
    )
    done = queue.get(task["id"])
    assert done["status"] == "succeeded", done
    dataset_id = done["result"]["dataset_id"]
    pinned = storage / "datasets" / dataset_id
    assert (pinned / "reference/name_history.parquet").is_file()
    saved = store.get("backtests", done["result"]["backtest_run_id"])
    assert saved["metadata"]["dataset_id"] == dataset_id
    assert saved["metadata"]["universe_filters"]["exclude_st"]
    assert saved["equity"] and saved["trades"]
    for category, key, url in [
        ("factors", "factor_run_id", "factor-runs"),
        ("evaluations", "evaluation_run_id", "evaluations"),
    ]:
        record = store.get(category, done["result"][key])
        assert record["metadata"]["dataset_id"] == dataset_id
        assert (
            client.get(
                f"/api/v1/{url}/{done['result'][key]}" + ("/scores" if category == "factors" else "")
            ).status_code
            == 200
        )
