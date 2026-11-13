"""Runtime contracts that must execute against a real migrated PostgreSQL."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from finance_quant.persistence.database import check_schema, database_url, make_engine
from finance_quant.persistence.results import PgResultStore
from finance_quant.persistence.schema import dataset
from finance_quant.persistence.tasks import PgTaskStore

pytestmark = pytest.mark.skipif(
    os.environ.get("FINANCE_QUANT_TEST_POSTGRES") != "1",
    reason="requires the CI PostgreSQL service",
)


@pytest.fixture()
def engine(monkeypatch):
    configured = database_url()
    if not (configured.database.startswith("fq_") or configured.database.endswith("_test")):
        pytest.fail("Integration tests require an explicit fq_* or *_test database; development databases are refused")
    make_engine.cache_clear()
    admin = make_engine()
    schema = "fq_test_" + uuid.uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    active = create_engine(database_url(), connect_args={"options": f"-c timezone=UTC -c search_path={schema}"})
    try:
        with active.connect() as connection:
            assert connection.execute(text("SELECT current_schema()")).scalar_one() == schema
        monkeypatch.setattr("finance_quant.persistence.database.make_engine", lambda: active)
        command.upgrade(Config("alembic.ini"), "head")
        check_schema(active)
        yield active
    finally:
        active.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        make_engine.cache_clear()


def test_postgres_task_queue_claims_and_finishes(engine, tmp_path):
    store = PgTaskStore(tmp_path / "runs", engine=engine)
    store.initialize()
    task = store.create(
        {"name": "integration task", "data": {"mode": "existing"}},
        {"mode": "existing", "snapshot_dir": "/tmp/not-used"},
        "integration-task-key",
    )

    identifier = store.claim(worker_id="pytest")
    assert identifier == task["id"]
    store.stage(identifier, "factors", "testing", 2)
    store.finish(identifier, "succeeded", result={"ok": True})

    saved = store.get(identifier, private=True)
    assert saved["status"] == "succeeded"
    assert saved["result"] == {"ok": True}
    assert saved["progress"] == {"completed": 4, "total": 4}


def test_postgres_result_store_persists_dataset_binding(engine, tmp_path):
    storage_root = tmp_path / "storage"
    runs_root = storage_root / "runs"
    with engine.begin() as connection:
        connection.execute(
            dataset.insert().values(
                id="dataset-integration",
                request={},
                manifest={"id": "dataset-integration"},
                relative_path="datasets/integration",
                status="ready",
            )
        )
    store = PgResultStore(
        runs_root,
        engine=engine,
        storage_root=storage_root,
        dataset_id="dataset-integration",
    )
    document = {
        "metadata": {
            "id": "integration-evaluation",
            "name": "integration evaluation",
            "is_synthetic": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "config": {"horizons": [5]},
        },
        "methodology": {"label": "forward return"},
    }

    store.publish("evaluations", "integration-evaluation", document)
    store.publish_report("integration-evaluation", "# integration")

    saved = store.get("evaluations", "integration-evaluation", hydrate=False)
    assert saved["metadata"]["dataset_id"] == "dataset-integration"
    assert saved["methodology"] == document["methodology"]
    records, unavailable = store.list_metadata("evaluations")
    assert records[0]["metadata"]["id"] == "integration-evaluation"
    assert unavailable == []


def test_financial_research_queue_to_qlib_and_postgres_results(engine, tmp_path):
    from test_fundamental_factors import statements
    from test_pool_workflow_matrix import DiversePrimary, codes_for, request_for

    from finance_quant.application.task_worker import run_task
    from finance_quant.application.tasks import TaskService
    from finance_quant.persistence.market import MarketRepository
    from support.financial import publish
    from support.fixed_pool import publish_fixture_snapshot

    root = tmp_path / "storage"
    data, runs = root / "data", root / "runs"
    codes = codes_for(3, 71)
    manifest = publish_fixture_snapshot(data, codes, "20241202", "20250331", provider=DiversePrimary())
    publish(data / "financials", statements(codes), seen_on="2026-11-07")
    queue = PgTaskStore(runs, engine=engine)
    service = TaskService(runs, data, task_store=queue)
    factory = lambda dataset_id=None: PgResultStore(runs, engine=engine, storage_root=root, dataset_id=dataset_id)
    output = []
    for policy in ("observed", "announcement_estimate"):
        request = request_for(codes, snapshot_id=manifest["id"])
        request.update(factors=[{"name": "roe_ttm", "weight": 1.0}])
        if policy == "observed":
            request["financial_policy"] = policy  # Legacy request is accepted but no longer selects another mode.
        submitted = service.submit(request, "financial-" + policy)
        assert queue.claim() == submitted["id"]
        run_task(
            runs,
            data,
            submitted["id"],
            task_store=queue,
            result_store_factory=factory,
            market_repository=MarketRepository(engine),
            storage_root=root,
        )
        task = queue.get(submitted["id"])
        assert task["status"] == "succeeded", task["error"]
        store = factory()
        result = task["result"]
        factors = store.get("factors", result["factor_run_id"])
        assert factors["metadata"]["financial_data"]["availability_policy"] == "announcement_estimate"
        assert "financial_policy" not in task["request"]
        backtest = store.get("backtests", result["backtest_run_id"])
        assert backtest["trades"] and backtest["equity"]
        assert backtest["metadata"]["dataset_id"]
        assert (
            store.get("evaluations", result["evaluation_run_id"])["metadata"]["financial_data"]["availability_policy"]
            == "announcement_estimate"
        )
        output.append(backtest)
    assert output[0]["trades"] == output[1]["trades"]
    assert output[0]["equity"] == output[1]["equity"]
    assert output[0]["summary"]["id"] == output[1]["summary"]["id"]
