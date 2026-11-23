"""Exercise the normal queue and worker with synthetic, pinned score inputs."""

from __future__ import annotations

import json
import os
from datetime import datetime, time, timedelta

import pandas as pd
import pytest
from sqlalchemy import select, text
from test_postgres_runtime import engine as engine

from finance_quant.application import forward_score
from finance_quant.application.forward_registration import ForwardCalendar, _freeze_registration
from finance_quant.application.task_worker import run_task
from finance_quant.checksums import file_sha256
from finance_quant.data.symbols import to_qlib
from finance_quant.persistence.forward import SHANGHAI, canonical_sha256, registration_evidence_digest
from finance_quant.persistence.schema import forward_attempt, forward_registration
from finance_quant.persistence.tasks import PgTaskStore, worker_lock
from finance_quant.task_contracts import TaskConflict

pytestmark = pytest.mark.skipif(
    os.environ.get("FINANCE_QUANT_TEST_POSTGRES") != "1", reason="requires isolated PostgreSQL"
)
CODES = ["000001.SZ", "600000.SH", "600001.SH"]
KEY = "synthetic-forward-score"


def _fixture(engine, root, *, late=False, missing=False, direction="high", capture=True, drift=False, future=False):
    with engine.connect() as connection:
        today = connection.scalar(text("select (clock_timestamp() at time zone 'Asia/Shanghai')::date"))
    signal = today - timedelta(days=3)
    history = (signal - timedelta(days=4), signal - timedelta(days=1), signal)
    entry = today + timedelta(days=-2 if late else 1)
    calendar = ForwardCalendar("synthetic-calendar", (*history, entry, entry + timedelta(days=1)))
    document = {
        "name": "Forward score fixture",
        "signal_date": signal.isoformat(),
        "pool_codes": CODES,
        "top_k": 1,
        "factor": "momentum",
        "factor_window": 2,
        "factor_direction": direction,
        "dataset_id": None,
    }
    # Historical registration evidence is synthetic. The queue, capture qualification,
    # worker, commit barriers and score deadline all use the real PostgreSQL clock.
    proof = datetime.combine(signal - timedelta(days=1), time(20), tzinfo=SHANGHAI)
    registration = {
        **_freeze_registration(document, signal, calendar),
        "id": "fwd-synthetic",
        "idempotency_key": "synthetic-registration",
        "fingerprint": canonical_sha256(document),
        "request": document,
        "status": "registered",
        "written_at": proof,
        "staging_at": proof,
        "confirmed_at": proof,
    }
    registration["confirmed_evidence_sha256"] = registration_evidence_digest(registration)
    with engine.begin() as connection:
        connection.execute(forward_registration.insert().values(**registration))

    leaf = root / "research" / "subsets" / "synthetic-score"
    leaf.mkdir(parents=True)
    rows = [
        {
            "instrument": to_qlib(code),
            "date": day,
            "close": [10.0, 11.0, 12.0 if code != CODES[-1] else 9.0][position],
            "adj_factor": 2.0,
            "is_suspended": False,
        }
        for code in CODES
        for position, day in enumerate(history)
        if not (missing and code == CODES[-1] and position == 0)
    ]
    if missing:
        rows[-1].update(close=None, is_suspended=None)
    if future:
        rows.append({**rows[-1], "date": entry})
    frames = {
        "market.parquet": pd.DataFrame(rows).assign(date=lambda frame: pd.to_datetime(frame.date)),
        "benchmark.parquet": pd.DataFrame({"date": pd.to_datetime(history)}),
        "calendar.parquet": pd.DataFrame(
            {"date": pd.to_datetime(calendar.sessions), "is_open": [not drift, True, True, True, True]}
        ),
    }
    for name, frame in frames.items():
        frame.to_parquet(leaf / name, index=False)
    manifest = {
        "id": leaf.name,
        "schema_version": "market-v1",
        "files": {name: file_sha256(leaf / name) for name in frames},
    }
    if capture:
        manifest["score_capture"] = {
            "captured_at": datetime.combine(signal, time(16), tzinfo=SHANGHAI).isoformat(),
            "data_end": signal.isoformat(),
            "pool_codes": CODES,
        }
    (leaf / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return registration["id"], leaf, datetime.combine(entry, time(9, 15), tzinfo=SHANGHAI)


def _execute(store, root, binding):
    task_id = store.claim("score-control")
    assert task_id == binding["task_id"]
    (root / "runs" / "tasks").mkdir(parents=True, exist_ok=True)
    run_task(root / "runs", root, task_id, task_store=store, result_store_factory=None)
    with store.engine.connect() as connection:
        row = (
            connection.execute(select(forward_attempt).where(forward_attempt.c.id == binding["attempt_id"]))
            .mappings()
            .one()
        )
    return row, store.get(task_id)


@pytest.mark.parametrize(
    ("case", "status", "reason"),
    [
        ("high", "succeeded", None),
        ("low", "succeeded", None),
        ("late", "missed", "deadline-passed"),
        ("missing", "failed", "incomplete-score"),
        ("calendar", "failed", "snapshot-calendar-mismatch"),
        ("future", "failed", "score-data-outside-frozen-calendar"),
        ("tampered-file", "failed", "invalid-score-snapshot"),
        ("unsealed", "failed", "registration-unsealed"),
        ("lock", "failed", "score-dependency-lock-mismatch"),
        ("code", "failed", "score-code-mismatch"),
        ("lost-response", "succeeded", None),
    ],
)
def test_score_worker(engine, tmp_path, monkeypatch, case, status, reason):
    reg, leaf, deadline = _fixture(
        engine,
        tmp_path,
        late=case == "late",
        missing=case == "missing",
        direction="low" if case == "low" else "high",
        drift=case == "calendar",
        future=case == "future",
    )
    store = PgTaskStore(engine=engine)
    submit = forward_score.enqueue_forward_score
    binding = submit(store, tmp_path, reg, leaf.name, key=KEY)
    assert submit(store, tmp_path, reg, leaf.name, key=KEY) == binding
    if case == "tampered-file":
        with (leaf / "market.parquet").open("ab") as stream:
            stream.write(b"altered")
    if case == "unsealed":
        with engine.begin() as connection:
            connection.execute(forward_registration.update().values(confirmed_at=None))
    if case in {"code", "lock"}:
        field = "code_sha256" if case == "code" else "dependency_lock_sha256"
        source = forward_score.source_provenance()
        monkeypatch.setattr(forward_score, "source_provenance", lambda: {**source, field: "0" * 64})
    if case == "lost-response":
        confirm = forward_score.confirm_attempt

        def lose_response(*args, **kwargs):
            assert confirm(*args, **kwargs).sealed
            raise ConnectionError("synthetic loss after COMMIT")

        monkeypatch.setattr(forward_score, "confirm_attempt", lose_response)

    row, task = _execute(store, tmp_path, binding)
    assert row["status"] == status and row["error"] == reason
    assert row["deadline_at"] == deadline
    assert task["status"] == ("succeeded" if status == "succeeded" else "failed")
    assert (task["result"] is not None) == (status == "succeeded")
    assert task["error"] == reason
    if status == "succeeded":
        evidence = row["evidence"]
        assert row["staging_at"] <= row["sealed_at"] < deadline
        assert canonical_sha256(evidence) == row["evidence_sha256"]
        assert (evidence["pool_size"], evidence["top_k"]) == (3, 1)
        assert [item["momentum"] for item in evidence["scores"]] == pytest.approx([0.2, 0.2, -0.1])
        assert evidence["candidate_codes"] == [CODES[-1] if case == "low" else CODES[0]]
        assert submit(store, tmp_path, reg, leaf.name, key=KEY) == binding
        with pytest.raises(TaskConflict, match="score-already-sealed"):
            submit(store, tmp_path, reg, leaf.name, key=KEY + "-new")
    else:
        assert row["sealed_at"] is None and row["staging_at"] is None
    if case == "late":
        assert row["evidence"]["candidate_codes"] == [CODES[0]]
    if case == "missing":
        evidence = row["evidence"]
        assert evidence["candidate_codes"] is None
        assert (evidence["pool_size"], evidence["top_k"]) == (3, 1)
        assert evidence["scores"][-1]["reasons"] == ["missing-warmup", "missing-price", "missing-trading-state"]
        assert row["evidence_sha256"] == canonical_sha256(evidence)
    print(f"{case}: queue={task['status']}, attempt={status}, Shanghai deadline={deadline.isoformat()}")


def test_unproven_capture_does_not_bind_or_enqueue(engine, tmp_path):
    reg, leaf, _ = _fixture(engine, tmp_path, capture=False)
    store = PgTaskStore(engine=engine)
    with pytest.raises(forward_score.ScoreInputError, match="missing-score-capture-proof"):
        forward_score.enqueue_forward_score(store, tmp_path, reg, leaf.name, key=KEY)
    assert store.list()["total"] == 0
    with engine.connect() as connection:
        assert connection.scalar(select(forward_registration.c.score_snapshot)) is None


@pytest.mark.parametrize("recovery", [False, True], ids=["cancel", "restart"])
def test_cancel_and_recover_close_the_attempt_and_allow_retry_with_same_binding(engine, tmp_path, recovery):
    reg, leaf, _ = _fixture(engine, tmp_path)
    store = PgTaskStore(engine=engine)
    submit = forward_score.enqueue_forward_score
    binding = submit(store, tmp_path, reg, leaf.name, key=KEY)
    if recovery:
        with worker_lock(engine):
            assert store.claim() == binding["task_id"]
            store.recover()
    else:
        store.cancel(binding["task_id"])
    with engine.connect() as connection:
        row = connection.execute(select(forward_attempt)).mappings().one()
    assert row["status"] == ("interrupted" if recovery else "cancelled")
    assert row["sealed_at"] is None
    assert submit(store, tmp_path, reg, leaf.name, key=KEY) == binding
    with pytest.raises(TaskConflict, match="score-snapshot-already-bound"):
        submit(store, tmp_path, reg, "different-leaf", key=KEY)
    retry = submit(store, tmp_path, reg, leaf.name, key=KEY + "-retry")
    assert retry != binding
    new, task = _execute(store, tmp_path, retry)
    assert new["attempt_no"] == 2 and new["status"] == task["status"] == "succeeded"
