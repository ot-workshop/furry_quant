"""Verify registration deadlines with real PostgreSQL commits and a blocked row lock."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import event, select, text
from test_forward_deadline_postgres import release_lock_after_deadline
from test_forward_registration_postgres import KEY
from test_forward_registration_postgres import submission as submission
from test_postgres_runtime import engine as engine

from finance_quant.application.forward_registration import ForwardCalendar, register_forward
from finance_quant.persistence import forward_registration as registration_store
from finance_quant.persistence.forward import registration_confirmed
from finance_quant.persistence.forward_registration import RegistrationExpired
from finance_quant.persistence.schema import forward_registration

pytestmark = pytest.mark.skipif(
    os.environ.get("FINANCE_QUANT_TEST_POSTGRES") != "1", reason="requires isolated PostgreSQL"
)


def _set_zone(connection, zone):
    with connection.connection.driver_connection.cursor() as cursor:
        cursor.execute("select set_config('TimeZone', %s, true), pg_backend_pid()", (zone,))
        applied_zone, pid = cursor.fetchone()
        assert applied_zone == zone
        cursor.execute("set local lock_timeout = '8s'")
    return pid


@pytest.mark.parametrize("zone", ["UTC", "Asia/Shanghai"])
def test_actual_shanghai_midnight_in_the_past_leaves_an_expired_audit_row(engine, submission, zone):
    request, _ = submission
    signal = request.signal_date - timedelta(days=3)
    request = replace(request, signal_date=signal)
    calendar = ForwardCalendar("past-calendar", (signal, signal + timedelta(days=1), signal + timedelta(days=2)))

    def set_zone(connection):
        _set_zone(connection, zone)

    event.listen(engine, "begin", set_zone)
    try:
        with engine.connect() as connection:
            db_deadline = connection.scalar(
                text("select cast(:signal as timestamp) at time zone 'Asia/Shanghai'"), {"signal": signal}
            )
        assert registration_store.registration_deadline(signal) == db_deadline
        assert db_deadline.utcoffset() == timedelta(hours=8 if zone == "Asia/Shanghai" else 0)
        with pytest.raises(RegistrationExpired, match="上海时间 00:00") as expired:
            register_forward(engine, request, calendar, key=KEY)
        with pytest.raises(RegistrationExpired) as replay:
            register_forward(engine, request, ForwardCalendar("replacement", ()), key=KEY)
        assert replay.value.registration_id == expired.value.registration_id
        with engine.connect() as connection:
            row = connection.execute(select(forward_registration)).mappings().one()
            assert not registration_confirmed(connection, expired.value.registration_id)
    finally:
        event.remove(engine, "begin", set_zone)
    assert row["id"] == expired.value.registration_id
    assert row["status"] == "expired" and row["invalid_reason"] == "registration-deadline-passed"
    assert row["written_at"] >= db_deadline
    assert row["staging_at"] is None and row["confirmed_at"] is None


@pytest.mark.parametrize("zone", ["UTC", "Asia/Shanghai"])
@pytest.mark.parametrize("barrier", ["evidence-commit", "staging-commit", "confirmation-lock", "success-commit"])
def test_registration_needs_two_committed_proofs_before_the_deadline(engine, submission, monkeypatch, zone, barrier):
    request, calendar = submission
    with engine.connect() as connection:
        cutoff = connection.scalar(text("select clock_timestamp() + interval '2 seconds'"))
    # Compress the business deadline for barriers. PostgreSQL still supplies the real clock.
    monkeypatch.setattr(registration_store, "registration_deadline", lambda signal: cutoff)
    delayed_commit = {"evidence-commit": 1, "staging-commit": 2, "success-commit": 3}.get(barrier)
    commits = []
    started = 0
    lock_wait = None

    def observe_commit(connection):
        with connection.connection.driver_connection.cursor() as cursor:
            cursor.execute("select pg_backend_pid(), pg_current_xact_id()::text, current_setting('TimeZone')")
            backend, xid, applied_zone = cursor.fetchone()
            phase = len(commits) + 1
            if phase == delayed_commit:
                with closing(engine.raw_connection()) as observer, observer.cursor() as outside:
                    outside.execute(
                        "select confirmed_evidence_sha256, staging_at, confirmed_at "
                        "from forward_registration where idempotency_key = %s",
                        (KEY,),
                    )
                    visible = outside.fetchone()
                if phase == 1:
                    assert visible is None
                else:
                    digest, staging, confirmed = visible
                    assert digest and confirmed is None
                    assert (staging is not None) == (phase == 3)
                cursor.execute("select clock_timestamp()")
                assert cursor.fetchone()[0] < cutoff
                cursor.execute("select pg_sleep_until(%s)", (cutoff + timedelta(milliseconds=100),))
            cursor.execute("select clock_timestamp()")
            commits.append((backend, xid, applied_zone, cursor.fetchone()[0]))

    with ThreadPoolExecutor(max_workers=1) as workers:

        def begin_phase(connection):
            nonlocal started, lock_wait
            started += 1
            pid = _set_zone(connection, zone)
            if barrier != "confirmation-lock" or started != 3:
                return
            locker = engine.raw_connection()
            try:
                with locker.cursor() as cursor:
                    cursor.execute(
                        "select pg_backend_pid() from forward_registration where idempotency_key = %s for update",
                        (KEY,),
                    )
                    locker_pid = cursor.fetchone()[0]
                lock_wait = workers.submit(release_lock_after_deadline, engine, locker, locker_pid, pid, cutoff)
            except BaseException:
                locker.close()
                raise

        event.listen(engine, "begin", begin_phase)
        event.listen(engine, "commit", observe_commit)
        try:
            if barrier == "success-commit":
                receipt = register_forward(engine, request, calendar, key=KEY)
            else:
                with pytest.raises(RegistrationExpired) as expired:
                    register_forward(engine, request, calendar, key=KEY)
        finally:
            event.remove(engine, "commit", observe_commit)
            event.remove(engine, "begin", begin_phase)

    expected_phases = 2 if barrier == "evidence-commit" else 3
    assert started == len(commits) == expected_phases
    assert len({pid for pid, _, _, _ in commits}) == 1
    assert len({xid for _, xid, _, _ in commits}) == expected_phases
    assert {session_zone for _, _, session_zone, _ in commits} == {zone}
    if delayed_commit is not None:
        assert commits[delayed_commit - 1][3] > cutoff
    if barrier == "confirmation-lock":
        assert lock_wait is not None
        blocked_at, release_at = lock_wait.result()
        assert blocked_at < cutoff < release_at <= commits[-1][3]
    with engine.connect() as connection:
        row = connection.execute(select(forward_registration)).mappings().one()
        confirmed = registration_confirmed(connection, row["id"])
    if barrier == "evidence-commit":
        assert row["staging_at"] is None
    else:
        assert commits[0][3] <= row["staging_at"] < cutoff
    if barrier == "success-commit":
        assert row["id"] == receipt.registration_id
        assert commits[1][3] <= receipt.confirmed_at == row["confirmed_at"] < cutoff
        assert receipt.confirmed_at.utcoffset() == timedelta(hours=8 if zone == "Asia/Shanghai" else 0)
        assert confirmed and row["status"] == "registered"
        assert register_forward(engine, request, ForwardCalendar("replacement", ()), key=KEY) == receipt
    else:
        assert row["id"] == expired.value.registration_id
        assert not confirmed and row["status"] == "expired" and row["confirmed_at"] is None
    print(
        f"zone={zone} barrier={barrier} cutoff={cutoff.isoformat()} status={row['status']} "
        f"staged={row['staging_at']} sealed={row['confirmed_at']} commit_probes={commits}"
    )
