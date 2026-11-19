"""Find past traded quotes for leading halts without changing a frozen download plan."""

from __future__ import annotations

import numpy as np
import pandas as pd

from finance_quant.data.managed import normalize_response
from finance_quant.data.market import SnapshotFetcher
from finance_quant.data.network_budget import DownloadPaused
from finance_quant.data.providers import normalize_calendar
from finance_quant.data.validation_errors import MarketDataError


def _check_pause(root):
    if (root / "PAUSE").exists():
        raise DownloadPaused("用户暂停；停牌补查响应保留，可续传")


def find_halt_anchor(root, provider, code, start, listed):
    """Look backwards, one bounded calendar year per cached request, never after start."""
    fetch = SnapshotFetcher(provider, root / "halt-anchor-raw")
    right, floor = pd.Timestamp(start) - pd.Timedelta(days=1), pd.Timestamp(listed)
    while right >= floor:
        _check_pause(root)
        left = max(floor, pd.Timestamp(year=right.year, month=1, day=1))
        params = {"ts_code": code, "start_date": left.strftime("%Y%m%d"), "end_date": right.strftime("%Y%m%d")}
        raw = fetch.get("daily", **params)
        frame = normalize_response("daily", raw, params, allow_empty=True)
        if not frame.empty:
            valid = frame[["open", "high", "low", "close", "pre_close", "volume", "amount"]].gt(0).all(axis=1)
            valid &= np.isfinite(frame[["open", "high", "low", "close", "pre_close", "volume", "amount"]]).all(axis=1)
            valid &= (frame.close / frame.pre_close - 1 - frame.change).abs().le(0.0002)
            valid &= frame.high.ge(frame[["open", "close", "low"]].max(axis=1))
            valid &= frame.low.le(frame[["open", "close", "high"]].min(axis=1))
            if valid.any():
                day = frame.loc[valid, "date"].max()
                return day, {
                    "date": day.strftime("%Y-%m-%d"),
                    "source": "tushare:daily",
                    "requests": fetch.requests,
                }
        right = left - pd.Timedelta(days=1)
    raise MarketDataError(
        f"{code}停牌起点之前未找到有效成交报价，已补查至上市日 {floor:%Y-%m-%d}。",
        category="missing_pre_halt_quote",
        dates=[pd.Timestamp(start).strftime("%Y-%m-%d")],
        action="核对官方历史行情与上市日期；不使用复牌后的价格倒填。已有数据和补查响应保留。",
    )


def extend_calendar(root, provider, calendar, anchor):
    """Extend the in-memory working calendar only; frozen reference hashes stay unchanged."""
    fetch = SnapshotFetcher(provider, root / "reference-raw")
    parts = [calendar]
    existing = pd.DatetimeIndex(calendar.date)
    for year in range(anchor.year, calendar.date.min().year + 1):
        _check_pause(root)
        start, end = f"{year}0101", f"{year}1231"
        if pd.date_range(start, end).difference(existing).empty:
            continue
        part = normalize_calendar(fetch.get("trade_cal", exchange="SSE", start_date=start, end_date=end))
        if not pd.DatetimeIndex(part.date).equals(pd.date_range(start, end)):
            raise MarketDataError("停牌补查的交易日历不完整。", category="incomplete_anchor_calendar")
        overlap = part.loc[part.date.isin(existing)].set_index("date").is_open
        before = calendar.set_index("date").is_open.reindex(overlap.index)
        if not overlap.equals(before):
            raise MarketDataError("停牌补查日历与冻结参考日历冲突。", category="conflicting_anchor_calendar")
        parts.append(part.loc[~part.date.isin(existing)])
    result = pd.concat(parts, ignore_index=True).sort_values("date").reset_index(drop=True)
    if anchor not in pd.DatetimeIndex(result.loc[result.is_open, "date"]):
        raise MarketDataError(
            "停牌前报价日期不在官方交易日历中。", category="invalid_pre_halt_quote", dates=[anchor.strftime("%Y-%m-%d")]
        )
    return result
