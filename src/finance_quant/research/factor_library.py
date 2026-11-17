"""Causal factor definitions and calculators, independent of scoring/execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from finance_quant.research.financial_features import GROSS_MARGIN_VERSION, STANDARD_FACTOR_VERSION

FILLABLE_FACTORS = {"value", "gross_margin_ttm"}

FUNDAMENTAL = "fundamental"
FAMILY_LABELS = {
    "value": "价值",
    "momentum": "动量与反转",
    "risk": "风险",
    "size": "规模",
    "activity": "交易活跃度",
    FUNDAMENTAL: "基本面",
}


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


def _reported(field):
    def calculate(frame, spec):
        return frame[field].astype("float64")

    return calculate


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
    description: str | None = None

    @property
    def fundamental(self) -> bool:
        return self.family == FUNDAMENTAL or self.name == "value"

    def public(self):
        if self.fundamental:
            availability = "按公告日后下一交易日09:30可见；信号取当日15:00已可用的最新报告期"
            note = "来自三张财务报表自算（TTM口径），历史更正链按调整前/调整数重建，仍非严格PIT认证；需先下载财务数据"
            if self.name == "value":
                note = "归母净利润按报告可用时间换算TTM，总市值使用当日估值；财报及市值的历史修订与发布时间未完全认证。缺失填补仅作用于评分。"
        else:
            availability = "T日18:00假设可用，仅供下一交易日开盘"
            note = (
                "PB/市值/PE来自兼容服务，历史发布时间与修订未独立认证"
                if self.family in {"value", "size"}
                else "行情及复权口径随冻结快照保留"
            )
        return {
            "name": self.name,
            "label": self.label,
            "family": self.family,
            "family_label": FAMILY_LABELS[self.family],
            "direction": self.direction,
            "required_fields": list(self.required_fields),
            "formula": self.formula,
            "description": self.description or self.formula,
            "default_window": self.default_window,
            "default_skip": self.default_skip,
            "default_baseline_window": self.default_baseline_window,
            "version": GROSS_MARGIN_VERSION
            if self.name == "gross_margin_ttm"
            else STANDARD_FACTOR_VERSION
            if self.fundamental
            else "causal-daily-v1",
            "missing_policy": "zero" if self.name in FILLABLE_FACTORS else "exclude",
            "missing_policies": ["zero", "neutral", "exclude"] if self.name in FILLABLE_FACTORS else ["exclude"],
            **({"not_applicable_score": 50} if self.name == "gross_margin_ttm" else {}),
            "availability": availability,
            "data_note": note,
            "requires_financials": self.fundamental,
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
        ("ttm_net_profit", "total_mv"),
        "信号日已可得的TTM归母净利润 / 当日总市值；保留零利润和亏损",
        lambda f, s: f.ttm_net_profit / _positive(f.total_mv),
        description="以当时可得的滚动十二个月归母净利润衡量市值对应的盈利，负数表示亏损。",
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
    FactorDefinition(
        "roe_ttm",
        "净资产收益率",
        FUNDAMENTAL,
        "higher",
        ("roe_ttm",),
        "TTM归母净利润 / 平均归母股东权益（本期与上年同期）；权益非正不参与",
        _reported("roe_ttm"),
        description="盈利能力：每单位股东权益在最近十二个月创造的归母净利润",
    ),
    FactorDefinition(
        "roa_ttm",
        "总资产收益率",
        FUNDAMENTAL,
        "higher",
        ("roa_ttm",),
        "TTM归母净利润 / 平均总资产",
        _reported("roa_ttm"),
        description="资产效率：不受杠杆影响的盈利能力",
    ),
    FactorDefinition(
        "gross_margin_ttm",
        "毛利率",
        FUNDAMENTAL,
        "higher",
        ("gross_margin_ttm",),
        "(TTM营业收入 − TTM营业成本) / TTM营业收入；专门金融报表不适用",
        _reported("gross_margin_ttm"),
        description="定价能力与产品竞争力；行业差异大，建议配合行业中性化",
    ),
    FactorDefinition(
        "asset_turnover_ttm",
        "总资产周转率",
        FUNDAMENTAL,
        "higher",
        ("asset_turnover_ttm",),
        "TTM营业总收入 / 平均总资产",
        _reported("asset_turnover_ttm"),
        description="运营效率；行业差异大，建议配合行业中性化",
    ),
    FactorDefinition(
        "debt_to_assets",
        "资产负债率",
        FUNDAMENTAL,
        "lower",
        ("debt_to_assets",),
        "最新报告期总负债 / 总资产（越低越好）",
        _reported("debt_to_assets"),
        description="财务杠杆与偿债风险；金融业天然偏高，建议配合行业中性化",
    ),
    FactorDefinition(
        "accruals_ttm",
        "应计比率",
        FUNDAMENTAL,
        "lower",
        ("accruals_ttm",),
        "(TTM归母净利润 − TTM经营活动现金流净额) / 平均总资产（越低越好）",
        _reported("accruals_ttm"),
        description="盈利质量：利润中未变成现金的部分越少越好",
    ),
    FactorDefinition(
        "profit_growth_ttm",
        "净利润增长",
        FUNDAMENTAL,
        "higher",
        ("profit_growth_ttm",),
        "(TTM归母净利润 − 上年同期TTM) / |上年同期TTM|",
        _reported("profit_growth_ttm"),
        description="盈利增长：与一年前同一报告期的滚动十二个月利润比较",
    ),
    FactorDefinition(
        "revenue_growth_ttm",
        "营收增长",
        FUNDAMENTAL,
        "higher",
        ("revenue_growth_ttm",),
        "TTM营业总收入 / 上年同期TTM − 1；上年同期需为正",
        _reported("revenue_growth_ttm"),
        description="业务扩张速度",
    ),
)
FACTOR_LIBRARY = {definition.name: definition for definition in _DEFINITIONS}


def uses_financials(config) -> bool:
    """Whether a factor configuration needs pinned financial statements."""
    return any(FACTOR_LIBRARY[spec.name].fundamental for spec in config.specs)


def requires_financials(config) -> bool:
    """Gross margin can resolve applicability from pinned historical industries without reports."""
    return any(
        FACTOR_LIBRARY[spec.name].fundamental
        and spec.name != "gross_margin_ttm"
        and (spec.name != "value" or spec.missing_policy == "exclude")
        for spec in config.specs
    )


def describe_spec(spec):
    definition = FACTOR_LIBRARY[spec.name]
    if spec.name == "momentum" and spec.skip == 0:
        formula = f"历史{spec.window}个交易日复权收盘收益率"
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
        "missing_policy": spec.missing_policy,
    }
