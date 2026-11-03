"""Explicit calendar-only date planning; never downloads stock quotes."""
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from filelock import FileLock

from finance_quant.configuration import DataConfig, BacktestConfig, ResearchConfig
from finance_quant.data.market import SnapshotFetcher
from finance_quant.data.providers import TushareProvider, TushareSettings, normalize_calendar
from finance_quant.research.registry import FactorConfig, FactorSpec


def _dates(document):
    automatic = isinstance(document, dict) and document.get('auto_range') is True
    fields = ({'auto_range','backtest_start','backtest_end','factors'} if automatic else
              {'data_start','data_end','backtest_start','backtest_end','factors'})
    if not isinstance(document,dict) or set(document)!=fields:
        raise ValueError('日期方案字段不完整或包含不支持的参数')
    for key in fields-{'factors','auto_range'}:
        value=document[key]
        if not isinstance(value,str) or date.fromisoformat(value).isoformat()!=value:
            raise ValueError('日期必须使用YYYY-MM-DD格式')
    if not isinstance(document['factors'],list): raise ValueError('请选择因子')
    specs=[]
    for item in document['factors']:
        if not isinstance(item,dict) or not {'name','weight'}.issubset(item) or set(item)-{'name','weight','window','skip','baseline_window','id'}:
            raise ValueError('因子参数无效')
        specs.append(FactorSpec(**item))
    factors=FactorConfig(tuple(specs))
    data=DataConfig(Path('calendar'),document.get('data_start',document['backtest_start']),
                    document.get('data_end',document['backtest_end']),('600000.SH','000001.SZ'))
    backtest=BacktestConfig(document['backtest_start'],document['backtest_end'],topk=1)
    ResearchConfig(data,backtest,Path('calendar-results'),factors)
    if date.fromisoformat(document.get('data_end',document['backtest_end']))>datetime.now(ZoneInfo('Asia/Shanghai')).date():
        raise ValueError('数据结束日期不能晚于今天')
    return factors


def make_date_plan(document, frames):
    """All proposals use validated sessions; padding only bounds calendar queries."""
    factors=_dates(document)
    automatic = document.get('auto_range') is True
    document = {**document, 'data_start':document.get('data_start',document['backtest_start']),
                'data_end':document.get('data_end',document['backtest_end'])}
    required=max(s.lookback for s in factors.specs)+1
    days=pd.concat(frames,ignore_index=True).sort_values('date')
    if days.duplicated('date').any(): raise ValueError('交易日历重复，不能规划日期')
    blocks=[group for _,group in days.groupby(days.date.diff().ne(pd.Timedelta(days=1)).cumsum())]
    sessions=pd.DatetimeIndex(days.loc[days.is_open,'date'])
    begin,end,bt,bt_end=map(pd.Timestamp,(document['data_start'],document['data_end'],document['backtest_start'],document['backtest_end']))
    start_block=next((g for g in blocks if g.date.iloc[0]<=bt<=g.date.iloc[-1]),None)
    end_block=next((g for g in blocks if g.date.iloc[0]<=bt_end<=g.date.iloc[-1]),None)
    if start_block is None or end_block is None: raise ValueError('回测起止日缺少完整日历覆盖')
    entries=sessions[(sessions>=bt)&(sessions<=min(bt_end,start_block.date.iloc[-1]))]
    exits=sessions[(sessions>=max(bt,end_block.date.iloc[0]))&(sessions<=bt_end)]
    if entries.empty or exits.empty: raise ValueError('回测起止区间没有可用交易日')
    first,last=entries[0],exits[-1]
    entry_block=next(group for group in blocks if group.date.iloc[0]<=first<=group.date.iloc[-1])
    history=sessions[(sessions>=entry_block.date.iloc[0])&(sessions<first)]
    begin_block=next((g for g in blocks if g.date.iloc[0]<=begin<=g.date.iloc[-1]),None)
    after_begin=sessions[(sessions>=begin)&(sessions<=min(end,begin_block.date.iloc[-1]))] if begin_block is not None else pd.DatetimeIndex([])
    proposals=[]
    def add(mode,start,entry):
        if entry>=last: return
        block=next((g for g in blocks if g.date.iloc[0]<=entry<=g.date.iloc[-1]),None)
        if block is None:return
        available=int(((sessions>=max(start,block.date.iloc[0]))&(sessions<entry)).sum())
        if available<required: return
        prior=sessions[(sessions>=start)&(sessions<entry)]
        proposal={'mode':mode,'data_start':start.strftime('%Y-%m-%d'),
                  'data_end':last.strftime('%Y-%m-%d') if automatic else document['data_end'],
                  'backtest_start':entry.strftime('%Y-%m-%d'),'backtest_end':last.strftime('%Y-%m-%d'),'history_days':available,
                  'warmup_start':start.strftime('%Y-%m-%d'),'warmup_end':prior[-1].strftime('%Y-%m-%d')}
        if not any(all(p[k]==proposal[k] for k in ('data_start','backtest_start','backtest_end')) for p in proposals):proposals.append(proposal)
    if len(history)>=required:add('auto_range' if automatic else 'extend_history',history[-required] if automatic else min(begin,history[-required]),first)
    if not automatic and len(after_begin)>required:add('delay_backtest',begin,max(first,after_begin[required]))
    if not proposals: raise ValueError('可用日历不足以满足当前窗口，请扩大数据区间或缩短窗口')
    calendars=[]
    # Keep disjoint ranges disjoint: no fabricated sessions across unqueried years.
    for group in blocks:
        calendars.append({'start':group.date.iloc[0].strftime('%Y-%m-%d'),'end':group.date.iloc[-1].strftime('%Y-%m-%d'),
                          'trading_dates':group.loc[group.is_open,'date'].dt.strftime('%Y-%m-%d').tolist()})
    return {'required_history':required,'requested_backtest_start':document['backtest_start'],
            'first_trading_day':first.strftime('%Y-%m-%d'),'proposals':proposals,'calendars':calendars}


def plan_dates(document, data_root, *, provider=None):
    factors=_dates(document)
    required=max(s.lookback for s in factors.specs)+1
    begin,end,bt,bt_end=map(pd.Timestamp,(document.get('data_start',document['backtest_start']),
        document.get('data_end',document['backtest_end']),document['backtest_start'],document['backtest_end']))
    padding=pd.Timedelta(days=required*2+60)
    windows=[(bt-padding,min(bt+pd.Timedelta(days=31),end)),(bt_end-pd.Timedelta(days=31),bt_end)]
    if document.get('auto_range') is not True:
        windows.append((begin,min(begin+padding,end)))
    years=sorted({year for start,finish in windows for year in range(start.year,finish.year+1)})
    provider=provider or TushareProvider(TushareSettings.from_env())
    cache=Path(data_root)/'research/calendar-plans'
    cache.mkdir(parents=True,exist_ok=True)
    with FileLock(cache/'calendar.lock',timeout=0):
        fetch=SnapshotFetcher(provider,cache/'raw')
        frames=[]
        for year in years:
            left,right=pd.Timestamp(year,1,1),min(pd.Timestamp(year,12,31),end)
            if left>right:continue
            raw=fetch.get('trade_cal',exchange='SSE',start_date=left.strftime('%Y%m%d'),end_date=right.strftime('%Y%m%d'))
            calendar=normalize_calendar(raw)
            if not pd.DatetimeIndex(calendar.date).equals(pd.date_range(left,right)):
                raise ValueError('交易日历缺失或重复，无法提供可靠日期方案')
            frames.append(calendar)
        return make_date_plan(document,frames)
