"""Two-phase confirmation for forward registrations and attempts.

The deadline is proven by *when a committed row became visible*, never by a timestamp the
writer sampled before its own COMMIT:

  phase A  in a new transaction on the primary connection: lock the target row, read the
           evidence row (its visibility proves the evidence committed), then sample
           clock_timestamp() once and write an unusable staging proof if that instant is
           strictly before the deadline;
  phase B  after the staging transaction committed: a further transaction locks and reads
           the committed staging proof, samples clock_timestamp() once more, and only if
           that instant is strictly before the deadline seals the row as succeeded.

Nothing here decides "on time" from a transaction start time, a pre-lock time, or a value
returned by RETURNING.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, select, text

from finance_quant.checksums import sha256_text
from finance_quant.task_contracts import FORWARD_SCORE_MODE, TaskConflict

from .schema import forward_attempt, forward_registration, research_task
from .tasks import PgTaskStore, lock_submissions


def canonical_json(payload: object) -> str:
    """Stable text form of a payload; the digest is taken over exactly this text."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_sha256(payload: object) -> str:
    return sha256_text(canonical_json(payload))


SHANGHAI = ZoneInfo("Asia/Shanghai")
_REGISTRATION_DATES = ("signal_date", "entry_date", "exit_date")
_REGISTRATION_EVIDENCE_FIELDS = (
    "id",
    "idempotency_key",
    "fingerprint",
    "request",
    "name",
    *_REGISTRATION_DATES,
    "calendar_version",
    "calendar_fingerprint",
    "pool_codes",
    "pool_size",
    "top_k",
    "factor",
    "factor_window",
    "factor_direction",
    "data_rules",
    "strategy_rules",
    "dataset_id",
    "code_sha256",
)


def registration_deadline(signal_date: date) -> datetime:
    return datetime.combine(signal_date, time.min, tzinfo=SHANGHAI)


def registration_evidence_digest(row: Mapping[str, Any]) -> str:
    evidence = {key: row[key] for key in _REGISTRATION_EVIDENCE_FIELDS}
    for key in _REGISTRATION_DATES:
        evidence[key] = row[key].isoformat()
    return canonical_sha256(evidence)


