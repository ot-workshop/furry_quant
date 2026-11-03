"""Download/verify an independent official benchmark, with a redacted receipt."""
from __future__ import annotations
import argparse
import hashlib
import json
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from finance_quant.data.benchmarks import benchmark_root,ensure_series
from finance_quant.data.managed import atomic_json
from finance_quant.data.providers.tushare import TushareProvider,TushareSettings,normalize_calendar


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--code',default='H00300.CSI')
    parser.add_argument('--start',default='20041231')
    parser.add_argument('--end')
    args=parser.parse_args()
    now=datetime.now(ZoneInfo('Asia/Shanghai'))
    end=args.end or (now.date() if now.hour>=18 else now.date()-timedelta(days=1)).strftime('%Y%m%d')
    provider=TushareProvider(TushareSettings(TushareSettings.from_env().token))
    calendar=normalize_calendar(provider.query('trade_cal',exchange='SSE',start_date=args.start,end_date=end))
    sessions=pd.DatetimeIndex(calendar.loc[calendar.is_open,'date'])
    if sessions.empty:raise ValueError('所选区间没有交易日')
    series,manifest=ensure_series(args.code,sessions,provider=provider)
    record={'status':'complete','code':args.code,'endpoint':'https://api.tushare.pro',
            'requested_start':args.start,'requested_end':end,'checked_at':now.isoformat(),
            'first':series.date.min().strftime('%Y-%m-%d'),'last':series.date.max().strftime('%Y-%m-%d'),
            'observations':len(series),'expected_sessions':len(sessions),'missing':0,'duplicates':0,'extra_dates':0,
            'calendar_sha256':hashlib.sha256(calendar.to_csv(index=False,date_format='%Y-%m-%d').encode()).hexdigest(),
            'series_sha256':manifest['series_sha256'],'snapshot_file':manifest['file'],
            'note':'2004-12-31基日为期初点位；没有前收盘，不虚构基日前收益' if args.code=='H00300.CSI' else '点位收益，不假设可直接交易指数'}
    root=benchmark_root()/args.code
    atomic_json(root/'availability.json',record)
    atomic_json(root/'versions'/manifest['series_sha256']/'availability.json',record)
    print(json.dumps(record,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
