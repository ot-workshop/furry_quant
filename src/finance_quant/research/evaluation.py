"""Offline factor diagnostics. Future labels never enter the trading signal."""

from __future__ import annotations

from itertools import pairwise
from operator import itemgetter

import numpy as np
import pandas as pd
from qlib.contrib.eva.alpha import calc_ic

from finance_quant.research.factor_library import FACTOR_LIBRARY
from finance_quant.research.registry import EvaluationConfig


def _number(value) -> float | None:
    return float(value) if pd.notna(value) and np.isfinite(value) else None


def _sessions(calendar) -> pd.DatetimeIndex:
    dates = pd.DatetimeIndex(calendar)
    if (
        dates.empty
        or dates.hasnans
        or dates.tz is not None
        or dates.has_duplicates
        or not dates.is_monotonic_increasing
    ):
        raise ValueError("Evaluation requires a unique, ordered, timezone-naive trading calendar")
    if not dates.equals(dates.normalize()):
        raise ValueError("Trading calendar must contain dates without times")
    return dates


def forward_open_labels(market: pd.DataFrame, calendar, horizon: int, end: str, *, instruments=None) -> pd.DataFrame:
    """T score -> T+1 open entry -> T+1+h open exit, on the official calendar.

    Reindexing BEFORE shifting is essential: an absent session must not shorten
    the holding period. Missing entry/exit quotes and boundary labels stay NaN.
    """
    if isinstance(horizon, bool) or not isinstance(horizon, int) or not 1 <= horizon <= 252:
        raise ValueError("horizon must be an integer in [1, 252]")
    sessions = _sessions(calendar)
    if (market.empty and instruments is None) or market.duplicated(["date", "instrument"]).any():
        raise ValueError("Evaluation quotes must be nonempty and unique")
    dates = pd.DatetimeIndex(market["date"])
    if not dates.isin(sessions).all():
        raise ValueError("Market dates are outside the exchange calendar")
    values = market[["open", "adj_factor"]].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= 0).any():
        raise ValueError("Evaluation requires finite positive opens and adjustment factors")
    frame = market[
        ["date", "instrument", "open", "adj_factor", *(["is_suspended"] if "is_suspended" in market else [])]
    ].copy()
    frame["adjusted_open"] = frame.open * frame.adj_factor
    if not np.isfinite(frame["adjusted_open"].to_numpy(dtype=float)).all():
        raise ValueError("Adjusted opens must be finite")
    grid = frame.pivot(index="date", columns="instrument", values="adjusted_open").reindex(sessions)
    if instruments is not None:
        grid = grid.reindex(columns=sorted(instruments))
    halted = (
        frame.assign(is_suspended=frame.get("is_suspended", False))
        .pivot(index="date", columns="instrument", values="is_suspended")
        .reindex(index=sessions, columns=grid.columns)
        .fillna(False)
    )
    entry_halted, exit_halted = halted.shift(-1), halted.shift(-horizon - 1)
    entry, exit_ = grid.shift(-1), grid.shift(-horizon - 1)
    entry_dates = pd.Series(sessions, index=sessions).shift(-1)
    exit_dates = pd.Series(sessions, index=sessions).shift(-horizon - 1)
    returns = exit_ / entry - 1
    # Build the calendar x instrument grid in bulk. This preserves missing
    # lifecycle sessions and boundary exclusions without per-cell .loc calls.
    entry_values = entry.to_numpy(float)
    exit_values = exit_.to_numpy(float)
    outside = (exit_dates.isna() | exit_dates.gt(pd.Timestamp(end))).to_numpy()[:, None]
    missing = ~np.isfinite(entry_values) | ~np.isfinite(exit_values)
    halted_values = entry_halted.fillna(False).to_numpy(bool) | exit_halted.fillna(False).to_numpy(bool)
    reasons = np.full(entry_values.shape, None, dtype=object)
    reasons[halted_values] = "non_trading_entry_or_exit"
    reasons[missing] = "missing_entry_or_exit"
    reasons[np.broadcast_to(outside, entry_values.shape)] = "outside_evaluation_end"
    values = returns.to_numpy(float)
    values[pd.notna(reasons)] = np.nan
    n = len(grid.columns)
    return pd.DataFrame(
        {
            "date": np.repeat(sessions.strftime("%Y-%m-%d").to_numpy(), n),
            "instrument": np.tile(grid.columns.to_numpy(), len(sessions)),
            "entry_date": np.repeat([d.strftime("%Y-%m-%d") if pd.notna(d) else None for d in entry_dates], n),
            "exit_date": np.repeat([d.strftime("%Y-%m-%d") if pd.notna(d) else None for d in exit_dates], n),
            "forward_return": values.ravel(),
            "exclusion": reasons.ravel(),
        }
    )


