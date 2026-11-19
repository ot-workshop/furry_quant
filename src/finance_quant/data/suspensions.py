"""Explicit two-basis suspension evidence and causal mark-to-last-close rows."""

from __future__ import annotations

import numpy as np
import pandas as pd

from finance_quant.data.symbols import to_baostock
from finance_quant.data.validation_errors import MarketDataError


class MemoizedSecondary:
    def __init__(self, provider):
        self.provider, self.bundles = provider, {}

    def fetch(self, code, start, end):
        key = (code, start, end)
        if key not in self.bundles:
            self.bundles[key] = self.provider.fetch(code, start, end)
        return self.bundles[key]


def confirmed_halts(bundle, code: str, sessions: pd.DatetimeIndex) -> pd.DatetimeIndex:
    masks = []
    expected_code = to_baostock(code)
    adjusted_flag = bundle.metadata.get("adjusted_flag", "1")
    if adjusted_flag not in {"1", "2"}:
        raise ValueError("Unsupported secondary adjustment basis")
    for frame, flag in ((bundle.raw, "3"), (bundle.adjusted, adjusted_flag)):
        required = {"date", "code", "adjustflag", "tradestatus"}
        if not required.issubset(frame) or not frame.code.eq(expected_code).all():
            raise ValueError("Suspension evidence has wrong instrument or adjustment basis")
        wrong_basis = ~frame.adjustflag.astype(str).eq(flag)
        if wrong_basis.any():
            raise MarketDataError(
                "Suspension evidence has wrong instrument or adjustment basis",
                category="secondary_adjustment_basis",
                dates=frame.loc[wrong_basis, "date"].astype(str).tolist(),
                action="复权标记混杂，拒绝使用该口径；可尝试另一完整复权口径，不拼接错误行。",
            )
        dates = pd.DatetimeIndex(pd.to_datetime(frame.date, format="%Y-%m-%d", errors="raise"))
        indexed = frame.assign(date=dates).sort_values("date").set_index("date")
        if indexed.index.has_duplicates or not indexed.index.equals(sessions):
            raise ValueError("Suspension evidence does not cover the required calendar")
        status = indexed.tradestatus.astype(str)
        if not status.isin(["0", "1"]).all():
            raise ValueError("Unknown secondary trading status")
        for field in ("volume", "amount"):
            if field not in indexed:
                raise ValueError("Secondary trading activity is missing")
            values = indexed.loc[status.eq("0"), field]
            numeric = pd.to_numeric(values, errors="coerce")
            blank = values.isna() | values.astype(str).str.strip().eq("")
            conflict = ~blank & (numeric.isna() | ~np.isfinite(numeric) | numeric.ne(0))
            if conflict.any():
                raise MarketDataError(
                    "Secondary trading activity conflicts with suspension status",
                    category="secondary_suspension_activity_conflict",
                    dates=values.index[conflict].strftime("%Y-%m-%d").tolist(),
                    action="BaoStock停牌标记与成交量或成交额冲突，不将该日当作停牌，也不采用矛盾数据。",
                )
        masks.append(status.eq("0"))
    if not masks[0].equals(masks[1]):
        raise ValueError("Raw and adjusted suspension evidence disagree")
    return sessions[masks[0]]


class LeadingHaltError(ValueError):
    """The requested range starts inside a suspension, so no prior traded close is available."""


def carry_halt_rows(frame: pd.DataFrame, dates: pd.DatetimeIndex) -> pd.DataFrame:
    """Only declared dates are carried; active-session holes remain missing."""
    out = frame.copy()
    mask = pd.Series(out.index.isin(dates), index=out.index)
    out["is_suspended"] = mask
    out["valuation_carried"] = False
    if not mask.any():
        return out
    if "volume" in out and out.loc[mask, "volume"].fillna(0).gt(0).any():
        raise ValueError("Primary trading activity conflicts with suspension evidence")
    prior_close = out["close"].where(~mask & out["close"].gt(0)).ffill()
    if prior_close.loc[mask].isna().any():
        raise LeadingHaltError("停牌区间缺少此前的有效报价，请把数据开始日期提前")
    for field in ("open", "high", "low", "close", "pre_close"):
        out.loc[mask, field] = prior_close.loc[mask]
    for field in ("volume", "amount", "change"):
        out.loc[mask, field] = 0.0
    if "adj_factor" in out:
        out.loc[mask, "adj_factor"] = out.adj_factor.where(~mask).ffill().loc[mask]
    for field in ("pe_ttm", "pb", "total_mv", "up_limit", "down_limit"):
        if field in out:
            missing = mask & out[field].isna()
            prior = out[field].where(~mask).ffill()
            if field == "pe_ttm":
                out.loc[missing & prior.notna(), "valuation_carried"] = True
            out.loc[missing, field] = prior.loc[missing]
    if "pe_ttm" in out:
        known = pd.Series(out.index, index=out.index).where(~mask & out.pe_ttm.notna()).ffill()
        out["valuation_asof"] = pd.Series(out.index.strftime("%Y-%m-%d"), index=out.index)
        out.loc[out.valuation_carried, "valuation_asof"] = known.loc[out.valuation_carried].dt.strftime("%Y-%m-%d")
    return out


def invalid_halt_rows(frame: pd.DataFrame) -> pd.Series:
    invalid = pd.Series(False, index=frame.index)
    if "is_suspended" not in frame:
        return invalid
    if frame.is_suspended.isna().any() or not pd.api.types.is_bool_dtype(frame.is_suspended.dtype):
        return ~invalid
    for _, section in frame.sort_values(["instrument", "date"]).groupby("instrument"):
        halted = section.is_suspended
        if not halted.any():
            continue
        bad = section.volume.ne(0) | section.amount.ne(0) | section.change.ne(0)
        for field in ("open", "high", "low", "close", "pre_close"):
            bad |= ~np.isclose(section[field], section.close.shift(), rtol=1e-10, atol=1e-8)
        bad |= ~np.isclose(section.adj_factor, section.adj_factor.shift(), rtol=1e-10, atol=1e-8)
        if "valuation_asof" in section:
            known_at = pd.to_datetime(section.valuation_asof, errors="coerce")
            # A legitimately unavailable PE is a factor-coverage issue, not a
            # fabricated suspended quote. A claimed carried value needs evidence.
            has_value = section.pe_ttm.notna() if "pe_ttm" in section else pd.Series(True, index=section.index)
            bad |= (has_value & known_at.isna()) | known_at.gt(section.date)
            if "valuation_carried" in section:
                bad |= section.valuation_carried & (~has_value | known_at.isna())
        invalid.loc[section.index] = halted & bad
    return invalid
