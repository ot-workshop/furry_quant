"""Persistent local task queue with one isolated numerical worker at a time."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock, Timeout

from finance_quant.application.research_requests import validate_request


def now():
    return datetime.now(timezone.utc).isoformat()


class TaskConflict(ValueError):
    pass


class TaskStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = self.root / "tasks/catalog.sqlite3"

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=10)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS research_tasks (
                id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
                request TEXT NOT NULL, execution TEXT NOT NULL, name TEXT NOT NULL,
                status TEXT NOT NULL, stage TEXT NOT NULL, message TEXT NOT NULL,
                completed INTEGER NOT NULL, total INTEGER NOT NULL,
                created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, result TEXT, error TEXT
            )""")
            if 'error_details' not in {row[1] for row in connection.execute('PRAGMA table_info(research_tasks)')}:
                connection.execute('ALTER TABLE research_tasks ADD COLUMN error_details TEXT')
            connection.commit()

    def _connect(self, *, write=False):
        if not self.path.exists():
            raise FileNotFoundError("任务不存在")
        connection = sqlite3.connect(self.path if write else self.path.as_uri() + "?mode=ro", uri=not write, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _public(row):
        return {**{key: row[key] for key in ("id", "name", "status", "stage", "message", "created_at", "started_at", "finished_at", "error")},
                "progress": {"completed": row["completed"], "total": row["total"]},
                "request": json.loads(row["request"]), "result": json.loads(row["result"]) if row["result"] else None,
                "error_details": json.loads(row['error_details']) if 'error_details' in row.keys() and row['error_details'] else None}

    def get(self, identifier, *, private=False):
        if not isinstance(identifier, str) or not re.fullmatch(r"task-[a-f0-9]{32}", identifier):
            raise FileNotFoundError("任务不存在")
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM research_tasks WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise FileNotFoundError("任务不存在")
        if private:
            return {**self._public(row), "execution": json.loads(row["execution"])}
        return self._public(row)

    def list(self):
        if not self.path.exists():
            return {"items": [], "total": 0}
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM research_tasks ORDER BY created_at DESC,id DESC LIMIT 100").fetchall()
            total = connection.execute("SELECT COUNT(*) FROM research_tasks").fetchone()[0]
        return {"items": [self._public(row) for row in rows], "total": total}

    def create(self, request: dict, execution: dict, key: str):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", key):
            raise ValueError("请求标识无效，请刷新后重新提交")
        encoded = json.dumps(request, ensure_ascii=False, sort_keys=True, allow_nan=False)
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        with closing(self._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM research_tasks WHERE idempotency_key=?", (key,)).fetchone()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise TaskConflict("同一次提交的参数已改变，请重新提交")
                return self._public(existing)
            count = connection.execute("SELECT COUNT(*) FROM research_tasks WHERE status IN ('queued','running')").fetchone()[0]
            if count >= 20:
                raise TaskConflict("排队任务已达20个，请等待或取消排队任务")
            identifier = "task-" + uuid.uuid4().hex
            connection.execute("""INSERT INTO research_tasks(id,idempotency_key,fingerprint,request,execution,name,
                status,stage,message,completed,total,created_at) VALUES(?,?,?,?,?,?,'queued','queued','等待运行',0,?,?)""",
                (identifier, key, fingerprint, encoded, json.dumps(execution, sort_keys=True, allow_nan=False), request["name"],
                 5 if request["data"]["mode"] == "download" else 4, now()))
            connection.commit()
        return self.get(identifier)

    def claim(self):
        with closing(self._connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT id FROM research_tasks WHERE status='queued' ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None:
                return None
            identifier = row["id"]
            connection.execute("UPDATE research_tasks SET status='running',stage='prepare',message='准备研究数据',started_at=? WHERE id=?", (now(), identifier))
            connection.commit()
        return identifier

    def cancel(self, identifier):
        self.get(identifier)
        with closing(self._connect(write=True)) as connection:
            with connection:
                cursor = connection.execute("UPDATE research_tasks SET status='cancelled',stage='finished',message='已取消排队',finished_at=? WHERE id=? AND status='queued'", (now(), identifier))
                if cursor.rowcount != 1:
                    raise TaskConflict("只能取消仍在排队的任务")
        return self.get(identifier)

    def stage(self, identifier, stage, message, completed):
        with closing(self._connect(write=True)) as connection:
            with connection:
                cursor = connection.execute("UPDATE research_tasks SET stage=?,message=?,completed=? WHERE id=? AND status='running'", (stage, message, completed, identifier))
                if cursor.rowcount != 1:
                    raise TaskConflict("任务已中断")

    def finish(self, identifier, status, *, result=None, error=None, error_details=None):
        with closing(self._connect(write=True)) as connection:
            with connection:
                connection.execute("""UPDATE research_tasks SET status=?,stage='finished',message=?,finished_at=?,result=?,error=?,
                    completed=CASE WHEN ?='succeeded' THEN total ELSE completed END,error_details=? WHERE id=? AND status='running'""",
                    (status, "计算完成" if status == "succeeded" else "运行中断" if status == "interrupted" else "运行失败", now(),
                     json.dumps(result, ensure_ascii=False, allow_nan=False) if result else None, error, status,
                     json.dumps(error_details, ensure_ascii=False, allow_nan=False) if error_details else None, identifier))

    def recover(self):
        with closing(self._connect(write=True)) as connection:
            with connection:
                connection.execute("""UPDATE research_tasks SET status='interrupted',stage='finished',message='上次服务运行已中断',
                    finished_at=?,error='服务停止或重启导致任务中断，可使用原配置重新运行' WHERE status='running'""", (now(),))

    def annotate_failure(self, identifier, details):
        self.get(identifier)
        with closing(self._connect(write=True)) as connection:
            with connection:
                cursor = connection.execute("UPDATE research_tasks SET error=?,error_details=? WHERE id=? AND status='failed'",
                                            (details['summary'], json.dumps(details, ensure_ascii=False, allow_nan=False), identifier))
                if cursor.rowcount != 1:
                    raise TaskConflict('只能补充失败任务的诊断')
        return self.get(identifier)


class TaskService:
    def __init__(self, runs_root: Path, data_root: Path):
        self.runs_root, self.data_root = runs_root.resolve(), data_root.resolve()
        self.store = TaskStore(self.runs_root)
        self._lock = FileLock(self.runs_root / "tasks/dispatcher.lock", timeout=0, thread_local=False)
        self._mutex = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self.last_error = None

    def start(self):
        with self._mutex:
            if self._thread is not None and self._thread.is_alive():
                return True
            self.runs_root.joinpath("tasks").mkdir(parents=True, exist_ok=True)
            try:
                self._lock.acquire()
            except Timeout:
                self.last_error = "已有网页服务管理此任务队列，请在原服务页面运行研究"
                return False
            try:
                self.store.initialize()
                self.store.recover()
                self._stop.clear()
                self._thread = threading.Thread(target=self._dispatch, name="research-task-dispatcher", daemon=True)
                self._thread.start()
                self.last_error = None
                return True
            except Exception:
                self._lock.release()
                raise

    def submit(self, document, key):
        request, execution = validate_request(document, self.data_root, self.runs_root)
        if not self.start():
            raise RuntimeError(self.last_error)
        result = self.store.create(request, execution, key)
        self._wake.set()
        return result

    def _dispatch(self):
        while not self._stop.is_set():
            identifier = None
            try:
                # A worker surviving a hard server crash must release its numerical
                # lock before a newly started service launches another worker.
                with FileLock(self.runs_root / "tasks/execution.lock", timeout=0):
                    identifier = self.store.claim()
                if identifier:
                    self._run_process(identifier)
                    continue
            except Timeout:
                pass
            except Exception:
                if identifier:
                    self.store.finish(identifier, "failed", error="后台任务调度失败，请重新运行")
            self._wake.wait(.5)
            self._wake.clear()

    def _run_process(self, identifier):
        environment = os.environ.copy()
        environment.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1",
                           PYTHONIOENCODING="utf-8", MLFLOW_DISABLE_AGENT_HINT="1")
        command = [sys.executable, "-m", "finance_quant.application.task_worker", "--runs-root", str(self.runs_root),
                   "--data-root", str(self.data_root), "--task-id", identifier]
        process = subprocess.Popen(command, cwd=Path(__file__).parents[3], env=environment,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            while process.poll() is None:
                if self._stop.wait(.2):
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
                    break
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            if self.store.get(identifier)["status"] == "running":
                self.store.finish(identifier, "interrupted" if self._stop.is_set() else "failed",
                                  error="服务已停止，可使用原配置重新运行" if self._stop.is_set() else f"计算进程异常退出（代码 {process.returncode}），请重新运行")

    def close(self):
        with self._mutex:
            self._stop.set()
            self._wake.set()
            if self._thread is not None:
                self._thread.join(timeout=20)
                if self._thread.is_alive():
                    raise RuntimeError("Research dispatcher is still stopping")
                self._thread = None
            if self._lock.is_locked:
                self._lock.release()
