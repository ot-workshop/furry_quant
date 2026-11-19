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


def published_event_mask(market: pd.DataFrame) -> pd.Series:
    """Read only persisted event flags on source-validated shared warehouse rows."""
    mask = pd.Series(False, index=market.index)
    if not {"reviewed_unbounded_session", "validation_level"}.issubset(market):
        return mask
    if not pd.api.types.is_bool_dtype(market.reviewed_unbounded_session.dtype):
        return mask
    identity = (market.close / market.pre_close - 1 - market.change).abs().le(0.0002)
    return (
        market.reviewed_unbounded_session.fillna(False)
        & market.validation_level.isin(["single_source_passed", "dual_source_verified", "secondary_fallback_passed"])
        & identity
    )


def published_adjustment_discontinuities(market: pd.DataFrame) -> list[dict]:
    """Re-check event return identities when loading an already validated shared projection.

    Acquisition continues to use the strict raw audit above. A boolean alone does
    not exempt a broken adjustment factor, and legacy/unvalidated rows get no exemption.
    """
    known = market.loc[published_event_mask(market), ["instrument", "date"]]
    keys = set(zip(known.instrument, known.date.dt.strftime("%Y-%m-%d"), strict=True))
    return [
        audit
        for audit in adjustment_discontinuities(market)
        if (audit["instrument"], audit["date"]) not in keys
        or audit["reported_change"] is None
        or abs(audit["adjusted_return"] - audit["reported_change"]) > 0.0002
    ]
