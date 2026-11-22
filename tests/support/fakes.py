"""In-memory doubles for the PostgreSQL task queue and HTTP composition."""

from __future__ import annotations

import copy
import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from finance_quant.api import create_app
from finance_quant.application import task_worker
from finance_quant.application.tasks import TaskService
from finance_quant.storage import ResultStore
from finance_quant.task_contracts import TaskConflict, submission_fingerprint

TERMINAL_MESSAGES = {"succeeded": "计算完成", "failed": "运行失败", "interrupted": "运行中断", "paused": "下载已暂停"}


def _now():
    return datetime.now(timezone.utc).isoformat()


class MemoryTaskStore:
    """Mirrors the observable contract of ``PgTaskStore`` without a database."""

    def __init__(self):
        self._rows: dict[str, dict] = {}
        self._keys: dict[str, str] = {}
        self._lock = threading.Lock()

    def initialize(self):
        pass

    @staticmethod
    def _public(row):
        keys = ("id", "kind", "name", "status", "stage", "message", "error", "request", "result", "error_details")
        result = {key: copy.deepcopy(row[key]) for key in keys}
        result.update({key: row[key] for key in ("created_at", "started_at", "finished_at")})
        result["progress"] = {"completed": row["completed"], "total": row["total"]}
        return result

    def get(self, identifier, *, private=False):
        if not isinstance(identifier, str) or not re.fullmatch(r"task-[a-f0-9]{32}", identifier):
            raise FileNotFoundError("任务不存在")
        with self._lock:
            row = self._rows.get(identifier)
            if row is None:
                raise FileNotFoundError("任务不存在")
            result = self._public(row)
            if private:
                result["execution"] = copy.deepcopy(row["execution"])
            return result

    def list(self):
        with self._lock:
            rows = sorted(self._rows.values(), key=lambda r: (r["created_at"], r["id"]), reverse=True)
            return {"items": [self._public(row) for row in rows[:100]], "total": len(rows)}

    def create(self, request, execution, key, *, connection=None):
        return self._create("research", request, execution, key, 4)

    def create_download(self, kind, request, execution, key, *, total=1, connection=None):
        if kind not in {"market_download", "financial_download", "industry_download", "data_update", "maintenance"}:
            raise ValueError("Unsupported download task kind")
        return self._create(kind, request, execution, key, total)

    def find_experiment_replay(self, request, key):
        fingerprint = submission_fingerprint("experiment", request, key)
        with self._lock:
            return self._find_replay(key, fingerprint)

    def create_experiment(self, request, execution, key, *, total):
        return self._create("experiment", request, execution, key, total)

    def _find_replay(self, key, fingerprint):
        identifier = self._keys.get(key)
        if identifier is None:
            return None
        row = self._rows[identifier]
        if row["fingerprint"] != fingerprint:
            raise TaskConflict("同一次提交的参数已改变，请重新提交")
        return self._public(row)

    def _create(self, kind, request, execution, key, total):
        fingerprint = submission_fingerprint(kind, request, key)
        json.dumps(execution, allow_nan=False)
        with self._lock:
            existing = self._find_replay(key, fingerprint)
            if existing is not None:
                return existing
            if sum(row["status"] in {"queued", "running"} for row in self._rows.values()) >= 20:
                raise TaskConflict("排队任务已达20个，请等待或取消排队任务")
            identifier = "task-" + uuid.uuid4().hex
            self._keys[key] = identifier
            self._rows[identifier] = {
                "id": identifier,
                "kind": kind,
                "fingerprint": fingerprint,
                "name": request.get("name", "数据更新"),
                "request": copy.deepcopy(request),
                "execution": copy.deepcopy(execution),
                "status": "queued",
                "stage": "queued",
                "message": "等待运行",
                "completed": 0,
                "total": total,
                "created_at": _now(),
                "started_at": None,
                "finished_at": None,
                "result": None,
                "error": None,
                "error_details": None,
            }
        return self.get(identifier)

    def claim(self, worker_id=None):
        with self._lock:
            queued = sorted((r for r in self._rows.values() if r["status"] == "queued"), key=lambda r: r["created_at"])
            if not queued:
                return None
            row = queued[0]
            row.update(status="running", stage="prepare", message="准备运行", started_at=_now())
            return row["id"]

    def heartbeat(self, identifier):
        pass

    def cancel(self, identifier):
        self.get(identifier)
        with self._lock:
            row = self._rows[identifier]
            if row["status"] != "queued":
                raise TaskConflict("只能取消仍在排队的任务")
            row.update(status="cancelled", stage="finished", message="已取消排队", finished_at=_now())
        return self.get(identifier)

    def stage(self, identifier, stage, message, completed):
        with self._lock:
            row = self._rows[identifier]
            if row["status"] != "running":
                raise TaskConflict("任务已中断")
            row.update(stage=stage, message=message, completed=completed)

    def save_prepared(self, identifier, execution):
        """Mirror PgTaskStore.save_prepared: only a still-pending running row is updated once."""

        with self._lock:
            row = self._rows.get(identifier)
            if row is None or row["status"] != "running" or row["kind"] != "experiment":
                raise TaskConflict("任务状态已变化，无法写入终态")
            if (row["execution"] or {}).get("prepare_state") != "pending":
                raise TaskConflict("任务状态已变化，无法写入终态")
            row["execution"] = copy.deepcopy(execution)

    def finish(self, identifier, status, *, result=None, error=None, error_details=None):
        if status not in TERMINAL_MESSAGES:
            raise ValueError("Unsupported terminal task status")
        with self._lock:
            row = self._rows[identifier]
            if row["status"] != "running":
                return
            row.update(
                status=status,
                stage="finished",
                finished_at=_now(),
                result=copy.deepcopy(result),
                error=error,
                error_details=copy.deepcopy(error_details),
                message=TERMINAL_MESSAGES[status],
            )
            if status == "succeeded":
                row["completed"] = row["total"]

    def recover(self):
        with self._lock:
            for row in self._rows.values():
                if row["status"] == "running":
                    row.update(
                        status="interrupted",
                        stage="finished",
                        message="上次计算进程已中断",
                        finished_at=_now(),
                        error="工作进程已重启，可使用原配置重新运行",
                    )

    def annotate_failure(self, identifier, details):
        self.get(identifier)
        with self._lock:
            row = self._rows[identifier]
            if row["status"] != "failed":
                raise TaskConflict("只能补充失败任务的诊断")
            row.update(error=details["summary"], error_details=copy.deepcopy(details))
        return self.get(identifier)


