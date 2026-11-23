"""Forward enqueue and replay against the migrated PostgreSQL schema."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Barrier

import pytest
from sqlalchemy import func, select
from test_postgres_runtime import engine as engine

from finance_quant.persistence.forward import AttemptActive, enqueue_attempt
from finance_quant.persistence.schema import forward_attempt, forward_registration, research_task
from finance_quant.persistence.tasks import PgTaskStore
from finance_quant.task_contracts import TaskConflict

pytestmark = pytest.mark.skipif(
    os.environ.get("FINANCE_QUANT_TEST_POSTGRES") != "1", reason="requires isolated PostgreSQL"
)

KEY = "forward-score-original"


@pytest.fixture
def store(engine):
    queue = PgTaskStore(engine=engine)
    with engine.begin() as connection:
        for identifier in ("original", "other"):
            connection.execute(
                forward_registration.insert().values(
                    id=identifier,
                    idempotency_key=f"registration-{identifier}",
                    fingerprint="a" * 64,
                    request={},
                    name=identifier,
                    status="registered",
                    signal_date=date(2026, 11, 24),
                    entry_date=date(2026, 11, 25),
                    exit_date=date(2026, 11, 26),
                    calendar_version="test",
                    calendar_fingerprint="b" * 64,
                    pool_codes=["A", "B"],
                    pool_size=2,
                    top_k=1,
                    factor="momentum",
                    factor_window=20,
                    factor_direction="high",
                    data_rules={},
                    strategy_rules={},
                    code_sha256="c" * 64,
                )
            )
    return queue


def enqueue(store, *, ready=None, **overrides):
    arguments = {
        "registration_id": "original",
        "kind": "score",
        "attempt_no": 1,
        "deadline": datetime.now(timezone.utc) + timedelta(hours=1),
        "task_key": KEY,
        "request": {"name": "frozen score", "window": 20},
        "execution": {"snapshot": "original-pin"},
    }
    with store.engine.begin() as connection:
        if ready is not None:
            ready.wait()
        return enqueue_attempt(store, connection, **(arguments | overrides))


def row_counts(store):
    with store.engine.connect() as connection:
        return tuple(
            connection.scalar(select(func.count()).select_from(table)) for table in (forward_attempt, research_task)
        )


def test_queued_key_replays_original_binding_and_detects_conflicts(store):
    original = enqueue(store, attempt_id="attempt-persisted")
    assert (
        enqueue(
            store,
            attempt_no=9,
            attempt_id="attempt-regenerated",
            request={"window": 20, "name": "frozen score"},
            execution={"snapshot": "new-pin"},
        )
        == original
    )
    assert store.get(original["task_id"], private=True)["execution"] == {"snapshot": "original-pin"}
    for change in ({"registration_id": "other"}, {"kind": "verify"}):
        with pytest.raises(TaskConflict, match="其他登记或阶段"):
            enqueue(store, **change)
    with pytest.raises(TaskConflict, match="参数已改变"):
        enqueue(store, request={"name": "frozen score", "window": 21})
    with pytest.raises(AttemptActive):
        enqueue(store, task_key="forward-score-new", attempt_no=2)
    assert row_counts(store) == (1, 1)


def test_concurrent_same_key_creates_one_attempt_and_task(store):
    ready = Barrier(2, timeout=10)

    def submit(attempt_no):
        return enqueue(store, ready=ready, attempt_no=attempt_no, attempt_id=f"concurrent-{attempt_no}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = tuple(pool.map(submit, (1, 2), timeout=15))
    assert first == second
    assert first["attempt_id"] in ("concurrent-1", "concurrent-2")
    with store.engine.connect() as connection:
        binding = connection.execute(select(forward_attempt.c.id, forward_attempt.c.task_id)).one()
    assert tuple(binding) == (first["attempt_id"], first["task_id"])
    assert row_counts(store) == (1, 1)


def test_failed_original_still_replays_after_retry_with_new_key(store):
    original = enqueue(store)
    assert store.claim() == original["task_id"]
    with store.engine.begin() as connection:
        store.finish(original["task_id"], "failed", error="failed fixture", connection=connection)
        connection.execute(
            forward_attempt.update().where(forward_attempt.c.id == original["attempt_id"]).values(status="failed")
        )
    assert enqueue(store, attempt_no=2) == original
    replacement = enqueue(store, attempt_no=2, task_key="forward-score-retry")
    assert replacement["attempt_id"] != original["attempt_id"]
    assert replacement["task_id"] != original["task_id"]
    assert enqueue(store, attempt_no=3) == original
    assert store.get(original["task_id"])["status"] == "failed"
    assert store.get(replacement["task_id"])["status"] == "queued"
    with store.engine.connect() as connection:
        statuses = connection.execute(
            select(forward_attempt.c.attempt_no, forward_attempt.c.status).order_by(forward_attempt.c.attempt_no)
        ).all()
    assert statuses == [(1, "failed"), (2, "queued")]
    assert row_counts(store) == (2, 2)


def test_full_queue_allows_replay_and_leaves_no_orphan_for_new_submission(store):
    original = enqueue(store)
    for number in range(19):
        store.create({"name": f"filler {number}"}, {}, f"forward-filler-{number}")
    assert enqueue(store, attempt_no=2) == original
    with pytest.raises(TaskConflict, match="排队任务已达20个"):
        enqueue(store, kind="verify", task_key="forward-verify-new")
    assert row_counts(store) == (1, 20)


def test_existing_research_key_cannot_be_bound_to_a_new_forward_attempt(store):
    existing = store.create({"name": "frozen score", "window": 20}, {}, KEY)
    with pytest.raises(TaskConflict, match="其他任务"):
        enqueue(store)
    assert row_counts(store) == (0, 1)
    assert store.get(existing["id"])["status"] == "queued"
