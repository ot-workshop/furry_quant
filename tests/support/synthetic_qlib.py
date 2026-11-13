"""Test fixture: a small, actual Qlib backtest on deterministic synthetic prices."""

from __future__ import annotations

import platform
from pathlib import Path

import numpy as np
import pandas as pd

from finance_quant.checksums import sha256_text
from finance_quant.integrations.qlib_runner import run_qlib
from support.synthetic import INSTRUMENTS, NOTICE

INITIAL_CASH = 100_000.0


def validate_sample_prices(prices: pd.DataFrame) -> None:
    required = {"instrument", "date", "open", "high", "low", "close", "volume", "factor"}
    if prices.empty or not required.issubset(prices.columns):
        raise ValueError("Sample prices are empty or missing required columns")
    expected_symbols = {row["instrument"] for row in INSTRUMENTS}
    if set(prices["instrument"]) != expected_symbols:
        raise ValueError("This demo adapter only supports its three declared synthetic instruments")
    if not pd.api.types.is_datetime64_any_dtype(prices["date"]) or prices["date"].dt.tz is not None:
        raise ValueError("Sample dates must be timezone-naive daily timestamps")
    if prices["date"].isna().any() or not prices["date"].equals(prices["date"].dt.normalize()):
        raise ValueError("Sample dates must be valid daily timestamps")
    if prices.duplicated(["instrument", "date"]).any():
        raise ValueError("Duplicate instrument/date in sample prices")
    numeric = prices[["open", "high", "low", "close", "volume", "factor"]].to_numpy(dtype=float)
    if not np.isfinite(numeric).all() or (numeric[:, :4] <= 0).any() or (numeric[:, 4] <= 0).any():
        raise ValueError(
            "Sample execution prices and volumes must be finite and positive; missing bars are unsupported"
        )
    if not (prices["factor"] == 1).all():
        raise ValueError("Sample adapter has not validated corporate actions or changing adjustment factors")
    if (
        (prices["high"] < prices[["open", "close", "low"]].max(axis=1))
        | (prices["low"] > prices[["open", "close", "high"]].min(axis=1))
    ).any():
        raise ValueError("Inconsistent sample OHLC prices")
    expected_dates = pd.bdate_range("2025-01-02", "2025-01-17")
    for _, group in prices.groupby("instrument"):
        if not pd.DatetimeIndex(group["date"].sort_values()).equals(expected_dates):
            raise ValueError("Sample calendar is incomplete; refuse shifted Qlib feature dates")


def export_sample(prices: pd.DataFrame, destination: Path) -> str:
    """Write documented Qlib local-provider layout with little-endian float32."""
    validate_sample_prices(prices)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "calendars").mkdir(exist_ok=True)
    (destination / "instruments").mkdir(exist_ok=True)
    dates = sorted(prices["date"].unique())
    # Calendar padding lets Qlib determine the final bar's end without future quotes.
    calendar = pd.bdate_range(pd.Timestamp(dates[0]), "2025-01-31")
    (destination / "calendars/day.txt").write_text("\n".join(calendar.strftime("%Y-%m-%d")) + "\n", encoding="utf-8")
    instrument_lines = []
    all_frames = []
    for instrument, group in prices.groupby("instrument", sort=True):
        all_frames.append((instrument, group.set_index("date")))
    close = prices.pivot(index="date", columns="instrument", values="close")
    benchmark_close = (close / close.iloc[0]).mean(axis=1) * 100
    benchmark = pd.DataFrame({field: benchmark_close for field in ["open", "close", "high", "low"]})
    benchmark["volume"] = 1_000_000.0
    benchmark["factor"] = 1.0
    all_frames.append(("SH000300", benchmark))
    for instrument, frame in all_frames:
        feature_dir = destination / "features" / instrument.lower()
        feature_dir.mkdir(parents=True, exist_ok=True)
        frame = frame.sort_index()
        frame["change"] = frame["close"].pct_change().fillna(0.0)
        for field in ["open", "close", "high", "low", "volume", "factor", "change"]:
            np.concatenate(([0.0], frame[field].to_numpy())).astype("<f4").tofile(feature_dir / f"{field}.day.bin")
        instrument_lines.append(f"{instrument}\t{frame.index[0]:%Y-%m-%d}\t{frame.index[-1]:%Y-%m-%d}")
    (destination / "instruments/all.txt").write_text("\n".join(instrument_lines) + "\n", encoding="utf-8")
    source = prices.to_csv(index=False, float_format="%.12g", lineterminator="\n")
    (destination / "source.csv").write_text(source, encoding="utf-8")
    return sha256_text(source)


def execute_qlib(
    prices: pd.DataFrame,
    factors: dict,
    data_dir: Path,
    initial_cash: float = INITIAL_CASH,
    topk: int = 2,
    open_cost: float = 0.001,
    close_cost: float = 0.001,
    min_cost: float = 5.0,
) -> dict:

    source_hash = export_sample(prices, data_dir)
    quotes = prices.copy()
    quotes["change"] = quotes.groupby("instrument")["close"].pct_change(fill_method=None).fillna(0.0)
    quotes["up_limit"], quotes["down_limit"] = 0.0, 0.0
    close = prices.pivot(index="date", columns="instrument", values="close")
    benchmark_close = (close / close.iloc[0]).mean(axis=1) * 100
    benchmark = pd.DataFrame({field: benchmark_close for field in ["open", "close", "high", "low"]})
    benchmark["instrument"] = "SH000300"
    benchmark["date"] = benchmark.index
    benchmark["volume"], benchmark["factor"] = 1_000_000.0, 1.0
    benchmark["up_limit"], benchmark["down_limit"] = 0.0, 0.0
    benchmark["change"] = benchmark["close"].pct_change(fill_method=None).fillna(0.0)
    quotes = pd.concat([quotes, benchmark.reset_index(drop=True)], ignore_index=True)
    result = run_qlib(
        quotes,
        factors,
        data_dir,
        calendar=pd.bdate_range("2025-01-02", "2025-01-31"),
        start="2025-01-07",
        end="2025-01-17",
        initial_date="2025-01-06",
        names={r["instrument"]: r["name"] for r in INSTRUMENTS},
        initial_cash=initial_cash,
        topk=topk,
        open_cost=open_cost,
        close_cost=close_cost,
        min_cost=min_cost,
    )
    result["summary"].update(id="demo-qlib", name="Qlib 样本轮换策略", is_synthetic=True)
    result["metadata"].update(
        data_version="synthetic-v1",
        source_sha256=source_hash,
        python_version=platform.python_version(),
        limitations=[
            NOTICE,
            "样本无停牌、退市、分红送转；这些口径尚未验收",
            "样本关闭涨跌停限制，不代表真实A股规则",
            "成交采用下一交易日开盘价，未模拟盘中排队与滑点",
            "样本基准为三个合成价格序列的均值，并非真实沪深300",
            "仅验证日线流程，未提供完整委托拒单原因",
        ],
    )
    return result
