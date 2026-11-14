"""Causal factor calculation followed by common-universe percentile scoring."""

from __future__ import annotations

import pandas as pd

from finance_quant.data.temporal_audit import factor_temporal_audit, validate_valuation_times
from finance_quant.research.factor_library import FACTOR_LIBRARY, describe_spec
from finance_quant.research.factor_sections import exclusion_rows, rank_section, section_eligibility
from finance_quant.research.fundamentals import attach_fundamentals
from finance_quant.research.neutralization import (
    NeutralizationConfig,
)
from finance_quant.research.neutralization import (
    industry_context as load_industry_context,
)
from finance_quant.research.registry import FACTOR_DIRECTIONS, FACTOR_DISPLAY_NAMES, FactorConfig

MARKET_WEIGHTS = FactorConfig().weights


def _direction_text(config):
    higher = [FACTOR_DISPLAY_NAMES[s.name] for s in config.specs if FACTOR_DIRECTIONS[s.name] == "higher"]
    lower = [FACTOR_DISPLAY_NAMES[s.name] for s in config.specs if FACTOR_DIRECTIONS[s.name] == "lower"]
    parts = ([f"{'和'.join(higher)}越高得分越高"] if higher else []) + (
        [f"{'和'.join(lower)}越低得分越高"] if lower else []
    )
    return "；".join(parts) + "。分数不是上涨概率。"


def _prepare_frame(market, config, dynamic_context, exchange_calendar, financials):
    keep = {
        "instrument",
        "date",
        "name",
        "industry",
        "close",
        "adj_factor",
        "pe_ttm",
        "pb",
        "total_mv",
        "amount",
        "is_suspended",
        "valuation_asof",
        "valuation_carried",
        "selection_member",
        "eligible_history",
        "pe_ttm_asof",
        "pb_asof",
        "total_mv_asof",
        "valuation_available_at",
        "pe_ttm_available_at",
        "pb_available_at",
        "total_mv_available_at",
    }
    frame = (
        market[[column for column in market.columns if column in keep]]
        .sort_values(["instrument", "date"])
        .reset_index(drop=True)
        .copy()
    )
    if frame.empty or frame.duplicated(["instrument", "date"]).any():
        raise ValueError("Market factor data must be nonempty and unique")
    if not pd.api.types.is_datetime64_any_dtype(frame.date):
        raise ValueError("Factor dates must be exchange-local dates")
    calendar = pd.DatetimeIndex(sorted(frame.date.unique()))
    actual_calendar = pd.DatetimeIndex(exchange_calendar) if exchange_calendar is not None else calendar
    if any(
        not pd.DatetimeIndex(section.date).equals(
            actual_calendar[(actual_calendar >= section.date.min()) & (actual_calendar <= section.date.max())]
            if dynamic_context is not None
            else calendar
        )
        for _, section in frame.groupby("instrument")
    ):
        raise ValueError("因子输入股票池存在交易日缺口，拒绝缩短滚动窗口")
    fundamental = any(FACTOR_LIBRARY[spec.name].fundamental for spec in config.specs)
    if fundamental:
        if financials is None:
            raise ValueError("所选基本面因子需要财务数据：请先在“数据管理 → 财务数据”下载后重新提交")
        frame = attach_fundamentals(frame, financials.events)
    required = {field for spec in config.specs for field in FACTOR_LIBRARY[spec.name].required_fields}
    if missing := required - set(frame):
        raise ValueError("所选因子缺少数据字段：" + "、".join(sorted(missing)))
    validate_valuation_times(frame, required)
    first_factor = frame.groupby("instrument").adj_factor.transform("first")
    frame["adjusted_close"] = frame.close * frame.adj_factor / first_factor
    frame["daily_return"] = frame.groupby("instrument").adjusted_close.pct_change(fill_method=None)
    return frame, actual_calendar, fundamental


def calculate_factor_values(market, config, *, dynamic_context=None, exchange_calendar=None, financials=None):
    """Calculate causal raw values; callers decide which cross-section to score or inspect."""
    frame, calendar, fundamental = _prepare_frame(market, config, dynamic_context, exchange_calendar, financials)
    for spec in config.specs:
        frame["factor_" + spec.key] = FACTOR_LIBRARY[spec.name].calculator(frame, spec)
    return frame, calendar, fundamental


