"""Status and cooperative pause for the CSI300 historical warehouse download.

Downloads run as ``index_download`` tasks in the PostgreSQL worker.
"""

from __future__ import annotations

from pathlib import Path

from filelock import FileLock, Timeout

from finance_quant.application.index_download import index_download_status
from finance_quant.paths import warehouse_root


def _active(root: Path) -> bool:
    """The worker holds ``warehouse.lock`` for the whole download."""
    if not root.exists():
        return False
    try:
        with FileLock(root / "warehouse.lock", timeout=0):
            return False
    except Timeout:
        return True


def status(root=None):
    root = Path(root) if root else warehouse_root()
    result = index_download_status(root)
    result["worker_running"] = _active(root)
    result["pause_requested"] = (root / "PAUSE").exists()
    result["verified"] = result.get("completed", 0)
    result["failed"] = len(result.get("failures", {}))
    result["limits"] = {
        "baostock_daily_requests": 10000,
        "baostock_per_minute": 60,
        "tushare_per_minute": 120,
        "tushare_daily_per_api": 90000,
    }
    result["failures"] = [{"code": code, **value} for code, value in list(result.get("failures", {}).items())[:30]]
    result.pop("assets", None)
    return result


def pause(root=None):
    root = Path(root) if root else warehouse_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "PAUSE").write_text("Pause before next stock; keep completed requests.\n", encoding="utf-8")
    return status(root)


__all__ = ["pause", "status", "warehouse_root"]
