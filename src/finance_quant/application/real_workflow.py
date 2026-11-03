"""Run native Qlib against a validated local snapshot from the paid endpoint."""

from __future__ import annotations

import hashlib
import json
import platform
from pathlib import Path

import numpy as np
import pandas as pd

from finance_quant.data.market import load_market
from finance_quant.data.execution_universe import validate_execution_universe
from finance_quant.integrations.qlib_runner import run_qlib
from finance_quant.application.factor_workflow import load_configured_snapshot, publish_factor_run
from finance_quant.research.registry import FactorConfig
from finance_quant.storage import ResultStore
from finance_quant.research.execution_policy import EXECUTION_FIELDS


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


def _run_real_execution(snapshot_dir: Path, runs_dir: Path = Path("runs"), start: str = "2026-09-01",
             end: str = "2026-09-30", topk: int = 2, *, initial_cash: float = 100_000.0,
             n_drop: int = 1, risk_degree: float = 0.9, open_cost: float = 0.001,
             close_cost: float = 0.001, min_cost: float = 5.0, factor_config: FactorConfig | None = None,
             rebalance_frequency: str = 'daily',cost_mode: str = 'legacy',commission_rate: float = .0003,
             tax_mode: str = 'historical',stamp_tax_rate: float = .0005,transfer_fee_rate: float = .00001,
             impact_cost: float = 0., neutralization=None) -> dict:
    execution_options={key:locals_value for key,locals_value in locals().copy().items() if key in EXECUTION_FIELDS}
    market, benchmark, calendar_frame, manifest = load_market(snapshot_dir)
    validate_execution_universe(manifest['codes'])
    sessions = pd.DatetimeIndex(calendar_frame.loc[calendar_frame["is_open"], "date"])
    if pd.Timestamp(start) not in sessions or pd.Timestamp(end) not in sessions:
        raise ValueError("Backtest start/end must be exchange trading dates")
    previous = sessions[sessions < pd.Timestamp(start)]
    if previous.empty:
        raise ValueError("No preceding trading day for signal and initial valuation")
    initial_date = previous[-1].strftime("%Y-%m-%d")
    if pd.Timestamp(end) > market["date"].max() or pd.Timestamp(start) > pd.Timestamp(end):
        raise ValueError("Backtest interval is outside the frozen snapshot")
    factors = publish_factor_run(market, manifest, runs_dir, factor_config, neutralization)
    if not any(row["date"] == initial_date for row in factors["history"]):
        raise ValueError("Warm-up does not provide prior-day factor scores")
    required_signals = sessions[(sessions >= pd.Timestamp(initial_date)) & (sessions < pd.Timestamp(end))]
    scored_dates = pd.DatetimeIndex(sorted({row["date"] for row in factors["history"]}))
    if not required_signals.isin(scored_dates).all():
        raise ValueError("A decision day lacks valid factor scores; refusing to silently reuse an old signal")
    source_files = [Path(__file__), Path(__file__).parents[1] / "research/market_factors.py",
                    Path(__file__).parents[1] / "integrations/qlib_runner.py",
                    Path(__file__).parents[1] / "data/market.py", Path(__file__).parents[1] / 'data/quality.py',
                    Path(__file__).parents[1] / 'data/execution_universe.py']
    source_files.append(Path(__file__).parents[1] / 'research/performance.py')
    source_files.append(Path(__file__).parents[1] / 'research/industry_exposure.py')
    source_files.append(Path(__file__).parents[1] / 'data/temporal_audit.py')
    source_files.append(Path(__file__).parents[1] / 'data/china_a_rules.py')
    source_files += [Path(__file__).parents[1]/'research/execution_policy.py',Path(__file__).parents[1]/'integrations/execution.py']
    code_hash = hashlib.sha256(b"".join(file.read_bytes() for file in source_files)).hexdigest()
    lock_path = Path(__file__).parents[3] / "uv.lock"
    benchmark_code = manifest.get("benchmark", "000300.SH")
    provenance = {"data_version": manifest["id"], "code_sha256": code_hash,
                  "dependency_lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest() if lock_path.exists() else None,
                  "python_version": platform.python_version(), "start": start, "end": end, "topk": topk,
                  "initial_cash": initial_cash, "n_drop": n_drop, "risk_degree": risk_degree,
                  "open_cost": open_cost, "close_cost": close_cost, "min_cost": min_cost, "benchmark": benchmark_code,
                  "factor_run_id": factors["metadata"]["id"],**execution_options}
    fingerprint = hashlib.sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest()[:12]
    run_id, factor_id = f"real-qlib-{fingerprint}", factors["metadata"]["id"]
    store = ResultStore(runs_dir)
    if store.path("backtests", run_id).exists() and store.path("factors", factor_id).exists():
        existing = store.get("backtests", run_id)
        if existing["metadata"].get("provenance") == provenance:
            return {"reused": True, "run_id": run_id, "factor_run_id": factor_id, "metrics": existing["summary"]["metrics"]}
    quotes = adjusted_quotes(market, benchmark)
    names = market.drop_duplicates("instrument").set_index("instrument")["name"].to_dict()
    qlib_dir = runs_dir / ".qlib" / fingerprint
    result = run_qlib(quotes, factors, qlib_dir, calendar=sessions, start=start, end=end,
                      initial_date=initial_date, names=names, topk=topk,
                      limit_expressions=("$open >= $up_limit - 0.0001", "$open <= $down_limit + 0.0001"),
                      require_availability=True, benchmark=benchmark_code[-2:] + benchmark_code[:6],
                      initial_cash=initial_cash, n_drop=n_drop, risk_degree=risk_degree,
                      open_cost=open_cost, close_cost=close_cost, min_cost=min_cost,**execution_options)
    result["summary"].update(id=run_id, name=f"真实行情：{len(names)}股多因子轮换", is_synthetic=False)
    result["metadata"].update(data_version=manifest["id"], provenance=provenance, factor_run_id=factor_id,
                              data_snapshot=manifest["snapshot_dir"], source=manifest.get('source', 'third_party_tushare_compatible'),
                              benchmark_name=f"{'沪深300价格指数' if benchmark_code == '000300.SH' else '价格指数'}（{benchmark_code}）",
                              position_note="Qlib复权口径的等效持股数量；发生现金分红时不等同于券商逐笔分红与实有股数账本",
                              limitations=[
                                  "真实行情由第三方Tushare兼容服务提供，未逐条对照交易所复核",
                                  "用户固定股票池，不是历史指数成分或全市场；可能存在选择与幸存者偏差",
                                  "价格与成交量按累计复权因子相对首日转换，采用Qlib复权收益/等效持股口径",
                                  "现金分红对应复权收益，不单独记现金分红；策略与价格指数基准的分红口径不同",
                                  "开盘触及买入涨停价或卖出跌停价保守不成交，未模拟盘中队列、滑点和成交量冲击",
                                  "费用按已保存模式和参数扣除；分项模式佣金含规费、印花税及过户费另计，冲击成本为成交额比例压力假设，未模拟盘中流动性；停牌日禁止买卖",
                                  "停牌日缺失PE只沿用此前已知值并记录行情日期；该日期不等同于已审计的实际发布时间",
                                  "未使用财报；每日估值的历史修订与实际发布时间仍待审计",
                              ])
    if manifest.get('cross_validation'):
        result['metadata']['cross_validation'] = manifest['cross_validation']
        result['metadata']['limitations'] += [
            '部分股票可能使用BaoStock整段行情和涨跌幅复权序列；估值、限制价及指数仍来自Tushare兼容服务',
            '两源验证的范围是未复权OHLC和复权收益，不代表全部估值、财报和历史交易规则均获认证',
        ]
    result["metadata"]["config"]["adjustment"] = "price * (adj_factor / first_adj_factor); volume divided by same factor"
    from finance_quant.data.industries import load_industries
    from finance_quant.research.industry_exposure import attach_industry_exposure
    industry_version=factors['metadata'].get('industry_data',{}).get('id')
    attach_industry_exposure(result,load_industries(industry_version) if industry_version else None,sessions,neutralization=neutralization)
    # Strict JSON serialization before either artifact becomes visible.
    json.dumps(result, allow_nan=False)
    path = store.publish("backtests", run_id, result)
    return {"reused": False, "run_id": run_id, "factor_run_id": factor_id, "result": str(path), "data_version": manifest["id"],
            "metrics": result["summary"]["metrics"]}


