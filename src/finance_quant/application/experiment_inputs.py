"""Split experiment calendars and causal candidate coverage checks."""

from __future__ import annotations

import pandas as pd

from finance_quant.application.historical_workflow import load_research_input
from finance_quant.research.experiments import ExperimentPlan
from finance_quant.research.fundamentals import fundamentals_for
from finance_quant.research.market_factors import market_factors
from finance_quant.research.neutralization import industry_context


def period_counts(config, plan, calendar):
    sessions = pd.DatetimeIndex(calendar.loc[calendar["is_open"], "date"])
    counts = {}
    previous_index = None
    all_decisions = set()
    for period in plan.periods:
        if period.start < config.backtest.start or period.end > config.backtest.end:
            raise ValueError("实验区间须位于回测区间之内")
        start, end = pd.Timestamp(period.start), pd.Timestamp(period.end)
        if start not in sessions or end not in sessions:
            raise ValueError("实验区间起止日期须为实际交易日")
        left, right = sessions.get_loc(start), sessions.get_loc(end)
        if left == 0:
            raise ValueError("开发区间开始前至少需要一个交易日用于生成首个信号")
        if previous_index is not None and left != previous_index + 1:
            raise ValueError("开发、验证、测试区间须首尾相接，中间不能留出交易日")
        previous_index = right
        counts[period.name] = right - left + 1
        if counts[period.name] <= max(config.evaluation.horizons):
            raise ValueError("每个区间的交易日数须超过最长评估持有期")
        all_decisions.update(sessions[left - 1 : right].strftime("%Y-%m-%d"))
    return counts, all_decisions


def preflight(config, plan: ExperimentPlan, market, calendar) -> dict[str, int]:
    counts, all_decisions = period_counts(config, plan, calendar)
    # Fair comparison: every scheme must score the entire declared universe on
    # every decision day. Reject silent filtering or insufficient warm-up early.
    universe = set(market["instrument"].unique())
    for candidate in plan.candidates:
        try:
            factors = market_factors(
                market,
                "preflight",
                candidate.factors,
                neutralization=config.neutralization,
                industry_history=industry_context(config.neutralization),
                financials=fundamentals_for(config.financials, candidate.factors, market),
            )
        except ValueError as exc:
            raise ValueError(f"候选方案 {candidate.id} 无法计算评分：{exc}") from None
        coverage: dict[str, set[str]] = {}
        for row in factors["history"]:
            if row["date"] in all_decisions:
                coverage.setdefault(row["date"], set()).add(row["instrument"])
        if any(coverage.get(day) != universe for day in all_decisions):
            raise ValueError(f"候选方案 {candidate.id} 在部分决策日缺少共同股票池评分；请检查历史预热与缺失值")
    return counts


def dynamic_preflight(config, plan, request, *, expected_sha=None, frozen_snapshot=None):
    """Check each candidate against its own history requirement and the same pool rules.

    Inactive/new/suspended members may legitimately be unscorable. Unlike a fixed
    pool, the union of all historical constituents is never required every day.
    """
    industry = industry_context(config.neutralization)
    coverage: dict[str, dict[str, dict[str, int]]] = {}
    metadata = None
    counts = None
    for candidate in plan.candidates:
        market, _, sessions, meta = load_research_input(
            request,
            config.data.root,
            candidate.factors,
            config.backtest,
            expected_sha=expected_sha,
            frozen_snapshot=frozen_snapshot,
        )
        calendar = pd.DataFrame({"date": sessions, "is_open": True})
        counts, _ = period_counts(config, plan, calendar)
        factors = market_factors(
            market,
            meta["id"],
            candidate.factors,
            dynamic_context=meta,
            exchange_calendar=sessions,
            neutralization=config.neutralization,
            industry_history=industry,
            financials=fundamentals_for(config.financials, candidate.factors, market),
        )
        coverage[candidate.id] = {}
        for period in plan.periods:
            left, right = sessions.get_loc(pd.Timestamp(period.start)), sessions.get_loc(pd.Timestamp(period.end))
            signals = sessions[left - 1 : right].strftime("%Y-%m-%d")
            available = factors["metadata"]["coverage_by_date"]
            eligible = [available.get(day, {}).get("eligible", 0) for day in signals]
            declared = [meta["declared_universe"].get(day, 0) for day in signals]
            if not any(value >= 2 for value in eligible):
                label = {"development": "开发", "validation": "验证", "test": "测试"}[period.name]
                raise ValueError(f"候选方案 {candidate.id} 在{label}区间没有可用评分，请检查筛选条件与预热")
            coverage[candidate.id][period.name] = {
                "signal_days": len(signals),
                "scored_days": sum(value >= 2 for value in eligible),
                "min_pool": min(declared),
                "max_pool": max(declared),
                "min_scored": min(eligible),
                "max_scored": max(eligible),
            }
        metadata = meta
    return metadata, counts, coverage
