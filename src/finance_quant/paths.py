"""Single source of truth for storage locations.

Every location derives from ``STORAGE_ROOT`` unless an explicit override is configured,
so native development and containers resolve identical layouts.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _configured(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).resolve() if value else default.resolve()


def storage_root() -> Path:
    return _configured("STORAGE_ROOT", Path("storage"))


def runs_root() -> Path:
    return _configured("FINANCE_QUANT_RUNS", storage_root() / "runs")


def data_root() -> Path:
    return _configured("FINANCE_QUANT_DATA", storage_root() / "data")


def quota_database() -> Path:
    return _configured("FINANCE_QUANT_QUOTA_DB", storage_root() / "network" / "quota.sqlite3")


def dependency_lock() -> Path:
    """``uv.lock`` is copied next to ``src`` in the container image as well."""
    return PROJECT_ROOT / "uv.lock"


def frontend_dist() -> Path:
    return _configured("FRONTEND_DIST", PROJECT_ROOT / "frontend" / "dist")
