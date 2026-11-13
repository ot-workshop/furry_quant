"""Point-in-time fundamental features from rebuilt statement versions.

Income and cash-flow statements report year-to-date cumulative values, so a Q1 figure
is roughly a quarter of an annual one. Every flow is therefore converted to trailing
twelve months (TTM) before it enters a cross section::

    TTM(P) = YTD(P)                                   if P is a fiscal year end
    TTM(P) = YTD(P) + Annual(P.year - 1) - YTD(P - 1 year)   otherwise

Balance-sheet denominators average the period end with the same period a year earlier.
Features are evaluated at every moment a new statement version becomes visible and are
joined to market rows "as of" the 15:00 decision time of each session.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from finance_quant.data.financials import (
    AVAILABILITY_POLICY,
    POLICY,
    FinancialBinding,
    FinancialStore,
    covered_codes,
    report_versions,
    statement_fields,
)
from finance_quant.data.point_in_time import EXCHANGE_TZ
from finance_quant.data.symbols import from_qlib
from finance_quant.research.factor_library import uses_financials
from finance_quant.research.financial_features import FEATURE_VERSION, FEATURES

MAX_REPORT_AGE_DAYS = 400
DECISION_TIME = pd.Timedelta(hours=15)
REPORT_COLUMNS = ("financial_report_period", "financial_available_at")
FEATURE_POLICY = (
    f"利润表与现金流量表为年初至今累计值，统一换算为TTM（滚动四季度）；资产负债表分母取本期与上年同期的平均；"
    f"每个交易日15:00取当时已可用的最新报告期，最新报告期距交易日超过{MAX_REPORT_AGE_DAYS}天视为过期"
)


def _finite(value) -> bool:
    return value is not None and isinstance(value, float) and math.isfinite(value)


def _year_ago(period: pd.Timestamp) -> pd.Timestamp:
    return period - pd.DateOffset(years=1)


class _State:
    """The latest visible value of every statement field, by report period."""

    def __init__(self):
        self.values = {"income": {}, "balance": {}, "cashflow": {}}

    def update(self, statement, period, values):
        self.values[statement][period] = values

    def get(self, statement, period, field) -> float:
        value = self.values[statement].get(period, {}).get(field)
        return float(value) if _finite(value) else math.nan

    def ttm(self, statement, period, field) -> float:
        current = self.get(statement, period, field)
        if period.month == 12:
            return current
        annual = self.get(statement, pd.Timestamp(year=period.year - 1, month=12, day=31), field)
        comparable = self.get(statement, _year_ago(period), field)
        return current + annual - comparable

    def average(self, period, field) -> float:
        current = self.get("balance", period, field)
        if not math.isfinite(current):
            return math.nan
        earlier = self.get("balance", _year_ago(period), field)
        return (current + earlier) / 2 if math.isfinite(earlier) else current

    def revenue_ttm(self, period) -> float:
        total = self.ttm("income", period, "total_revenue")
        return total if math.isfinite(total) else self.ttm("income", period, "revenue")

    def features(self) -> dict | None:
        if not self.values["income"]:
            return None
        period = max(self.values["income"])
        previous = _year_ago(period)
        profit = self.ttm("income", period, "n_income_attr_p")
        profit_before = self.ttm("income", previous, "n_income_attr_p")
        revenue = self.revenue_ttm(period)
        revenue_before = self.revenue_ttm(previous)
        cost = self.ttm("income", period, "oper_cost")
        cash = self.ttm("cashflow", period, "n_cashflow_act")
        equity = self.average(period, "total_hldr_eqy_exc_min_int")
        assets = self.average(period, "total_assets")
        latest_assets = self.get("balance", period, "total_assets")
        liabilities = self.get("balance", period, "total_liab")

        def ratio(numerator, denominator, *, positive=True):
            if not (math.isfinite(numerator) and math.isfinite(denominator)):
                return math.nan
            if (positive and denominator <= 0) or denominator == 0:
                return math.nan
            return numerator / denominator

        return {
            "financial_report_period": period,
            "roe_ttm": ratio(profit, equity),
            "roa_ttm": ratio(profit, assets),
            "gross_margin_ttm": ratio(revenue - cost, revenue),
            "asset_turnover_ttm": ratio(revenue, assets),
            "debt_to_assets": ratio(liabilities, latest_assets),
            "accruals_ttm": ratio(profit - cash, assets),
            "profit_growth_ttm": ratio(profit - profit_before, abs(profit_before)),
            "revenue_growth_ttm": ratio(revenue, revenue_before) - 1,
        }


def fundamental_events(versions: pd.DataFrame) -> pd.DataFrame:
    """One feature row per stock and moment a new statement version became visible."""
    columns = ["instrument", "financial_available_at", "financial_report_period", *FEATURES]
    if versions.empty:
        return pd.DataFrame(columns=columns).astype({"financial_available_at": "datetime64[ns, UTC]"})
    ordered = versions.sort_values(["instrument", "available_at", "priority", "revision"])
    fields = {statement: statement_fields(statement) for statement in ("income", "balance", "cashflow")}
    rows = []
    for instrument, part in ordered.groupby("instrument", sort=False):
        state, moment = _State(), None
        for record in part.itertuples(index=False):
            if moment is not None and record.available_at != moment and (found := state.features()):
                rows.append({"instrument": instrument, "financial_available_at": moment, **found})
            state.update(
                record.statement,
                record.report_period,
                {field: getattr(record, field) for field in fields[record.statement]},
            )
            moment = record.available_at
        if found := state.features():
            rows.append({"instrument": instrument, "financial_available_at": moment, **found})
    events = pd.DataFrame(rows, columns=columns)
    events["financial_available_at"] = pd.to_datetime(events.financial_available_at, utc=True).astype(
        "datetime64[ns, UTC]"
    )
    events["financial_report_period"] = pd.to_datetime(events.financial_report_period).astype("datetime64[ns]")
    for feature in FEATURES:
        events[feature] = events[feature].astype("float64")
    return events


def attach_fundamentals(frame: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """Add the features visible at each session's 15:00 decision time; stale reports become NaN."""
    result = frame.copy()
    cutoff = (pd.to_datetime(result.date) + DECISION_TIME).dt.tz_localize(EXCHANGE_TZ).dt.tz_convert("UTC")
    left = pd.DataFrame({"instrument": result.instrument.to_numpy(), "_cutoff": cutoff.astype("datetime64[ns, UTC]")})
    left["_row"] = np.arange(len(left))
    right = events.sort_values("financial_available_at")
    merged = pd.merge_asof(
        left.sort_values("_cutoff"),
        right,
        left_on="_cutoff",
        right_on="financial_available_at",
        by="instrument",
        direction="backward",
    ).sort_values("_row")
    age = (pd.to_datetime(result.date).to_numpy() - merged.financial_report_period.to_numpy()).astype("timedelta64[D]")
    stale = ~(age <= np.timedelta64(MAX_REPORT_AGE_DAYS, "D"))
    for column in FEATURES:
        values = merged[column].to_numpy(dtype="float64")
        values[stale] = np.nan
        result[column] = values
    result["financial_report_period"] = merged.financial_report_period.to_numpy()
    result["financial_available_at"] = merged.financial_available_at.to_numpy()
    return result


