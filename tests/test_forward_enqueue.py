"""Enqueue semantics that must hold without a database: idempotency, activity, retries."""

from __future__ import annotations

from finance_quant.persistence import forward
from support.fakes import MemoryTaskStore


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None


class _ActiveConnection:
    """Reports whether an active attempt exists for the given stage."""

    def __init__(self, active: bool):
        self.active = active
        self.queries = []

    def execute(self, statement, params=None):
        self.queries.append((str(statement), params))
        return _Result([(1,)] if self.active else [])


def _request():
    return {"name": "前瞻评分", "top_k": 2}


def _execution():
    return {"mode": "snapshot", "snapshot_id": "ds-1"}


def test_same_key_returns_the_same_task():
    store = MemoryTaskStore()
    first = store.create(_request(), _execution(), "forward-score-1")
    second = store.create(_request(), _execution(), "forward-score-1")
    assert first["id"] == second["id"]


def test_different_key_creates_a_second_task():
    store = MemoryTaskStore()
    a = store.create(_request(), _execution(), "forward-score-1")
    b = store.create(_request(), _execution(), "forward-score-2")
    assert a["id"] != b["id"]


def test_active_attempt_blocks_a_second_one_for_the_same_stage():
    connection = _ActiveConnection(active=True)
    assert forward.attempt_active(connection, "fwd-1", "score") is True


def test_no_active_attempt_after_the_previous_one_is_terminal():
    connection = _ActiveConnection(active=False)
    assert forward.attempt_active(connection, "fwd-1", "score") is False


def test_active_attempt_is_scoped_to_the_stage():
    score = _ActiveConnection(active=True)
    verify = _ActiveConnection(active=False)
    assert forward.attempt_active(score, "fwd-1", "score") is True
    assert forward.attempt_active(verify, "fwd-1", "verify") is False
