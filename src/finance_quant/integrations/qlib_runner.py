"""Shared Qlib adapter: documented data layout, native execution, result export."""

from __future__ import annotations

from functools import wraps
from pathlib import Path

import numpy as np
import pandas as pd
import qlib
from filelock import FileLock, Timeout
from qlib.backtest import backtest

from finance_quant.integrations.qlib_results import export_qlib_result
from finance_quant.integrations.qlib_runtime import (
    build_execution,
    build_strategy,
    prepare_signal,
    validate_quote_coverage,
    validate_request,
)


def exclusive_workspace(function):
    @wraps(function)
    def execute(quotes, factors, data_dir, *args, **kwargs):
        directory = Path(data_dir)
        directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            with FileLock(directory.with_name(directory.name + ".lock"), timeout=0):
                return function(quotes, factors, directory, *args, **kwargs)
        except Timeout:
            raise ValueError("This Qlib workspace is already running in another process") from None

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
    suspended = quotes.get("is_suspended", pd.Series(False, index=quotes.index)).fillna(False).astype(bool)
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
def run_qlib(
    quotes: pd.DataFrame,
    factors: dict,
    data_dir: Path,
    *,
    calendar: pd.DatetimeIndex,
    start: str,
    end: str,
    initial_date: str,
    names: dict[str, str],
    initial_cash: float = 100_000.0,
    topk: int = 2,
    open_cost: float = 0.001,
    close_cost: float = 0.001,
    min_cost: float = 5.0,
    limit_expressions: tuple[str, str] = ("$open < 0", "$open < 0"),
    require_availability: bool = False,
    benchmark: str = "SH000300",
    n_drop: int = 1,
    risk_degree: float = 0.9,
    historical: dict | None = None,
    rebalance_frequency: str = "daily",
    cost_mode: str = "legacy",
    commission_rate: float = 0.0003,
    tax_mode: str = "historical",
    stamp_tax_rate: float = 0.0005,
    transfer_fee_rate: float = 0.00001,
    impact_cost: float = 0.0,
) -> dict:

    cost_arguments = {
        "cost_mode": cost_mode,
        "commission_rate": commission_rate,
        "tax_mode": tax_mode,
        "stamp_tax_rate": stamp_tax_rate,
        "transfer_fee_rate": transfer_fee_rate,
        "impact_cost": impact_cost,
        "open_cost": open_cost,
        "close_cost": close_cost,
        "min_cost": min_cost,
    }
    costs_policy, scheduled = validate_request(
        calendar=calendar,
        start=start,
        end=end,
        initial_date=initial_date,
        initial_cash=initial_cash,
        topk=topk,
        names=names,
        n_drop=n_drop,
        risk_degree=risk_degree,
        rebalance_frequency=rebalance_frequency,
        cost_arguments=cost_arguments,
    )
    validate_quote_coverage(
        quotes,
        calendar=calendar,
        initial_date=initial_date,
        end=end,
        names=names,
        benchmark=benchmark,
        historical=historical,
    )
    write_features(quotes, calendar, data_dir)
    qlib.init(
        provider_uri=str(data_dir.resolve()),
        region="cn",
        kernels=1,
        expression_cache=None,
        dataset_cache=None,
        joblib_backend="threading",
    )
    signal = prepare_signal(
        factors,
        names=names,
        calendar=calendar,
        require_availability=require_availability,
    )
    strategy = build_strategy(
        signal=signal,
        factors=factors,
        names=names,
        costs=costs_policy,
        historical=historical,
        scheduled=scheduled,
        topk=topk,
        n_drop=n_drop,
        risk_degree=risk_degree,
    )
    config, call_config = build_execution(
        quotes=quotes,
        names=names,
        historical=historical,
        costs=costs_policy,
        start=start,
        end=end,
        initial_cash=initial_cash,
        benchmark=benchmark,
        limit_expressions=limit_expressions,
        open_cost=open_cost,
        close_cost=close_cost,
        min_cost=min_cost,
        impact_cost=impact_cost,
    )
    portfolio, _ = backtest(strategy=strategy, **call_config)
    report, positions = portfolio["1day"]
    return export_qlib_result(
        qlib_version=qlib.__version__,
        report=report,
        positions=positions,
        strategy=strategy,
        quotes=quotes,
        factors=factors,
        names=names,
        start=start,
        end=end,
        initial_date=initial_date,
        initial_cash=initial_cash,
        costs_policy=costs_policy,
        scheduled=scheduled,
        rebalance_frequency=rebalance_frequency,
        config=config,
        topk=topk,
        n_drop=n_drop,
        risk_degree=risk_degree,
        historical=historical,
    )
