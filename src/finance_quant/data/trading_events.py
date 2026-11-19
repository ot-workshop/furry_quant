"""Dated no-band candidates; prices and reported returns must still validate them.

Historical name changes are supplier evidence, not exchange-certified event data.
Only the first dated delisting name with a prior announcement and a preceding
non-delisting name is considered. Current names and future delist dates are unused.
"""

from __future__ import annotations

import pandas as pd

from finance_quant.data.china_a_rules import ipo_no_band_mask
from finance_quant.data.symbols import to_qlib


def trading_events(code, listed, history, sessions):
    sessions = pd.DatetimeIndex(sessions)
    instrument = to_qlib(code)
    listed = pd.Timestamp(listed)
    events = [
        {
            "code": code,
            "date": day.strftime("%Y-%m-%d"),
            "known_by": listed.strftime("%Y-%m-%d"),
            "known_at_open": True,
            "event": "ipo_first_five_sessions",
            "no_daily_price_limit": True,
            "source": "tushare:stock_basic.list_date+exchange_calendar+dated_board_rule",
            "evidence_basis": "listing_date_and_board_rule",
        }
        for day in sessions[ipo_no_band_mask(instrument, listed, sessions, sessions)]
    ]
    if history.empty:
        return events
    names = history.loc[history.instrument.eq(instrument)].copy()
    for column in ("start_date", "ann_date"):
        names[column] = pd.to_datetime(names[column])
    names["terminal"] = names.name.str.fullmatch(r"(?:退市.+|.+退)", na=False)
    candidates = names.loc[names.terminal].sort_values("start_date")
    if candidates.empty:
        return events
    # The 2020-08 IPO/20% ChiNext reform is not the delisting first-day reform.
    cutoff = pd.Timestamp("2021-01-01")
    for day in candidates.start_date.drop_duplicates():
        known = names.loc[names.ann_date.lt(day) & names.start_date.le(day)]
        records = known.loc[known.start_date.eq(day)]
        prior = known.loc[known.start_date.lt(day)]
        # Evaluate only information visible then, including when later corrections
        # carry an earlier effective date. Future end dates are deliberately unused.
        if (
            day < cutoff
            or day not in sessions
            or records.empty
            or records.name.nunique() != 1
            or not records.terminal.all()
            or prior.empty
            or prior.terminal.any()
        ):
            continue
        events.append(
            {
                "code": code,
                "date": day.strftime("%Y-%m-%d"),
                "known_by": max(records.ann_date.max(), cutoff - pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                "event": "delisting_consolidation_first_session",
                "no_daily_price_limit": True,
                "source": "tushare:namechange+dated_delisting_rule",
                "evidence_basis": "historical_namechange_and_return_identities",
                "point_in_time_certified": False,
                "historical_name": records.name.iloc[0],
            }
        )
        break
    return events
