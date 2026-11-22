"""Causal daily sample intersections for retrospective return controls."""

from dataclasses import replace

import pandas as pd

from finance_quant.application.historical_workflow import load_research_input
from finance_quant.provenance import fingerprint
from finance_quant.research.fundamentals import fundamentals_for
from finance_quant.research.market_factors import market_factors
from finance_quant.research.neutralization import NeutralizationConfig, industry_context


def candidate_config(config, candidate):
    override = candidate.neutralization or config.neutralization
    version = config.neutralization.industry_version
    if override.industry_version and override.industry_version != version:
        raise ValueError("所有方案必须使用同一历史行业版本")
    neutral = NeutralizationConfig(override.mode, version, override.industry_level)
    return replace(config, factors=candidate.factors, neutralization=neutral)


def validate_comparison_inputs(config, plan, request, *, expected_sha=None, frozen_snapshot=None):
    """Check calendars and warmup here; expensive sample scoring belongs to the worker."""
    for candidate in plan.candidates:
        candidate_config(config, candidate)
        _, _, sessions, _ = load_research_input(
            request,
            config.data.root,
            candidate.factors,
            config.backtest,
            expected_sha=expected_sha,
            frozen_snapshot=frozen_snapshot,
        )
        start, end = (
            sessions.get_loc(pd.Timestamp(config.backtest.start)),
            sessions.get_loc(pd.Timestamp(config.backtest.end)),
        )
        if not start or end - start + 1 <= max(config.evaluation.horizons):
            raise ValueError("对照区间需要信号预热，且交易日数须超过最长评估持有期")


def _sample(config, candidate, request, expected_sha, allowed=None):
    current = candidate_config(config, candidate)
    market, _, sessions, meta = load_research_input(
        request, config.data.root, candidate.factors, config.backtest, expected_sha=expected_sha
    )
    members: dict[str, set[str]] = {}

    def capture(path, rows):
        if path == "history":
            for row in rows:
                members.setdefault(row["date"], set()).add(row["instrument"])

    try:
        market_factors(
            market,
            meta["id"],
            candidate.factors,
            dynamic_context=meta,
            exchange_calendar=sessions,
            row_sink=capture,
            neutralization=current.neutralization,
            industry_history=industry_context(current.neutralization),
            financials=fundamentals_for(config.financials, candidate.factors, market),
            scoring_universe=allowed,
        )
    except ValueError as exc:
        raise ValueError(f"方案 {candidate.name} 无法计算评分：{exc}") from None
    return members, sessions, meta


def prepare_comparison(config, plan, request, *, expected_sha=None):
    first = plan.candidates[0]
    members, sessions, meta = _sample(config, first, request, expected_sha)
    common = {day: set(values) for day, values in members.items()}
    counts = {first.id: {day: len(values) for day, values in members.items()}}
    for candidate in plan.candidates[1:]:
        members, sessions, meta = _sample(config, candidate, request, expected_sha)
        for day in common:
            common[day].intersection_update(members.get(day, ()))
        counts[candidate.id] = {day: len(values) for day, values in members.items()}
    start = sessions.get_loc(pd.Timestamp(config.backtest.start))
    finish = sessions.get_loc(pd.Timestamp(config.backtest.end))
    if not start:
        raise ValueError("研究起点之前需要至少一个信号交易日")
    if finish - start + 1 <= max(config.evaluation.horizons):
        raise ValueError("研究区间的交易日数须超过最长评估持有期")
    days = sessions[start - 1 : finish].strftime("%Y-%m-%d").tolist()
    allowed = {day: sorted(common.get(day, ())) for day in days}
    if plan.comparison.sample_mode == "common":
        if not any(len(values) >= 2 for values in allowed.values()):
            raise ValueError("各方案没有至少两只股票的共同可评分样本，请扩大股票池或调整配置")
        # Re-ranking on the intersection can remove an entire day if industry
        # residuals have no variation. Such a day must be removed for EVERY plan.
        for candidate in plan.candidates:
            members, _, _ = _sample(config, candidate, request, expected_sha, allowed)
            for day in days:
                if set(members.get(day, ())) != set(allowed[day]):
                    allowed[day] = []
        if not any(len(values) >= 2 for values in allowed.values()):
            raise ValueError("共同样本在行业处理后没有有效区分，请扩大股票池或调整因子")
    daily = [
        {
            "date": day,
            "pool": meta["declared_universe"].get(day, 0),
            "common": len(allowed[day]),
            "candidates": {c.id: counts[c.id].get(day, 0) for c in plan.candidates},
        }
        for day in days
    ]
    return {
        "sample_days": daily,
        "allowed": allowed,
        "sample_sha256": fingerprint(allowed),
        "meta": meta,
        "trading_days": finish - start + 1,
    }