def _statistics(values: pd.Series) -> dict:
    clean = values.dropna()
    std = clean.std(ddof=1)
    return {
        "mean": _number(clean.mean()),
        "std": _number(std),
        "ir": _number(clean.mean() / std) if pd.notna(std) and std > 1e-12 else None,
        "positive_fraction": _number((clean > 0).mean()) if len(clean) else None,
        "valid_dates": len(clean),
    }


def _diagnostics(section: pd.DataFrame, config: EvaluationConfig) -> dict:
    """Qlib computes IC; guards distinguish undefined correlations from zero."""
    section = section[["date", "instrument", "entry_date", "exit_date", "forward_return", "prediction"]].reset_index(
        drop=True
    )
    daily, valid = [], np.zeros(len(section), dtype=bool)
    for date, day in section.groupby("date", sort=True):
        usable = day.dropna(subset=["forward_return", "prediction"])
        count = len(usable)
        reason = (
            "insufficient_cross_section"
            if count < config.min_cross_section
            else "constant_score"
            if usable["prediction"].nunique() < 2
            else "constant_return"
            if usable["forward_return"].nunique() < 2
            else None
        )
        record = {
            "date": date,
            "entry_date": day.iloc[0]["entry_date"],
            "exit_date": day.iloc[0]["exit_date"],
            "observations": count,
            "ic": None,
            "rank_ic": None,
            "ic_exclusion": reason,
            "group_returns": {str(i): None for i in range(1, config.groups + 1)},
            "group_counts": {str(i): 0 for i in range(1, config.groups + 1)},
            "high_minus_low": None,
        }
        if reason is None:
            valid[usable.index.to_numpy()] = True
        # Do not split ties by ticker. Equal values receive the same group;
        # an absent end group means its spread is undefined.
        if count >= config.min_cross_section and usable["prediction"].nunique() >= 2:
            percentile = (usable["prediction"].rank(method="average") - 1) / (count - 1)
            groups = (np.floor(percentile * config.groups).clip(0, config.groups - 1) + 1).astype(int)
            for group, members in usable.groupby(groups):
                record["group_returns"][str(group)] = float(members["forward_return"].mean())
                record["group_counts"][str(group)] = len(members)
            low, high = record["group_returns"]["1"], record["group_returns"][str(config.groups)]
            if low is not None and high is not None:
                record["high_minus_low"] = high - low
        daily.append(record)
    if valid.any():
        joined = section.loc[valid, ["date", "instrument", "prediction", "forward_return"]].set_index(
            ["date", "instrument"]
        )
        ic, rank_ic = calc_ic(joined["prediction"], joined["forward_return"], date_col="date")
        for row in daily:
            row["ic"] = _number(ic.get(row["date"], np.nan))
            row["rank_ic"] = _number(rank_ic.get(row["date"], np.nan))
    ic_stats = _statistics(pd.Series([row["ic"] for row in daily], dtype=float))
    rank_stats = _statistics(pd.Series([row["rank_ic"] for row in daily], dtype=float))
    spread = pd.Series([row["high_minus_low"] for row in daily], dtype=float)
    return {
        "summary": {
            "ic": ic_stats,
            "rank_ic": rank_stats,
            "mean_high_minus_low": _number(spread.mean()),
            "spread_dates": int(spread.notna().sum()),
            "signal_dates": len(daily),
            "label_observations": int(section["forward_return"].notna().sum()),
        },
        "groups": [
            {
                "group": i,
                "mean_return": _number(pd.Series([r["group_returns"][str(i)] for r in daily], dtype=float).mean()),
                "dates": sum(r["group_returns"][str(i)] is not None for r in daily),
                "observations": sum(r["group_counts"][str(i)] for r in daily),
            }
            for i in range(1, config.groups + 1)
        ],
        "daily": daily,
    }


