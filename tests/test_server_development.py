import os
import sys
import threading
from unittest.mock import patch

import pytest

from finance_quant import server
from finance_quant.application.tasks import TaskConflict
from finance_quant.data.financials import financial_root
from support.fakes import MemoryTaskStore


def download_task(store, kind, execution, key):
    task = store.create_download(kind, {"name": kind}, execution, key)
    store.claim()
    return task["id"]


def test_service_stop_pauses_a_resumable_market_download(tmp_path):
    store = MemoryTaskStore()
    market = tmp_path / "market"
    identifier = download_task(store, "market_download", {"root": str(market), "plan_id": "plan"}, "pause-market-1")

    server.request_task_pause(store, identifier, tmp_path / "data")

    assert (market / "PAUSE").read_text(encoding="utf-8") == "service stopping"


def test_service_stop_pauses_a_financial_download(tmp_path):
    store = MemoryTaskStore()
    data = tmp_path / "data"
    identifier = download_task(store, "financial_download", {"root": "unused", "plan_id": "plan"}, "pause-finance-1")

    server.request_task_pause(store, identifier, data)

    assert (financial_root(data) / "PAUSE").read_text(encoding="utf-8") == "service stopping"


def test_service_stop_pauses_an_industry_download(tmp_path):
    store = MemoryTaskStore()
    root = tmp_path / "industries"
    identifier = download_task(store, "industry_download", {"root": str(root), "plan_id": "plan"}, "pause-industry-1")

    server.request_task_pause(store, identifier, tmp_path / "data")

    assert (root / "PAUSE").read_text(encoding="utf-8") == "service stopping"


def test_service_stop_does_not_touch_research_or_experiment(tmp_path):
    store = MemoryTaskStore()
    data = tmp_path / "data"
    research = store.create({}, {"mode": "snapshot"}, "pause-research-1")["id"]
    experiment = store.create_experiment({}, {"mode": "snapshot"}, "pause-experiment-1", total=5)["id"]

    for identifier in (research, experiment):
        server.request_task_pause(store, identifier, data)

    assert not (financial_root(data) / "PAUSE").exists()
    assert not list(tmp_path.rglob("PAUSE"))