@dataclass(frozen=True)
class FundamentalData:
    """Feature events of one pinned financial version for one research universe."""

    version: str
    events: pd.DataFrame
    covered: tuple[str, ...]
    requested: int

    def metadata(self, universe) -> dict:
        members = set(universe)
        return {
            "version": self.version,
            "feature_version": FEATURE_VERSION,
            "covered": len(members.intersection(self.covered)),
            "universe": len(members),
            "max_report_age_days": MAX_REPORT_AGE_DAYS,
            "policy": POLICY,
            "availability_policy": AVAILABILITY_POLICY,
            "feature_policy": FEATURE_POLICY,
            "point_in_time_certified": False,
        }


@lru_cache(maxsize=4)
def _load(root: Path, version: str, ts_codes: tuple[str, ...]) -> FundamentalData:
    store = FinancialStore(root)
    manifest = store.version(version)
    observations, sessions = store.load(manifest, ts_codes)
    events = fundamental_events(report_versions(observations, sessions))
    covered = tuple(sorted(set(events.instrument)))
    return FundamentalData(version, events, covered, len(ts_codes))


def load_fundamentals(binding: FinancialBinding, ts_codes) -> FundamentalData:
    """Load (and memoize) one version for a universe; callers must not mutate ``events``."""
    return _load(binding.root.resolve(), binding.version, tuple(sorted(set(ts_codes))))


def fundamentals_for(binding: FinancialBinding | None, config, market: pd.DataFrame) -> FundamentalData | None:
    """The features a factor configuration needs for this market frame, or ``None`` if unused."""

    if not uses_financials(config):
        return None
    if binding is None:
        raise ValueError("所选基本面因子需要财务数据：请先在“数据管理 → 财务数据”下载后重新提交")
    return load_fundamentals(binding, [from_qlib(code) for code in market.instrument.unique()])


def financial_provenance(binding: FinancialBinding | None, config) -> dict:
    """Fingerprint entry; empty when no fundamental factor is selected so old results stay reusable."""

    if binding is None or not uses_financials(config):
        return {}
    return {
        "financial_version": binding.version,
        "financial_availability_policy": binding.availability_policy,
        "financial_feature_version": FEATURE_VERSION,
    }


def coverage(binding_root: Path, ts_codes) -> dict | None:
    """Coverage of the current version for a universe, for request options and validation."""
    manifest = FinancialStore(binding_root).current()
    if manifest is None:
        return None
    codes = sorted(set(ts_codes))
    return {"version": manifest["id"], "covered": len(covered_codes(manifest, codes)), "total": len(codes)}
