"""Validate and pin the data and filters of a dynamic market research request."""

from pathlib import Path

import pandas as pd

from finance_quant.application.research_readiness import validate_warmup
from finance_quant.data.industries import optional_industries
from finance_quant.data.market_universe import market_header, market_option, market_roots
from finance_quant.research.universe import parse_filters


def validate_pool_filters(value, option, backtest, factors, calendar):
    filters = parse_filters(value)
    if filters.exclude_st and option.get("st_coverage", {}).get("covered", 0) < 2:
        raise ValueError("历史名称记录不足两只，无法进行ST筛选；请在数据管理更新该范围的历史状态，或明确关闭ST筛选")
    if filters.min_listing_days and option.get("listing_coverage", {}).get("covered", 0) < 2:
        raise ValueError("历史上市日期不足两只，无法进行上市天数筛选；请更新该数据范围，或将上市天数设为0")
    if filters.industries:
        industry = optional_industries(filters.industry_version)
        if industry is None:
            raise ValueError("行业筛选需要历史行业数据，请先在数据管理下载")
        industry.validate_range(backtest.end)
        industry.validate_level(filters.industry_level)
        known = set(industry.frame.loc[industry.frame.level.eq(filters.industry_level), "industry_code"])
        if set(filters.industries) - known:
            raise ValueError("筛选行业代码不在所选历史行业版本中")
        filters = parse_filters({**filters.as_dict(), "industry_version": industry.manifest["id"]})
    sessions = pd.DatetimeIndex(calendar.loc[calendar.is_open, "date"])
    required = (
        max(
            max(spec.lookback for spec in factors.specs),
            filters.amount_window - 1 if filters.min_avg_amount is not None else 0,
        )
        + 1
    )
    if ((sessions >= pd.Timestamp(option["data_start"])) & (sessions < pd.Timestamp(backtest.start))).sum() < required:
        raise ValueError(f"股票池筛选需要至少{required}个回测前交易日，请补充历史数据或延后起点")
    return filters.as_dict()


def pool_industry_display(data, neutral_extra, neutralization):
    filters = data.get("filters", {})
    pinned = filters.get("industry_version") if filters.get("industries") else None
    if neutralization.mode == "off" and pinned:
        return {"neutralization": {"mode": "off", "industry_version": pinned}}
    if not neutral_extra and neutralization.allow_latest and (industry := optional_industries()):
        return {"neutralization": {"mode": "off", "industry_version": industry.manifest["id"]}}
    return neutral_extra


def validate_market_request(document, data_root, backtest, factors, evaluation):
    if not {"mode", "warehouse_id"}.issubset(document) or set(document) - {"mode", "warehouse_id", "filters", "codes"}:
        raise ValueError("动态股票池须选择行情版本并配置筛选条件")
    root = next(
        (
            path
            for path in market_roots(data_root)
            if (option := market_option(path)) and option["id"] == document["warehouse_id"]
        ),
        None,
    )
    if root is None:
        raise ValueError("股票池行情版本不存在或已变化，请刷新数据列表")
    option = market_option(root)
    _, calendar, digest = market_header(root)
    codes = document.get("codes")
    if codes is not None:
        if (
            not isinstance(codes, list)
            or any(not isinstance(code, str) for code in codes)
            or len(set(codes)) < 2
            or len(codes) != len(set(codes))
            or set(codes) - set(option["codes"])
        ):
            raise ValueError("自选股票须至少两只、不重复，且位于所选行情覆盖范围内")
        option = {**option, "codes": sorted(codes), "codes_count": len(codes)}
        from_coverage = option["security_by_code"]
        for key in ("st_coverage", "listing_coverage"):
            option[key] = {"covered": sum(from_coverage[code][key] for code in codes), "total": len(codes)}
    filters = (
        validate_pool_filters(document["filters"], option, backtest, factors, calendar)
        if "filters" in document
        else None
    )
    if not option["start"] <= backtest.start <= backtest.end <= option["end"]:
        raise ValueError("回测区间超出股票池行情范围")
    sessions = pd.DatetimeIndex(calendar.loc[calendar.is_open, "date"])
    if pd.Timestamp(backtest.start) not in sessions or pd.Timestamp(backtest.end) not in sessions:
        raise ValueError("回测起止日期必须为交易日")
    if len(sessions[(sessions >= backtest.start) & (sessions <= backtest.end)]) <= max(evaluation.horizons):
        raise ValueError("回测交易日数须超过最长评估持有期")
    if backtest.topk > min(300, option["codes_count"]) or backtest.initial_cash > 1e12:
        raise ValueError("目标持股不能超过行情覆盖或300只，资金不超过一万亿元")
    validate_warmup(option["data_start"], option["end"], backtest.start, factors, calendar)
    return (
        {
            "mode": "market_warehouse",
            "warehouse_id": option["id"],
            **({"filters": filters} if filters is not None else {}),
            **({"codes": sorted(codes)} if codes is not None else {}),
        },
        {"mode": "market_warehouse", "warehouse_dir": str(Path(root).resolve()), "manifest_sha256": digest},
        option,
    )
