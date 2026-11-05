"""Runtime contracts that must execute against a real migrated PostgreSQL."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from finance_quant.persistence.database import check_schema, make_engine
from finance_quant.persistence.results import PgResultStore
from finance_quant.persistence.schema import dataset
from finance_quant.persistence.tasks import PgTaskStore

pytestmark = pytest.mark.skipif(
    os.environ.get("FINANCE_QUANT_TEST_POSTGRES") != "1",
    reason="requires the CI PostgreSQL service",
)


@pytest.fixture()
def engine():
    make_engine.cache_clear()
    active = make_engine()
    check_schema(active)
    with active.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE run_artifact, run_trade, run_equity, run_metrics, research_run, "
                "dataset, update_partition, research_task RESTART IDENTITY CASCADE"
            )
        )
    yield active
    active.dispose()
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
