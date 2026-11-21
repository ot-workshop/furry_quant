"""consume must survive a terminal-state conflict and keep processing the next task."""

from __future__ import annotations

import threading

import pytest

from finance_quant import server
from finance_quant.task_contracts import TaskConflict


class _Stop:
    def __init__(self):
        self._event = threading.Event()
        self.waits = 0

    def is_set(self):
        return self._event.is_set()

    def set(self):
        self._event.set()

    def wait(self, timeout=None):
        self.waits += 1
        return self._event.is_set()


class _LockConnection:
    def __init__(self):
        self.executed = 0

    def execute(self, _statement):
        self.executed += 1

    def commit(self):
        pass


class _Store:
    """Claims two tasks; the first terminal write loses the race."""

    def __init__(self, stop, conflict_on):
        self._ids = ["task-" + "a" * 32, "task-" + "b" * 32]
        self.stop = stop
        self.conflict_on = conflict_on
        self.finished = []
        self.claims = 0

    def claim(self):
        if not self._ids:
            self.stop.set()
            return None
        identifier = self._ids.pop(0)
        self.claims += 1
        return identifier

    def get(self, identifier, *, private=False):
        return {"id": identifier, "status": "running", "stage": "prepare"}

    def heartbeat(self, identifier):
        pass

    def finish(self, identifier, status, *, result=None, error=None, error_details=None):
        if identifier == self.conflict_on:
            raise TaskConflict("任务状态已变化，无法写入终态")
        self.finished.append((identifier, status))


class _Process:
    pid = 4242
    returncode = 0

    def poll(self):
        return 0


@pytest.fixture()
def patched(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "enqueue_due", lambda store, data: None)
    monkeypatch.setattr(server, "read_oom_kill_count", lambda: None)
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: _Process())
    (tmp_path / "logs").mkdir()
    return tmp_path


def _consume(root, stop, store):
    server.consume(store, root, root, root, stop, _LockConnection())


def test_conflict_at_finish_does_not_stop_the_loop(patched):
    stop = _Stop()
    store = _Store(stop, conflict_on="task-" + "a" * 32)

    _consume(patched, stop, store)  # returns normally

    assert store.claims == 2, "the loop must claim the second task after the conflict"
    assert store.finished == [("task-" + "b" * 32, "failed")]


def test_conflict_on_the_last_task_still_returns(patched):
    stop = _Stop()
    store = _Store(stop, conflict_on="task-" + "b" * 32)

    _consume(patched, stop, store)

    assert store.claims == 2
    assert store.finished == [("task-" + "a" * 32, "failed")]
