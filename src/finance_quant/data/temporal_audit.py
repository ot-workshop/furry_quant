"""Explicit observation-time guards and bounded research/execution claims.

Checking chronology does not certify the historical vendor publication vintage.
"""

from __future__ import annotations

import copy

import numpy as np
import pandas as pd

from finance_quant.data.china_a_rules import RULE_VERSION

VERSION = "temporal-execution-audit-v1"
VALUATION_FIELDS = {"pe_ttm", "pb", "total_mv"}
VALUATION_DATE_COLUMNS = {"pe_ttm": "valuation_asof", "pb": "pb_asof", "total_mv": "total_mv_asof"}


def validate_valuation_times(frame, required_fields):
    """Only used inputs are mandatory; unprovided historical timestamps stay unknown."""
    for field in VALUATION_FIELDS.intersection(required_fields):
        if field not in frame:
            continue
        present = pd.to_numeric(frame[field], errors="coerce").notna()
        column = field + "_asof" if field + "_asof" in frame else VALUATION_DATE_COLUMNS[field]
        if column in frame:
            observed = pd.to_datetime(frame[column], errors="coerce")
            if not pd.api.types.is_datetime64_any_dtype(observed) or observed.dt.tz is not None:
                raise ValueError(f"{field}估值对应日期必须为不带时区的交易日期")
            provided = frame[column].notna() & frame[column].astype(str).str.strip().ne("")
            invalid = present & (
                (provided & observed.isna())
                | observed.gt(frame.date)
                | observed.ne(observed.dt.normalize()).where(observed.notna(), False)
            )
            if field == "pe_ttm" and "valuation_carried" in frame:
                invalid |= present & frame.valuation_carried.fillna(False) & observed.isna()
            if invalid.any():
                raise ValueError(f"{field}估值对应日期缺失或晚于信号日，拒绝使用未来估值")
        available = field + "_available_at" if field + "_available_at" in frame else "valuation_available_at"
        if available not in frame:
            continue
        cutoff = frame.date.dt.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=18)
        for value, until in zip(frame.loc[present, available], cutoff.loc[present], strict=True):
            if value is None or pd.isna(value):
                continue  # Unknown stays unverified, like an absent optional field.
            try:
                stamp = pd.Timestamp(value)
            except (ValueError, TypeError):
                raise ValueError(f"{field}估值可用时间无效") from None
            if pd.isna(stamp) or stamp.tzinfo is None or stamp > until:
                raise ValueError(f"{field}估值可用时间未知或晚于当日决策时点")


def restore_valuation_dates(frame):
    """Recover a missing PE carry date from explicit carry flags and prior values.

    Old immutable archives omitted this annotation. No price/value is changed;
    an unreconcilable carry is rejected instead of pretending it is fresh.
    """
    out = frame.copy()
    if "valuation_asof" in out or "pe_ttm" not in out:
        return out
    out["valuation_asof"] = out.date.dt.strftime("%Y-%m-%d").where(out.pe_ttm.notna())
    if "valuation_carried" not in out:
        return out
    for _, part in out.groupby("instrument", sort=False):
        part = part.sort_values("date")
        carried = part.valuation_carried.fillna(False).astype(bool)
        if not carried.any():
            continue
        observed = ~part.get("is_suspended", pd.Series(False, index=part.index)).fillna(False) & part.pe_ttm.notna()
        previous_value = part.pe_ttm.where(observed).ffill()
        previous_date = part.date.where(observed).ffill()
        valid = (
            previous_date.notna()
            & previous_date.lt(part.date)
            & np.isclose(part.pe_ttm, previous_value, rtol=1e-10, atol=1e-10)
        )
        if (carried & ~valid).any():
            raise ValueError("历史沿用PE缺少可核对的此前记录，请增加预热或核验原始数据")
        out.loc[part.index[carried], "valuation_asof"] = previous_date.loc[carried].dt.strftime("%Y-%m-%d")
    return out


