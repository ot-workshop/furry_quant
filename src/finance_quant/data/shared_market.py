"""Immutable verified quote segments, independent of universe membership.

The authoritative store is PostgreSQL (:class:`finance_quant.data.postgres_market.PostgresMarketStore`).
Entry points configure it once per process with :func:`configure_store`.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

import pandas as pd
import pyarrow.parquet as pq

from finance_quant.checksums import file_sha256
from finance_quant.data.suspensions import invalid_halt_rows

FIELDS = {
    "instrument",
    "date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "amount",
    "change",
    "adj_factor",
    "pe_ttm",
    "pb",
    "total_mv",
    "up_limit",
    "down_limit",
    "is_suspended",
}
POOL_FIELDS = {
    "index_member",
    "selection_member",
    "eligible_history",
    "has_required_pre_history",
    "history_sessions_before",
}
VERSION = "shared-market-v1"


def validation_counts(frame):
    counts = {level: 0 for level in ["single_source_passed", "dual_source_verified", "secondary_fallback_passed"]}
    for _, part in frame.groupby("instrument", sort=False):
        levels = set(part.validation_level)
        level = (
            "single_source_passed"
            if "single_source_passed" in levels
            else "secondary_fallback_passed"
            if "secondary_fallback_passed" in levels
            else "dual_source_verified"
        )
        counts[level] += 1
    return counts


def digest(path):
    return file_sha256(path)


class MarketStore(Protocol):
    """Authoritative market facts; implemented by ``PostgresMarketStore``."""

    def inventory(self, root: Path) -> dict: ...

    def covered_dates(self, document: dict, code: str, sessions: pd.DatetimeIndex, provider_id: str): ...

    def publish_segment(self, root: Path, record: dict, path: Path, *, batch_id: str): ...

    def read_asset(self, root: Path, code: str, expected: pd.DatetimeIndex, provider_id: str): ...

    def publish_references(self, components: dict, *, batch_id: str, checks: list[dict]): ...

    def consistent_read(self) -> AbstractContextManager: ...


_store: MarketStore | None = None


def configure_store(store: MarketStore | None) -> None:
    """Select the market authority once, at a process entry point (visible to all threads)."""
    global _store
    _store = store


def market_store() -> MarketStore:
    if _store is None:
        raise RuntimeError("行情存储未配置；请通过 finance_quant.server 启动服务或工作进程")
    return _store


def catalog(root):
    return market_store().inventory(Path(root))


def register_segment(root, record, path, *, batch_id=None):
    path = Path(path).resolve()
    root = Path(root).resolve()
    if not path.is_relative_to(root) or digest(path) != record["sha256"]:
        raise ValueError("新行情分区校验失败")
    if not FIELDS.issubset(pq.read_schema(path).names):
        raise ValueError("新行情分区缺少必要字段")
    return market_store().publish_segment(root, record, path, batch_id=batch_id or record["sha256"])


def covered_dates(document, code, sessions, provider_id):
    return market_store().covered_dates(document, code, sessions, provider_id)


def read_asset(root, code, expected, provider_id):
    """First published values win; incompatible overlap is an error, never overwritten."""
    return market_store().read_asset(Path(root), code, expected, provider_id)


def gap_ranges(expected, covered):
    missing = ~expected.isin(covered)
    ranges = []
    left = None
    for i, flag in enumerate(missing):
        if flag and left is None:
            left = i
        if left is not None and (not flag or i == len(missing) - 1):
            right = i if flag else i - 1
            ranges.append((expected[left], expected[right]))
            left = None
    return ranges


def validate_snapshot(market, manifest):
    """Shared projections preserve per-row source levels, never invent dual verification."""

    if manifest.get("source") != "official_tushare_shared" or manifest.get("coverage", {}).get("status") != "complete":
        raise ValueError("共享研究快照未通过覆盖检查")
    if not {"price_source", "adjustment_source", "valuation_source", "limit_source", "validation_level"}.issubset(
        market
    ):
        raise ValueError("共享研究快照缺少逐行来源")
    if not market.validation_level.isin(
        ["single_source_passed", "dual_source_verified", "secondary_fallback_passed"]
    ).all():
        raise ValueError("共享研究快照存在未核验行")
    if invalid_halt_rows(market).any():
        raise ValueError("共享研究快照停牌估值无效")
    for field in ["price_source", "adjustment_source"]:
        if not market[field].isin(["tushare", "baostock"]).all():
            raise ValueError("共享研究快照来源无效")
        fallback = market.validation_level.eq("secondary_fallback_passed")
        if not market.loc[fallback, field].eq("baostock").all() or not market.loc[~fallback, field].eq("tushare").all():
            raise ValueError("共享研究快照核验等级与来源不一致")
    if not market.valuation_source.eq("tushare").all() or not market.limit_source.eq("tushare").all():
        raise ValueError("估值和限价来源无效")
