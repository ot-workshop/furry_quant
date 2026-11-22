"""Reviewable comparison drafts and server-side validation of MILP provenance."""

from __future__ import annotations

import math

from finance_quant.application.selection_context import checked_selection, factor_configs, source_context
from finance_quant.research.neutralization import parse_neutralization


def selection_draft(store, reference, data_root, runs_root):
    selection = checked_selection(store, reference)
    context = source_context(store, reference["evaluation_id"], data_root, runs_root)
    baseline, selected = factor_configs(context["evaluation"], selection)
    cutoff = selection["parameters"]["cutoff"]
    dates = [
        day
        for day in context["option"].get("trading_dates", [])
        if day > cutoff and day >= context["option"]["start"] and day <= context["option"]["end"]
    ]
    horizon = selection["parameters"]["horizon"]
    if len(dates) < 3 * (horizon + 1):
        raise ValueError(
            f"推荐截止日之后至少需要 {3 * (horizon + 1)} 个交易日才能分成三段；当前只有 {len(dates)} 个。请提前截止日或补充后续行情"
        )
    boundary = len(dates) // 3
    periods = {
        "development": {"start": dates[0], "end": dates[boundary - 1]},
        "validation": {"start": dates[boundary], "end": dates[2 * boundary - 1]},
        "test": {"start": dates[2 * boundary], "end": dates[-1]},
    }
    backtest = {**context["backtest"], "start": dates[0], "end": dates[-1]}
    request = {
        "name": "因子筛选对照",
        "data": context["data"],
        "backtest": backtest,
        "factors": selected.as_list(),
        "evaluation": {**context["evaluation"]["metadata"]["evaluation"], "horizons": [horizon]},
        "neutralization": context["neutralization"],
    }
    candidates = [{"id": "milp", "name": "MILP 等权组合", "factors": selected.as_list()}]
    if selected.as_list() != baseline.as_list():
        candidates.append({"id": "baseline", "name": "原因子组合", "factors": baseline.as_list()})
    warnings = ["所有比较分段均从推荐截止日之后开始，来源数据、筛选条件和行业设置保持固定；交易参数可以调整。"]
    if len(candidates) == 1:
        warnings.append("推荐组合与原组合相同，请另添一个不同的候选方案后提交比较。")
    return {
        "research": request,
        "candidates": candidates,
        "periods": periods,
        "reference": selection["reference"],
        "selection": selection,
        "warnings": warnings,
        "financial_data": context["financial_data"],
        "financial_coverage": context["option"].get("financial_coverage"),
        "industry_data": context["industry_data"],
        "financials": context["financials"].as_dict() if context["financials"] else None,
    }


def selection_validation(store, document, plan, data_root, runs_root, *, frozen_snapshot=None):
    reference = document.get("factor_selection")
    if reference is None:
        return None
    selection = checked_selection(store, reference)
    context = source_context(store, reference["evaluation_id"], data_root, runs_root, frozen_snapshot=frozen_snapshot)
    _, selected = factor_configs(context["evaluation"], selection)
    cutoff = selection["parameters"]["cutoff"]
    if any(period.start <= cutoff for period in plan.periods):
        raise ValueError("因子筛选后的开发、验证、测试区间必须全部晚于推荐截止日")
    candidate = next((candidate for candidate in plan.candidates if candidate.id == "milp"), None)
    if candidate is None:
        raise ValueError("比较计划缺少来源筛选的 MILP 候选方案")
    actual = candidate.factors.as_list()
    expected = selected.as_list()
    if len(actual) != len(expected):
        raise ValueError("推荐组合已修改，请重新筛选后创建比较")
    for first, second in zip(actual, expected, strict=True):
        if {key: value for key, value in first.items() if key != "weight"} != {
            key: value for key, value in second.items() if key != "weight"
        } or not math.isclose(first["weight"], second["weight"], abs_tol=1e-8):
            raise ValueError("推荐组合的因子、窗口或等权权重已修改，请重新筛选")
    return {"selection": selection, "context": context}


def validate_selection_inputs(bound, research):
    if bound is None:
        return
    context = bound["context"]
    if research["data"] != context["data"]:
        raise ValueError("比较股票池或筛选条件与来源评估不一致，请回到相关性页面重新筛选")
    if parse_neutralization(research.get("neutralization")).as_dict() != context["neutralization"]:
        raise ValueError("比较的行业处理与来源评估不一致，请重新筛选")


def verify_queued_selection(store, evidence):
    if evidence and store.digest("evaluations", evidence["evaluation_id"]) != evidence["source_sha256"]:
        raise ValueError("排队期间来源因子评估发生变化，未运行比较，请重新筛选")
