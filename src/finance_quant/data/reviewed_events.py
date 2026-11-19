"""Dated public evidence for individually reviewed unbounded trading sessions.

This registry never changes prices or factors. Event dates still require price
and return identities; the caller records whether one or two sources were checked.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

REVIEWED_EVENTS = (
    {
        "code": "000995.SZ",
        "date": "2020-12-16",
        "known_by": "2020-12-09",
        "event": "listing_resumption",
        "no_daily_price_limit": True,
        "source": "https://static.cninfo.com.cn/finalpage/2020-12-09/1208871265.PDF",
    },
    {
        "code": "000792.SZ",
        "date": "2021-08-10",
        "known_by": "2021-08-03",
        "event": "listing_resumption",
        "no_daily_price_limit": True,
        "source": "https://money.finance.sina.com.cn/corp/view/vISSUE_MarketBulletinDetail.php?id=7413220&stockid=000792",
    },
    {
        "code": "000670.SZ",
        "date": "2022-08-22",
        "known_by": "2022-08-15",
        "event": "listing_resumption",
        "no_daily_price_limit": True,
        "source": "https://disc.static.szse.cn/download/disc/disk03/finalpage/2022-08-15/87fef1d8-b6ce-4561-b0d0-d152df0772a0.PDF",
    },
    {
        "code": "002411.SZ",
        "date": "2023-06-19",
        "known_by": "2023-06-13",
        "event": "delisting_consolidation_first_session",
        "no_daily_price_limit": True,
        "source": "https://3g.zszq.com/ywgg/bulletin?id=LrgO1Iwz",
    },
)


def _legacy_suspension(primary, day):
    """A halt continuing from the old regime needs an explicit notice, not a name inference."""
    prior = primary.loc[primary.index < day]
    if prior.empty or "is_suspended" not in prior:
        return False
    active = np.flatnonzero(~prior.is_suspended.fillna(False).to_numpy(bool))
    tail = prior.index[active[-1] + 1 if len(active) else 0 :]
    return len(tail) > 0 and tail[0] < pd.Timestamp("2021-01-01")


def verified_event_moves(primary, secondary, code, events):
    """Check the supplied source pair against the dated event and return identities."""
    one = (primary.close * primary.adj_factor).pct_change(fill_method=None)
    two = (secondary.close * secondary.adj_factor).pct_change(fill_method=None)
    verified = []
    for event in events:
        day = pd.Timestamp(event["date"])
        if (
            event["code"] != code
            or not event.get("no_daily_price_limit")
            or not event.get("source")
            or pd.Timestamp(event["known_by"]) > day
            or (
                pd.Timestamp(event["known_by"]) == day
                and not (event.get("known_at_open") and event["event"] == "ipo_first_five_sessions")
            )
            or day not in primary.index
            or day not in secondary.index
        ):
            continue
        if event.get("evidence_basis") == "historical_namechange_and_return_identities" and _legacy_suspension(
            primary, day
        ):
            continue
        values = [one.loc[day], two.loc[day], primary.loc[day, "change"], secondary.loc[day, "change"]]
        opening_ipo = (
            event["event"] == "ipo_first_five_sessions"
            and day == pd.Timestamp(event["known_by"])
            and day == primary.index.min() == secondary.index.min()
            and np.isnan(values[:2]).all()
            and np.isfinite(values[2:]).all()
            and abs(values[2] - values[3]) <= 0.0002
        )
        matching_returns = (
            np.isfinite(values).all()
            and abs(values[0] - values[1]) <= 0.0002
            and abs(values[0] - values[2]) <= 0.0002
            and abs(values[1] - values[3]) <= 0.0002
        )
        if (
            (matching_returns or opening_ipo)
            and abs(primary.loc[day, "close"] / primary.loc[day, "pre_close"] - 1 - values[2]) <= 0.0002
            and abs(secondary.loc[day, "close"] / secondary.loc[day, "pre_close"] - 1 - values[3]) <= 0.0002
        ):
            verified.append(
                {
                    **event,
                    "adjusted_return": None if opening_ipo else float(values[0]),
                    "secondary_return": None if opening_ipo else float(values[1]),
                    "return_basis": "ipo_reference_price" if opening_ipo else "observed_previous_close",
                }
            )
    return verified
