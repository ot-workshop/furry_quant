"""Exact daily suspension evidence; intraday interruptions are never full halts."""
from __future__ import annotations
import numpy as np
import pandas as pd

class AmbiguousSuspensionError(ValueError):
    """The official status table cannot decide a full-day halt on its own."""


def full_day_halts(raw, code, start, end, market):
    required={'ts_code','trade_date','suspend_type','suspend_timing'}
    if not required.issubset(raw):raise ValueError('Official suspension response schema is incomplete')
    if raw.empty:return pd.DatetimeIndex([])
    if not raw.ts_code.eq(code).all():raise ValueError('Official suspension instrument mismatch')
    dates=pd.to_datetime(raw.trade_date,format='%Y%m%d',errors='raise')
    if not dates.between(pd.Timestamp(start),pd.Timestamp(end)).all():raise ValueError('Official suspension dates outside request')
    if not raw.suspend_type.isin(['S','R']).all():raise ValueError('Unknown official suspension type')
    timing=raw.suspend_timing.fillna('').astype(str).str.strip()
    full=raw.suspend_type.eq('S')&timing.eq('')
    selected=pd.DatetimeIndex(dates[full]).unique().sort_values()
    # Ambiguous same-day resume or intraday records cannot certify an entire day.
    ambiguous=pd.DatetimeIndex(dates[~full])
    if len(selected.intersection(ambiguous)):raise AmbiguousSuspensionError('Conflicting official full-day suspension records')
    rows=market.loc[market.date.isin(selected)]
    for field in ['volume','amount']:
        observed=pd.to_numeric(rows[field],errors='coerce')
        if (np.isfinite(observed)&observed.ne(0)).any():raise ValueError('Official full-day suspension conflicts with trading activity')
    return selected.intersection(pd.DatetimeIndex(market.date))
