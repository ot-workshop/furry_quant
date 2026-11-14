"""Read-only factor coverage at a research run's first signal date."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from finance_quant.application import market_download
from finance_quant.application.historical_workflow import load_research_input
from finance_quant.application.research_requests import _fields, snapshot_registry
from finance_quant.configuration import BacktestConfig, DataConfig
from finance_quant.data.financials import FinancialBinding, FinancialStore, financial_root
from finance_quant.data.historical_warehouse import warehouse_option
from finance_quant.data.market_universe import market_option, market_roots
from finance_quant.data.symbols import from_qlib, to_qlib
from finance_quant.research.factor_library import FACTOR_LIBRARY
from finance_quant.research.fundamentals import (
    MAX_REPORT_AGE_DAYS,
    FundamentalData,
    fundamental_events,
    load_fundamentals,
)
from finance_quant.research.market_factors import calculate_factor_values
from finance_quant.research.registry import FactorConfig, FactorSpec

LABELS = {
    "missing_market": "缺少该信号日行情",
    "history": "历史窗口不足或滚动样本无效",
    "suspended": "当日停牌",
    "valuation": "估值字段缺失或非正值",
    "no_financials": "尚无财务数据版本",
    "report_unavailable": "当时没有可用财报",
    "stale_report": "当时可用财报已过期",
    "financial_inputs": "财报必要字段或TTM历史无效",
}


def _source(data, data_root):
    if not isinstance(data, dict):
        raise ValueError("请选择股票池")
    mode = data.get("mode")
    if mode == "snapshot":
        _fields(data, ("mode", "snapshot_id", "codes"), ("filters",), label="股票池")
        snapshot = snapshot_registry(data_root).get(data["snapshot_id"])
        if snapshot is None:
            raise ValueError("行情快照不可用，请重新选择")
        market, _, calendar, manifest = snapshot
        root = Path(manifest["snapshot_dir"])
        codes = DataConfig(root, manifest["start"], manifest["end"], data["codes"]).codes
        if not set(codes) <= set(manifest["codes"]):
            raise ValueError("所选股票不在该行情快照中")
        market = market.loc[market.instrument.isin({to_qlib(code) for code in codes})].copy()
        return root, (market, calendar, manifest), codes
    if mode not in {"index_warehouse", "market_warehouse"}:
        raise ValueError("不支持的股票池类型")
    _fields(
        data,
        ("mode", "warehouse_id"),
        ("filters", "codes") if mode == "market_warehouse" else ("filters",),
        label="股票池",
    )
    roots = market_roots(data_root) if mode == "market_warehouse" else market_download.warehouse_roots(data_root)
    describe = market_option if mode == "market_warehouse" else warehouse_option
    for root in roots:
        option = describe(root)
        if option and option["id"] == data["warehouse_id"]:
            if data.get("codes") is not None:
                codes = DataConfig(root, option["data_start"], option["end"], data["codes"]).codes
                if not set(codes) <= set(option["codes"]):
                    raise ValueError("所选股票不在该行情版本中")
            return root, None, None
    raise ValueError("行情版本不可用，请重新选择")


def _inputs(document, root, snapshot, codes, config, backtest):
    data = document["data"]
    dynamic = data["mode"] != "snapshot" or "filters" in data
    if dynamic:
        market, _, sessions, meta = load_research_input(document, root, config, backtest)
        return market, sessions, meta, True
    market, calendar, manifest = snapshot
    sessions = pd.DatetimeIndex(calendar.loc[calendar.is_open, "date"])
    beginning = pd.Timestamp(backtest.start)
    if beginning not in sessions or not pd.Timestamp(manifest["start"]) <= beginning <= pd.Timestamp(manifest["end"]):
        raise ValueError("回测起点须为行情范围内的交易日")
    position = sessions.get_loc(beginning)
    if position == 0:
        raise ValueError("回测起点之前没有信号交易日，请延后起点")
    day = sessions[position - 1]
    market = market.loc[market.date.le(day)].copy()
    return market, sessions, {"id": manifest["id"], "initial_date": day.strftime("%Y-%m-%d"), "codes": codes}, False


def _financials(document, data_root, market, required):
    if not required:
        return None, None
    store = FinancialStore(financial_root(data_root))
    manifest = store.version(document["financial_version"]) if document.get("financial_version") else store.current()
    codes = tuple(from_qlib(code) for code in market.instrument.unique())
    if manifest is None:
        return FundamentalData("unavailable", fundamental_events(pd.DataFrame()), (), len(codes)), None
    return load_fundamentals(FinancialBinding(store.root, manifest["id"]), codes), manifest["id"]


def _reason(row, spec, day, financial_version, dynamic):
    if dynamic and bool(row.get("is_suspended", False)):
        return "suspended"
    if dynamic and not bool(row.get("eligible_history", False)):
        return "history"
    if FACTOR_LIBRARY[spec.name].fundamental:
        if financial_version is None:
            return "no_financials"
        period = row.get("financial_report_period")
        if pd.isna(period):
            return "report_unavailable"
        if (pd.Timestamp(day) - period).days > MAX_REPORT_AGE_DAYS:
            return "stale_report"
        return "financial_inputs"
    return "valuation" if spec.name in {"value", "book_to_price", "size"} else "history"


def factor_coverage(document, data_root):
    _fields(document, ("data", "start", "factor"), ("financial_version",), label="因子覆盖查询")
    _fields(document["factor"], ("name",), ("window", "skip", "baseline_window", "id"), label="因子")
    spec = FactorSpec(**document["factor"], weight=1.0)
    config = FactorConfig((spec,))
    backtest = BacktestConfig(document["start"], document["start"], topk=1, n_drop=0)
    root, snapshot, codes = _source(document["data"], Path(data_root))
    market, sessions, meta, dynamic = _inputs(document, root, snapshot, codes, config, backtest)
    day = meta["initial_date"]
    # Exclude execution-day rows even from intermediate calculations.
    market = market.loc[market.date.le(day)].copy()
    financials, version = _financials(document, data_root, market, FACTOR_LIBRARY[spec.name].fundamental)
    frame, _, _ = calculate_factor_values(
        market, config, dynamic_context=meta if dynamic else None, exchange_calendar=sessions, financials=financials
    )
    section = frame.loc[frame.date.eq(day)].copy()
    if dynamic:
        section = section.loc[section.selection_member]
    total = meta["declared_universe"].get(day, 0) if dynamic else len(codes)
    valid = np.isfinite(section["factor_" + spec.key].to_numpy(float))
    if dynamic:
        valid &= section.eligible_history.to_numpy(bool) & ~section.is_suspended.to_numpy(bool)
    reasons: Counter[str] = Counter()
    examples: list[dict] = []
    if total > len(section):
        reasons["missing_market"] = total - len(section)
    for (_, row), included in zip(section.iterrows(), valid, strict=True):
        if not included:
            reason = _reason(row, spec, day, version, dynamic)
            reasons[reason] += 1
            if len(examples) < 5:
                examples.append({"code": from_qlib(row.instrument), "name": row["name"], "reason": LABELS[reason]})
    notes = [
        "仅统计该信号日；区间内的成分、财报和有效数量会变化。",
        "组合评分要求所有已选因子共同有效，最终可评分数量可能更少。",
    ]
    if spec.name == "gross_margin_ttm":
        notes.append("银行、证券、保险等企业可能没有通用营业成本字段，毛利率缺失可能集中于金融行业。")
    elif spec.name == "value":
        notes.append("非正PE不参与评分，样本会偏向盈利公司。")
    elif spec.name == "book_to_price":
        notes.append("非正PB和缺失估值不参与评分，样本可能偏向正净资产公司。")
    if FACTOR_LIBRARY[spec.name].fundamental:
        notes.append("缺少必要字段、TTM历史或当时可用的报告会被排除；财报历史修订时点未完全认证。")
    if reasons:
        notes.append("缺失值不会补成零；样本减少可能改变行业分布、因子相关性和回测结果。")
    return {
        "as_of": day,
        "execution_date": backtest.start,
        "data_version": meta["id"],
        "financial_version": version,
        "factor": spec.as_dict(),
        "total": total,
        "available": int(valid.sum()),
        "unavailable": total - int(valid.sum()),
        "reasons": [{"code": key, "label": LABELS[key], "count": count} for key, count in reasons.items()],
        "examples": examples,
        "notes": notes,
    }
