"""Per-capture request ledger for resumable downloads; observations stay in Parquet files."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Catalog:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.root / "catalog.sqlite3", timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS partitions (
                key TEXT PRIMARY KEY, api TEXT NOT NULL, params TEXT NOT NULL,
                provider_id TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL,
                rows INTEGER, fetched_at TEXT, sha256 TEXT, raw_path TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, started_at TEXT NOT NULL,
                finished_at TEXT, config TEXT NOT NULL, total INTEGER NOT NULL DEFAULT 0,
                completed INTEGER NOT NULL DEFAULT 0, cached INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0, snapshot_id TEXT, report TEXT
            );
            CREATE TABLE IF NOT EXISTS job_partitions (
                job_id TEXT NOT NULL, key TEXT NOT NULL, status TEXT NOT NULL,
                cached INTEGER NOT NULL, error TEXT, PRIMARY KEY (job_id, key)
            );
        """)
        self.connection.commit()

    def close(self):
        self.connection.close()

    def partition(self, key: str) -> dict | None:
        row = self.connection.execute("SELECT * FROM partitions WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def begin_job(self, identifier: str, config: dict, total: int = 0):
        # The caller holds the process lock; old running rows belonged to an interrupted process.
        with self.connection:
            self.connection.execute(
                "UPDATE jobs SET status='interrupted', finished_at=? WHERE status='running'", (utc_now(),)
            )
            self.connection.execute(
                "INSERT INTO jobs(id,status,started_at,config,total) VALUES(?,?,?,?,?)",
                (identifier, "running", utc_now(), json.dumps(config, sort_keys=True), total),
            )

    def set_total(self, job_id: str, total: int):
        with self.connection:
            self.connection.execute("UPDATE jobs SET total=? WHERE id=?", (total, job_id))

    def add_total(self, job_id: str, count: int):
        with self.connection:
            self.connection.execute("UPDATE jobs SET total=total+? WHERE id=?", (count, job_id))

    def job_status(self, job_id: str) -> str:
        return self.connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0]

    def save_partition(
        self,
        key: str,
        api: str,
        params: dict,
        provider_id: str,
        *,
        status: str,
        attempts: int,
        metadata: dict | None = None,
        error: str | None = None,
    ):
        metadata = metadata or {}
        with self.connection:
            self.connection.execute(
                """INSERT OR REPLACE INTO partitions VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    key,
                    api,
                    json.dumps(params, sort_keys=True),
                    provider_id,
                    status,
                    attempts,
                    metadata.get("rows"),
                    metadata.get("fetched_at"),
                    metadata.get("sha256"),
                    metadata.get("raw_path"),
                    error,
                ),
            )

    def record(self, job_id: str, key: str, *, status: str, cached: bool = False, error: str | None = None):
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO job_partitions VALUES(?,?,?,?,?)", (job_id, key, status, int(cached), error)
            )
            self.connection.execute(
                """UPDATE jobs SET
                completed=(SELECT COUNT(*) FROM job_partitions WHERE job_id=? AND status='succeeded'),
                cached=(SELECT COUNT(*) FROM job_partitions WHERE job_id=? AND cached=1),
                failed=(SELECT COUNT(*) FROM job_partitions WHERE job_id=? AND status='failed') WHERE id=?""",
                (job_id, job_id, job_id, job_id),
            )

    def finish(self, job_id: str, status: str, report: dict, snapshot_id: str | None = None):
        with self.connection:
            self.connection.execute(
                "UPDATE jobs SET status=?,finished_at=?,report=?,snapshot_id=? WHERE id=?",
                (status, utc_now(), json.dumps(report, ensure_ascii=False, allow_nan=False), snapshot_id, job_id),
            )
