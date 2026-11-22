"""The prepared payload is written exactly once, and only while the task is running."""

from __future__ import annotations

import pytest

from finance_quant.task_contracts import TaskConflict
from support.fakes import MemoryTaskStore


def _pending_task(store):
    task = store.create_experiment({"name": "准备写一次"}, {"prepare_state": "pending"}, "prep-key-1", total=6)
    assert store.claim() == task["id"]
    return task["id"]


def test_save_prepared_writes_once():
    store = MemoryTaskStore()
    identifier = _pending_task(store)
    store.save_prepared(identifier, {"prepare_state": "prepared", "version": 1})
    assert store.get(identifier, private=True)["execution"]["prepare_state"] == "prepared"


def test_second_save_prepared_is_a_conflict():
    store = MemoryTaskStore()
    identifier = _pending_task(store)
    store.save_prepared(identifier, {"prepare_state": "prepared", "version": 1})
    with pytest.raises(TaskConflict):
        store.save_prepared(identifier, {"prepare_state": "prepared", "version": 2})
    assert store.get(identifier, private=True)["execution"] == {"prepare_state": "prepared", "version": 1}


def test_save_prepared_refuses_a_task_that_is_not_running():
    store = MemoryTaskStore()
    task = store.create_experiment({"name": "未认领"}, {"prepare_state": "pending"}, "prep-key-2", total=6)
    with pytest.raises(TaskConflict):
        store.save_prepared(task["id"], {"prepare_state": "prepared"})
    assert store.get(task["id"], private=True)["execution"] == {"prepare_state": "pending"}


def test_save_prepared_refuses_a_running_non_experiment():
    store = MemoryTaskStore()
    execution = {"prepare_state": "pending"}
    task = store.create_download("maintenance", {"name": "非实验任务"}, execution, "prep-wrong-kind")
    assert store.claim() == task["id"]
    assert store.get(task["id"])["status"] == "running"

    with pytest.raises(TaskConflict):
        store.save_prepared(task["id"], {"prepare_state": "prepared"})
    assert store.get(task["id"], private=True)["execution"] == execution
