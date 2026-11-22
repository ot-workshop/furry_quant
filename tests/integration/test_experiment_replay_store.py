"""Experiment replay contract shared by the in-memory and PostgreSQL queue stores."""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from test_postgres_runtime import engine as engine

from finance_quant.persistence.tasks import PgTaskStore
from finance_quant.task_contracts import TaskConflict
from support.fakes import MemoryTaskStore


@pytest.fixture(params=["memory", "postgres"])
def store(request):
    if request.param == "memory":
        return MemoryTaskStore()
    if os.environ.get("FINANCE_QUANT_TEST_POSTGRES") != "1":
        pytest.skip("requires isolated PostgreSQL")
    return PgTaskStore(engine=request.getfixturevalue("engine"))


def test_replay_returns_public_original_without_changing_execution(store):
    key = "exp-replay-original"
    request = {"name": "固定 A", "data": {"mode": "snapshot", "snapshot_id": "A"}}
    execution = {"prepare_state": "pending", "data_version": "A"}
    assert store.find_experiment_replay(request, key) is None
    assert store.list()["total"] == 0
    original = store.create_experiment(request, execution, key, total=6)
    identifier = original["id"]
    assert store.claim() == identifier
    prepared = {**execution, "prepare_state": "prepared", "snapshot_dir": "abc"}
    store.save_prepared(identifier, prepared)
    store.finish(identifier, "failed", error="fixture failure")

    reordered = {"data": {"snapshot_id": "A", "mode": "snapshot"}, "name": "固定 A"}
    replay = store.find_experiment_replay(reordered, key)
    assert replay == store.get(identifier)
    assert replay["request"] == request
    assert replay["status"] == "failed" and "execution" not in replay
    assert store.get(identifier, private=True)["execution"] == prepared
    assert store.list()["total"] == 1


def test_replay_uses_enqueue_fingerprint_and_global_key_namespace(store):
    key = "exp-replay-shared-key"
    request = {"name": "测试", "data": {"snapshot_id": "A"}}
    execution = {"prepare_state": "pending", "data_version": "A"}
    original = store.create_experiment(request, execution, key, total=6)
    changed = {**request, "data": {"snapshot_id": "B"}}
    assert store.find_experiment_replay(changed, "exp-unclaimed-key") is None
    with pytest.raises(TaskConflict, match="参数已改变"):
        store.find_experiment_replay(changed, key)
    with pytest.raises(TaskConflict, match="参数已改变"):
        store.create_experiment(changed, {"data_version": "B"}, key, total=6)
    assert store.get(original["id"], private=True)["execution"] == execution

    other_key = "non-experiment-key"
    other = store.create(request, execution, other_key)
    with pytest.raises(TaskConflict, match="参数已改变"):
        store.find_experiment_replay(request, other_key)
    assert store.get(other["id"]) == other
    assert store.list()["total"] == 2


def test_two_replay_misses_still_use_atomic_enqueue(store):
    key = "exp-replay-racing"
    request = {"name": "same request"}
    both_missed = Barrier(2, timeout=10)

    def submit(data_version):
        assert store.find_experiment_replay(request, key) is None
        both_missed.wait()
        return store.create_experiment(request, {"data_version": data_version}, key, total=6)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(submit, "A")
        second = executor.submit(submit, "B")
        original, retry = first.result(timeout=15), second.result(timeout=15)
    assert retry == original
    assert store.list()["total"] == 1
    assert store.find_experiment_replay(request, key) == original
    stored_pin = store.get(original["id"], private=True)["execution"]
    other_pin = {"data_version": "B" if stored_pin == {"data_version": "A"} else "A"}
    assert stored_pin in ({"data_version": "A"}, {"data_version": "B"})
    assert store.create_experiment(request, other_pin, key, total=6) == original
    assert store.get(original["id"], private=True)["execution"] == stored_pin


def test_replay_reuses_enqueue_validation(store):
    for key in (None, "short", "exp/key-invalid", "x" * 81):
        with pytest.raises(ValueError, match="请求标识无效"):
            store.find_experiment_replay({}, key)
        with pytest.raises(ValueError, match="请求标识无效"):
            store.create_experiment({}, {}, key, total=6)
    invalid_request = {"name": "non-finite", "parameter": float("nan")}
    with pytest.raises(ValueError, match="Out of range float"):
        store.find_experiment_replay(invalid_request, "exp-invalid")
    with pytest.raises(ValueError, match="Out of range float"):
        store.create_experiment(invalid_request, {}, "exp-invalid", total=6)
    assert store.list()["total"] == 0
