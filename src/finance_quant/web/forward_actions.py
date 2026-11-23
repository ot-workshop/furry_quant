"""Submit the existing forward workflows under the shared maintenance lock."""

from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import Connection, Engine, select

from finance_quant.application.forward_score import ScoreInputError, enqueue_forward_score
from finance_quant.application.forward_verification import enqueue_forward_verification
from finance_quant.persistence.forward import attempt_for_submission_key, find_attempt_replay
from finance_quant.persistence.schema import forward_registration
from finance_quant.persistence.tasks import PgTaskStore
from finance_quant.task_contracts import TaskConflict, submission_fingerprint
from finance_quant.web.write_requests import research_write_gate

_SNAPSHOT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
_PUBLISHED_SCORE_ID = re.compile(r"forward-score-[0-9a-f]{64}")


class _SubmissionConnection:
    """Borrow the gated session for the enqueue services' existing transaction scopes."""

    def __init__(self, connection: Connection):
        self.connection = connection

    @contextmanager
    def connect(self):
        try:
            yield self.connection
        finally:
            self.connection.rollback()

    @contextmanager
    def begin(self):
        with self.connection.begin():
            yield self.connection


def _registration(connection: Connection, identifier: str) -> dict:
    table = forward_registration.c
    row = (
        connection.execute(select(table.id, table.name, table.score_snapshot).where(table.id == identifier))
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise HTTPException(404, "前瞻登记不存在")
    return dict(row)


def submit_forward_score(engine: Engine, data_root: Path, identifier: str, payload: object, key: str) -> dict:
    if not isinstance(payload, dict) or payload.keys() != {"score_snapshot_id"}:
        raise ValueError("评分提交必须指定一个已发布的评分快照标识")
    snapshot_id = payload["score_snapshot_id"]
    if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID.fullmatch(snapshot_id):
        raise ValueError("评分快照标识无效")
    submission_fingerprint("research", {}, key)
    with research_write_gate(engine) as connection:
        scoped = _SubmissionConnection(connection)
        with scoped.connect() as read:
            registration = _registration(read, identifier)
            attempt_for_submission_key(read, identifier, "score", key)
        bound = registration["score_snapshot"]
        if isinstance(bound, dict):
            if bound.get("data_version") != snapshot_id:
                raise TaskConflict("score-snapshot-already-bound")
        elif not _PUBLISHED_SCORE_ID.fullmatch(snapshot_id):
            raise ScoreInputError("首次评分须使用已发布的受管评分快照")
        store = PgTaskStore(engine=scoped)
        try:
            return enqueue_forward_score(store, data_root, identifier, snapshot_id, key=key)
        except (ScoreInputError, TaskConflict):
            raise
        except ValueError:
            if bound is None:
                # A concurrent submission can commit while the first snapshot lookup is failing.
                # The original transaction is closed. Only replay a committed pair in a fresh read.
                request = {
                    "name": registration["name"],
                    "registration_id": identifier,
                    "score_snapshot_id": snapshot_id,
                }
                with scoped.connect() as read:
                    replay = find_attempt_replay(store, read, identifier, "score", key, request)
                if replay is not None:
                    return replay
            raise


def submit_forward_verification(
    engine: Engine, data_root: Path, identifier: str, payload: object, key: str
) -> dict | None:
    if payload != {}:
        raise ValueError("到期验证提交不接受额外参数")
    submission_fingerprint("research", {}, key)
    with research_write_gate(engine) as connection:
        scoped = _SubmissionConnection(connection)
        with scoped.connect() as read:
            _registration(read, identifier)
            attempt_for_submission_key(read, identifier, "verify", key)
        return enqueue_forward_verification(PgTaskStore(engine=scoped), data_root, identifier, key=key)
