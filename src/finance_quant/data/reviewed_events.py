"""Dated public evidence for individually reviewed unbounded trading sessions.

This registry never changes prices or factors. An exception additionally needs
both sources' prices and reported/adjusted returns to agree on the exact day.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

REVIEWED_EVENTS = (
    {'code': '000792.SZ', 'date': '2021-08-10', 'known_by': '2021-08-03',
     'event': 'listing_resumption', 'no_daily_price_limit': True,
     'source': 'https://money.finance.sina.com.cn/corp/view/vISSUE_MarketBulletinDetail.php?id=7413220&stockid=000792'},
    {'code': '002411.SZ', 'date': '2023-06-19', 'known_by': '2023-06-13',
     'event': 'delisting_consolidation_first_session', 'no_daily_price_limit': True,
     'source': 'https://3g.zszq.com/ywgg/bulletin?id=LrgO1Iwz'},
)


def verified_event_moves(primary, secondary, code, events):
    """Accept only a real, independently matching move known before that day."""
    one = (primary.close * primary.adj_factor).pct_change(fill_method=None)
    two = (secondary.close * secondary.adj_factor).pct_change(fill_method=None)
    verified = []
    for event in events:
        day = pd.Timestamp(event['date'])
        if (event['code'] != code or not event.get('no_daily_price_limit')
                or not event.get('source') or pd.Timestamp(event['known_by']) >= day
                or day not in primary.index or day not in secondary.index):
            continue
        values = [one.loc[day], two.loc[day], primary.loc[day, 'change'], secondary.loc[day, 'change']]
        if (np.isfinite(values).all() and abs(values[0]) > .5
                and abs(values[0] - values[1]) <= .0002
                and abs(values[0] - values[2]) <= .0002
                and abs(values[1] - values[3]) <= .0002
                and abs(primary.loc[day, 'close'] / primary.loc[day, 'pre_close'] - 1 - values[2]) <= .0002):
            verified.append({**event, 'adjusted_return': float(values[0]),
                             'secondary_return': float(values[1])})
    return verified