def factor_temporal_audit(metadata):
    fields = sorted(
        {field for d in metadata.get("factor_definitions", {}).values() for field in d.get("required_fields", [])}
        & VALUATION_FIELDS
    )
    dynamic = bool(metadata.get("membership_policy"))
    market_universe = metadata.get("universe_kind") in {"market", "custom"}
    pool_filters = metadata.get("universe_filters", {})
    if pool_filters.get("min_market_cap") is not None or pool_filters.get("max_market_cap") is not None:
        fields = sorted(set(fields) | {"total_mv"})
    industry = metadata.get("industry_data")
    industry_used = metadata.get("neutralization", {}).get("mode") == "industry" or bool(pool_filters.get("industries"))
    items = [
        {
            "key": "membership",
            "label": "股票池时间口径",
            "status": "assumption" if dynamic else "selection_bias",
            "detail": "按信号日历史生命周期与条件筛选；ST使用历史名称变更估计，开启ST剔除时同时剔除未知状态。"
            if market_universe
            else "月度权重快照后下一交易日起使用；trade_date是观察日，未取得完整调整公告、生效日和历史发布时刻，不能视为精确历史成分。"
            if dynamic
            else "用户固定股票池；历史表现可能包含选择和幸存者偏差，不视为历史全市场或历史指数成分。",
        },
        {
            "key": "industry",
            "label": "历史行业",
            "status": "unverified" if industry_used else "diagnostic_only" if industry else "not_used",
            "detail": (
                "使用保存版本"
                + (
                    f"的{industry.get('level_label', '一级')}行业"
                    if isinstance(industry, dict) and industry.get("level_label")
                    else "的"
                )
                + "纳入/剔除区间及分类切换约定；实际公告与修订发布时间未认证。"
                if industry
                else "本批次未使用独立历史行业数据。"
            )
            + ("行业仅用于诊断，不参与本次评分。" if industry and not industry_used else "")
            + (f"股票池另按{pool_filters['industry_level']}历史行业筛选。" if pool_filters.get("industries") else ""),
        },
        {
            "key": "valuation",
            "label": "PE/PB与市值",
            "status": "unverified" if fields else "not_used",
            "detail": (
                "使用供应商交易日历史值："
                + ", ".join(fields)
                + "。官方daily_basic通常盘后15—17点更新，本模型假设当日18点可用；不附带财报原始版本、完整更正链和历史首次发布时间，尚非严格PIT估值。"
                if fields
                else "本次入选因子不依赖PE/PB或市值字段。"
            ),
        },
        {
            "key": "financials",
            "label": "财务报表",
            "status": "unverified" if metadata.get("financial_data") else "not_used",
            "detail": (
                "使用财务数据版本 "
                + metadata["financial_data"]["version"]
                + "："
                + metadata["financial_data"].get("policy", "按公告日估计历史可见时间")
                + "。"
                "供应商未留痕的历史更正无法还原，因此不是严格PIT。"
                if metadata.get("financial_data")
                else "本次入选因子不使用财务报表。"
            ),
        },
        {
            "key": "signal",
            "label": "信号与交易时序",
            "status": "guarded",
            "detail": "日线与估值按当日18点可用的假设生成评分，下一交易日开盘执行；显式估值日期不能晚于信号日，信号必须在执行开盘前可用。",
        },
    ]
    return {
        "version": VERSION,
        "point_in_time_certified": False,
        "valuation_fields_used": fields,
        "items": items,
        "note": "程序时序检查、数据内部检查与历史公开时点认证是不同结论；本批次仍保留未认证项。",
    }


def capacity_diagnostics(quotes, trades):
    """Diagnostic only, using PRIOR turnover, never an execution-day fill input."""
    totals = {}
    for t in trades:
        key = (t["instrument"], t["date"])
        totals[key] = totals.get(key, 0.0) + t["value"]
    ratios = []
    unknown = 0
    selected = quotes.loc[
        quotes.instrument.isin({k[0] for k in totals}),
        ["instrument", "date", "close", "volume", *(["amount"] if "amount" in quotes else [])],
    ]
    for instrument, part in selected.groupby("instrument", sort=False):
        part = part.sort_values("date")
        turnover = part["amount"] if "amount" in part else part.volume * part.close
        prior = turnover.shift(1).rolling(20, min_periods=20).mean()
        for date, mean in zip(part.date, prior, strict=True):
            value = totals.get((instrument, date.strftime("%Y-%m-%d")))
            if value is None:
                continue
            if not np.isfinite(mean) or mean <= 0:
                unknown += 1
            else:
                ratios.append(value / mean)
    return {
        "lookback": 20,
        "valid_stock_days": len(ratios),
        "missing_lookback_stock_days": unknown,
        "max_prior_turnover_ratio": max(ratios, default=None),
        "above_ten_percent": sum(x > 0.1 for x in ratios),
        "above_full_prior_turnover": sum(x > 1.0 for x in ratios),
        "turnover_source": "reported_amount" if "amount" in quotes else "close_times_volume_proxy",
        "note": "当日标的总成交额／此前20交易日日均成交额，仅作容量风险诊断；未限制成交，不代表开盘竞价可成交量。",
    }


def add_execution_audit(result, quotes, factors):

    audit = copy.deepcopy(factors["metadata"].get("temporal_audit") or factor_temporal_audit(factors["metadata"]))
    audit["items"] += [
        {
            "key": "execution",
            "label": "日线成交规则",
            "status": "model_assumption",
            "detail": "按开盘价模拟，开盘涨停限制买入、跌停限制卖出，停牌禁止成交；已支持板块数量、单笔上限及当前日频策略的T+1。开盘价用于定量与成交是日线模型假设，未重建竞价队列、价格笼子和盘中流动性。",
        },
        {
            "key": "capacity",
            "label": "冲击成本与容量",
            "status": "not_modeled",
            "detail": "冲击成本按成交额固定比例扣费；单笔申报数量上限不等于市场容量。没有竞价量约束，较大资金结果不能直接认定可成交。",
        },
        {
            "key": "corporate_actions",
            "label": "分红与退市",
            "status": "model_assumption",
            "detail": "使用复权收益与等效持股，不是逐笔现金分红、到账和红利税账本；生命周期结束未卖出的持仓按最后可得价冻结估值，尚未模拟退市清算。",
        },
    ]
    audit["capacity_diagnostics"] = capacity_diagnostics(quotes, result["trades"])
    result["metadata"]["temporal_audit"] = audit
    result["metadata"]["market_rule_version"] = RULE_VERSION
    result["metadata"]["point_in_time_certified"] = False
