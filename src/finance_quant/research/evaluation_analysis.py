"""Read-only rolling diagnostics derived from immutable evaluation records.

Rows are indexed by signal date, not by the later date on which their return
labels become observable. These diagnostics never feed back into trading.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd


def _number(value):
    return float(value) if pd.notna(value) and np.isfinite(value) else None


def _date(value):
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("日期须为 YYYY-MM-DD") from None
    if parsed.isoformat() != value:
        raise ValueError("日期须为 YYYY-MM-DD")
    return value


def _stats(values):
    clean = values.dropna()
    std = clean.std(ddof=1)
    return {
        "mean": _number(clean.mean()),
        "std": _number(std),
        "ir": _number(clean.mean() / std) if pd.notna(std) and std > 1e-12 else None,
        "positive_fraction": _number((clean > 0).mean()) if len(clean) else None,
        "valid_dates": len(clean),
    }


def evaluation_analysis(
    result, *, horizon, factor="composite", window=60, min_periods=None, start=None, end=None, first=None, second=None
):
    """Calendar-row windows retain invalid days; filtering follows rolling calculation."""
    if isinstance(window, bool) or not isinstance(window, int) or not 2 <= window <= 2520:
        raise ValueError("滚动窗口须为 2–2520 个交易日")
    minimum = window if min_periods is None else min_periods
    if isinstance(minimum, bool) or not isinstance(minimum, int) or not 2 <= minimum <= window:
        raise ValueError("最少有效日期须介于 2 和滚动窗口之间")
    start, end = _date(start), _date(end)
    if start and end and start > end:
        raise ValueError("开始日期不能晚于结束日期")
    if factor not in result["factors"] or str(horizon) not in result["factors"][factor]["horizons"]:
        raise ValueError("所选因子或持有期不存在")
    daily = result["factors"][factor]["horizons"][str(horizon)]["daily"]
    frame = pd.DataFrame(daily, columns=["date", "exit_date", "observations", "ic", "rank_ic"])
    calendar = sorted({row["signal_date"] for row in result.get("signal_coverage", [])})
    if calendar:
        frame = frame.set_index("date").reindex(calendar).rename_axis("date").reset_index()
    else:
        calendar = sorted(frame["date"].tolist())
        frame = frame.sort_values("date").reset_index(drop=True)
    frame["observations"] = frame["observations"].fillna(0).astype(int)
    series = []
    for metric in ("ic", "rank_ic"):
        values = pd.to_numeric(frame[metric], errors="coerce").replace([np.inf, -np.inf], np.nan)
        frame[metric] = values
        rolling = values.rolling(window, min_periods=minimum)
        mean, std = rolling.mean(), rolling.std(ddof=1)
        frame[metric + "_mean"] = mean
        frame[metric + "_ir"] = (mean / std).where(std > 1e-12)
        frame[metric + "_count"] = values.rolling(window, min_periods=1).count().astype(int)
        frame["cumulative_" + metric] = values.cumsum()
    for index, row in frame.iterrows():
        day = row["date"]
        if (start and day < start) or (end and day > end):
            continue
        exits = frame.iloc[max(0, index - window + 1) : index + 1]
        exits = exits.loc[exits.ic.notna() | exits.rank_ic.notna(), "exit_date"].dropna()
        series.append(
            {
                "date": day,
                "observations": int(row["observations"]),
                **{
                    key: _number(row[key])
                    for key in (
                        "ic",
                        "rank_ic",
                        "ic_mean",
                        "rank_ic_mean",
                        "ic_ir",
                        "rank_ic_ir",
                        "cumulative_ic",
                        "cumulative_rank_ic",
                    )
                },
                "ic_count": int(row["ic_count"]),
                "rank_ic_count": int(row["rank_ic_count"]),
                "available_on": max(exits) if len(exits) else None,
            }
        )
    names = [name for name in result["factors"] if name != "composite"]
    if first is None:
        first = names[0] if names else None
    if second is None:
        second = names[1] if len(names) > 1 else first
    if (first is not None and first not in names) or (second is not None and second not in names):
        raise ValueError("相关性因子不存在")
    cells, pair = [], {"first": first, "second": second, "daily": []}
    for item in result["correlations"]["pairs"]:
        rows = [row for row in item["daily"] if (not start or row["date"] >= start) and (not end or row["date"] <= end)]
        values = pd.Series([row["rank_correlation"] for row in rows], dtype=float).replace([np.inf, -np.inf], np.nan)
        cells.append(
            {
                "first": item["first"],
                "second": item["second"],
                "value": _number(values.mean()),
                "mean_absolute": _number(values.abs().mean()),
                "valid_dates": int(values.count()),
            }
        )
        if {item["first"], item["second"]} == {first, second}:
            by_date = {row["date"]: row for row in rows}
            pair["daily"] = [
                by_date.get(day, {"date": day, "rank_correlation": None})
                for day in calendar
                if (not start or day >= start) and (not end or day <= end)
            ]
    visible = pd.DataFrame(series, columns=["ic", "rank_ic"])
    return {
        "factor": factor,
        "horizon": horizon,
        "window": window,
        "min_periods": minimum,
        "start": start,
        "end": end,
        "series": series,
        "summary": {metric: _stats(visible[metric]) for metric in ("ic", "rank_ic")},
        "correlations": {"names": names, "cells": cells, "pair": pair, "method": result["correlations"]["method"]},
        "methodology": {
            "window": "最近 window 个信号交易日，缺失日期占用窗口；有效观察不足时为空，IR 未年化。",
            "availability": "横轴为信号日；available_on 为窗口内已使用收益标签的最晚退出日，届时才可得知统计。",
            "filter": "先在完整评估区间计算滚动统计，再按选择日期展示；相关性按选择区间重新汇总。",
            "usage": "仅用于历史诊断，不作为当日已知信息或交易信号；多日收益标签可能重叠。",
        },
    }