def run_real(snapshot_dir: Path, runs_dir: Path = Path('runs'), start: str = '2026-09-01',
             end: str = '2026-09-30', topk: int = 2, *, benchmark: str | None = None, **kwargs) -> dict:
    if benchmark is not None:
        from finance_quant.data.benchmarks import normalize_benchmark
        benchmark=normalize_benchmark(benchmark)
    execution=_run_real_execution(snapshot_dir,runs_dir,start,end,topk,**kwargs)
    if benchmark is None:return execution
    from finance_quant.application.benchmark_workflow import publish_benchmark_view
    calendar=load_market(snapshot_dir)[2]
    view=publish_benchmark_view(runs_dir,execution['run_id'],benchmark,
                               calendar=pd.DatetimeIndex(calendar.loc[calendar.is_open,'date']))
    result=ResultStore(runs_dir).get('backtests',view['run_id'],hydrate=False)
    return {**execution,**view,'execution_run_id':execution['run_id'],
            'result':str(ResultStore(runs_dir).path('backtests',view['run_id'])),
            'metrics':result['summary']['metrics']}


def run_configured(config) -> dict:
    """A config must match the frozen data selection, including the scoring universe."""
    from dataclasses import asdict
    load_configured_snapshot(config)
    return run_real(config.data.root, config.runs_dir, **asdict(config.backtest), factor_config=config.factors,neutralization=config.neutralization)
