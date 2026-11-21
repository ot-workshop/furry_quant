"""Durable task queue shared by the API and the single Linux worker."""

from __future__ import annotations

import json
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import func, select, text, update

from finance_quant.checksums import sha256_text
from finance_quant.task_contracts import TaskConflict

from .database import check_schema, make_engine
from .schema import research_task as task

WORKER_LOCK = 663420119
SUBMISSION_LOCK = 663420120


def now():
    return datetime.now(timezone.utc)


def conflict(message):
    return TaskConflict(message)


@contextmanager
def worker_lock(engine=None):
    """Session lock: hold this connection until all children are stopped.

    Recovery is permitted only inside this lock. A second worker fails rather than
    stealing a live task based solely on an expired heartbeat.
    """
    with (engine or make_engine()).connect() as connection:
        acquired = connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": WORKER_LOCK}).scalar_one()
        connection.commit()
        if not acquired:
            raise RuntimeError("Another worker already owns this database queue")
        try:
            yield connection
        finally:
            # A lost connection already released its server-side lock.
            if not connection.invalidated:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": WORKER_LOCK})
                connection.commit()


class PgTaskStore:
    def __init__(self, root=None, *, engine=None):
        self.root = root
        self.engine = engine or make_engine()

    def initialize(self):
        check_schema(self.engine)

    @staticmethod
    def _public(row):
        result = {
            key: row[key]
            for key in (
                "id",
                "kind",
                "name",
                "status",
                "stage",
                "message",
                "error",
                "request",
                "result",
                "error_details",
            )
        }
        result.update(
            {key: row[key].isoformat() if row[key] else None for key in ("created_at", "started_at", "finished_at")}
        )
        result["progress"] = {"completed": row["completed"], "total": row["total"]}
        return result

    def get(self, identifier, *, private=False):
        if not isinstance(identifier, str) or not re.fullmatch(r"task-[a-f0-9]{32}", identifier):
            raise FileNotFoundError("任务不存在")
        with self.engine.connect() as connection:
            row = connection.execute(select(task).where(task.c.id == identifier)).mappings().first()
        if row is None:
            raise FileNotFoundError("任务不存在")
        result = self._public(row)
        if private:
            result["execution"] = row["execution"]
        return result

    def list(self):
        with self.engine.connect() as connection:
            rows = (
                connection.execute(select(task).order_by(task.c.created_at.desc(), task.c.id.desc()).limit(100))
                .mappings()
                .all()
            )
            count = connection.execute(select(func.count()).select_from(task)).scalar_one()
        return {"items": [self._public(row) for row in rows], "total": count}

    def create(self, request, execution, key, *, connection=None):
        return self._create("research", request, execution, key, 4, connection=connection)

    def create_experiment(self, request, execution, key, *, total):
        return self._create("experiment", request, execution, key, total)

    def create_download(self, kind, request, execution, key, *, total=1, connection=None):
        if kind not in {"market_download", "financial_download", "industry_download", "data_update", "maintenance"}:
            raise ValueError("Unsupported download task kind")
        return self._create(kind, request, execution, key, total, connection=connection)

    def _create(self, kind, request, execution, key, total, *, connection=None):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", key):
            raise ValueError("请求标识无效，请刷新后重新提交")
        encoded = json.dumps({"kind": kind, "request": request}, ensure_ascii=False, sort_keys=True, allow_nan=False)
        json.dumps(execution, allow_nan=False)
        fingerprint = sha256_text(encoded)
        if connection is not None:
            return self._create_in(connection, kind, request, fingerprint, execution, key, total)
        with self.engine.begin() as own:
            return self._create_in(own, kind, request, fingerprint, execution, key, total)

    def _create_in(self, connection, kind, request, fingerprint, execution, key, total):
        """Enqueue inside the caller's transaction.

        The caller owns the transaction boundary: this never commits or rolls back, and on
        any exception the whole transaction (including the attempt row an outer caller may
        have written) disappears. `pg_advisory_xact_lock` is held until the caller commits,
        so the caller must not do slow I/O before committing. The queue-limit count sees
        uncommitted rows of the same transaction on purpose; do not add caching here.
        """
        if True:
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": SUBMISSION_LOCK})
            existing = connection.execute(select(task).where(task.c.idempotency_key == key)).mappings().first()
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise conflict("同一次提交的参数已改变，请重新提交")
                return self._public(existing)
            count = connection.execute(
                select(func.count()).select_from(task).where(task.c.status.in_(["queued", "running"]))
            ).scalar_one()
            if count >= 20:
                raise conflict("排队任务已达20个，请等待或取消排队任务")
            identifier = "task-" + uuid.uuid4().hex
            connection.execute(
                task.insert().values(
                    id=identifier,
                    idempotency_key=key,
                    fingerprint=fingerprint,
                    kind=kind,
                    request=request,
                    execution=execution,
                    name=request.get("name", "数据更新"),
                    status="queued",
                    stage="queued",
                    message="等待运行",
                    completed=0,
                    total=total,
                )
            )
        # Same connection: the caller's transaction has not committed yet, so a fresh
        # connection would not see this row.
        row = connection.execute(select(task).where(task.c.id == identifier)).mappings().first()
        return self._public(row)

    @staticmethod
    def _require_single_row(result):
        """A terminal update that touched no row is a conflict, never a silent success."""
        if result.rowcount != 1:
            raise conflict("任务状态已变化，无法写入终态")

    def claim(self, worker_id=None):
        with self.engine.begin() as connection:
            identifier = connection.execute(
                select(task.c.id)
                .where(task.c.status == "queued")
                .order_by(task.c.created_at, task.c.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            ).scalar_one_or_none()
            if identifier:
                connection.execute(
                    update(task)
                    .where(task.c.id == identifier)
                    .values(
                        status="running",
                        stage="prepare",
                        message="准备运行",
                        started_at=now(),
                        heartbeat_at=now(),
                        worker_id=worker_id,
                    )
                )
        return identifier

    def heartbeat(self, identifier):
        with self.engine.begin() as connection:
            connection.execute(
                update(task).where(task.c.id == identifier, task.c.status == "running").values(heartbeat_at=now())
            )

    def cancel(self, identifier):
        self.get(identifier)
        with self.engine.begin() as connection:
            changed = connection.execute(
                update(task)
                .where(task.c.id == identifier, task.c.status == "queued")
                .values(status="cancelled", stage="finished", message="已取消排队", finished_at=now())
            ).rowcount
            if changed != 1:
                raise conflict("只能取消仍在排队的任务")
        return self.get(identifier)

    def stage(self, identifier, stage, message, completed):
        with self.engine.begin() as connection:
            changed = connection.execute(
                update(task)
                .where(task.c.id == identifier, task.c.status == "running")
                .values(stage=stage, message=message, completed=completed, heartbeat_at=now())
            ).rowcount
            if changed != 1:
                raise conflict("任务已中断")

    def finish(self, identifier, status, *, result=None, error=None, error_details=None, connection=None):
        if status not in {"succeeded", "failed", "interrupted", "paused"}:
            raise ValueError("Unsupported terminal task status")
        values = dict(
            status=status,
            stage="finished",
            finished_at=now(),
            result=result,
            error=error,
            error_details=error_details,
            message={"succeeded": "计算完成", "failed": "运行失败", "interrupted": "运行中断", "paused": "下载已暂停"}[
                status
            ],
        )
        if status == "succeeded":
            values["completed"] = task.c.total
        statement = update(task).where(task.c.id == identifier, task.c.status == "running").values(**values)
        if connection is not None:
            self._require_single_row(connection.execute(statement))
            return
        with self.engine.begin() as own:
            self._require_single_row(own.execute(statement))

    def recover(self):
        """Caller must hold worker_lock; no recovery based solely on heartbeat age."""
        with self.engine.begin() as connection:
            connection.execute(
                update(task)
                .where(task.c.status == "running")
                .values(
                    status="interrupted",
                    stage="finished",
                    message="上次计算进程已中断",
                    finished_at=now(),
                    error="工作进程已重启，可使用原配置重新运行",
                )
            )

    def annotate_failure(self, identifier, details):
        self.get(identifier)
        with self.engine.begin() as connection:
            changed = connection.execute(
                update(task)
                .where(task.c.id == identifier, task.c.status == "failed")
                .values(error=details["summary"], error_details=details)
            ).rowcount
            if changed != 1:
                raise conflict("只能补充失败任务的诊断")
        return self.get(identifier)
