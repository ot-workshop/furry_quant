"""Durable process-shared request accounting; never stores credentials or bodies."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


class DownloadPaused(RuntimeError):
    """A resumable stop, never an instruction to bypass the source."""


def budget_path():
    from finance_quant.paths import quota_database

    return quota_database()


class NetworkBudget:
    def __init__(self, path=None, *, clock=time.time, sleep=time.sleep):
        self.path = Path(path) if path else budget_path()
        self.clock, self.sleep = clock, sleep
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as c:
            c.executescript("""CREATE TABLE IF NOT EXISTS requests(source TEXT,api TEXT,day TEXT,at REAL);
                CREATE INDEX IF NOT EXISTS requests_lookup ON requests(source,api,day,at);
                CREATE INDEX IF NOT EXISTS requests_time ON requests(source,at);
                CREATE TABLE IF NOT EXISTS holds(source TEXT PRIMARY KEY,reason TEXT NOT NULL);""")
            c.commit()

    def connect(self):
        return sqlite3.connect(self.path, timeout=15)

    def reserve(self, source, api, *, daily, rpm, interval):
        if daily < 1 or rpm < 1 or interval < 0:
            raise ValueError("Invalid network budget")
        while True:
            now = self.clock()
            day = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).date().isoformat()
            with closing(self.connect()) as c:
                c.execute("BEGIN IMMEDIATE")
                if c.execute("SELECT 1 FROM holds WHERE source=?", (source,)).fetchone():
                    c.rollback()
                    raise DownloadPaused(f"{source}已暂停：数据源限制访问，需检查后人工解除")
                clause = "source=? AND day=?" + ("" if source == "baostock" else " AND api=?")
                args = (source, day) if source == "baostock" else (source, day, api)
                used = c.execute("SELECT COUNT(*) FROM requests WHERE " + clause, args).fetchone()[0]
                if used >= daily:
                    c.rollback()
                    raise DownloadPaused(f"{source}达到本日下载预算，已保留断点，次日可续传")
                recent = [
                    r[0]
                    for r in c.execute(
                        "SELECT at FROM requests WHERE source=? AND at>? ORDER BY at", (source, now - 60)
                    )
                ]
                last = c.execute("SELECT MAX(at) FROM requests WHERE source=?", (source,)).fetchone()[0]
                delay = max(
                    0,
                    (last + interval - now) if last is not None else 0,
                    (recent[-rpm] + 60.01 - now) if len(recent) >= rpm else 0,
                )
                if delay <= 0:
                    c.execute("INSERT INTO requests VALUES(?,?,?,?)", (source, api, day, now))
                    c.commit()
                    return
                c.rollback()
            self.sleep(min(delay, 1.0))

    def hold(self, source, reason="source_restricted"):
        with closing(self.connect()) as c:
            c.execute("INSERT OR REPLACE INTO holds VALUES(?,?)", (source, reason))
            c.commit()

    def assert_available(self, source, api, *, daily):
        state = self.status()
        if any(row["source"] == source for row in state["holds"]):
            raise DownloadPaused(f"{source}已暂停：数据源限制访问，需检查后人工解除")
        used = sum(
            row["count"]
            for row in state["requests"]
            if row["source"] == source and (source == "baostock" or row["api"] == api)
        )
        if used >= daily:
            raise DownloadPaused(f"{source}达到本日下载预算，已保留断点，次日可续传")

    def clear_hold(self, source):
        """Operator action after verifying service recovery; does not reset usage."""
        with closing(self.connect()) as c:
            c.execute("DELETE FROM holds WHERE source=?", (source,))
            c.commit()

    def status(self):
        day = datetime.fromtimestamp(self.clock(), ZoneInfo("Asia/Shanghai")).date().isoformat()
        with closing(self.connect()) as c:
            return {
                "day": day,
                "requests": [
                    {"source": s, "api": a, "count": n}
                    for s, a, n in c.execute(
                        "SELECT source,api,COUNT(*) FROM requests WHERE day=? GROUP BY source,api", (day,)
                    )
                ],
                "holds": [{"source": s, "reason": r} for s, r in c.execute("SELECT * FROM holds")],
            }


def reserve_tushare(api):
    NetworkBudget().reserve("tushare_official", api, daily=90000, rpm=120, interval=0.5)


def reserve_baostock():
    NetworkBudget().reserve("baostock", "sdk_message", daily=10000, rpm=60, interval=1.0)
