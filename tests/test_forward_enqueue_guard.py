"""An active attempt must short-circuit before any task row is created."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from finance_quant.persistence import forward


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def mappings(self):
        return self

    def scalar_one(self):
        return self._rows[0]


class _Connection:
    """Reports one active attempt and records whether anything was written."""

    def __init__(self):
        self.writes = []

    def execute(self, statement, params=None):
        sql = str(statement).lower()
        if sql.strip().startswith("select 1 from forward_attempt"):
            return _Result([(1,)])
        self.writes.append(sql)
        return _Result([])


class _Store:
    def __init__(self):
        self.create_calls = 0

    def create(self, request, execution, key, *, connection=None):
        self.create_calls += 1
        raise AssertionError("create must not be called when an attempt is active")


def test_active_attempt_raises_before_creating_a_task():
    connection = _Connection()
    store = _Store()
    with pytest.raises(forward.AttemptActive):
        forward.enqueue_attempt(
            store,
            connection,
            registration_id="fwd-1",
            kind="score",
            attempt_no=1,
            deadline=datetime.now(timezone.utc) + timedelta(hours=1),
            task_key="forward-score-1",
            request={"name": "x"},
            execution={"mode": "snapshot"},
        )
    assert store.create_calls == 0
    assert connection.writes == []
