"""Dated A-share fee assumptions and causal calendar-based rebalance schedules."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from numbers import Real

import pandas as pd

EXECUTION_FIELDS = (
    "rebalance_frequency",
    "cost_mode",
    "commission_rate",
    "tax_mode",
    "stamp_tax_rate",
    "transfer_fee_rate",
    "impact_cost",
)
POLICY_VERSION = "scheduled-itemized-costs-v1"


def validate_frequency(value):
    if value not in ("daily", "weekly", "monthly"):
        raise ValueError("调仓频率须为daily、weekly或monthly")
    return value


def rebalance_dates(calendar, start, end, frequency="daily"):

    validate_frequency(frequency)
    sessions = pd.DatetimeIndex(calendar)
    if sessions.has_duplicates or sessions.hasnans or not sessions.is_monotonic_increasing:
        raise ValueError("调仓计划需要有序且唯一的交易日历")
    selected = sessions[(sessions >= pd.Timestamp(start)) & (sessions <= pd.Timestamp(end))]
    output = []
    previous = None
    for day in selected:
        period = (
            day.date()
            if frequency == "daily"
            else tuple(day.isocalendar()[:2])
            if frequency == "weekly"
            else (day.year, day.month)
        )
        if period != previous:
            output.append(day.strftime("%Y-%m-%d"))
        previous = period
    return output


@dataclass(frozen=True)
class ExecutionCosts:
    cost_mode: str = "legacy"
    commission_rate: float = 0.0003
    tax_mode: str = "historical"
    stamp_tax_rate: float = 0.0005
    transfer_fee_rate: float = 0.00001
    impact_cost: float = 0.0
    open_cost: float = 0.001
    close_cost: float = 0.001
    min_cost: float = 5.0

    def __post_init__(self):
        if self.cost_mode not in ("legacy", "itemized"):
            raise ValueError("费用模式须为legacy或itemized")
        if self.tax_mode not in ("historical", "fixed"):
            raise ValueError("税费模式须为historical或fixed")
        for key in (
            "commission_rate",
            "stamp_tax_rate",
            "transfer_fee_rate",
            "impact_cost",
            "open_cost",
            "close_cost",
            "min_cost",
        ):
            value = getattr(self, key)
            try:
                number = float(value) if not isinstance(value, bool) and isinstance(value, Real) else float("nan")
            except (OverflowError, ValueError, TypeError):
                number = float("nan")
            if not math.isfinite(number) or number < 0 or (key != "min_cost" and number >= 1):
                raise ValueError(f"{key}须为非负有限数，费率须小于1")
            object.__setattr__(self, key, number)
        highest = (
            max(self.open_cost, self.close_cost)
            if self.cost_mode == "legacy"
            else self.commission_rate
            + (self.stamp_tax_rate if self.tax_mode == "fixed" else 0.001)
            + (self.transfer_fee_rate if self.tax_mode == "fixed" else 0.00002)
        )
        if highest + self.impact_cost >= 1:
            raise ValueError("合计比例费用须小于100%")

    def validate_range(self, start):
        if self.cost_mode == "itemized" and self.tax_mode == "historical" and str(start) < "2015-08-01":
            raise ValueError("历史税费表仅支持2015-08-01起；更早区间请使用自定义税费")

    def rates(self, day, sell):
        day = str(day)[:10]
        if self.tax_mode == "historical":
            self.validate_range(day)
            stamp = 0.001 if day < "2023-08-28" else 0.0005
            transfer = 0.00002 if day < "2022-04-29" else 0.00001
        else:
            stamp, transfer = self.stamp_tax_rate, self.transfer_fee_rate
        return (stamp if sell else 0.0), transfer

    def breakdown(self, value, sell, day):
        values = dict(commission=0.0, stamp_tax=0.0, transfer_fee=0.0, impact=0.0, legacy_fee=0.0)
        if value <= 1e-5:
            return {**values, "total": 0.0}
        if self.cost_mode == "legacy":
            values["legacy_fee"] = max(value * (self.close_cost if sell else self.open_cost), self.min_cost)
        else:
            stamp, transfer = self.rates(day, sell)
            values.update(
                commission=max(value * self.commission_rate, self.min_cost),
                stamp_tax=value * stamp,
                transfer_fee=value * transfer,
            )
        values["impact"] = value * self.impact_cost
        return {**values, "total": sum(values.values())}

    def max_buy_value(self, cash, day):
        commission = self.open_cost if self.cost_mode == "legacy" else self.commission_rate
        extra = self.impact_cost + (self.rates(day, False)[1] if self.cost_mode == "itemized" else 0.0)
        return max(0.0, min(cash / (1 + commission + extra), (cash - self.min_cost) / (1 + extra)))

    def as_dict(self):
        return {**asdict(self), "version": POLICY_VERSION, "impact_model": "fixed_notional_charge"}
