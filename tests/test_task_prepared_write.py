"""The prepared payload is written exactly once, and only while the task is running."""

from __future__ import annotations

import pytest

from finance_quant.task_contracts import TaskConflict
from support.fakes import MemoryTaskStore


def _pending_task(store):
    task = store.create_download(
        "maintenance", {"name": "准备写一次"}, {"prepare_state": "pending"}, "prep-key-1"
    )
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


def test_save_prepared_refuses_a_task_that_is_not_running():
    store = MemoryTaskStore()
    task = store.create_download(
        "maintenance", {"name": "未认领"}, {"prepare_state": "pending"}, "prep-key-2"
    )
    with pytest.raises(TaskConflict):
        store.save_prepared(task["id"], {"prepare_state": "prepared"})
