"""Conservative consistency checks for the currently supported daily equity data."""

from __future__ import annotations

import numpy as np
import pandas as pd


def adjustment_discontinuities(market: pd.DataFrame) -> list[dict]:
    frame = market.sort_values(["instrument", "date"]).copy()
    adjusted = frame["close"] * frame["adj_factor"]
    frame["adjusted_return"] = adjusted.groupby(frame["instrument"]).pct_change(fill_method=None)
    frame["previous_date"] = frame.groupby("instrument")["date"].shift()
    # These are audit thresholds, not exchange trading rules. Unusual corporate
    # actions need explicit verification instead of automatic rescaling/filling.
    invalid = frame["adjusted_return"].abs() > 0.5
    if "change" in frame:
        invalid |= (frame["adjusted_return"] - frame["change"]).abs() > 0.05
    result = []
    for row in frame.loc[invalid].itertuples():
        result.append(
            {
                "instrument": row.instrument,
                "date": row.date.strftime("%Y-%m-%d"),
                "previous_date": row.previous_date.strftime("%Y-%m-%d"),
                "adjusted_return": float(row.adjusted_return),
                "reported_change": float(row.change) if hasattr(row, "change") and np.isfinite(row.change) else None,
            }
        )
    return result