def test_worker_shutdown_pauses_the_running_download_instead_of_crashing(tmp_path, monkeypatch):
    """Regression: the pause branch read ``task`` before it was assigned and killed the worker."""

    store = MemoryTaskStore()
    data, runs, root = tmp_path / "data", tmp_path / "runs", tmp_path / "storage"
    market = root / "market"
    store.create_download(
        "market_download", {"name": "行情下载"}, {"root": str(market), "plan_id": "plan"}, "shutdown-market-1"
    )
    stop = threading.Event()

    class Process:
        returncode = 0

        def poll(self):
            return 0 if (market / "PAUSE").exists() else None

    class Connection:
        def execute(self, statement):
            return None

        def commit(self):
            return None

    class Claiming(MemoryTaskStore):
        def claim(self, worker_id=None):
            identifier = super().claim(worker_id)
            stop.set()
            return identifier

    queue = Claiming()
    queue._rows = store._rows
    queue._keys = store._keys
    (root / "logs").mkdir(parents=True)

    monkeypatch.setattr(server.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(server.time, "sleep", lambda seconds: None)
    server.consume(queue, root, runs, data, stop, Connection())

    assert (market / "PAUSE").read_text(encoding="utf-8") == "service stopping"
    assert queue.list()["items"][0]["status"] == "interrupted"


def test_native_api_loads_development_env_and_passes_uvicorn_options(tmp_path, monkeypatch):
    env_file = tmp_path / ".env.development"
    env_file.write_text(
        "POSTGRES_HOST=127.0.0.1\n"
        "POSTGRES_DB=finance_quant_dev\n"
        "POSTGRES_USER=quant_dev\n"
        "POSTGRES_PASSWORD=development-only\n"
        "HTTP_HOST=127.0.0.2\n"
        "HTTP_PORT=8123\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "check_schema", lambda: None)
    monkeypatch.setattr(server, "paths", lambda: (tmp_path, tmp_path / "runs", tmp_path / "data"))
    captured = {}

    class FakeUvicorn:
        @staticmethod
        def run(app, **options):
            captured.update(app=app, **options)

    monkeypatch.setattr(server.uvicorn, "run", FakeUvicorn.run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["finance-quant-server", "api", "--env-file", str(env_file), "--reload"],
    )

    with patch.dict(os.environ, {}, clear=False):
        for name in ("POSTGRES_HOST", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD", "HTTP_HOST", "HTTP_PORT"):
            os.environ.pop(name, None)
        server.main()

    assert captured == {
        "app": "finance_quant.server:create_http_app",
        "factory": True,
        "host": "127.0.0.2",
        "port": 8123,
        "reload": True,
    }


def test_missing_development_env_is_rejected_before_database_access(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "missing.env"
    monkeypatch.setattr(sys, "argv", ["finance-quant-server", "check", "--env-file", str(missing)])

    try:
        server.main()
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("missing env file must stop startup")

    assert "env file does not exist" in capsys.readouterr().err


def _run_child_exit(tmp_path, monkeypatch, *, returncode, stage=None, oom_counts=None):
    store = MemoryTaskStore()
    data, runs, root = tmp_path / "data", tmp_path / "runs", tmp_path / "storage"
    (root / "logs").mkdir(parents=True)
    task_id = store.create({"name": "测试研究"}, {"mode": "snapshot"}, f"exit-{returncode}-{stage}")["id"]
    stop = threading.Event()
    counts = iter(oom_counts or [])

    class Process:
        def __init__(self, code):
            self.returncode = code

        def poll(self):
            return self.returncode

    class Connection:
        def execute(self, statement):
            return None

        def commit(self):
            return None

    class Claiming(MemoryTaskStore):
        def claim(self, worker_id=None):
            identifier = super().claim(worker_id)
            if identifier:
                if stage:
                    self.stage(identifier, stage, "运行中", 2)
                stop.clear()
            return identifier

        def finish(self, identifier, status, *, result=None, error=None, error_details=None):
            super().finish(identifier, status, result=result, error=error, error_details=error_details)
            stop.set()

    queue = Claiming()
    queue._rows = store._rows
    queue._keys = store._keys
    monkeypatch.setattr(server, "read_oom_kill_count", lambda: next(counts, None))
    monkeypatch.setattr(server.subprocess, "Popen", lambda *args, **kwargs: Process(returncode))
    monkeypatch.setattr(server.time, "sleep", lambda seconds: None)
    server.consume(queue, root, runs, data, stop, Connection())
    return queue.get(task_id)


@pytest.mark.parametrize("returncode", [-9, 137])
def test_child_sigkill_records_stage_and_omits_oom_without_cgroup_proof(tmp_path, monkeypatch, returncode):
    task = _run_child_exit(tmp_path, monkeypatch, returncode=returncode, stage="backtest", oom_counts=[None, None])
    assert task["status"] == "failed"
    assert task["stage"] == "finished"
    assert task["error"] == f"在回测阶段被系统强制终止（退出码 {returncode}）"
    assert "内存不足" not in task["error"]
    assert "超出容器配额" not in task["error"]
    assert task["error_details"]["stage"] == "backtest"
    assert task["error_details"]["exit_code"] == returncode


def test_child_sigkill_with_cgroup_oom_delta_records_oom_evidence(tmp_path, monkeypatch):
    task = _run_child_exit(tmp_path, monkeypatch, returncode=-9, stage="backtest", oom_counts=[0, 1])
    assert task["status"] == "failed"
    assert task["error"] == "在回测阶段因内存不足被系统终止（退出码 -9）"
    assert "超出容器配额" not in task["error"]
    assert task["error_details"]["stage"] == "backtest"
    assert task["error_details"]["oom_killed"] is True


def test_child_sigkill_without_oom_delta_does_not_claim_oom(tmp_path, monkeypatch):
    task = _run_child_exit(tmp_path, monkeypatch, returncode=-9, stage="factors", oom_counts=[2, 2])
    assert task["error"] == "在计算因子阶段被系统强制终止（退出码 -9）"
    assert task["error_details"]["oom_killed"] is False
    assert "内存不足" not in task["error"]


@pytest.mark.parametrize("returncode", [-15, 143])
def test_child_sigterm_marks_interrupted_and_not_sigkill(tmp_path, monkeypatch, returncode):
    task = _run_child_exit(tmp_path, monkeypatch, returncode=returncode, stage="backtest")
    assert task["status"] == "interrupted"
    assert task["error"] == f"在回测阶段被要求停止（退出码 {returncode}）"
    assert "强制终止" not in task["error"]
    assert "内存不足" not in task["error"]
    assert task["error_details"]["stage"] == "backtest"
    assert task["error_details"]["exit_code"] == returncode


@pytest.mark.parametrize("returncode", [1, 3221225477])
def test_child_nonzero_exit_preserves_exact_code_without_guessing_oom(tmp_path, monkeypatch, returncode):
    task = _run_child_exit(tmp_path, monkeypatch, returncode=returncode, stage="evaluation")
    assert task["status"] == "failed"
    assert task["error"] == f"在因子评估阶段异常退出（退出码 {returncode}），请查看任务日志"
    assert "内存不足" not in task["error"]
    assert task["error_details"]["stage"] == "evaluation"
    assert task["error_details"]["exit_code"] == returncode


def test_read_oom_kill_count_parses_cgroup_events_and_handles_missing(tmp_path):
    missing = tmp_path / "missing.events"
    assert server.read_oom_kill_count(missing) is None
    events = tmp_path / "memory.events"
    events.write_text("low 0\nhigh 0\noom 1\noom_kill 3\n", encoding="utf-8")
    assert server.read_oom_kill_count(events) == 3


def test_memory_store_annotate_failure_updates_failed_task_only():
    store = MemoryTaskStore()
    queued = store.create({"name": "q"}, {"mode": "snapshot"}, "annotate-1")
    with pytest.raises(TaskConflict):
        store.annotate_failure(queued["id"], {"summary": "bad"})
    store.claim()
    store.stage(queued["id"], "backtest", "回测中", 2)
    store.finish(queued["id"], "failed", error="raw")
    updated = store.annotate_failure(queued["id"], {"summary": "回测失败", "stage": "backtest"})
    assert updated["error"] == "回测失败"
    assert updated["error_details"]["stage"] == "backtest"
