"""Deterministic synthetic prices and transparent factor scores for acceptance."""

from __future__ import annotations

import numpy as np
import pandas as pd

INSTRUMENTS = [
    {"instrument": "SH600000", "name": "样本甲", "exchange": "SH", "industry": "样本金融"},
    {"instrument": "SZ000001", "name": "样本乙", "exchange": "SZ", "industry": "样本制造"},
    {"instrument": "SH600519", "name": "样本丙", "exchange": "SH", "industry": "样本消费"},
]
WEIGHTS = {"momentum": 0.4, "value": 0.3, "quality": 0.3}
NOTICE = "合成样本，仅验证系统流程，不代表真实股票表现"


def sample_prices() -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-02", "2025-01-17")
    steps = np.arange(len(dates))
    records = []
    for index, instrument in enumerate(INSTRUMENTS):
        close = (10 + index * 5) * (
            1 + 0.003 * steps + 0.025 * np.sin(steps * 0.8 + index * 2)
        )
        for step, date in enumerate(dates):
            opening = close[max(0, step - 1)] * (1 + 0.002 * np.cos(step + index))
            records.append({
                **instrument, "date": date, "open": float(opening),
                "close": float(close[step]),
                "high": float(max(opening, close[step]) * 1.01),
                "low": float(min(opening, close[step]) * 0.99),
                "volume": 1_000_000.0, "factor": 1.0,
                "pe": float(8 + ((step // 3 + index) % 3) * 7),
                "roe": float(0.08 + ((step // 4 + index) % 3) * 0.04),
            })
    return pd.DataFrame(records)


def sample_factors(prices: pd.DataFrame) -> dict:
    required = {"instrument", "date", "close", "pe", "roe", "name", "industry"}
    if prices.empty or not required.issubset(prices.columns):
        raise ValueError("Sample factor data are empty or missing columns")
    if prices.duplicated(["instrument", "date"]).any():
        raise ValueError("Duplicate instrument/date in factor inputs")
    if not np.isfinite(prices[["close", "pe", "roe"]].to_numpy(dtype=float)).all():
        raise ValueError("Sample factor inputs must be finite")
    if (prices[["close", "pe"]] <= 0).any().any():
        raise ValueError("Sample prices and PE must be positive")
    frame = prices.sort_values(["instrument", "date"]).copy()
    frame["momentum"] = frame.groupby("instrument")["close"].pct_change(2, fill_method=None)
    frame["value"] = 1 / frame["pe"]
    frame["quality"] = frame["roe"]
    frame = frame.dropna(subset=list(WEIGHTS))
    if frame.empty:
        raise ValueError("Insufficient history to calculate sample factors")
    history = []
    for date, cross_section in frame.groupby("date", sort=True):
        # All three raw factors have an explicit higher-is-preferred direction.
        scores = cross_section[list(WEIGHTS)].rank(pct=True) * 100
        composite = sum(scores[name] * weight for name, weight in WEIGHTS.items())
        ranks = composite.rank(method="min", ascending=False).astype(int)
        for row_index, row in cross_section.iterrows():
            history.append({
                "instrument": row["instrument"], "name": row["name"],
                "industry": row["industry"], "date": date.strftime("%Y-%m-%d"),
                "raw": {name: float(row[name]) for name in WEIGHTS},
                "scores": {name: float(scores.loc[row_index, name]) for name in WEIGHTS},
                "composite": float(composite.loc[row_index]), "rank": int(ranks.loc[row_index]),
            })
    return {
        "metadata": {
            "id": "demo-factors", "name": "样本多因子评分", "as_of": history[-1]["date"],
            "universe": "合成样本股票池", "sample_size": len(INSTRUMENTS),
            "scheme": "demo-v1", "data_version": "synthetic-v1", "is_synthetic": True,
            "weights": WEIGHTS, "score_unit": "percentile_0_100",
            "direction": "高分表示更符合样本因子的偏好，不代表上涨概率",
            "definitions": {"momentum": "过去两个交易日收盘收益率", "value": "样本市盈率的倒数", "quality": "样本净资产收益率"},
            "notice": NOTICE,
        },
        "history": history,
    }
