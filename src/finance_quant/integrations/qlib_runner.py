"""Shared Qlib adapter: documented data layout, native execution, result export."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from functools import wraps

import numpy as np
import pandas as pd
from filelock import FileLock, Timeout
from finance_quant.research.performance import enrich_backtest
from finance_quant.research.execution_policy import ExecutionCosts,rebalance_dates


def exclusive_workspace(function):
    @wraps(function)
    def execute(quotes, factors, data_dir, *args, **kwargs):
        directory = Path(data_dir)
        directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            with FileLock(directory.with_name(directory.name + '.lock'), timeout=0):
                return function(quotes, factors, directory, *args, **kwargs)
        except Timeout:
            raise ValueError('This Qlib workspace is already running in another process') from None
    return execute


def write_features(quotes: pd.DataFrame, calendar: pd.DatetimeIndex, destination: Path) -> None:
    fields = ["open", "close", "high", "low", "volume", "factor", "change", "up_limit", "down_limit"]
    calendar = calendar.sort_values().unique()
    if calendar.hasnans or calendar.tz is not None or not calendar.equals(calendar.normalize()):
        raise ValueError("Qlib calendar must contain unique exchange-local dates")
    if quotes.duplicated(["instrument", "date"]).any():
        raise ValueError("Duplicate Qlib instrument/date")
    if not np.isfinite(quotes[fields].to_numpy(dtype=float)).all():
        raise ValueError("Missing/nonfinite Qlib quotes must be resolved before execution")
    suspended = quotes.get('is_suspended', pd.Series(False, index=quotes.index)).fillna(False).astype(bool)
    invalid_volume = quotes.volume.lt(0) | (quotes.volume.eq(0) & ~suspended) | (suspended & quotes.volume.ne(0))
    if (quotes[["open", "close", "high", "low", "factor"]] <= 0).any().any() or invalid_volume.any():
        raise ValueError("Nonpositive execution quotes are unsupported")
    for directory in ["calendars", "instruments", "features"]:
        (destination / directory).mkdir(parents=True, exist_ok=True)
    calendar_text = "\n".join(calendar.strftime("%Y-%m-%d")) + "\n"
    (destination / "calendars/day.txt").write_text(calendar_text, encoding="utf-8")
    (destination / "calendars/day_future.txt").write_text(calendar_text, encoding="utf-8")
    lines = []
    for instrument, frame in quotes.groupby("instrument", sort=True):
        if not isinstance(instrument, str) or not instrument.isalnum():
            raise ValueError("Invalid Qlib instrument identifier")
        frame = frame.sort_values("date")
        expected = calendar[(calendar >= frame["date"].min()) & (calendar <= frame["date"].max())]
        if not pd.DatetimeIndex(frame["date"]).equals(expected):
            raise ValueError("Qlib feature dates do not match the official calendar")
        start_index = calendar.get_indexer([frame["date"].iloc[0]])[0]
        if start_index < 0:
            raise ValueError("Quote starts outside official calendar")
        feature_dir = destination / "features" / instrument.lower()
        feature_dir.mkdir(exist_ok=True)
        for field in fields:
            values = np.concatenate(([start_index], frame[field].to_numpy(dtype=float)))
            values.astype("<f4").tofile(feature_dir / f"{field}.day.bin")
        lines.append(f"{instrument}\t{frame['date'].iloc[0]:%Y-%m-%d}\t{frame['date'].iloc[-1]:%Y-%m-%d}")
    (destination / "instruments/all.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


@exclusive_workspace
def run_qlib(quotes: pd.DataFrame, factors: dict, data_dir: Path, *, calendar: pd.DatetimeIndex,
             start: str, end: str, initial_date: str, names: dict[str, str],
             initial_cash: float = 100_000.0, topk: int = 2, open_cost: float = 0.001,
             close_cost: float = 0.001, min_cost: float = 5.0,
             limit_expressions: tuple[str, str] = ("$open < 0", "$open < 0"),
             require_availability: bool = False, benchmark: str = "SH000300",
             n_drop: int = 1, risk_degree: float = 0.9, historical: dict | None = None,
             rebalance_frequency: str = 'daily',cost_mode: str = 'legacy',commission_rate: float = .0003,
             tax_mode: str = 'historical',stamp_tax_rate: float = .0005,transfer_fee_rate: float = .00001,
             impact_cost: float = 0.) -> dict:
    import qlib
    from qlib.backtest import backtest
    from qlib.backtest.decision import Order
    from qlib.contrib.strategy.signal_strategy import TopkDropoutStrategy
    from finance_quant.integrations.execution import CostAwareExchange,ScheduledTopkStrategy

    if (not np.isfinite([initial_cash, open_cost, close_cost, min_cost, risk_degree]).all()
        or initial_cash <= 0 or isinstance(topk, bool) or not isinstance(topk, int) or topk <= 0
        or topk > len(names) or isinstance(n_drop, bool) or not isinstance(n_drop, int)
        or not 0 <= n_drop <= topk or not 0 < risk_degree <= 1
        or any(value < 0 for value in [open_cost, close_cost, min_cost])):
        raise ValueError("Invalid capital, holding count, or fees")
    if not pd.Timestamp(initial_date) < pd.Timestamp(start) <= pd.Timestamp(end):
        raise ValueError("Initial valuation must precede the execution interval")
    if any(pd.Timestamp(day) not in calendar for day in (initial_date,start,end)):
        raise ValueError('期初估值与回测起止必须位于实际交易日历中')
    costs_policy=ExecutionCosts(cost_mode,commission_rate,tax_mode,stamp_tax_rate,transfer_fee_rate,impact_cost,open_cost,close_cost,min_cost)
    costs_policy.validate_range(start)
    scheduled=rebalance_dates(calendar,start,end,rebalance_frequency)
    required_sessions = calendar[(calendar >= pd.Timestamp(initial_date)) & (calendar <= pd.Timestamp(end))]
    for instrument in [*names, benchmark]:
        if historical is not None and instrument!=benchmark:continue
        observed = pd.DatetimeIndex(quotes.loc[quotes["instrument"] == instrument, "date"])
        if not required_sessions.isin(observed).all():
            raise ValueError("Quotes do not cover the full requested account interval")
    write_features(quotes, calendar, data_dir)
    qlib.init(provider_uri=str(data_dir.resolve()), region="cn", kernels=1,
              expression_cache=None, dataset_cache=None, joblib_backend="threading")
    signal = pd.DataFrame(factors["history"])
    if signal.empty or signal.duplicated(["date", "instrument"]).any():
        raise ValueError("Signals are empty or duplicated")
    if not np.isfinite(signal["composite"].to_numpy(dtype=float)).all():
        raise ValueError("Signals must be finite")
    if not set(signal["instrument"]).issubset(names):
        raise ValueError("Signal instrument is not in the declared universe")
    if require_availability and "available_at" not in signal:
        raise ValueError("Real signals require an explicit availability timestamp")
    signal["datetime"] = pd.to_datetime(signal["date"], errors="raise")
    if signal.datetime.dt.tz is not None or not signal.datetime.eq(signal.datetime.dt.normalize()).all() or not signal.datetime.isin(calendar).all():
        raise ValueError('信号日期必须为交易日历内的完整交易日期')
    if "available_at" in signal.columns:
        for row in signal[['date','datetime','available_at']].drop_duplicates().itertuples():
            known_at = pd.Timestamp(row.available_at)
            if known_at.tzinfo is None:
                raise ValueError("Signal availability must include a timezone")
            if require_availability and factors['metadata'].get('signal_uses_close') and known_at < row.datetime.tz_localize('Asia/Shanghai')+pd.Timedelta(hours=15):
                raise ValueError('收盘因子不能声称在信号日收盘前可用')
            following = calendar[calendar > row.datetime]
            if len(following) and known_at >= following[0].tz_localize("Asia/Shanghai") + pd.Timedelta(hours=9, minutes=30):
                raise ValueError("Signal was not available before next-session opening execution")
    signal = signal.set_index(["datetime", "instrument"])["composite"].sort_index()

    if historical is not None:
        from finance_quant.integrations.china_a import HistoricalIndexStrategy,ChinaAExchange,MarketStates
    strategy_base=HistoricalIndexStrategy if historical is not None else ScheduledTopkStrategy
    class RecordingStrategy(strategy_base):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.fills, self.orders = [], []

        def post_exe_step(self, execute_result):
            for order, value, cost, price in execute_result or []:
                factor = float(order.factor) if order.factor is not None else 1.0
                record = {"date": order.start_time.strftime("%Y-%m-%d"), "instrument": order.stock_id,
                          "name": names[order.stock_id], "side": "buy" if order.direction == Order.BUY else "sell",
                          "requested_amount": float(order.amount * factor), "amount": float(order.deal_amount * factor),
                          "price": float(price / factor) if np.isfinite(price) else None,
                          "value": float(value), "cost": float(cost)}
                if hasattr(order,'quantity_clipped_from'):record['quantity_clipped_from']=float(order.quantity_clipped_from)
                record['cost_breakdown']=costs_policy.breakdown(float(value),order.direction==Order.SELL,order.start_time)
                if not np.isclose(record['cost_breakdown']['total'],float(cost),rtol=1e-9,atol=1e-7):
                    raise ValueError('费用明细与Qlib实际账户扣费不一致')
                if historical is not None:
                    record['reason']=self.pending_exits.get(order.stock_id,'rank_selection' if order.direction==Order.BUY else 'score_rotation')
                self.orders.append(record)
                if order.deal_amount > 0:
                    self.fills.append(record)

    extra=({'membership_by_execution_date':historical['membership_by_execution_date'],
            'decision_coverage':factors['metadata']['availability_by_date']} if historical is not None else {})
    strategy = RecordingStrategy(signal=signal, topk=topk, n_drop=n_drop, risk_degree=risk_degree,
                                 hold_thresh=1, forbid_all_trade_at_limit=False,rebalance_dates=scheduled,**extra)
    # A zero-volume row is only permitted with an explicit validated suspension
    # marker above. Lock both buy and sell orders on those valuation-only rows.
    limit_expressions = tuple(f"($volume <= 0) | ({expression})" for expression in limit_expressions)
    config = {"start_time": start, "end_time": end, "account": initial_cash, "benchmark": benchmark,
              "exchange_kwargs": {"codes": sorted(names), "deal_price": "$open", "open_cost": open_cost,
                                  "close_cost": close_cost, "min_cost": min_cost, "trade_unit": 100,
                                  "limit_threshold": limit_expressions, "impact_cost": impact_cost}}
    if historical is not None:
        states=MarketStates(quotes,names)
        kwargs={**config['exchange_kwargs'],'start_time':start,'end_time':end,'market_states':states,
                'limit_threshold':('($volume <= 0)','($volume <= 0)')}
        exchange=ChinaAExchange(**kwargs,execution_costs=costs_policy)
    else:
        exchange=CostAwareExchange(**config['exchange_kwargs'],start_time=start,end_time=end,execution_costs=costs_policy)
    executor = {"class": "DailySimulatorExecutor", "module_path": "finance_quant.integrations.execution",
                "kwargs": {"time_per_step": "day", "generate_portfolio_metrics": True}}
    call_config={**config,'exchange_kwargs':{'exchange':exchange}}
    portfolio, _ = backtest(strategy=strategy, executor=executor, **call_config)
    report, positions = portfolio["1day"]
    if report.empty:
        raise ValueError("Qlib returned an empty report")
    equity = [{"date": initial_date, "equity": 1.0, "benchmark": 1.0, "drawdown": 0.0, "cost": 0.0}]
    benchmark_value, peak = 1.0, 1.0
    for date, row in report.iterrows():
        value = float(row["account"] / initial_cash)
        benchmark_value *= 1 + float(row["bench"])
        peak = max(peak, value)
        equity.append({"date": date.strftime("%Y-%m-%d"), "equity": value, "benchmark": benchmark_value,
                       "drawdown": value / peak - 1, "cost": float(row["cost"]),
                       "cash": float(row["cash"]), "account_value": float(row["account"])})
    quote_factors = quotes.set_index(["instrument", "date"])["factor"]
    holdings = []
    for date, position in positions.items():
        weights = position.get_stock_weight_dict()
        for instrument in sorted(position.get_stock_list()):
            factor = float(quote_factors.loc[(instrument, date)])
            holdings.append({"date": date.strftime("%Y-%m-%d"), "instrument": instrument, "name": names[instrument],
                             "amount": float(position.get_stock_amount(instrument) * factor),
                             "price": float(position.get_stock_price(instrument) / factor),
                             "weight": float(weights.get(instrument, 0)), "cash": float(position.get_cash())})
    costs = sum(fill["cost"] for fill in strategy.fills)
    metrics = {"total_return": equity[-1]["equity"] - 1,
               "max_drawdown": min(point["drawdown"] for point in equity),
               "total_cost": costs / initial_cash, "total_cost_amount": costs,
               "avg_turnover": float(report["turnover"].mean()), "benchmark_return": equity[-1]["benchmark"] - 1}
    result = {"summary": {"id": "unpublished", "name": "未发布回测", "status": "succeeded",
                        "strategy": "TopkDropoutStrategy", "start_date": start, "end_date": end,
                        "created_at": datetime.now(timezone.utc).isoformat(), "metrics": metrics},
            "metadata": {"engine": "qlib", "engine_version": qlib.__version__,
                         "execution_costs":costs_policy.as_dict(),
                         "cost_summary":{key:sum(t['cost_breakdown'][key] for t in strategy.fills)
                             for key in ['commission','stamp_tax','transfer_fee','impact','legacy_fee','total']},
                         "rebalance_policy":{'frequency':rebalance_frequency,'dates':scheduled,'scheduled_count':len(scheduled),
                             'timing':'区间首日建仓；随后每周/每月首个交易日开盘，使用前一交易日评分；每日估值',
                             'exit_policy':'动态池成分调出每日重试，非调仓日卖出后现金等下个调仓日补仓' if historical is not None else '只在调仓日轮换'},
                         "config": {**config, "topk": topk, "n_drop": n_drop, "risk_degree": risk_degree,
                                    "rebalance_frequency":rebalance_frequency,'execution_costs':costs_policy.as_dict(),
                                    "signal_lag": "previous_trading_day", "factor_run_id": factors["metadata"]["id"]}},
            "equity": equity, "positions": holdings, "trades": strategy.fills, "orders": strategy.orders,
            "qlib_report": [{"date": date.strftime("%Y-%m-%d"), **{key: float(value) for key, value in row.items()}}
                            for date, row in report.iterrows()]}
    if historical is not None:
        result['execution_events']=strategy.events
        final_position=positions[report.index[-1]]
        result['metadata']['execution_summary']={'events':len(strategy.events),
            'blocked_exits':sum(e['action']=='sell' and e['reason']!='score_rotation' and e['status']=='blocked' for e in strategy.events),
            'pending_exits':[{'instrument':code,'reason':reason} for code,reason in strategy.pending_exits.items()
                if code in final_position.get_stock_list()]}
        result['metadata']['universe_policy']=historical['membership_policy']
        from finance_quant.data.china_a_rules import RULE_VERSION
        result['metadata']['market_rule_version']=RULE_VERSION
        result['summary']['strategy']='HistoricalIndexTopkStrategy'
    from finance_quant.data.temporal_audit import add_execution_audit
    add_execution_audit(result,quotes,factors)
    return enrich_backtest(result)
