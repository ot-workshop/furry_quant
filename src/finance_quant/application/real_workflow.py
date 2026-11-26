"""Run native Qlib against a validated fixed-pool snapshot."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from finance_quant.application.benchmark_workflow import publish_benchmark_view
from finance_quant.application.factor_workflow import load_configured_snapshot, publish_factor_run
from finance_quant.checksums import sha256_text
from finance_quant.data.benchmarks import normalize_benchmark
from finance_quant.data.execution_universe import validate_execution_universe
from finance_quant.data.industries import load_industries
from finance_quant.data.market import FrozenMarketSnapshot, load_market
from finance_quant.data.symbols import to_qlib
from finance_quant.integrations.qlib_runner import run_qlib
from finance_quant.provenance import source_provenance
from finance_quant.research.execution_policy import EXECUTION_FIELDS
from finance_quant.research.fundamentals import financial_provenance
from finance_quant.research.industry_exposure import attach_industry_exposure
from finance_quant.research.registry import FactorConfig
from finance_quant.storage import ResultStore


def financial_limitation(factor_metadata: dict) -> str:
    """Result limitation describing whether, and how, financial statements were used."""
    financial = factor_metadata.get("financial_data")
    if not financial:
        return "未使用财报；每日估值的历史修订与实际发布时间仍待审计"
    return (
        f"财报因子使用财务数据版本 {financial['version']}（覆盖 {financial['covered']}/{financial['universe']} 只）："
        "按公告日后下一交易日可见，供应商未留痕的历史更正无法还原；每日估值的历史修订与实际发布时间仍待审计"
    )


def adjusted_quotes(market: pd.DataFrame, benchmark: pd.DataFrame) -> pd.DataFrame:
    frame = market.sort_values(["instrument", "date"]).copy()
    frame["factor"] = frame["adj_factor"] / frame.groupby("instrument")["adj_factor"].transform("first")
    for field in ["open", "close", "high", "low", "up_limit", "down_limit"]:
        frame[field] = frame[field] * frame["factor"]
    frame["volume"] = frame["volume"] / frame["factor"]
    # Qlib needs change for bookkeeping, but execution limits use open/limit-price expressions.
    frame["change"] = frame.groupby("instrument")["close"].pct_change(fill_method=None).fillna(0.0)
    index = benchmark.copy()
    index["factor"], index["up_limit"], index["down_limit"] = 1.0, 0.0, 0.0
    index["change"] = index["close"].pct_change(fill_method=None).fillna(0.0)
    return pd.concat([frame, index], ignore_index=True)


def _run_real_execution(
    snapshot_dir: Path,
    runs_dir: Path,
    start: str,
    end: str,
    topk: int,
    *,
    initial_cash: float,
    n_drop: int,
    risk_degree: float,
    open_cost: float,
    close_cost: float,
    min_cost: float,
    rebalance_frequency: str,
    cost_mode: str,
    commission_rate: float,
    tax_mode: str,
    stamp_tax_rate: float,
    transfer_fee_rate: float,
    impact_cost: float,
    factor_config: FactorConfig | None = None,
    neutralization=None,
    result_store=None,
    financials=None,
    frozen_snapshot: FrozenMarketSnapshot | None = None,
) -> dict:
    """Parameters come from a validated :class:`BacktestConfig`; there are no hidden defaults."""
    execution_options = {key: locals_value for key, locals_value in locals().copy().items() if key in EXECUTION_FIELDS}
    market, benchmark, calendar_frame, manifest = (
        frozen_snapshot.inputs if frozen_snapshot is not None else load_market(snapshot_dir)
    )
    validate_execution_universe(manifest["codes"])
    sessions = pd.DatetimeIndex(calendar_frame.loc[calendar_frame["is_open"], "date"])
    if pd.Timestamp(start) not in sessions or pd.Timestamp(end) not in sessions:
        raise ValueError("Backtest start/end must be exchange trading dates")
    previous = sessions[sessions < pd.Timestamp(start)]
    if previous.empty:
        raise ValueError("No preceding trading day for signal and initial valuation")
    initial_date = previous[-1].strftime("%Y-%m-%d")
    if pd.Timestamp(end) > market["date"].max() or pd.Timestamp(start) > pd.Timestamp(end):
        raise ValueError("Backtest interval is outside the frozen snapshot")
    factors = publish_factor_run(
        market,
        manifest,
        runs_dir,
        factor_config,
        neutralization,
        result_store=result_store,
        financials=financials,
        frozen_snapshot=frozen_snapshot,
    )
    if not any(row["date"] == initial_date for row in factors["history"]):
        raise ValueError("Warm-up does not provide prior-day factor scores")
    required_signals = sessions[(sessions >= pd.Timestamp(initial_date)) & (sessions < pd.Timestamp(end))]
    scored_dates = pd.DatetimeIndex(sorted({row["date"] for row in factors["history"]}))
    if not required_signals.isin(scored_dates).all():
        raise ValueError("A decision day lacks valid factor scores; refusing to silently reuse an old signal")
    benchmark_code = manifest.get("benchmark", "000300.SH")
    provenance = {
        "data_version": manifest["id"],
        **source_provenance(),
        "start": start,
        "end": end,
        "topk": topk,
        "initial_cash": initial_cash,
        "n_drop": n_drop,
        "risk_degree": risk_degree,
        "open_cost": open_cost,
        "close_cost": close_cost,
        "min_cost": min_cost,
        "benchmark": benchmark_code,
        "factor_run_id": factors["metadata"]["id"],
        **execution_options,
        **financial_provenance(financials, factor_config or FactorConfig()),
    }
    if frozen_snapshot is not None:
        provenance["frozen_snapshot"] = frozen_snapshot.binding()
        provenance["factors"] = factors["metadata"]["factor_config"]
    fingerprint = sha256_text(json.dumps(provenance, sort_keys=True))[:12]
    run_id, factor_id = f"real-qlib-{fingerprint}", factors["metadata"]["id"]
    store = result_store or ResultStore(runs_dir)
    if store.exists("backtests", run_id) and store.exists("factors", factor_id):
        existing = store.get("backtests", run_id)
        if existing["metadata"].get("provenance") == provenance:
            return {
                "reused": True,
                "run_id": run_id,
                "factor_run_id": factor_id,
                "metrics": existing["summary"]["metrics"],
            }
    quotes = adjusted_quotes(market, benchmark)
    names = market.drop_duplicates("instrument").set_index("instrument")["name"].to_dict()
    qlib_dir = runs_dir / ".qlib" / fingerprint
    result = run_qlib(
        quotes,
        factors,
        qlib_dir,
        calendar=sessions,
        start=start,
        end=end,
        initial_date=initial_date,
        names=names,
        topk=topk,
        limit_expressions=("$open >= $up_limit - 0.0001", "$open <= $down_limit + 0.0001"),
        require_availability=True,
        benchmark=to_qlib(benchmark_code),
        initial_cash=initial_cash,
        n_drop=n_drop,
        risk_degree=risk_degree,
        open_cost=open_cost,
        close_cost=close_cost,
        min_cost=min_cost,
        **execution_options,
    )
    result["summary"].update(id=run_id, name=f"真实行情：{len(names)}股多因子轮换", is_synthetic=False)
    result["metadata"].update(
        data_version=manifest["id"],
        provenance=provenance,
        factor_run_id=factor_id,
        data_snapshot=manifest["snapshot_dir"],
        source=manifest.get("source", "third_party_tushare_compatible"),
        benchmark_name=f"{'沪深300价格指数' if benchmark_code == '000300.SH' else '价格指数'}（{benchmark_code}）",
        position_note="Qlib复权口径的等效持股数量；发生现金分红时不等同于券商逐笔分红与实有股数账本",
        limitations=[
            "行情来自 Tushare（部分经 BaoStock 核验或补缺），未逐条对照交易所复核",
            "用户固定股票池，不是历史指数成分或全市场；可能存在选择与幸存者偏差",
            "价格与成交量按累计复权因子相对首日转换，采用Qlib复权收益/等效持股口径",
            "现金分红对应复权收益，不单独记现金分红；策略与价格指数基准的分红口径不同",
            "开盘触及买入涨停价或卖出跌停价保守不成交；滑点按固定成交额比例计入冲击成本，未模拟盘中队列和成交量冲击",
            "费用按已保存模式和参数扣除；分项模式佣金含规费、印花税及过户费另计，冲击成本为成交额比例压力假设，未模拟盘中流动性；停牌日禁止买卖",
            "停牌日缺失PE只沿用此前已知值并记录行情日期；该日期不等同于已审计的实际发布时间",
            financial_limitation(factors["metadata"]),
        ],
    )
    if manifest.get("cross_validation"):
        result["metadata"]["cross_validation"] = manifest["cross_validation"]
        result["metadata"]["limitations"] += [
            "部分股票可能使用BaoStock整段行情和涨跌幅复权序列；估值、限制价及指数仍来自Tushare兼容服务",
            "两源验证的范围是未复权OHLC和复权收益，不代表全部估值、财报和历史交易规则均获认证",
        ]
    result["metadata"]["config"]["adjustment"] = (
        "price * (adj_factor / first_adj_factor); volume divided by same factor"
    )

    industry_version = factors["metadata"].get("industry_data", {}).get("id")
    attach_industry_exposure(
        result,
        load_industries(industry_version) if industry_version else None,
        sessions,
        neutralization=neutralization,
        level=getattr(neutralization, "industry_level", "L1"),
    )
    # Strict JSON serialization before either artifact becomes visible.
    json.dumps(result, allow_nan=False)
    path = store.publish("backtests", run_id, result)
    return {
        "reused": False,
        "run_id": run_id,
        "factor_run_id": factor_id,
        "result": str(path),
        "data_version": manifest["id"],
        "metrics": result["summary"]["metrics"],
    }


def run_real(
    snapshot_dir: Path,
    runs_dir: Path,
    start: str,
    end: str,
    topk: int,
    *,
    benchmark: str | None = None,
    **kwargs,
) -> dict:
    result_store = kwargs.get("result_store")
    if benchmark is not None:
        benchmark = normalize_benchmark(benchmark)
    execution = _run_real_execution(snapshot_dir, runs_dir, start, end, topk, **kwargs)
    if benchmark is None:
        return execution

    frozen_snapshot = kwargs.get("frozen_snapshot")
    calendar = frozen_snapshot.inputs[2] if frozen_snapshot is not None else load_market(snapshot_dir)[2]
    view = publish_benchmark_view(
        runs_dir,
        execution["run_id"],
        benchmark,
        calendar=pd.DatetimeIndex(calendar.loc[calendar.is_open, "date"]),
        result_store=result_store,
    )
    store = result_store or ResultStore(runs_dir)
    result = store.get("backtests", view["run_id"], hydrate=False)
    return {
        **execution,
        **view,
        "execution_run_id": execution["run_id"],
        "result": str(store.path("backtests", view["run_id"])),
        "metrics": result["summary"]["metrics"],
    }


def run_configured(config, *, result_store=None, frozen_snapshot: FrozenMarketSnapshot | None = None) -> dict:
    """A config must match the frozen data selection, including the scoring universe."""

    load_configured_snapshot(config, frozen_snapshot=frozen_snapshot)
    return run_real(
        config.data.root,
        config.runs_dir,
        **asdict(config.backtest),
        factor_config=config.factors,
        neutralization=config.neutralization,
        result_store=result_store,
        financials=config.financials,
        frozen_snapshot=frozen_snapshot,
    )