def registration_evidence_matches(row: Mapping[str, Any]) -> bool:
    rules = row["data_rules"]
    lock_digest = rules.get("dependency_lock_sha256") if isinstance(rules, dict) else None
    return (
        isinstance(lock_digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", lock_digest) is not None
        and registration_evidence_digest(row) == row["confirmed_evidence_sha256"]
    )


def registration_proof_times_valid(row: Mapping[str, Any], deadline: datetime) -> bool:
    staged, sealed = row["staging_at"], row["confirmed_at"]
    return staged is not None and sealed is not None and staged <= sealed < deadline


class AttemptActive(RuntimeError):
    """The stage already has a queued/running attempt; map this to 409 at the route layer."""


@dataclass(frozen=True)
class Confirmation:
    sealed: bool
    sampled_at: datetime | None
    detail: str


def _lock_attempt(connection: Connection, attempt_id: str) -> dict:
    row = (
        connection.execute(text("select * from forward_attempt where id = :id for update"), {"id": attempt_id})
        .mappings()
        .first()
    )
    return dict(row) if row else {}


def confirm_attempt(
    store: PgTaskStore,
    *,
    attempt_id: str,
    evidence: dict,
    task_result: dict | None = None,
    finish_on_miss: bool = False,
    record_registration_result: bool = False,
) -> Confirmation:
    """Publish, stage, and seal evidence in separate transactions on one primary connection.

    On retry after an uncertain commit, the first row lock waits for the prior transaction
    and observes the persisted status before the deadline can be evaluated again.
    A sealed result is replayed only for the same bound evidence.
    """
    digest = canonical_sha256(evidence)
    with store.engine.connect().execution_options(isolation_level="READ COMMITTED") as connection:
        with connection.begin():
            stopped = _record_evidence(connection, attempt_id, evidence, digest)
        if stopped is not None:
            return stopped
        with connection.begin():
            staged = _stage_evidence(connection, attempt_id, digest, store if finish_on_miss else None)
        if staged.detail != "staged":
            return staged
        with connection.begin():
            return _seal_success(
                store,
                connection,
                attempt_id,
                digest,
                task_result,
                finish_on_miss=finish_on_miss,
                record_registration_result=record_registration_result,
            )


def _stopped_attempt(row: dict, digest: str) -> Confirmation | None:
    if not row:
        return Confirmation(False, None, "attempt-missing")
    if row["status"] == "succeeded":
        if not _matches_evidence(row, digest):
            return Confirmation(False, None, "evidence-mismatch")
        return Confirmation(True, row["sealed_at"], "already-sealed")
    if row["status"] not in ("queued", "running"):
        return Confirmation(False, None, f"attempt-status-{row['status']}")
    return None


def _matches_evidence(row: dict, digest: str) -> bool:
    return (
        row["evidence_row_id"] == row["id"]
        and row["evidence"] is not None
        and row["evidence_sha256"] == digest
        and canonical_sha256(row["evidence"]) == digest
    )


def attempt_sealed(row: dict, boundary: datetime) -> bool:
    """Check a persisted seal against its expected frozen time and evidence."""
    staged, sealed = row["staging_at"], row["sealed_at"]
    return (
        row["status"] == "succeeded"
        and row["deadline_at"] == boundary
        and staged is not None
        and sealed is not None
        and staged <= sealed
        and _eligible_sample(row, staged)
        and _eligible_sample(row, sealed)
        and _matches_evidence(row, row["evidence_sha256"])
    )


def score_task_matches(connection: Connection, score: Mapping[str, Any], binding: dict, *, lock: bool = False) -> bool:
    """Check the queue's successful result and execution against the sealed score."""
    task = research_task.c
    query = select(task.status, task.result, task.execution).where(task.id == score["task_id"])
    row = connection.execute(query.with_for_update() if lock else query).one_or_none()
    return (
        row is not None
        and row.status == "succeeded"
        and row.result == {"registration_id": score["registration_id"], "score_attempt_id": score["id"]}
        and isinstance(row.execution, dict)
        and row.execution.get("mode") == FORWARD_SCORE_MODE
        and row.execution.get("binding_sha256") == canonical_sha256(binding)
    )


def _record_evidence(connection: Connection, attempt_id: str, evidence: dict, digest: str) -> Confirmation | None:
    row = _lock_attempt(connection, attempt_id)
    if (stopped := _stopped_attempt(row, digest)) is not None:
        return stopped
    if any(row[key] is not None for key in ("evidence", "evidence_sha256", "evidence_row_id", "staging_at")):
        return None if _matches_evidence(row, digest) else Confirmation(False, None, "evidence-mismatch")
    connection.execute(
        forward_attempt.update()
        .where(forward_attempt.c.id == attempt_id)
        .values(evidence=evidence, evidence_sha256=digest, evidence_row_id=attempt_id)
    )
    return None


def _mark_missed(connection: Connection, row: dict, sampled: datetime, store: PgTaskStore | None) -> Confirmation:
    reason = "deadline-passed"
    connection.execute(
        forward_attempt.update().where(forward_attempt.c.id == row["id"]).values(status="missed", error=reason)
    )
    if store is not None:
        store.finish(row["task_id"], "failed", error=reason, connection=connection)
    return Confirmation(False, sampled, reason)


def _eligible_sample(row: dict, sampled: datetime) -> bool:
    # The existing time column stores the upper bound for a score and the earliest
    # permissible instant for a maturity verification. Verification has no upper bound.
    if row["kind"] == "verify":
        return sampled >= row["deadline_at"]
    return sampled < row["deadline_at"]


def _outside_time_bound(
    connection: Connection, row: dict, sampled: datetime, store: PgTaskStore | None
) -> Confirmation:
    if row["kind"] == "verify":
        return Confirmation(False, sampled, "before-verification-maturity")
    return _mark_missed(connection, row, sampled, store)


def _stage_evidence(
    connection: Connection, attempt_id: str, digest: str, store: PgTaskStore | None = None
) -> Confirmation:
    row = _lock_attempt(connection, attempt_id)
    if (stopped := _stopped_attempt(row, digest)) is not None:
        return stopped
    if not _matches_evidence(row, digest):
        return Confirmation(False, None, "evidence-mismatch")
    if row["staging_at"] is not None:
        if not _eligible_sample(row, row["staging_at"]):
            return Confirmation(False, None, "invalid-staging-proof")
        return Confirmation(False, row["staging_at"], "staged")

    sampled = connection.execute(text("select clock_timestamp()")).scalar_one()
    if not _eligible_sample(row, sampled):
        return _outside_time_bound(connection, row, sampled, store)
    connection.execute(forward_attempt.update().where(forward_attempt.c.id == attempt_id).values(staging_at=sampled))
    return Confirmation(False, sampled, "staged")


def _seal_success(
    store: PgTaskStore,
    connection: Connection,
    attempt_id: str,
    digest: str,
    task_result: dict | None,
    *,
    finish_on_miss: bool = False,
    record_registration_result: bool = False,
) -> Confirmation:
    row = _lock_attempt(connection, attempt_id)
    if (stopped := _stopped_attempt(row, digest)) is not None:
        return stopped
    if row["staging_at"] is None:
        return Confirmation(False, None, "no-staging-proof")
    if not _matches_evidence(row, digest):
        return Confirmation(False, None, "evidence-mismatch")
    if not _eligible_sample(row, row["staging_at"]):
        return Confirmation(False, None, "invalid-staging-proof")
    if row["task_id"] is None:
        return Confirmation(False, None, "attempt-task-missing")
    if record_registration_result and not _verification_evidence_current(connection, row):
        return Confirmation(False, None, "verification-evidence-changed")

    sampled = connection.execute(text("select clock_timestamp()")).scalar_one()
    if not _eligible_sample(row, sampled):
        return _outside_time_bound(connection, row, sampled, store if finish_on_miss else None)
    if sampled < row["staging_at"]:
        return Confirmation(False, None, "invalid-staging-proof")
    connection.execute(
        forward_attempt.update().where(forward_attempt.c.id == attempt_id).values(status="succeeded", sealed_at=sampled)
    )
    if record_registration_result:
        evidence = row["evidence"]
        registration = forward_registration
        changed = connection.execute(
            registration.update()
            .where(
                registration.c.id == row["registration_id"],
                registration.c.confirmed_evidence_sha256 == evidence["registration_sha256"],
                registration.c.label_snapshot == evidence["label_snapshot"],
            )
            .values(result=evidence["result"], result_recorded_at=sampled)
        )
        if changed.rowcount != 1:
            raise TaskConflict("verification-registration-binding-changed")
    store.finish(row["task_id"], "succeeded", result=task_result, connection=connection)
    return Confirmation(True, sampled, "sealed")


def _verification_evidence_current(connection: Connection, attempt: dict) -> bool:
    """Lock the registration, sealed score and its queue task before publishing a metric."""
    if attempt["kind"] != "verify":
        return False
    evidence = attempt["evidence"]
    registration = (
        connection.execute(
            select(forward_registration)
            .where(forward_registration.c.id == attempt["registration_id"])
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if (
        registration is None
        or not registration_row_confirmed(registration)
        or registration["confirmed_evidence_sha256"] != evidence["registration_sha256"]
        or registration["label_snapshot"] != evidence["label_snapshot"]
        or registration["result_recorded_at"] is not None
        or attempt["staging_at"] < datetime.fromisoformat(evidence["label_snapshot"]["captured_at"])
    ):
        return False
    score = _lock_attempt(connection, evidence["score_attempt_id"])
    binding = registration["score_snapshot"]
    return (
        isinstance(binding, dict)
        and bool(score)
        and score["kind"] == "score"
        and score["registration_id"] == registration["id"]
        and score["task_id"] == evidence.get("score_task_id")
        and attempt_sealed(score, datetime.combine(registration["entry_date"], time(9, 15), tzinfo=SHANGHAI))
        and score["evidence_sha256"] == evidence["score_evidence_sha256"]
        and isinstance(score["evidence"], dict)
        and score["evidence"].get("score_snapshot") == binding
        and score_task_matches(connection, score, binding, lock=True)
    )


def _lock_forward_task_attempt(connection: Connection, task_id: str, kind: str) -> dict | None:
    row = (
        connection.execute(
            select(forward_attempt)
            .where(forward_attempt.c.task_id == task_id, forward_attempt.c.kind == kind)
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def start_forward_attempt(connection: Connection, task_id: str, registration_id: str, *, kind: str) -> dict:
    row = _lock_forward_task_attempt(connection, task_id, kind)
    if row is None or row["registration_id"] != registration_id or row["status"] not in {"queued", "running"}:
        raise TaskConflict(f"{kind}-attempt-unavailable")
    connection.execute(forward_attempt.update().where(forward_attempt.c.id == row["id"]).values(status="running"))
    return row


def fail_forward_task(
    store: PgTaskStore, task_id: str, reason: str, *, kind: str, evidence: dict | None = None
) -> bool:
    """Retain diagnostics and finish the queue task together, including after an uncertain COMMIT."""
    try:
        with store.engine.connect().execution_options(isolation_level="READ COMMITTED") as connection:
            with connection.begin():
                row = _lock_forward_task_attempt(connection, task_id, kind)
                if row is not None and row["status"] == "succeeded":
                    return False
                if row is not None and evidence is not None and row["status"] == "running" and row["evidence"] is None:
                    _record_evidence(connection, row["id"], evidence, canonical_sha256(evidence))
                store.finish(task_id, "failed", error=reason, connection=connection)
        return True
    except TaskConflict:
        # The failed update rolled back. A concurrent seal or a prior terminal write won.
        if store.get(task_id)["status"] == "running":
            raise
        return False


def registration_confirmed(connection: Connection, registration_id: str) -> bool:
    """Check the frozen evidence and both proof samples on the same persisted row."""
    row = (
        connection.execute(select(forward_registration).where(forward_registration.c.id == registration_id))
        .mappings()
        .first()
    )
    return row is not None and registration_row_confirmed(row)


def registration_row_confirmed(row: Mapping[str, Any]) -> bool:
    return (
        row["status"] != "expired"
        and registration_proof_times_valid(row, registration_deadline(row["signal_date"]))
        and registration_evidence_matches(row)
    )


REGISTRATION_SUMMARY_FIELDS = (
    "id",
    "name",
    "status",
    "signal_date",
    "entry_date",
    "exit_date",
    "pool_size",
    "top_k",
    "factor",
    "factor_window",
    "factor_direction",
    "created_at",
)


def list_registrations(connection: Connection, *, limit: int = 50, offset: int = 0) -> list[dict]:
    """Newest first, narrow projection: no snapshots, no results, no evidence payloads."""
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    rows = connection.execute(
        text(
            "select " + ", ".join(REGISTRATION_SUMMARY_FIELDS) + " from forward_registration "
            "order by created_at desc, id desc limit :limit offset :offset"
        ),
        {"limit": limit, "offset": offset},
    ).mappings()
    return [dict(row) for row in rows]


def get_registration(connection: Connection, registration_id: str, *, include_evidence: bool = False) -> dict | None:
    """One row plus its attempts; the caller decides which fields to expose."""
    row = (
        connection.execute(text("select * from forward_registration where id = :id"), {"id": registration_id})
        .mappings()
        .first()
    )
    if row is None:
        return None
    detail = dict(row)
    attempt = forward_attempt.c
    fields = [
        attempt.id,
        attempt.kind,
        attempt.attempt_no,
        attempt.status,
        attempt.deadline_at,
        attempt.staging_at,
        attempt.sealed_at,
        attempt.evidence_sha256,
        attempt.error,
        attempt.created_at,
    ]
    if include_evidence:
        fields.extend((attempt.registration_id, attempt.task_id, attempt.evidence_row_id, attempt.evidence))
    attempts = connection.execute(
        select(*fields).where(attempt.registration_id == registration_id).order_by(attempt.kind, attempt.attempt_no)
    ).mappings()
    detail["attempts"] = [dict(item) for item in attempts]
    return detail


def enqueue_attempt(
    store,
    connection: Connection,
    *,
    registration_id: str,
    kind: str,
    attempt_no: int,
    deadline,
    task_key: str,
    request: dict,
    execution: dict,
    attempt_id: str | None = None,
) -> dict:
    """Replay or create a task/attempt pair inside the caller's transaction.

    The queue's submission lock serializes the first lookup and write of each pair.
    Existing bindings bypass active-attempt and queue-limit checks. For a new pair,
    both writes use the caller's connection and commit or roll back together.
    """
    if kind not in ("score", "verify"):
        raise ValueError("unknown attempt kind")
    lock_submissions(connection)
    replay = find_attempt_replay(store, connection, registration_id, kind, task_key, request)
    if replay is not None:
        return replay

    if attempt_active(connection, registration_id, kind):
        raise AttemptActive("该阶段已有排队/运行中的尝试")
    if store.find_research_replay(request, task_key, connection=connection) is not None:
        raise TaskConflict("该请求标识已用于其他任务")
    task = store.create(request, execution, task_key, connection=connection)
    identifier = attempt_id or "fat-" + canonical_sha256({"reg": registration_id, "kind": kind, "no": attempt_no})[:32]
    connection.execute(
        forward_attempt.insert().values(
            id=identifier,
            registration_id=registration_id,
            kind=kind,
            attempt_no=attempt_no,
            status="queued",
            idempotency_key=task_key,
            task_id=task["id"],
            deadline_at=deadline,
        )
    )
    return {"attempt_id": identifier, "task_id": task["id"]}


def find_attempt_replay(
    store: PgTaskStore, connection: Connection, registration_id: str, kind: str, key: str, request: dict
) -> dict | None:
    """Look up an already committed pair without rereading its physical snapshot."""
    attempt = connection.execute(
        select(
            forward_attempt.c.id,
            forward_attempt.c.registration_id,
            forward_attempt.c.kind,
            forward_attempt.c.task_id,
        ).where(forward_attempt.c.idempotency_key == key)
    ).one_or_none()
    if attempt is not None:
        if (attempt.registration_id, attempt.kind) != (registration_id, kind):
            raise TaskConflict("该请求标识已用于其他登记或阶段")
        task = store.find_research_replay(request, key, connection=connection)
        if task is None or task["id"] != attempt.task_id:
            raise TaskConflict("该请求标识的前瞻尝试与任务绑定不一致")
        return {"attempt_id": attempt.id, "task_id": attempt.task_id}

    return None


def attempt_active(connection: Connection, registration_id: str, kind: str) -> bool:
    """Only one active attempt per stage is allowed; the partial unique index enforces it."""
    row = connection.execute(
        text(
            "select 1 from forward_attempt where registration_id = :reg and kind = :kind "
            "and status in ('queued','running') limit 1"
        ),
        {"reg": registration_id, "kind": kind},
    ).first()
    return row is not None
