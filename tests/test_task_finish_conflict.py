"""finish(): a terminal write that touches no row is a conflict, never a silent success."""

from __future__ import annotations

import pytest

from finance_quant.persistence import tasks
from finance_quant.task_contracts import TaskConflict


class _Result:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class _Connection:
    def __init__(self, rowcount):
        self.rowcount = rowcount

    def execute(self, _statement):
        return _Result(self.rowcount)


def test_zero_row_terminal_write_raises_conflict():
    with pytest.raises(TaskConflict):
        tasks.PgTaskStore._require_single_row(_Connection(0))


def test_single_row_terminal_write_is_accepted():
    tasks.PgTaskStore._require_single_row(_Connection(1))


def test_more_than_one_row_is_also_a_conflict():
    with pytest.raises(TaskConflict):
        tasks.PgTaskStore._require_single_row(_Connection(2))