def evaluate_factors(
    market: pd.DataFrame,
    factors: dict,
    calendar,
    *,
    start: str,
    end: str,
    config: EvaluationConfig | None = None,
    raw_predictions: pd.DataFrame | None = None,
    row_sink=None,
) -> dict:
    config = config or EvaluationConfig()
    sessions = _sessions(calendar)
    if pd.Timestamp(start) not in sessions or pd.Timestamp(end) not in sessions or start > end:
        raise ValueError("Evaluation start/end must be ordered exchange trading dates")
    history = pd.DataFrame(factors["history"]) if raw_predictions is None else raw_predictions
    if history.empty or history.duplicated(["date", "instrument"]).any():
        raise ValueError("Factor observations must be nonempty and unique")
    if not pd.DatetimeIndex(history["date"]).isin(sessions).all():
        raise ValueError("Factor dates are outside the exchange calendar")
    if not set(history["instrument"]).issubset(set(market["instrument"])):
        raise ValueError("Factor instruments are outside the quote universe")
    names = list(factors["metadata"]["weights"])
    configs = {spec.get("id", spec["name"]): spec["name"] for spec in factors["metadata"].get("factor_config", [])}
    directions = {
        name: FACTOR_LIBRARY[configs.get(name, name)].direction
        if configs.get(name, name) in FACTOR_LIBRARY
        else "lower"
        if name == "volatility"
        else "higher"
        for name in names
    }
    predictions = history[["date", "instrument", "available_at"]].copy()
    for name in names:
        # IC uses direction-aligned raw values, not clipped/weighted composite ranks.
        neutralized = factors["metadata"].get("neutralization", {}).get("mode") == "industry"
        values = (
            history["raw_" + name]
            if raw_predictions is not None
            else history["processed" if neutralized else "raw"].map(itemgetter(name))
        )
        values = pd.to_numeric(values, errors="raise")
        predictions[name] = values if neutralized else values * (-1 if directions[name] == "lower" else 1)
    predictions["composite"] = history["composite"]
    if np.isinf(predictions[names].to_numpy(dtype=float)).any() or not np.isfinite(predictions.composite).all():
        raise ValueError("Factor predictions cannot contain infinity and composite scores must be finite")
    next_session = {d.strftime("%Y-%m-%d"): n.strftime("%Y-%m-%d") for d, n in pairwise(sessions)}
    predictions["entry_date"] = predictions["date"].map(next_session)
    predictions = predictions.loc[predictions["entry_date"].between(start, end)].copy()
    if predictions.empty:
        raise ValueError("No factor signals precede an entry in the evaluation range")
    for row in predictions[["available_at", "entry_date"]].drop_duplicates().itertuples():
        available = pd.Timestamp(row.available_at)
        opening = pd.Timestamp(row.entry_date).tz_localize("Asia/Shanghai") + pd.Timedelta(hours=9, minutes=30)
        if pd.isna(available) or available.tzinfo is None or available > opening:
            raise ValueError("Factor signal is not available before the next-session opening")
    output = {"factors": {name: {"horizons": {}} for name in names + ["composite"]}, "labels": {}, "correlations": {}}
    counts = predictions.groupby("date").size()
    output["signal_coverage"] = [
        {
            "signal_date": day.strftime("%Y-%m-%d"),
            "entry_date": entry.strftime("%Y-%m-%d"),
            "scored_instruments": int(counts.get(day.strftime("%Y-%m-%d"), 0)),
            "universe": int(market["instrument"].nunique()),
        }
        for day, entry in pairwise(sessions)
        if start <= entry.strftime("%Y-%m-%d") <= end
    ]
    for horizon in config.horizons:
        labels = forward_open_labels(market, sessions, horizon, end)
        joined = predictions.drop(columns="entry_date").merge(
            labels, on=["date", "instrument"], how="left", validate="one_to_one"
        )
        coverage = [
            {
                "date": date,
                "scored": len(day),
                "valid_labels": int(day["forward_return"].notna().sum()),
                "exclusions": {str(key): int(count) for key, count in day["exclusion"].dropna().value_counts().items()},
            }
            for date, day in joined.groupby("date", sort=True)
        ]
        observations = joined.loc[
            joined["forward_return"].notna(), ["date", "instrument", "entry_date", "exit_date", "forward_return"]
        ]
        if row_sink is not None:
            for position in range(0, len(observations), 8192):
                row_sink(
                    f"labels/{horizon}/observations", observations.iloc[position : position + 8192].to_dict("records")
                )
        output["labels"][str(horizon)] = {
            "coverage": coverage,
            "observations": observations.to_dict("records") if row_sink is None else [],
            "valid_observations": len(observations),
            "excluded_observations": len(joined) - len(observations),
        }
        if row_sink is not None and len(observations):
            output["labels"][str(horizon)]["observations_storage"] = "external_table"
        for name in names + ["composite"]:
            section = joined[["date", "instrument", "entry_date", "exit_date", "forward_return", name]].rename(
                columns={name: "prediction"}
            )
            output["factors"][name]["horizons"][str(horizon)] = _diagnostics(section, config)
        del labels, joined, observations, section
    pair_daily = {(first, second): [] for i, first in enumerate(names) for second in names[i:]}
    for day, section in predictions.groupby("date", sort=True):
        if len(section) < config.min_cross_section:
            continue
        usable = [name for name in names if section[name].nunique() >= 2]
        correlations = section[usable].corr(method="spearman", min_periods=config.min_cross_section)
        observed = section[usable].notna().astype(int)
        pair_counts = observed.T.dot(observed)
        for first, second in pair_daily:
            if first in usable and second in usable:
                count = int(pair_counts.loc[first, second])
                if count < config.min_cross_section:
                    continue
                pair_daily[first, second].append(
                    {"date": day, "rank_correlation": _number(correlations.loc[first, second]), "observations": count}
                )
    pairs = []
    for i, first in enumerate(names):
        for second in names[i:]:
            daily = pair_daily[first, second]
            pairs.append(
                {
                    "first": first,
                    "second": second,
                    "mean_rank_correlation": _number(
                        pd.Series([r["rank_correlation"] for r in daily], dtype=float).mean()
                    ),
                    "valid_dates": sum(r["rank_correlation"] is not None for r in daily),
                    "daily": daily,
                }
            )
    neutralized = factors["metadata"].get("neutralization", {}).get("mode") == "industry"
    output["correlations"] = {
        "method": "mean daily cross-sectional Spearman on industry residuals"
        if neutralized
        else "mean daily cross-sectional Spearman on direction-aligned raw factors",
        "pairs": pairs,
    }
    output["methodology"] = {
        "ic_engine": "qlib.contrib.eva.alpha.calc_ic",
        "label": "adjusted_open[T+1+h] / adjusted_open[T+1] - 1; h exchange sessions; exit <= evaluation end",
        "signal_range": "entry dates in [start,end]; signal uses preceding session",
        "directions": directions,
        "factor_values": "direction-aligned standardized industry residuals"
        if neutralized
        else "direction-aligned raw values",
        "groups": "Q1 low to QG high; floor(((average_rank-1)/(N-1))*G)+1 capped at G; ties never split by ticker",
        "statistics": "Daily cross-sections have equal weight; IR = mean / sample std without annualization; undefined values are null",
        "missing_values": "Single-factor IC, groups and pairwise correlations exclude missing raw values; composite diagnostics include the configured missing-score policy. No imputed score is treated as an observed factor value.",
        "limitations": [
            "仅为因子诊断，未考虑涨跌停、停牌、费用和成交约束，分组收益不是可交易净值",
            "持有期标签可能重叠，不将分组收益连乘，不提供独立样本显著性或年化ICIR",
            "固定小股票池存在选择与幸存者偏差；当前样本只适合验证工程流程",
            "标签只进入评估，不进入因子计算或Qlib策略；未来价格不足的尾部不填充",
        ],
    }
    return output
