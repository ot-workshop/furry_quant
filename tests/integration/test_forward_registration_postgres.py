"""The internal registration service against the migrated PostgreSQL schema."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import event, func, select, text
from test_postgres_runtime import engine as engine

from finance_quant import provenance
from finance_quant.application.forward_registration import ForwardCalendar, ForwardRegistrationRequest, register_forward
from finance_quant.checksums import file_sha256
from finance_quant.paths import dependency_lock
from finance_quant.persistence.forward import canonical_sha256, get_registration, registration_confirmed
from finance_quant.persistence.schema import forward_registration, research_task
from finance_quant.task_contracts import TaskConflict

pytestmark = pytest.mark.skipif(
    os.environ.get("FINANCE_QUANT_TEST_POSTGRES") != "1", reason="requires isolated PostgreSQL"
)

KEY = "forward-registration-original"


@pytest.fixture
def submission(engine):
    with engine.connect() as connection:
        today = connection.scalar(text("select (clock_timestamp() at time zone 'Asia/Shanghai')::date"))
    signal = today + timedelta(days=2)
    request = ForwardRegistrationRequest(
        name="Synthetic registration",
        signal_date=signal,
        pool_codes=("600000.SH", "000001.SZ", "600001.SH"),
        top_k=1,
        factor_window=20,
        factor_direction="high",
        dataset_id="fixture-dataset",
    )
    calendar = ForwardCalendar(
        "synthetic-calendar-v1", (signal, signal + timedelta(days=3), signal + timedelta(days=4))
    )
    return request, calendar


def test_freezes_synchronous_registration_and_reuses_the_original_proof(engine, submission):
    request, calendar = submission
    receipt = register_forward(engine, request, calendar, key=KEY)
    # A replay reads the originally committed calendar even if a replacement cannot be used.
    assert register_forward(engine, request, ForwardCalendar("replacement", ()), key=KEY) == receipt
    with pytest.raises(TaskConflict, match="参数已改变"):
        register_forward(engine, replace(request, top_k=2), calendar, key=KEY)

    with engine.connect() as connection:
        row = connection.execute(select(forward_registration)).mappings().one()
        deadline = connection.scalar(
            text("select cast(:signal as timestamp) at time zone 'Asia/Shanghai'"), {"signal": request.signal_date}
        )
        assert registration_confirmed(connection, receipt.registration_id)
        assert connection.scalar(select(func.count()).select_from(research_task)) == 0
    assert row["id"] == receipt.registration_id
    assert row["status"] == "registered"
    assert row["written_at"] <= row["staging_at"] <= row["confirmed_at"] == receipt.confirmed_at < deadline
    assert row["pool_codes"] == sorted(request.pool_codes)
    assert (row["pool_size"], row["top_k"]) == (3, 1)
    assert (row["factor"], row["factor_window"], row["factor_direction"]) == ("momentum", 20, "high")
    assert (row["signal_date"], row["entry_date"], row["exit_date"]) == calendar.sessions
    assert row["calendar_version"] == calendar.version
    assert row["calendar_fingerprint"] == canonical_sha256(row["data_rules"]["calendar"])
    assert row["data_rules"]["calendar"]["trading_dates"] == [day.isoformat() for day in calendar.sessions]
    assert row["data_rules"]["label_capture_after"] == f"{calendar.sessions[2]}T15:00:00+08:00"
    assert row["data_rules"]["dependency_lock_sha256"] == file_sha256(dependency_lock())
    assert row["strategy_rules"]["holding_sessions"] == 1
    assert row["dataset_id"] == "fixture-dataset"
    assert len(row["confirmed_evidence_sha256"]) == 64
    print(
        f"registration={receipt.registration_id}, confirmation={receipt.confirmed_at.isoformat()}, "
        f"Shanghai midnight={deadline.isoformat()}, queued=0, original N/K=3/1"
    )


@pytest.mark.parametrize("conflict", [False, True], ids=["same-request", "changed-request"])
def test_concurrent_first_submission_can_only_insert_one_registration(engine, submission, conflict):
    request, calendar = submission
    other = replace(request, top_k=2) if conflict else replace(request, pool_codes=request.pool_codes[::-1])
    inserts = Barrier(2)

    def wait_for_both_inserts(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO forward_registration"):
            inserts.wait(timeout=8)

    def submit(document):
        try:
            return register_forward(engine, document, calendar, key=KEY)
        except TaskConflict as exc:
            return exc

    event.listen(engine, "before_cursor_execute", wait_for_both_inserts)
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(submit, (request, other)))
    finally:
        event.remove(engine, "before_cursor_execute", wait_for_both_inserts)

    with engine.connect() as connection:
        row = connection.execute(select(forward_registration)).mappings().one()
        assert connection.scalar(select(func.count()).select_from(research_task)) == 0
    conflicts = [outcome for outcome in outcomes if isinstance(outcome, TaskConflict)]
    assert len(conflicts) == int(conflict)
    assert all("参数已改变" in str(outcome) for outcome in conflicts)
    for receipt in (outcome for outcome in outcomes if not isinstance(outcome, TaskConflict)):
        assert (receipt.registration_id, receipt.confirmed_at) == (row["id"], row["confirmed_at"])


@pytest.mark.parametrize("changed_field", ["pool_size", "dependency_lock_sha256", "legacy_without_lock"])
def test_confirmed_replay_and_read_gate_use_the_same_evidence(engine, submission, changed_field):
    request, calendar = submission
    receipt = register_forward(engine, request, calendar, key=KEY)
    with engine.begin() as connection:
        row = connection.execute(select(forward_registration)).mappings().one()
        if changed_field == "pool_size":
            changes = {"pool_size": 4}
        else:
            rules = dict(row["data_rules"])
            if changed_field == "dependency_lock_sha256":
                rules["dependency_lock_sha256"] = "f" * 64
            else:
                rules.pop("dependency_lock_sha256", None)
            changes = {"data_rules": rules}
        connection.execute(
            forward_registration.update().where(forward_registration.c.id == receipt.registration_id).values(**changes)
        )
    with engine.connect() as connection:
        assert get_registration(connection, receipt.registration_id)["confirmed_at"] == receipt.confirmed_at
        assert not registration_confirmed(connection, receipt.registration_id)
    with pytest.raises(TaskConflict, match="证据与原摘要不匹配"):
        register_forward(engine, request, calendar, key=KEY)


@pytest.mark.parametrize(
    "case", ["missing-stage", "missing-confirmation", "stage-at-deadline", "confirmation-at-deadline", "reversed"]
)
def test_incomplete_or_inconsistent_two_phase_proof_is_unusable(engine, submission, case):
    request, calendar = submission
    receipt = register_forward(engine, request, calendar, key=KEY)
    with engine.begin() as connection:
        deadline = connection.scalar(
            text("select cast(:signal as timestamp) at time zone 'Asia/Shanghai'"), {"signal": request.signal_date}
        )
        changes = {
            "missing-stage": {"staging_at": None},
            "missing-confirmation": {"confirmed_at": None},
            "stage-at-deadline": {"staging_at": deadline},
            "confirmation-at-deadline": {"confirmed_at": deadline},
            "reversed": {"staging_at": receipt.confirmed_at + timedelta(microseconds=1)},
        }[case]
        connection.execute(
            forward_registration.update().where(forward_registration.c.id == receipt.registration_id).values(**changes)
        )
    with engine.connect() as connection:
        assert get_registration(connection, receipt.registration_id)["id"] == receipt.registration_id
        assert not registration_confirmed(connection, receipt.registration_id)


def test_new_registration_requires_a_lock_but_replay_uses_the_frozen_lock(engine, submission, monkeypatch, tmp_path):
    request, calendar = submission
    receipt = register_forward(engine, request, calendar, key=KEY)
    monkeypatch.setattr(provenance, "dependency_lock", lambda: tmp_path / "missing-uv.lock")
    assert register_forward(engine, request, calendar, key=KEY) == receipt
    with pytest.raises(ValueError, match="缺少依赖锁"):
        register_forward(engine, request, calendar, key="another-registration-key")
    with engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(forward_registration)) == 1
