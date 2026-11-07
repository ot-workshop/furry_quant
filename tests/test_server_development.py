import os
import sys
from unittest.mock import patch

from finance_quant import server
from finance_quant.data.financials import financial_root
from finance_quant.persistence import database
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
    import threading

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
    monkeypatch.setattr(database, "check_schema", lambda: None)
    monkeypatch.setattr(server, "paths", lambda: (tmp_path, tmp_path / "runs", tmp_path / "data"))
    captured = {}

    class FakeUvicorn:
        @staticmethod
        def run(app, **options):
            captured.update(app=app, **options)

    monkeypatch.setitem(sys.modules, "uvicorn", FakeUvicorn)
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
