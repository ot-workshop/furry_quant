"""Causal daily eligibility filters, independent from factor scores and execution."""

import math
from dataclasses import asdict, dataclass
from itertools import pairwise

import numpy as np
import pandas as pd

from finance_quant.data.china_a_rules import board
from finance_quant.data.security_history import ST_POLICY, name_states
from finance_quant.data.temporal_audit import validate_valuation_times


@dataclass(frozen=True)
class UniverseFilters:
    boards: tuple[str, ...] = ("main", "chinext", "star")
    exclude_st: bool = True
    min_listing_days: int = 180
    min_market_cap: float | None = None
    max_market_cap: float | None = None
    min_avg_amount: float | None = None
    amount_window: int = 20
    industries: tuple[str, ...] = ()
    industry_level: str = "L1"
    industry_version: str | None = None

    def __post_init__(self):
        if not isinstance(self.exclude_st, bool):
            raise ValueError("排除ST须为开关值")
        for key, low, high in (("min_listing_days", 0, 36500), ("amount_window", 2, 252)):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{key}须为{low}至{high}的整数")
        if (
            not isinstance(self.boards, (list, tuple))
            or not self.boards
            or set(self.boards) - {"main", "chinext", "star"}
        ):
            raise ValueError("请选择沪深主板、创业板或科创板")
        object.__setattr__(self, "boards", tuple(sorted(set(self.boards))))
        for key in ("min_market_cap", "max_market_cap", "min_avg_amount"):
            value = getattr(self, key)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0
            ):
                raise ValueError("市值与成交额条件须为非负有限金额")
        if (
            self.min_market_cap is not None
            and self.max_market_cap is not None
            and self.min_market_cap > self.max_market_cap
        ):
            raise ValueError("市值下限不能大于上限")
        if not isinstance(self.industries, (list, tuple)) or any(
            not isinstance(code, str) or not code or len(code) > 32 for code in self.industries
        ):
            raise ValueError("行业筛选须使用有效行业代码")
        object.__setattr__(self, "industries", tuple(sorted(set(self.industries))))
        if self.industry_level not in {"L1", "L2", "L3"}:
            raise ValueError("行业筛选级别无效")
        if self.industry_version is not None and (
            not isinstance(self.industry_version, str)
            or len(self.industry_version) != 64
            or any(c not in "0123456789abcdef" for c in self.industry_version)
        ):
            raise ValueError("行业筛选版本无效")

    def as_dict(self):
        result = asdict(self)
        result["boards"] = list(self.boards)
        result["industries"] = list(self.industries)
        return result


def parse_filters(value):
    if not isinstance(value, dict) or set(value) - set(UniverseFilters.__dataclass_fields__):
        raise ValueError("股票池筛选字段无效")
    return UniverseFilters(**value)


def select_universe(market, sessions, start, end, filters, names, *, industry=None, base_membership=None):
    """Use only signal-date observations; retired holdings remain in market for valuation."""
    market = market.copy()
    market["selection_member"] = False
    market["listing_days"] = (market.date - market.list_date).dt.days
    market["is_st"] = pd.Series(pd.NA, index=market.index, dtype="boolean")
    for symbol, part in market.groupby("instrument", sort=False):
        states = name_states(part.date, names.loc[names.instrument.eq(symbol)])
        market.loc[part.index, "is_st"] = states.to_numpy()
    masks = {
        "board": market.instrument.map(board).isin(filters.boards),
        "lifecycle": (market.list_date.isna() | market.list_date.le(market.date))
        & (market.delist_date.isna() | market.date.lt(market.delist_date)),
    }
    if filters.min_listing_days:
        masks["listing_days"] = market.listing_days.ge(filters.min_listing_days)
    if filters.exclude_st:
        masks["st_unknown"] = market.is_st.notna()
        masks["st"] = ~market.is_st.fillna(False)
    if filters.min_market_cap is not None or filters.max_market_cap is not None:
        validate_valuation_times(market, {"total_mv"})
        cap = market.total_mv.where(np.isfinite(market.total_mv) & market.total_mv.gt(0))
        masks["market_cap"] = cap.notna()
        if filters.min_market_cap is not None:
            masks["market_cap"] &= cap.ge(filters.min_market_cap)
        if filters.max_market_cap is not None:
            masks["market_cap"] &= cap.le(filters.max_market_cap)
    if filters.min_avg_amount is not None:
        amounts = market.amount.where(np.isfinite(market.amount) & market.amount.ge(0))
        market["average_amount"] = amounts.groupby(market.instrument).transform(
            lambda values: values.rolling(filters.amount_window, min_periods=filters.amount_window).mean()
        )
        masks["liquidity"] = market.average_amount.ge(filters.min_avg_amount)
    if filters.industries:
        if industry is None or industry.manifest["id"] != filters.industry_version:
            raise ValueError("行业筛选缺少已绑定的历史行业版本")
        industry.validate_range(end)
        industry.validate_level(filters.industry_level)
        masks["industry"] = pd.Series(False, index=market.index)
        for date, part in market.groupby("date", sort=True):
            labels = industry.at(date, part.instrument, sessions, level=filters.industry_level)
            masks["industry"].loc[part.index] = [
                labels[s]["industry_code"] in filters.industries for s in part.instrument
            ]
    accepted = pd.Series(True, index=market.index)
    for mask in masks.values():
        accepted &= mask.fillna(False)
    active = sessions[(sessions >= pd.Timestamp(start)) & (sessions <= pd.Timestamp(end))]
    before = {b: a for a, b in pairwise(sessions)}
    membership, declared, coverage, exclusions = {}, {}, {}, {}
    by_day = {day: part.index for day, part in market.groupby("date", sort=False)}
    for execution in active:
        signal = before[execution]
        indexes = by_day.get(signal, pd.Index([], dtype=int))
        if base_membership is not None:
            members = base_membership[execution.strftime("%Y-%m-%d")]
            indexes = indexes[market.loc[indexes, "instrument"].isin(members)]
        chosen = indexes[accepted.loc[indexes].to_numpy()]
        market.loc[chosen, "selection_member"] = True
        key = signal.strftime("%Y-%m-%d")
        membership[execution.strftime("%Y-%m-%d")] = sorted(market.loc[chosen, "instrument"])
        declared[key] = len(chosen)
        rejected = market.loc[indexes.difference(chosen)]
        exclusions[key] = rejected.instrument.head(20).tolist()
        coverage[key] = {
            "available": len(indexes),
            "selected": len(chosen),
            "excluded": {name: int((~mask.loc[indexes].fillna(False)).sum()) for name, mask in masks.items()},
        }
    return market, {
        "membership_by_execution_date": membership,
        "declared_universe": declared,
        "lifecycle_exclusions": exclusions,
        "pool_coverage_by_date": coverage,
        "universe_filters": filters.as_dict(),
        "st_policy": ST_POLICY,
    }
