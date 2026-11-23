"""Exercise real commit and row-lock barriers around the forward proof deadline."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
from time import monotonic, sleep

import pytest
from sqlalchemy import event, select, text
from test_forward_enqueue_postgres import enqueue
from test_forward_enqueue_postgres import store as store
from test_postgres_runtime import engine as engine

from finance_quant.persistence.forward import canonical_sha256, confirm_attempt
from finance_quant.persistence.schema import forward_attempt
from finance_quant.task_contracts import TaskConflict

pytestmark = pytest.mark.skipif(
    os.environ.get("FINANCE_QUANT_TEST_POSTGRES") != "1", reason="requires isolated PostgreSQL"
)

EVIDENCE = {"ranked": ["A"], "pool_size": 2, "top_k": 1}


def install_commit_probe(connection, delayed_phase):
    connection.execute(
        text("""create table deadline_probe (
        phase text primary key, entered_at timestamptz not null, released_at timestamptz not null,
        transaction_id text not null, backend_pid integer not null, zone text not null
    )""")
    )
    connection.execute(
        text("""create function probe_forward_commit() returns trigger language plpgsql as $$
    declare
        phase text;
        entered timestamptz;
    begin
        phase := case
            when old.evidence_sha256 is distinct from new.evidence_sha256 then 'evidence'
            when old.staging_at is distinct from new.staging_at then 'staging'
            when old.sealed_at is distinct from new.sealed_at then 'seal'
            when old.status is distinct from new.status and new.status = 'missed' then 'missed'
        end;
        if phase is null then
            return null;
        end if;
        entered := clock_timestamp();
        if phase = TG_ARGV[0] then
            if entered >= new.deadline_at then
                raise exception 'commit did not reach the barrier before the deadline';
            end if;
            perform pg_sleep_until(new.deadline_at + interval '100 milliseconds');
        end if;
        insert into deadline_probe values (
            phase, entered, clock_timestamp(), pg_current_xact_id()::text,
            pg_backend_pid(), current_setting('TimeZone')
        );
        return null;
    end
    $$""")
    )
    connection.exec_driver_sql(f"""create constraint trigger forward_commit_probe
    after update on forward_attempt deferrable initially deferred
    for each row execute function probe_forward_commit('{delayed_phase}')""")


def release_lock_after_deadline(engine, locker, locker_pid, waiting_pid, deadline):
    with closing(locker), closing(engine.raw_connection()) as observer, observer.cursor() as cursor:
        timeout = monotonic() + 5
        while monotonic() < timeout:
            cursor.execute("select clock_timestamp(), pg_blocking_pids(%s)", (waiting_pid,))
            blocked_at, blockers = cursor.fetchone()
            if locker_pid in blockers:
                assert blocked_at < deadline
                break
            sleep(0.01)
        else:
            pytest.fail("confirmation was never blocked by the held row lock")
        cursor.execute("select pg_sleep_until(%s)", (deadline + timedelta(milliseconds=100),))
        cursor.execute("select clock_timestamp()")
        released_at = cursor.fetchone()[0]
        locker.commit()
        return blocked_at, released_at


@pytest.mark.parametrize("zone", ["UTC", "Asia/Shanghai"])
@pytest.mark.parametrize("barrier", ["evidence", "staging", "confirmation-lock", "seal"])
def test_confirmation_uses_committed_proofs_and_the_post_lock_clock(store, zone, barrier):
    engine = store.engine
    with engine.begin() as connection:
        install_commit_probe(connection, barrier)
        # A near-future absolute instant decoded through the explicit Shanghai calendar zone.
        deadline = connection.scalar(
            text("""
            select ((clock_timestamp() + interval '2 seconds') at time zone 'Asia/Shanghai')
                at time zone 'Asia/Shanghai'
        """)
        )
    binding = enqueue(store, deadline=deadline)
    attempt_id = binding["attempt_id"]
    assert store.claim(worker_id="deadline-test") == binding["task_id"]

    phase_count = 0
    lock_wait = None
    with ThreadPoolExecutor(max_workers=1) as workers:

        def begin_confirmation_phase(connection):
            nonlocal phase_count, lock_wait
            phase_count += 1
            with connection.connection.driver_connection.cursor() as cursor:
                cursor.execute("select set_config('TimeZone', %s, true), pg_backend_pid()", (zone,))
                applied_zone, waiting_pid = cursor.fetchone()
                assert applied_zone == zone
                cursor.execute("set local lock_timeout = '8s'")
            if barrier != "confirmation-lock" or phase_count != 3:
                return
            locker = engine.raw_connection()
            try:
                with locker.cursor() as cursor:
                    cursor.execute(
                        "select id, pg_backend_pid() from forward_attempt where id = %s for update", (attempt_id,)
                    )
                    locked_id, locker_pid = cursor.fetchone()
                    assert locked_id == attempt_id
                lock_wait = workers.submit(
                    release_lock_after_deadline, engine, locker, locker_pid, waiting_pid, deadline
                )
            except BaseException:
                locker.close()
                raise

        event.listen(engine, "begin", begin_confirmation_phase)
        try:
            result = confirm_attempt(store, attempt_id=attempt_id, evidence=EVIDENCE, task_result={"proof": "sealed"})
        finally:
            event.remove(engine, "begin", begin_confirmation_phase)

    with engine.connect() as connection:
        row = connection.execute(select(forward_attempt).where(forward_attempt.c.id == attempt_id)).mappings().one()
        probes = {
            probe["phase"]: probe
            for probe in connection.execute(text("select * from deadline_probe order by entered_at")).mappings()
        }
    task = store.get(binding["task_id"])
    expected_phases = ["evidence"]
    if barrier != "evidence":
        expected_phases.append("staging")
    expected_phases.append("seal" if barrier == "seal" else "missed")
    assert list(probes) == expected_phases
    assert {p["zone"] for p in probes.values()} == {zone}
    assert len({p["transaction_id"] for p in probes.values()}) == len(probes)
    assert len({p["backend_pid"] for p in probes.values()}) == 1
    assert phase_count == len(probes)
    assert row["deadline_at"] == deadline
    assert result.sampled_at.utcoffset() == timedelta(hours=8 if zone == "Asia/Shanghai" else 0)
    assert row["evidence_row_id"] == attempt_id
    assert row["evidence"] == EVIDENCE
    assert row["evidence_sha256"] == canonical_sha256(EVIDENCE)

    if barrier == "confirmation-lock":
        assert lock_wait is not None
        blocked_at, released_at = lock_wait.result()
        assert blocked_at < deadline < released_at <= result.sampled_at
    else:
        delayed = probes[barrier]
        assert delayed["entered_at"] < deadline < delayed["released_at"]
    if barrier == "evidence":
        assert row["staging_at"] is None
        assert probes["evidence"]["released_at"] <= result.sampled_at
    else:
        assert probes["evidence"]["released_at"] <= row["staging_at"] < deadline
        assert probes["staging"]["released_at"] <= result.sampled_at

    if barrier == "seal":
        assert result.sealed and result.detail == "sealed"
        assert row["sealed_at"] == result.sampled_at < deadline
        assert row["status"] == task["status"] == "succeeded"
        assert task["result"] == {"proof": "sealed"}
        replay = confirm_attempt(store, attempt_id=attempt_id, evidence=EVIDENCE, task_result={"proof": "replacement"})
        assert replay.sealed and replay.detail == "already-sealed" and replay.sampled_at == row["sealed_at"]
        mismatch = confirm_attempt(
            store, attempt_id=attempt_id, evidence={**EVIDENCE, "ranked": ["B"]}, task_result={"proof": "replacement"}
        )
        assert not mismatch.sealed and mismatch.detail == "evidence-mismatch" and mismatch.sampled_at is None
        with engine.connect() as connection:
            reread = (
                connection.execute(select(forward_attempt).where(forward_attempt.c.id == attempt_id)).mappings().one()
            )
        assert dict(reread) == dict(row)
        assert store.get(binding["task_id"]) == task
    else:
        assert not result.sealed and result.detail == "deadline-passed"
        assert result.sampled_at >= deadline
        assert row["sealed_at"] is None and row["status"] == "missed"
        assert task["status"] != "succeeded"
    print(
        f"zone={zone} barrier={barrier} deadline={deadline.isoformat()} "
        f"sample={result.sampled_at.isoformat()} state={row['status']} "
        f"transactions={[(phase, p['transaction_id'], p['released_at'].isoformat()) for phase, p in probes.items()]}"
    )


def test_conflicting_task_completion_rolls_back_the_seal(store):
    binding = enqueue(store)
    attempt_id = binding["attempt_id"]
    # A task that is still queued cannot be finished by the normal task store.
    with pytest.raises(TaskConflict):
        confirm_attempt(store, attempt_id=attempt_id, evidence=EVIDENCE)
    with store.engine.connect() as connection:
        row = connection.execute(select(forward_attempt).where(forward_attempt.c.id == attempt_id)).mappings().one()
    assert row["staging_at"] is not None and row["evidence"] == EVIDENCE
    assert row["status"] == "queued" and row["sealed_at"] is None
    assert store.get(binding["task_id"])["status"] == "queued"

    mismatch = confirm_attempt(store, attempt_id=attempt_id, evidence={"ranked": ["B"]})
    assert mismatch.detail == "evidence-mismatch" and not mismatch.sealed
    assert store.claim(worker_id="retry-confirmation") == binding["task_id"]
    sealed = confirm_attempt(store, attempt_id=attempt_id, evidence=EVIDENCE)
    assert sealed.sealed and store.get(binding["task_id"])["status"] == "succeeded"
