"""A benchmark view of a pinned execution; no strategy rerun or signal changes."""
from __future__ import annotations
import copy
import hashlib
import json
from datetime import datetime,timezone
from pathlib import Path
import pandas as pd
from filelock import FileLock

from finance_quant.data.benchmarks import ensure_series,normalize_benchmark,_digest,VERSION
from finance_quant.research.performance import enrich_backtest
from finance_quant.storage import ResultStore


def publish_benchmark_view(runs_root, execution_id, code, *, calendar, benchmark_data_root=None):
    locks=Path(runs_root)/'.locks';locks.mkdir(parents=True,exist_ok=True)
    with FileLock(locks/'benchmark-views.lock',timeout=60):
        return _publish_benchmark_view(runs_root,execution_id,code,calendar=calendar,benchmark_data_root=benchmark_data_root)


def _publish_benchmark_view(runs_root, execution_id, code, *, calendar, benchmark_data_root=None):
    code=normalize_benchmark(code)
    store=ResultStore(Path(runs_root))
    original=store.get('backtests',execution_id)
    dates=pd.DatetimeIndex(pd.to_datetime([row['date'] for row in original['equity']]))
    sessions=pd.DatetimeIndex(calendar)
    if not dates.equals(sessions[(sessions>=dates[0])&(sessions<=dates[-1])]):
        raise ValueError('账户净值交易日不完整，无法对齐评价基准')
    series,manifest=ensure_series(code,dates,root=benchmark_data_root)
    provenance={'version':VERSION,'execution_run_id':execution_id,'code':code,
                'data_adapter_code_sha256':hashlib.sha256((Path(__file__).parents[1]/'data/benchmarks.py').read_bytes()).hexdigest(),
                'series_sha256':_digest(series),'performance_code_sha256':hashlib.sha256(
                    (Path(__file__).parents[1]/'research/performance.py').read_bytes()).hexdigest(),
                'adapter_code_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    digest=hashlib.sha256(json.dumps(provenance,sort_keys=True).encode()).hexdigest()[:20]
    identifier='benchmark-'+digest
    if store.path('backtests',identifier).exists():return {'run_id':identifier,'reused':True}
    result=copy.deepcopy(original)
    result.pop('tables',None)
    nav=series.close/float(series.close.iloc[0]);returns=series.close.pct_change(fill_method=None)
    for point,value in zip(result['equity'],nav):point['benchmark']=float(value)
    by_date=dict(zip(series.date.dt.strftime('%Y-%m-%d'),returns))
    for row in result.get('qlib_report',[]):row['bench']=float(by_date[row['date']])
    meta=result['metadata']
    meta['execution_reference_benchmark']=meta.get('config',{}).get('benchmark')
    meta.update(execution_run_id=execution_id,benchmark_name=f'{manifest["name"]}（{code}）',
                benchmark={'code':code,'name':manifest['name'],'return_type':manifest['return_type'],
                    'source':manifest['source'],'snapshot':manifest,'series_sha256':_digest(series),
                    'start':dates[0].strftime('%Y-%m-%d'),'end':dates[-1].strftime('%Y-%m-%d'),
                    'initial_date':dates[0].strftime('%Y-%m-%d'),'observations':len(series),
                    'methodology':'相同账户估值日；首日前一交易日收盘为锚点；不含基准交易费用'},
                benchmark_provenance=provenance)
    meta.setdefault('config',{})['evaluation_benchmark']=code
    if manifest['return_type']=='gross_total_return':
        meta['limitations']=[s for s in meta.get('limitations',[]) if '策略与价格指数' not in s]
        meta['limitations'].append('全收益基准计入分红再投资；策略仍为复权等效持股，未逐笔模拟现金红利、红利税及到账再投资时点')
    result['summary'].update(id=identifier,name=f'{original["summary"]["name"]} · {manifest["name"]}',
                             created_at=datetime.now(timezone.utc).isoformat())
    result.pop('performance',None)
    result=enrich_backtest(result)
    store.publish('backtests',identifier,result)
    return {'run_id':identifier,'reused':False}
