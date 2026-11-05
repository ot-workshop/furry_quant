"""Validation and runtime construction for the Qlib adapter."""

from __future__ import annotations

import numpy as np
import pandas as pd

from finance_quant.integrations.execution import CostAwareExchange
from finance_quant.integrations.recording_strategy import (
    RecordingHistoricalStrategy,
    RecordingScheduledStrategy,
)
from finance_quant.research.execution_policy import ExecutionCosts, rebalance_dates


def validate_request(
    *,
    calendar: pd.DatetimeIndex,
    start: str,
    end: str,
    initial_date: str,
    initial_cash: float,
    topk: int,
    names: dict[str, str],
    n_drop: int,
    risk_degree: float,
    rebalance_frequency: str,
    cost_arguments: dict,
) -> tuple[ExecutionCosts, pd.DatetimeIndex]:
    scalar_values = [
        initial_cash,
        cost_arguments["open_cost"],
        cost_arguments["close_cost"],
        cost_arguments["min_cost"],
        risk_degree,
    ]
    if (
        not np.isfinite(scalar_values).all()
        or initial_cash <= 0
        or isinstance(topk, bool)
        or not isinstance(topk, int)
        or topk <= 0
        or topk > len(names)
        or isinstance(n_drop, bool)
        or not isinstance(n_drop, int)
        or not 0 <= n_drop <= topk
        or not 0 < risk_degree <= 1
        or any(cost_arguments[name] < 0 for name in ("open_cost", "close_cost", "min_cost"))
    ):
        raise ValueError("Invalid capital, holding count, or fees")
    if not pd.Timestamp(initial_date) < pd.Timestamp(start) <= pd.Timestamp(end):
        raise ValueError("Initial valuation must precede the execution interval")
    if any(pd.Timestamp(day) not in calendar for day in (initial_date, start, end)):
        raise ValueError("期初估值与回测起止必须位于实际交易日历中")
    costs = ExecutionCosts(**cost_arguments)
    costs.validate_range(start)
    return costs, rebalance_dates(calendar, start, end, rebalance_frequency)


def validate_quote_coverage(
    quotes: pd.DataFrame,
    *,
    calendar: pd.DatetimeIndex,
    initial_date: str,
    end: str,
    names: dict[str, str],
    benchmark: str,
    historical: dict | None,
) -> None:
    required_sessions = calendar[(calendar >= pd.Timestamp(initial_date)) & (calendar <= pd.Timestamp(end))]
    for instrument in [*names, benchmark]:
        if historical is not None and instrument != benchmark:
            continue
        observed = pd.DatetimeIndex(quotes.loc[quotes["instrument"] == instrument, "date"])
        if not required_sessions.isin(observed).all():
            raise ValueError("Quotes do not cover the full requested account interval")


def prepare_signal(
    factors: dict,
    *,
    names: dict[str, str],
    calendar: pd.DatetimeIndex,
    require_availability: bool,
) -> pd.Series:
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
    if (
        signal.datetime.dt.tz is not None
        or not signal.datetime.eq(signal.datetime.dt.normalize()).all()
        or not signal.datetime.isin(calendar).all()
    ):
        raise ValueError("信号日期必须为交易日历内的完整交易日期")
    if "available_at" in signal.columns:
        _validate_availability(signal, factors, calendar, require_availability)
    return signal.set_index(["datetime", "instrument"])["composite"].sort_index()


def _validate_availability(signal, factors, calendar, require_availability):
    for row in signal[["date", "datetime", "available_at"]].drop_duplicates().itertuples():
        known_at = pd.Timestamp(row.available_at)
        if known_at.tzinfo is None:
            raise ValueError("Signal availability must include a timezone")
        if (
            require_availability
            and factors["metadata"].get("signal_uses_close")
            and known_at < row.datetime.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=15)
        ):
            raise ValueError("收盘因子不能声称在信号日收盘前可用")
        following = calendar[calendar > row.datetime]
        if len(following) and known_at >= following[0].tz_localize("Asia/Shanghai") + pd.Timedelta(hours=9, minutes=30):
            raise ValueError("Signal was not available before next-session opening execution")


def build_strategy(
    *,
    signal: pd.Series,
    factors: dict,
    names: dict[str, str],
    costs: ExecutionCosts,
    historical: dict | None,
    scheduled: pd.DatetimeIndex,
    topk: int,
    n_drop: int,
    risk_degree: float,
):
    extra = (
        {
            "membership_by_execution_date": historical["membership_by_execution_date"],
            "decision_coverage": factors["metadata"]["availability_by_date"],
        }
        if historical is not None
        else {}
    )
    strategy_class = RecordingHistoricalStrategy if historical is not None else RecordingScheduledStrategy
    return strategy_class(
        instrument_names=names,
        execution_costs=costs,
        historical_context=historical,
        signal=signal,
        topk=topk,
        n_drop=n_drop,
        risk_degree=risk_degree,
        hold_thresh=1,
        forbid_all_trade_at_limit=False,
        rebalance_dates=scheduled,
        **extra,
    )


def build_execution(
    *,
    quotes: pd.DataFrame,
    names: dict[str, str],
    historical: dict | None,
    costs: ExecutionCosts,
    start: str,
    end: str,
    initial_cash: float,
    benchmark: str,
    limit_expressions: tuple[str, str],
    open_cost: float,
    close_cost: float,
    min_cost: float,
    impact_cost: float,
) -> tuple[dict[str, object], dict[str, object]]:
    locked_limits = tuple(f"($volume <= 0) | ({expression})" for expression in limit_expressions)
    exchange_kwargs: dict[str, object] = {
        "codes": sorted(names),
        "deal_price": "$open",
        "open_cost": open_cost,
        "close_cost": close_cost,
        "min_cost": min_cost,
        "trade_unit": 100,
        "limit_threshold": locked_limits,
        "impact_cost": impact_cost,
    }
    config: dict[str, object] = {
        "start_time": start,
        "end_time": end,
        "account": initial_cash,
        "benchmark": benchmark,
        "exchange_kwargs": exchange_kwargs,
    }
    exchange_kwargs = {**exchange_kwargs, "start_time": start, "end_time": end}
    if historical is not None:
        from finance_quant.integrations.china_a import ChinaAExchange, MarketStates

        historical_kwargs = {
            **exchange_kwargs,
            "limit_threshold": ("($volume <= 0)", "($volume <= 0)"),
        }
        exchange = ChinaAExchange(
            **historical_kwargs,
            market_states=MarketStates(quotes, names),
            execution_costs=costs,
        )
    else:
        exchange = CostAwareExchange(**exchange_kwargs, execution_costs=costs)
    executor = {
        "class": "DailySimulatorExecutor",
        "module_path": "finance_quant.integrations.execution",
        "kwargs": {"time_per_step": "day", "generate_portfolio_metrics": True},
    }
    return config, {"executor": executor, **config, "exchange_kwargs": {"exchange": exchange}}