def make_service(runs_root: Path, data_root: Path, store: MemoryTaskStore | None = None) -> TaskService:
    return TaskService(runs_root, data_root, task_store=store or MemoryTaskStore())


def make_app(runs_root: Path, data_root: Path | None = None, *, service: TaskService | None = None, **kwargs):
    data_root = data_root or Path(runs_root).parent / "data"
    service = service or make_service(runs_root, data_root)
    return create_app(result_store=ResultStore(runs_root), task_service=service, **kwargs)


def run_claimed(service: TaskService, identifier: str, **kwargs):
    """Execute one claimed task in-process, as the worker child process would."""
    runs_root = service.runs_root
    task_worker.run_task(
        runs_root,
        service.data_root,
        identifier,
        task_store=service.store,
        result_store_factory=lambda dataset_id=None: ResultStore(runs_root),
        **kwargs,
    )
    return service.store.get(identifier)


def fixture_root(data_root: Path) -> Path:
    """Where ``support.fixed_pool.publish_fixture_snapshot`` publishes by default."""
    return Path(data_root) / "research/downloads/fixture"


def make_client(data_root: Path, runs_root: Path):
    service = make_service(runs_root, data_root)
    client = TestClient(make_app(runs_root, data_root=data_root, service=service), base_url="http://127.0.0.1")
    return client, service
