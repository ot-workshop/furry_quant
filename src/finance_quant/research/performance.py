"""Daily account performance, derived from saved after-cost NAV, never signals."""

from __future__ import annotations

import math
import statistics
from datetime import date
from itertools import pairwise

VERSION = "daily-nav-performance-v3"
TRADING_DAYS = 252
RISK_FREE_RATE = 0.03  # Explicit annual assumption retained from the old project.
METRIC_KEYS = (
    "total_return",
    "annualized_return",
    "volatility",
    "sharpe_ratio",
    "max_drawdown",
    "calmar_ratio",
    "win_rate",
    "profit_loss_ratio",
    "total_days",
    "years",
)


def _valid(value) -> bool:
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def _finite(value):
    return float(value) if _valid(value) else None


def _stats(values, dates):
    empty = {key: None for key in METRIC_KEYS}
    drawdown = {"peak_date": None, "trough_date": None}
    if len(values) < 2 or any(not _valid(v) or v < 0 for v in values) or any(v <= 0 for v in values[:-1]):
        return empty, drawdown, None
    returns = [current / previous - 1 for previous, current in pairwise(values)]
    if not all(_valid(r) for r in returns):
        return empty, drawdown, None
    n = len(returns)
    total = values[-1] / values[0] - 1
    try:
        annual = math.expm1(math.log(values[-1] / values[0]) * TRADING_DAYS / n) if values[-1] else -1.0
    except (OverflowError, ValueError):
        annual = None
    vol = statistics.stdev(returns) * math.sqrt(TRADING_DAYS) if n >= 2 else None
    peak, peak_index, worst = values[0], 0, 0.0
    for index, value in enumerate(values):
        if value >= peak:
            peak, peak_index = value, index
        dd = value / peak - 1
        if dd < worst:
            worst = dd
            drawdown = {"peak_date": dates[peak_index], "trough_date": dates[index]}
    positive, negative = [r for r in returns if r > 0], [r for r in returns if r < 0]
    return (
        {
            "total_return": _finite(total),
            "annualized_return": _finite(annual),
            "volatility": _finite(vol),
            "sharpe_ratio": _finite((statistics.fmean(returns) * TRADING_DAYS - RISK_FREE_RATE) / vol)
            if vol is not None and vol > 1e-12
            else None,
            "max_drawdown": float(worst),
            "calmar_ratio": _finite(annual / abs(worst)) if annual is not None and worst < -1e-12 else None,
            "win_rate": len(positive) / (len(positive) + len(negative)) if positive or negative else None,
            "profit_loss_ratio": _finite(statistics.fmean(positive) / abs(statistics.fmean(negative)))
            if positive and negative
            else None,
            "total_days": n,
            "years": n / TRADING_DAYS,
        },
        drawdown,
        returns,
    )


def calculate_performance(equity: list[dict]) -> dict:
    """The first NAV is the opening anchor; each subsequent row is one session.

    Missing/invalid NAV invalidates the series; observations are never dropped
    or forward-filled. A missing benchmark does not invalidate strategy metrics.
    """
    if not isinstance(equity, list) or any(not isinstance(point, dict) for point in equity):
        raise ValueError("Performance requires a saved list of NAV observations")
    dates = [point.get("date") for point in equity]
    try:
        parsed = [date.fromisoformat(d) for d in dates]
    except (ValueError, TypeError):
        raise ValueError("Performance dates must be ISO calendar dates") from None
    if len(set(parsed)) != len(parsed) or parsed != sorted(parsed):
        raise ValueError("Performance dates must be unique and ordered")
    metrics, drawdown, returns = _stats([point.get("equity") for point in equity], dates)
    benchmark, benchmark_drawdown, benchmark_returns = _stats([point.get("benchmark") for point in equity], dates)
    annual = []
    if returns is not None:
        grouped = {}
        for index, value in enumerate(returns, 1):
            grouped.setdefault(parsed[index].year, []).append((index, value))
        for year, group in grouped.items():
            indices = [index for index, _ in group]
            annual.append(
                {
                    "year": year,
                    "start_date": dates[indices[0]],
                    "end_date": dates[indices[-1]],
                    "trading_days": len(indices),
                    "strategy_return": _finite(math.prod(1 + value for _, value in group) - 1),
                    "benchmark_return": _finite(math.prod(1 + benchmark_returns[index - 1] for index in indices) - 1)
                    if benchmark_returns is not None
                    else None,
                }
            )
    relative = {
        "relative_return": None,
        "tracking_error": None,
        "information_ratio": None,
        "annualized_active_return": None,
    }
    if returns is not None and benchmark_returns is not None:
        active = [a - b for a, b in zip(returns, benchmark_returns, strict=True)]
        average = statistics.fmean(active) * TRADING_DAYS
        tracking = statistics.stdev(active) * math.sqrt(TRADING_DAYS) if len(active) >= 2 else None
        relative.update(
            annualized_active_return=_finite(average),
            tracking_error=_finite(tracking),
            information_ratio=_finite(average / tracking) if tracking is not None and tracking > 1e-12 else None,
            relative_return=_finite((1 + metrics["total_return"]) / (1 + benchmark["total_return"]) - 1)
            if benchmark["total_return"] > -1
            else None,
        )
    return {
        "version": VERSION,
        "available": returns is not None,
        "methodology": {
            "trading_days_per_year": TRADING_DAYS,
            "annual_risk_free_rate": RISK_FREE_RATE,
            "std_ddof": 1,
            "return_source": "saved_after_cost_account_nav",
            "annualized_return": "(last_nav / initial_nav) ** (252 / return_observations) - 1",
            "sharpe_ratio": "(mean(daily_return) * 252 - annual_risk_free_rate) / annualized_volatility",
            "calmar_ratio": "annualized_return / abs(max_drawdown)",
            "win_rate": "positive_return_days / nonzero_return_days",
            "profit_loss_ratio": "mean(positive_daily_returns) / abs(mean(negative_daily_returns))",
            "annual_returns": "compound actual observed daily returns in each calendar year; not annualized",
            "relative_return": "(1 + strategy_total_return) / (1 + benchmark_total_return) - 1",
            "tracking_error": "sample_std(strategy_daily_return - benchmark_daily_return) * sqrt(252)",
            "information_ratio": "mean(strategy_daily_return - benchmark_daily_return) * 252 / tracking_error",
        },
        "metrics": metrics,
        "benchmark_metrics": benchmark,
        "drawdown": drawdown,
        "benchmark_drawdown": benchmark_drawdown,
        "annual_returns": annual,
        "relative_metrics": relative,
    }


def enrich_backtest(result: dict) -> dict:
    """A derived API view for old artifacts; never mutates the input or disk."""
    if result.get("summary", {}).get("status") != "succeeded":
        return result
    if isinstance(result.get("performance"), dict) and result["performance"].get("version") == VERSION:
        return result
    try:
        performance = calculate_performance(result.get("equity", []))
    except (ValueError, TypeError, OverflowError, ZeroDivisionError):
        performance = calculate_performance([])
    original = result.get("summary", {}).get("metrics") or {}
    metrics = {**original, **performance["metrics"], **performance["relative_metrics"]}
    metrics["benchmark_return"] = performance["benchmark_metrics"]["total_return"]
    metrics["excess_return"] = (
        metrics["total_return"] - metrics["benchmark_return"]
        if metrics["total_return"] is not None and metrics["benchmark_return"] is not None
        else None
    )
    metrics["trade_count"] = len(result["trades"]) if isinstance(result.get("trades"), list) else None
    return {**result, "summary": {**result["summary"], "metrics": metrics}, "performance": performance}
