"""Daily auction model: dated no-band sessions and board-specific quantities."""
from __future__ import annotations
import math
import numpy as np
import pandas as pd

RULE_VERSION='china-a-daily-auction-v2'

def board(instrument):
    if instrument.startswith('SH688'):return 'star'
    if instrument.startswith('SZ30'):return 'chinext'
    if instrument.startswith('SH60') or instrument.startswith('SZ00'):return 'main'
    raise ValueError('动态回测尚不支持该证券板块：'+instrument)

def max_order_shares(instrument,day=None):
    kind=board(instrument)
    if kind=='star':return 100000
    if kind=='chinext' and (day is None or pd.Timestamp(day)>=pd.Timestamp('2020-08-24')):return 300000
    return 1000000

def round_shares(amount,factor,instrument,day=None):
    if not np.isfinite([amount,factor]).all() or factor<=0:return 0.
    real=min(max_order_shares(instrument,day),max(0,math.floor(amount*factor+1e-4)))
    if board(instrument)=='star':
        return min(real,100000)/factor if real>=200 else 0.
    return (real//100*100)/factor

def apply_execution_rules(frame,sessions):
    out=frame.copy();out['has_daily_price_limit']=True
    for instrument,part in out.groupby('instrument'):
        kind=board(instrument)
        listed=pd.Timestamp(part.list_date.iloc[0])
        age=sessions.get_indexer(part.date)-sessions.searchsorted(listed)+1
        no_band=(age>=1)&(age<=5)&(listed in sessions)&(kind=='star' or (kind=='chinext' and listed>=pd.Timestamp('2020-08-24'))
            or (kind=='main' and listed>=pd.Timestamp('2023-04-10')))
        reviewed=part.get('reviewed_unbounded_session',pd.Series(False,index=part.index)).fillna(False).to_numpy()
        out.loc[part.index,'has_daily_price_limit']=~(no_band|reviewed)
    valid=np.isfinite(out.up_limit)&np.isfinite(out.down_limit)&out.down_limit.gt(0)&out.up_limit.gt(out.down_limit)
    unknown=out.has_daily_price_limit&~valid&out.execution_available
    out.loc[unknown,'execution_available']=False
    out.loc[unknown,'execution_state']='unknown_price_limits'
    out.loc[~out.has_daily_price_limit|~valid,['up_limit','down_limit']]=0.
    return out
