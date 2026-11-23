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
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, select, text

from finance_quant.checksums import sha256_text
from finance_quant.task_contracts import TaskConflict

from .schema import forward_attempt
from .tasks import lock_submissions


def canonical_json(payload: object) -> str:
    """Stable text form of a payload; the digest is taken over exactly this text."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_sha256(payload: object) -> str:
    return sha256_text(canonical_json(payload))


class AttemptActive(RuntimeError):
    """The stage already has a queued/running attempt; map this to 409 at the route layer."""


@dataclass(frozen=True)
class Confirmation:
    sealed: bool
    sampled_at: datetime | None
    detail: str


def _lock_row(connection: Connection, table, row_id: str) -> dict | None:
    row = (
        connection.execute(text(f"select * from {table.name} where id = :id for update"), {"id": row_id})
        .mappings()
        .first()
    )
    return dict(row) if row else None


def stage_evidence(  # phase A
    connection: Connection,
    *,
    attempt_id: str,
    deadline: datetime,
    evidence: dict,
) -> Confirmation:
    """Lock, read the evidence, sample once, and record the staging proof if it is early.

    Evidence is embedded in the attempt row itself, so `evidence_row_id` names that row (the
    attempt id) and `evidence_sha256` pins the canonical payload digest — the pair identifies
    exactly which bytes were read when the sample was taken.

    When the single sample is not strictly before the deadline the attempt is marked
    `missed` **but the evidence stays**: a missed attempt keeps its payload for audit, while
    `status=missed` makes it unusable for success. Callers must not clear it.
    """
    row = _lock_row(connection, forward_attempt, attempt_id)
    if row is None:
        return Confirmation(False, None, "attempt-missing")
    if row["status"] not in ("queued", "running"):
        return Confirmation(False, None, f"attempt-status-{row['status']}")

    digest = canonical_sha256(evidence)
    connection.execute(
        forward_attempt.update()
        .where(forward_attempt.c.id == attempt_id)
        .values(evidence=evidence, evidence_sha256=digest)
    )

    sampled = connection.execute(text("select clock_timestamp()")).scalar_one()
    if not sampled < deadline:
        connection.execute(forward_attempt.update().where(forward_attempt.c.id == attempt_id).values(status="missed"))
        return Confirmation(False, sampled, "deadline-passed")

    connection.execute(
        forward_attempt.update()
        .where(forward_attempt.c.id == attempt_id)
        .values(staging_at=sampled, evidence_row_id=attempt_id)
    )
    return Confirmation(False, sampled, "staged")


def seal_success(  # phase B
    connection: Connection,
    *,
    attempt_id: str,
    deadline: datetime,
) -> Confirmation:
    """Lock and read the committed staging proof, sample once, then seal or miss."""
    row = _lock_row(connection, forward_attempt, attempt_id)
    if row is None:
        return Confirmation(False, None, "attempt-missing")
    if row["status"] == "succeeded":
        return Confirmation(True, row["sealed_at"], "already-sealed")
    if row["staging_at"] is None:
        return Confirmation(False, None, "no-staging-proof")
    if row["evidence"] is None or row["evidence_sha256"] is None:
        return Confirmation(False, None, "no-evidence")
    if sha256_text(canonical_json(row["evidence"])) != row["evidence_sha256"]:
        # The stored payload no longer matches the digest bound at staging time: the proof
        # is unusable, so this can never be sealed as success.
        return Confirmation(False, None, "evidence-mismatch")

    sampled = connection.execute(text("select clock_timestamp()")).scalar_one()
    if not sampled < deadline:
        connection.execute(forward_attempt.update().where(forward_attempt.c.id == attempt_id).values(status="missed"))
        return Confirmation(False, sampled, "deadline-passed")

    connection.execute(
        forward_attempt.update().where(forward_attempt.c.id == attempt_id).values(status="succeeded", sealed_at=sampled)
    )
    return Confirmation(True, sampled, "sealed")


def registration_confirmed(connection: Connection, registration_id: str) -> bool:
    """A registration counts only when both phases happened before its signal-day deadline."""
    row = (
        connection.execute(
            text("select confirmed_at, status from forward_registration where id = :id"),
            {"id": registration_id},
        )
        .mappings()
        .first()
    )
    return bool(row) and row["status"] != "expired" and row["confirmed_at"] is not None


_LIST_COLUMNS = (
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
            "select " + ", ".join(_LIST_COLUMNS) + " from forward_registration "
            "order by created_at desc, id desc limit :limit offset :offset"
        ),
        {"limit": limit, "offset": offset},
    ).mappings()
    return [dict(row) for row in rows]


def get_registration(connection: Connection, registration_id: str) -> dict | None:
    """One row plus its attempts; the caller decides which fields to expose."""
    row = (
        connection.execute(text("select * from forward_registration where id = :id"), {"id": registration_id})
        .mappings()
        .first()
    )
    if row is None:
        return None
    detail = dict(row)
    attempts = connection.execute(
        text(
            "select id, kind, attempt_no, status, deadline_at, staging_at, sealed_at, "
            "evidence_sha256, error, created_at from forward_attempt "
            "where registration_id = :id order by kind, attempt_no"
        ),
        {"id": registration_id},
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
    attempt = connection.execute(
        select(
            forward_attempt.c.id,
            forward_attempt.c.registration_id,
            forward_attempt.c.kind,
            forward_attempt.c.task_id,
        ).where(forward_attempt.c.idempotency_key == task_key)
    ).one_or_none()
    if attempt is not None:
        if (attempt.registration_id, attempt.kind) != (registration_id, kind):
            raise TaskConflict("该请求标识已用于其他登记或阶段")
        task = store.find_research_replay(request, task_key, connection=connection)
        if task is None or task["id"] != attempt.task_id:
            raise TaskConflict("该请求标识的前瞻尝试与任务绑定不一致")
        return {"attempt_id": attempt.id, "task_id": attempt.task_id}

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
