"""HTTP and worker entry points shared by native development and containers."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import uvicorn
from alembic import command
from alembic.config import Config
from dotenv import load_dotenv
from sqlalchemy import text

from finance_quant import paths as locations
from finance_quant.api import create_app
from finance_quant.application.data_updates import enqueue_due, pause
from finance_quant.application.tasks import TaskService
from finance_quant.data.financials import financial_root
from finance_quant.data.postgres_market import PostgresMarketStore, market_summary
from finance_quant.data.postgres_market import status as market_status
from finance_quant.data.shared_market import configure_store
from finance_quant.paths import PROJECT_ROOT
from finance_quant.persistence.database import SchemaNotReady, check_schema, make_engine
from finance_quant.persistence.results import PgResultStore
from finance_quant.persistence.tasks import PgTaskStore, worker_lock
from finance_quant.runtime import configure_runtime
from finance_quant.task_contracts import TaskConflict

CGROUP_MEMORY_EVENTS = Path("/sys/fs/cgroup/memory.events")

STAGE_LABELS = {
    "prepare": "准备数据",
    "factors": "计算因子",
    "backtest": "回测",
    "evaluation": "因子评估",
    "experiment": "方案比较",
    "download": "下载数据",
}


def read_oom_kill_count(events_path: Path | None = None) -> int | None:
    """Return the cgroup v2 memory.events oom_kill counter when readable, else None."""
    path = events_path or CGROUP_MEMORY_EVENTS
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition(" ")
            if key == "oom_kill":
                return int(value.strip())
    except (OSError, ValueError):
        return None
    return None


def exit_failure_details(
    returncode: int,
    stage: str | None,
    identifier: str,
    *,
    oom_killed: bool | None = None,
) -> tuple[str, str, dict]:
    """Translate a child process exit code and captured stage into task status and details."""
    known_stage = stage if stage in STAGE_LABELS else None
    stage_prefix = f"在{STAGE_LABELS[known_stage]}阶段" if known_stage else ""
    if returncode in (-15, 143):
        status = "interrupted"
        summary = f"{stage_prefix}被要求停止（退出码 {returncode}）"
    elif returncode in (-9, 137):
        status = "failed"
        reason = "因内存不足被系统终止" if oom_killed is True else "被系统强制终止"
        summary = f"{stage_prefix}{reason}（退出码 {returncode}）"
    else:
        status = "failed"
        summary = f"{stage_prefix}异常退出（退出码 {returncode}），请查看任务日志"
    details: dict = {
        "kind": "runtime",
        "job_id": identifier,
        "issues": [],
        "stage": known_stage,
        "exit_code": returncode,
        "summary": summary,
    }
    if returncode in (-9, 137) and oom_killed is not None:
        details["oom_killed"] = oom_killed
    return status, summary, details


def paths():

    root, runs, data = locations.storage_root(), locations.runs_root(), locations.data_root()
    for path in (root, runs / "tasks", data, locations.quota_database().parent, root / "logs"):
        path.mkdir(parents=True, exist_ok=True)
    return root, runs, data


def create_http_app():
    """Uvicorn factory so reload workers rebuild the app from current sources."""

    configure_store(PostgresMarketStore())
    engine = make_engine()
    check_schema(engine)
    root, runs, data = paths()

    return create_app(
        result_store=PgResultStore(runs, engine=engine, storage_root=root),
        task_service=TaskService(runs, data, task_store=PgTaskStore(runs, engine=engine)),
        database_probe=lambda: check_schema(engine),
        market_status=market_status,
        market_summary=market_summary,
    )


def terminate_group(process):
    if process.poll() is not None:
        return
    if os.name == "posix":
        getattr(os, "killpg")(process.pid, signal.SIGTERM)
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            getattr(os, "killpg")(process.pid, getattr(signal, "SIGKILL"))
        else:
            process.kill()
        process.wait()


def request_task_pause(store, identifier, data_root):
    """Ask a resumable download to stop at its next safe point.

    Only downloads read the ``PAUSE`` file; research and experiment children have no
    checkpoint and are terminated after the grace period instead.
    """
    task = store.get(identifier, private=True)
    if task["kind"] == "data_update":
        pause(data_root)
        return
    if task["kind"] in {"market_download", "industry_download"}:
        directory = Path(task["execution"]["root"])
    elif task["kind"] == "financial_download":
        directory = financial_root(data_root)
    else:
        return
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "PAUSE").write_text("service stopping", encoding="utf-8")


def consume(store, root, runs, data, stop, lock_connection):
    """Lock connection is checked during every child: loss of ownership stops work."""

    log = logging.getLogger(__name__)
    next_schedule = 0.0
    while not stop.is_set():
        lock_connection.execute(text("SELECT 1"))
        lock_connection.commit()
        if time.monotonic() >= next_schedule:
            enqueue_due(store, data)
            next_schedule = time.monotonic() + 60
        identifier = store.claim()
        if not identifier:
            stop.wait(0.5)
            continue
        command = [
            sys.executable,
            "-m",
            "finance_quant.application.task_worker",
            "--runs-root",
            str(runs),
            "--data-root",
            str(data),
            "--task-id",
            identifier,
            "--storage-root",
            str(root),
        ]
        process = None
        deadline = None
        oom_before = read_oom_kill_count()
        try:
            with (root / "logs" / f"{identifier}.log").open("ab") as output:
                process = subprocess.Popen(
                    command, stdout=output, stderr=subprocess.STDOUT, start_new_session=os.name == "posix"
                )
                next_heartbeat = 0.0
                while process.poll() is None:
                    if stop.is_set() and deadline is None:
                        deadline = time.monotonic() + 75
                        request_task_pause(store, identifier, data)
                    if deadline is not None and time.monotonic() >= deadline:
                        terminate_group(process)
                        break
                    if time.monotonic() >= next_heartbeat:
                        lock_connection.execute(text("SELECT 1"))
                        lock_connection.commit()
                        store.heartbeat(identifier)
                        next_heartbeat = time.monotonic() + 5
                    time.sleep(0.2)
            current = store.get(identifier)
            if current["status"] == "running":
                last_stage = current.get("stage")
                known_stage = last_stage if last_stage in STAGE_LABELS else None
                if stop.is_set():
                    summary = "服务停止，任务可使用原参数重新运行"
                    details = {
                        "kind": "runtime",
                        "job_id": identifier,
                        "issues": [],
                        "stage": known_stage,
                        "summary": summary,
                    }
                    if process.returncode is not None:
                        details["exit_code"] = process.returncode
                    _finish_or_log(store, identifier, "interrupted", error=summary, error_details=details)
                else:
                    oom_killed = None
                    if process.returncode in (-9, 137):
                        oom_after = read_oom_kill_count()
                        if oom_before is not None and oom_after is not None:
                            oom_killed = oom_after > oom_before
                    status, summary, details = exit_failure_details(
                        process.returncode, known_stage, identifier, oom_killed=oom_killed
                    )
                    _finish_or_log(store, identifier, status, error=summary, error_details=details)
        except BaseException:
            if process is not None:
                terminate_group(process)
            # Let supervisor restart and recover while holding a new exclusive lock.
            log.exception("Worker stopped while processing %s", identifier)
            raise


def _finish_or_log(store, identifier, status, **values):
    """A task that already left `running` must not tear the worker loop down."""
    try:
        store.finish(identifier, status, **values)
    except TaskConflict as conflict:
        logging.getLogger(__name__).warning("Task %s already left the running state: %s", identifier, conflict)


def migrate():
    """Apply Alembic migrations with the already-loaded environment (same as `alembic upgrade head`)."""

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    command.upgrade(config, "head")


def main():

    configure_runtime()
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["migrate", "api", "worker", "check"])
    parser.add_argument("--env-file", type=Path, help="Load development or server settings from this file")
    parser.add_argument("--host", help="API listen address; defaults to HTTP_HOST or 0.0.0.0")
    parser.add_argument("--port", type=int, help="API listen port; defaults to HTTP_PORT or 8000")
    parser.add_argument("--reload", action="store_true", help="Reload the API when Python sources change")
    args = parser.parse_args()
    if args.env_file:
        if not args.env_file.is_file():
            parser.error(f"env file does not exist: {args.env_file}")

        load_dotenv(args.env_file, override=False)
    logging.basicConfig(level=logging.INFO)
    if not os.environ.get("POSTGRES_HOST"):
        parser.error("POSTGRES_HOST is required; configure a native development database or Docker Compose")
    if args.role == "migrate":
        migrate()
        print("Database schema is up to date")
        return

    try:
        check_schema()
    except SchemaNotReady as error:
        parser.exit(2, f"{error}\n")
    root, runs, data = paths()
    if args.role == "check":
        print("Database schema and storage are ready")
    elif args.role == "api":
        host = args.host or os.environ.get("HTTP_HOST", "0.0.0.0")
        port = args.port or int(os.environ.get("HTTP_PORT", "8000"))
        uvicorn.run("finance_quant.server:create_http_app", factory=True, host=host, port=port, reload=args.reload)
    else:
        stop = threading.Event()
        for name in (signal.SIGTERM, signal.SIGINT):
            signal.signal(name, lambda *_: stop.set())
        store = PgTaskStore(runs)
        with worker_lock(store.engine) as connection:
            store.recover()
            consume(store, root, runs, data, stop, connection)


if __name__ == "__main__":
    main()
