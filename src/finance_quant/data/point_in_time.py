"""Financial visibility rules, independent of a provider's announcement fields.

Adapters must supply verified, timezone-aware available_at timestamps. This module
cannot reconstruct missing revision history from a vendor's latest-only values.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import pandas as pd


def _aware_timestamp(value, name: str) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError(f"{name} must include a timezone and a known availability time")
    return stamp.tz_convert("UTC")


EXCHANGE_TZ = "Asia/Shanghai"
SESSION_OPEN = pd.Timedelta(hours=9, minutes=30)


def _sessions(trading_sessions: Sequence) -> pd.DatetimeIndex:
    sessions = pd.DatetimeIndex(pd.to_datetime(list(trading_sessions), errors="raise"))
    if sessions.tz is not None or sessions.hasnans or not sessions.equals(sessions.normalize()):
        raise ValueError("trading_sessions must contain exchange-local dates")
    return sessions.sort_values().unique()


def availability_from_announcement_dates(announcement_dates, trading_sessions: Sequence) -> pd.Series:
    """Vectorized :func:`availability_from_announcement_date`.

    Returns UTC timestamps; ``NaT`` where the date is missing or the calendar does not
    extend beyond it. Callers decide whether such rows are quarantined.
    """
    days = pd.Series(pd.to_datetime(pd.Series(announcement_dates), errors="raise"))
    if days.dt.tz is not None or not days.dropna().equals(days.dropna().dt.normalize()):
        raise ValueError("announcement dates must be dates without an assumed publication time")
    sessions = _sessions(trading_sessions)
    positions = sessions.searchsorted(days.to_numpy(), side="right")
    known = days.notna().to_numpy() & (positions < len(sessions))
    result = pd.Series(pd.NaT, index=days.index, dtype="datetime64[ns, UTC]")
    if known.any():
        opens = (sessions[positions[known]] + SESSION_OPEN).tz_localize(EXCHANGE_TZ).tz_convert("UTC")
        result.loc[known] = pd.Series(opens, index=days.index[known])
    return result


def availability_from_announcement_date(announcement_date: str | date, trading_sessions: Sequence) -> pd.Timestamp:
    """Conservatively make date-only announcements usable next session at open.

    The caller supplies an official trading calendar; no weekday-only inference.
    This intentionally does not make the report visible at announcement-day close.
    """
    day = pd.Timestamp(announcement_date)
    if pd.isna(day) or day.tzinfo is not None or day != day.normalize():
        raise ValueError("announcement_date must be a date without an assumed publication time")
    available = availability_from_announcement_dates([day], trading_sessions).iloc[0]
    if pd.isna(available):
        raise ValueError("Trading calendar does not extend beyond the announcement date")
    return available.tz_convert(EXCHANGE_TZ)


def financial_history_as_of(reports: pd.DataFrame, as_of, periods: int | None = None) -> pd.DataFrame:
    """Select visible revisions first, then the last N report periods per stock.

    Required fields: instrument, report_period, available_at, revision. Metric
    columns are passed through. report_period is never used as availability time.
    """
    cutoff = _aware_timestamp(as_of, "as_of")
    required = {"instrument", "report_period", "available_at", "revision"}
    missing = required - set(reports.columns)
    if missing:
        raise ValueError(f"Missing financial visibility fields: {sorted(missing)}")
    if periods is not None and (isinstance(periods, bool) or not isinstance(periods, int) or periods <= 0):
        raise ValueError("periods must be a positive integer")
    frame = reports.copy()
    if frame.empty:
        return frame
    if frame[list(required)].isna().any().any():
        raise ValueError("Financial visibility fields cannot be missing")
    if not frame["instrument"].map(lambda value: isinstance(value, str) and bool(value.strip())).all():
        raise ValueError("Financial instruments must be nonempty identifiers")
    frame["available_at"] = pd.to_datetime(
        [_aware_timestamp(value, "available_at") for value in frame["available_at"]], utc=True
    )
    frame["report_period"] = pd.to_datetime(frame["report_period"], errors="raise")
    if frame["report_period"].dt.tz is not None or not frame["report_period"].equals(
        frame["report_period"].dt.normalize()
    ):
        raise ValueError("report_period must contain dates without intraday times")
    revisions = pd.to_numeric(frame["revision"], errors="raise")
    if ((revisions < 0) | (revisions % 1 != 0)).any():
        raise ValueError("revision must be a nonnegative integer")
    frame["revision"] = revisions.astype("int64")
    if frame.duplicated(["instrument", "report_period", "revision"]).any():
        raise ValueError("Duplicate financial revision; resolve source conflicts before research")
    local_date = frame["available_at"].dt.tz_convert("Asia/Shanghai").dt.tz_localize(None).dt.normalize()
    if (local_date < frame["report_period"]).any():
        raise ValueError("Financial report cannot be available before its report period ends")
    # Filtering happens BEFORE any tail/head or revision selection.
    visible = frame[frame["available_at"] <= cutoff]
    visible = visible.sort_values(["instrument", "report_period", "available_at", "revision"])
    visible = visible.drop_duplicates(["instrument", "report_period"], keep="last")
    if periods is not None:
        visible = visible.groupby("instrument", group_keys=False).tail(periods)
    return visible.reset_index(drop=True)


def latest_financials_as_of(reports: pd.DataFrame, as_of) -> pd.DataFrame:
    return financial_history_as_of(reports, as_of, periods=1)
