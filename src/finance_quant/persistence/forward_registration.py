"""Synchronous forward registration and its committed deadline proof."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Connection, Engine, func, select, text
from sqlalchemy.dialects.postgresql import insert

from finance_quant.task_contracts import TaskConflict, submission_fingerprint

from . import forward
from .forward import Confirmation
from .schema import forward_registration


@dataclass(frozen=True)
class RegistrationReceipt:
    registration_id: str
    confirmed_at: datetime


class RegistrationExpired(TaskConflict):
    def __init__(self, registration_id: str):
        self.registration_id = registration_id
        super().__init__("登记确认未在信号日上海时间 00:00 前完成")


def register_and_confirm(
    engine: Engine,
    *,
    key: str,
    request: dict,
    freeze: Callable[[], dict],
    connection: Connection | None = None,
) -> RegistrationReceipt:
    """Commit frozen evidence, then stage and seal it in new transactions on one connection."""
    fingerprint = submission_fingerprint("forward-registration", request, key)
    with engine.connect() if connection is None else nullcontext(connection) as active:
        active.execution_options(isolation_level="READ COMMITTED")
        with active.begin():
            identifier = _record_registration(active, key, request, fingerprint, freeze)
        with active.begin():
            proof = _advance_proof(active, identifier, stage=True)
        if proof.detail == "staged":
            with active.begin():
                proof = _advance_proof(active, identifier, stage=False)
    if proof.detail == "expired":
        # The terminal audit row has committed before the caller receives the rejection.
        raise RegistrationExpired(identifier)
    assert proof.sealed and proof.sampled_at is not None
    return RegistrationReceipt(identifier, proof.sampled_at)


def _record_registration(
    connection: Connection, key: str, request: dict, fingerprint: str, freeze: Callable[[], dict]
) -> str:
    table = forward_registration
    by_key = select(table.c.id, table.c.request, table.c.fingerprint).where(table.c.idempotency_key == key)
    existing = connection.execute(by_key).mappings().one_or_none()
    if existing is None:
        try:
            frozen = freeze()
        except ValueError:
            # Source validation ran after the first miss. A new READ COMMITTED query
            # can see a registration another request committed in that interval.
            existing = connection.execute(by_key).mappings().one_or_none()
            if existing is None:
                raise
        else:
            row = dict(
                frozen,
                id=f"fwd-{uuid.uuid4().hex}",
                idempotency_key=key,
                request=request,
                fingerprint=fingerprint,
                status="registered",
            )
            # Bind the initial evidence at its commit. confirmed_at controls usability.
            row["confirmed_evidence_sha256"] = forward.registration_evidence_digest(row)
            identifier = connection.scalar(
                insert(table)
                .values(**row)
                .on_conflict_do_nothing(index_elements=[table.c.idempotency_key])
                .returning(table.c.id)
            )
            if identifier is not None:
                connection.execute(
                    table.update().where(table.c.id == identifier).values(written_at=func.clock_timestamp())
                )
                return identifier
            # The unique key waits out a competing insert. READ COMMITTED now sees the winner.
            existing = connection.execute(by_key).mappings().one()
    if existing["fingerprint"] != fingerprint or existing["request"] != request:
        raise TaskConflict("同一登记请求标识的参数已改变")
    return existing["id"]


def _advance_proof(connection: Connection, identifier: str, *, stage: bool) -> Confirmation:
    table = forward_registration
    row = dict(connection.execute(select(table).where(table.c.id == identifier).with_for_update()).mappings().one())
    if not forward.registration_evidence_matches(row):
        raise TaskConflict("登记证据与原摘要不匹配")
    if row["status"] == "expired":
        return Confirmation(False, None, "expired")

    deadline = forward.registration_deadline(row["signal_date"])
    staged_at, confirmed_at = row["staging_at"], row["confirmed_at"]
    valid_stage = staged_at is not None and staged_at < deadline
    if confirmed_at is not None:
        if not forward.registration_proof_times_valid(row, deadline):
            raise TaskConflict("已封存的登记证明无效")
        return Confirmation(True, confirmed_at, "already-sealed")
    if (staged_at is not None or not stage) and not valid_stage:
        raise TaskConflict("暂存的登记证明无效")
    if stage and valid_stage:
        return Confirmation(False, staged_at, "staged")

    sampled = connection.execute(text("select clock_timestamp()")).scalar_one()
    update = table.update().where(table.c.id == identifier)
    if sampled >= deadline:
        connection.execute(update.values(status="expired", invalid_reason="registration-deadline-passed"))
        return Confirmation(False, sampled, "expired")
    if stage:
        connection.execute(update.values(staging_at=sampled))
        return Confirmation(False, sampled, "staged")
    if sampled < staged_at:
        raise TaskConflict("封存采样早于暂存证明")
    connection.execute(update.values(confirmed_at=sampled))
    return Confirmation(True, sampled, "sealed")