def market_factors(
    market: pd.DataFrame,
    data_version: str,
    config: FactorConfig | None = None,
    *,
    dynamic_context: dict | None = None,
    exchange_calendar=None,
    row_sink=None,
    neutralization=None,
    industry_history=None,
    financials=None,
) -> dict:
    """Score a market frame.

    ``financials`` is the :class:`~finance_quant.research.fundamentals.FundamentalData`
    of the pinned financial version; it is required exactly when a fundamental factor
    is selected.
    """
    config = FactorConfig() if config is None else config
    if not isinstance(config, FactorConfig):
        raise ValueError("config must be a FactorConfig")
    neutralization = neutralization or NeutralizationConfig()
    industry_history = load_industry_context(neutralization, industry_history)
    if neutralization.mode == "industry":
        industry_history.validate_range(market.date.max())
    industry_days = {}
    frame, actual_calendar, fundamental = calculate_factor_values(
        market, config, dynamic_context=dynamic_context, exchange_calendar=exchange_calendar, financials=financials
    )
    specs = {spec.key: spec for spec in config.specs}
    descriptors = {key: describe_spec(spec) for key, spec in specs.items()}
    history: list[dict]
    diagnostics: list[dict]
    history, diagnostics, coverage, availability = [], [], {}, {}
    universe = int(frame.instrument.nunique())
    history_count = 0
    as_of = None
    available_dates = []
    for date, section in frame.groupby("date", sort=True):
        day = date.strftime("%Y-%m-%d")
        if row_sink is not None:
            history, diagnostics = [], []
        if dynamic_context is not None:
            if day not in dynamic_context["declared_universe"]:
                continue
            universe = dynamic_context["declared_universe"][day]
            section = section.loc[section.selection_member].copy()
        eligible, assigned, daily_availability, industry_coverage = section_eligibility(
            section,
            date,
            specs,
            universe,
            dynamic=dynamic_context is not None,
            industry_history=industry_history,
            actual_calendar=actual_calendar,
            neutralization=neutralization,
        )
        availability[day] = daily_availability
        if industry_coverage is not None:
            industry_days[day] = industry_coverage
        diagnostics.extend(
            exclusion_rows(
                section,
                eligible,
                specs,
                day=day,
                assigned=assigned,
                neutralization=neutralization,
                dynamic=dynamic_context is not None,
            )
        )
        if row_sink is not None:
            row_sink("diagnostics", diagnostics)
        section = section.loc[eligible]
        if len(section) < 2:
            continue
        ranked = rank_section(section, date, specs, assigned=assigned, neutralization=neutralization)
        if neutralization.mode == "industry":
            industry_days[day].update(ranked.industry)
        if ranked.excluded:
            coverage[day] = {"eligible": 0, "universe": universe}
            availability[day]["eligible"] = 0
            if row_sink is None:
                diagnostics.extend(ranked.excluded)
            else:
                row_sink("diagnostics", ranked.excluded)
            continue
        history.extend(ranked.rows)
        coverage[day] = {"eligible": len(section), "universe": universe}
        history_count += len(section)
        as_of = day
        available_dates.append(day)
        if row_sink is not None:
            row_sink("history", history)
    if dynamic_context is not None and "universe_filters" in dynamic_context:
        for day, declared in dynamic_context["declared_universe"].items():
            availability.setdefault(
                day,
                {
                    "universe": declared,
                    "eligible": 0,
                    "factors": {key: {"valid": 0, "missing": declared} for key in specs},
                },
            )
            coverage.setdefault(day, {"eligible": 0, "universe": declared})
    if not history_count:
        if dynamic_context is not None and "universe_filters" in dynamic_context:
            raise ValueError(
                "当前区间筛选后没有足够的有效股票：评分至少需要两只。请检查ST历史覆盖、上市天数、市值、成交额、行业和因子预热条件"
            )
        last = availability[max(availability)]
        counts = "、".join(
            f"{descriptors[key]['label']}({item['valid']}/{universe})" for key, item in last["factors"].items()
        )
        if neutralization.mode == "industry":
            raise ValueError("行业中性化后无有效区分：请检查历史行业覆盖，或扩大股票池使行业内至少有两只有效股票")
        raise ValueError("有效评分不足：" + counts + "；请检查窗口预热、缺失字段与负收益样本")
    result: dict = {
        "metadata": {
            "id": "unpublished",
            "name": "真实行情：" + "、".join(FACTOR_DISPLAY_NAMES[s.name] for s in config.specs),
            "as_of": as_of,
            "available_dates": available_dates,
            "universe": f"用户固定A股池（{universe}只）",
            "sample_size": universe,
            "universe_members": frame.drop_duplicates("instrument")[["instrument", "name", "industry"]].to_dict(
                "records"
            ),
            "scheme": config.scheme,
            "factor_config": config.as_list(),
            "factor_definitions": descriptors,
            "data_version": data_version,
            "is_synthetic": False,
            "weights": config.weights,
            "coverage_by_date": coverage,
            "availability_by_date": availability,
            "score_unit": "percentile_0_100",
            "direction": _direction_text(config),
            "signal_uses_close": True,
            "definitions": {key: desc["formula"] for key, desc in descriptors.items()},
            "availability_policy": "假设日线与每日估值在当日18:00可用，仅用于下一交易日开盘；供应商历史发布时间未审计",
            "notes": [
                "各因子使用当日及以前数据，不补未来值；按所有入选因子均有效的交集排名，不按缺失项偷偷重分权重",
                "缺失和预热不足的股票保留逐因子原因与覆盖率；PB/市值/PE历史修订及发布时间未独立认证",
                "行业仅作展示；当前没有动态权重",
            ],
        },
        "history": history if row_sink is None else [],
        "diagnostics": diagnostics if row_sink is None else [],
    }
    if fundamental:
        result["metadata"]["financial_data"] = financials.metadata(frame.instrument.unique())
        result["metadata"]["notes"].append(
            "基本面因子由三张财务报表自算（TTM口径）；"
            + financials.metadata(frame.instrument.unique())["policy"]
            + "；仍非严格PIT认证；财务数据缺失或过期的股票不参与排名",
        )
    result["metadata"]["neutralization"] = neutralization.as_dict()
    if industry_history:
        result["metadata"]["point_in_time_certified"] = False
        result["metadata"]["industry_data"] = {
            k: industry_history.manifest[k] for k in ["id", "source", "policy", "point_in_time_certified"]
        }
        result["metadata"]["industry_data"]["data_asof"] = industry_history.data_asof
        result["metadata"]["industry_data"]["level"] = neutralization.industry_level
        result["metadata"]["industry_data"]["level_label"] = {
            "L1": "一级",
            "L2": "二级",
            "L3": "三级",
        }[neutralization.industry_level]
        result["metadata"]["industry_coverage_by_date"] = industry_days
        result["metadata"]["notes"][-1] = (
            f"历史行业按保存版本的{result['metadata']['industry_data']['level_label']}行业与信号日区间匹配；"
            "实际公告时点未认证。"
        )
    if neutralization.mode == "industry":
        result["metadata"]["notes"] += [
            "方向统一后按当日截面1%/99%缩尾，减去行业均值，再以截面残差标准差标准化并计算百分位；保留处理前评分；缺失/冲突行业不参与评分",
            "单股票行业残差为0；行业中性化不保证持仓行业权重中性，不设行业持仓上限",
        ]
    if dynamic_context is not None:
        result["metadata"].update(
            universe="沪深300历史动态成分（月度快照下一交易日口径）",
            sample_size=int(frame.instrument.nunique()),
            membership_policy=dynamic_context["membership_policy"],
            point_in_time_certified=False,
            lifecycle_exclusions=dynamic_context["lifecycle_exclusions"],
            membership_by_execution_date=dynamic_context["membership_by_execution_date"],
        )
        result["metadata"]["notes"] += [
            "调入前历史仅用于预热，排名仅用信号日已知、下一交易日生效的成分名单；精确公告时点未认证",
            "停牌和历史不足不参与排名，保留覆盖原因；调出持仓由回测持续尝试退出，不能按未持仓处理",
        ]
        if "universe_filters" in dynamic_context:
            for key in ("universe_kind", "universe_filters", "pool_coverage_by_date", "st_policy"):
                result["metadata"][key] = dynamic_context[key]
            result["metadata"]["universe"] = dynamic_context["universe_label"]
            result["metadata"].pop("membership_by_execution_date", None)
            result["metadata"].pop("lifecycle_exclusions", None)
            result["metadata"]["notes"][-2] = (
                "按信号日历史生命周期和配置筛选；财务和行业使用已绑定的历史版本，候选不足时保留现金。"
            )
    result["metadata"]["temporal_audit"] = factor_temporal_audit(result["metadata"])
    return result
