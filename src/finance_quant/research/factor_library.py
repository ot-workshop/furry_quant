"""Causal factor definitions and calculators, independent of scoring/execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd


def _positive(series):
    return series.where(np.isfinite(series) & series.gt(0))


def _momentum(frame, spec):
    def one(s):
        return s.shift(spec.skip) / s.shift(spec.window) - 1

    return frame.groupby("instrument").adjusted_close.transform(one)


def _volatility(frame, spec):
    return frame.groupby("instrument").daily_return.transform(
        lambda s: s.rolling(spec.window, min_periods=spec.window).std()
    ) * np.sqrt(252)


def _downside(frame, spec):
    def one(s):
        complete = s.rolling(spec.window, min_periods=spec.window).count().eq(spec.window)
        return s.where(s < 0).rolling(spec.window, min_periods=2).std().where(complete)

    return frame.groupby("instrument").daily_return.transform(one) * np.sqrt(252)


def _drawdown(frame, spec):
    return frame.groupby("instrument").adjusted_close.transform(
        lambda s: s.rolling(spec.window + 1, min_periods=spec.window + 1).apply(
            lambda values: np.min(values / np.maximum.accumulate(values) - 1), raw=True
        )
    )


def _amount(frame, spec):
    def one(s):
        s = s.where(np.isfinite(s) & s.ge(0))
        recent = s.rolling(spec.window, min_periods=spec.window).mean()
        baseline = s.shift(spec.window).rolling(spec.baseline_window, min_periods=spec.baseline_window).mean()
        # Retain exchange calendar positions. Long suspensions do not create a
        # shortened window or a misleading ratio against a nearly empty base.
        active = s.gt(0).astype(float)
        enough = active.rolling(spec.window).sum().ge(np.ceil(0.8 * spec.window))
        enough &= active.shift(spec.window).rolling(spec.baseline_window).sum().ge(np.ceil(0.8 * spec.baseline_window))
        return (recent / baseline.where(baseline > 0)).where(enough & s.gt(0))

    return frame.groupby("instrument").amount.transform(one)


def _valuation(frame, field, logarithm=False):
    value = _positive(frame[field])
    # New daily-basic factors do not assert a precise publication/as-of time
    # for carried auxiliary values on suspension days.
    value = value.where(~frame.get("is_suspended", pd.Series(False, index=frame.index)).fillna(False))
    return -np.log(value) if logarithm else 1 / value


@dataclass(frozen=True)
class FactorDefinition:
    name: str
    label: str
    family: str
    direction: str
    required_fields: tuple[str, ...]
    formula: str
    calculator: Callable
    default_window: int | None = None
    default_skip: int | None = None
    default_baseline_window: int | None = None

    def public(self):
        return {
            "name": self.name,
            "label": self.label,
            "family": self.family,
            "family_label": {
                "value": "价值",
                "momentum": "动量与反转",
                "risk": "风险",
                "size": "规模",
                "activity": "交易活跃度",
            }[self.family],
            "direction": self.direction,
            "required_fields": list(self.required_fields),
            "formula": self.formula,
            "description": self.formula,
            "default_window": self.default_window,
            "default_skip": self.default_skip,
            "default_baseline_window": self.default_baseline_window,
            "version": "causal-daily-v1",
            "missing_policy": "strict_selected_factor_intersection",
            "availability": "T日18:00假设可用，仅供下一交易日开盘",
            "data_note": "PB/市值/PE来自兼容服务，历史发布时间与修订未独立认证"
            if self.family in {"value", "size"}
            else "行情及复权口径随冻结快照保留",
        }


_DEFINITIONS = (
    FactorDefinition(
        "momentum",
        "动量",
        "momentum",
        "higher",
        ("close", "adj_factor"),
        "P[t-skip] / P[t-window] - 1；window为总回看跨度",
        _momentum,
        20,
        0,
    ),
    FactorDefinition(
        "value",
        "盈利收益率",
        "value",
        "higher",
        ("pe_ttm",),
        "1 / 正PE_TTM；缺失或非正值不参与",
        lambda f, s: 1 / _positive(f.pe_ttm),
    ),
    FactorDefinition(
        "volatility",
        "波动率",
        "risk",
        "lower",
        ("close", "adj_factor"),
        "过去window个复权日收益的样本标准差 × √252",
        _volatility,
        10,
    ),
    FactorDefinition(
        "book_to_price",
        "账面价值收益率",
        "value",
        "higher",
        ("pb",),
        "1 / 正PB；停牌沿用值不参与",
        lambda f, s: _valuation(f, "pb"),
    ),
    FactorDefinition(
        "size",
        "小市值",
        "size",
        "higher",
        ("total_mv",),
        "-ln(总市值/CNY)；非正或停牌沿用值不参与",
        lambda f, s: _valuation(f, "total_mv", True),
    ),
    FactorDefinition(
        "reversal",
        "短期反转",
        "momentum",
        "higher",
        ("close", "adj_factor"),
        "-(P[t-skip] / P[t-window] - 1)；window为总跨度",
        lambda f, s: -_momentum(f, s),
        21,
        1,
    ),
    FactorDefinition(
        "medium_momentum",
        "中期动量",
        "momentum",
        "higher",
        ("close", "adj_factor"),
        "P[t-skip] / P[t-window] - 1；默认回看126天、剔除最近21天",
        _momentum,
        126,
        21,
    ),
    FactorDefinition(
        "downside_volatility",
        "下行波动率",
        "risk",
        "lower",
        ("close", "adj_factor"),
        "完整window日窗口中负收益的样本标准差 × √252；至少2个负收益",
        _downside,
        20,
    ),
    FactorDefinition(
        "max_drawdown",
        "滚动最大回撤",
        "risk",
        "higher",
        ("close", "adj_factor"),
        "过去window个交易日的复权价格最大回撤（负数，越接近0越好）",
        _drawdown,
        60,
    ),
    FactorDefinition(
        "amount_anomaly",
        "成交额异动",
        "activity",
        "higher",
        ("amount",),
        "近期window日平均成交额 / 此前不重叠baseline_window日均额；两段至少80%有成交",
        _amount,
        20,
        None,
        60,
    ),
)
FACTOR_LIBRARY = {definition.name: definition for definition in _DEFINITIONS}


def describe_spec(spec):
    definition = FACTOR_LIBRARY[spec.name]
    if spec.name == "momentum" and spec.skip == 0:
        formula = f"历史{spec.window}个交易日复权收盘收益率"
    elif spec.name == "value":
        formula = "当日pe_ttm的倒数；非正或缺失估值不参与"
    elif spec.name == "volatility":
        formula = f"过去{spec.window}日复权收益率的年化标准差（越低越好）"
    else:
        formula = definition.formula
    return {
        **definition.public(),
        "id": spec.key,
        "params": spec.parameters,
        "lookback": spec.lookback,
        "formula": formula,
    }
