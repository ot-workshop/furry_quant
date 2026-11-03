"""Resumable historical index acquisition, independent of fixed-pool research."""
from __future__ import annotations
import hashlib
import json
import os
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from filelock import FileLock
from finance_quant.data.market import SnapshotFetcher
from finance_quant.data.managed import atomic_json,year_ranges,month_ranges,instrument,ManagedFetcher,repair_missing_dates
from finance_quant.data.providers import TushareProvider,TushareSettings,normalize_calendar,normalize_index_daily
from finance_quant.data.catalog import Catalog,utc_now
from finance_quant.data.index_universe import normalize_memberships,historical_codes,members_on,membership_mask
from finance_quant.data.network_budget import DownloadPaused,NetworkBudget
from finance_quant.data.providers.baostock import BaoStockProvider
from finance_quant.data.reconciliation import reconcile_market,normalize_baostock,CrosscheckError,BAO_IDENTITY_TOLERANCE,_primary_issues
from finance_quant.data.quality import adjustment_discontinuities
from finance_quant.data.suspensions import invalid_halt_rows,confirmed_halts,carry_halt_rows
from finance_quant.data.reviewed_events import REVIEWED_EVENTS,verified_event_moves
from finance_quant.data.official_suspensions import full_day_halts,AmbiguousSuspensionError

WAREHOUSE_POLICY='official-primary-sample-sha256-mod10-v1'

def sample_code(code):
    # Stable across ordering, batches and resumes. Approximately 10% of codes.
    return int(hashlib.sha256(code.encode()).hexdigest(),16)%10==0

def primary_events(frame,code):
    indexed=frame.set_index('date')
    return [{**event,'evidence_level':'single_source'} for event in
            verified_event_moves(indexed,indexed,code,REVIEWED_EVENTS)]

def primary_valid(frame,expected,eligible,events):
    if _primary_issues(frame,expected,{e['date'] for e in events}):return False
    # A plausible OHLC series also needs its own reference close/return identity.
    if ((frame.close/frame.pre_close-1-frame.change).abs()>.0002).any():return False
    adjusted=(frame.close*frame.adj_factor).pct_change(fill_method=None)
    if (adjusted.sub(frame.change).abs().iloc[1:]>.002).any():return False
    return _quality(frame,expected,eligible,events)['complete']

def _save_frame(path,frame):
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.pending.parquet');frame.to_parquet(temporary,index=False);temporary.replace(path)

def validation_signature():
    base=Path(__file__).resolve().parents[1]
    files=[Path(__file__),*[base/'data'/name for name in ['index_universe.py','quality.py','reconciliation.py','suspensions.py','reviewed_events.py','official_suspensions.py',
        'providers/tushare.py','providers/baostock.py','providers/baostock_worker.py']]]
    return hashlib.sha256(b''.join(p.read_bytes() for p in files)).hexdigest()

def asset_key(task,provider_id,membership_sha,validation_sha):
    return hashlib.sha256(json.dumps({'code':task['code'],'start':task['start'],'end':task['end'],
        'provider_id':provider_id,'membership_sha256':membership_sha,'validation_sha256':validation_sha,
        'policy':'index-asset-v1'},sort_keys=True).encode()).hexdigest()[:20]

def prepare_index(root,start='2020-01-01',end=None,warmup=127,*,provider=None):
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True)
    now=datetime.now(ZoneInfo('Asia/Shanghai'))
    finish=pd.Timestamp(end or now.date().isoformat())
    if finish.date()>now.date() or pd.Timestamp(start)>=finish or not 1<=warmup<=504:raise ValueError('指数下载日期或预热长度无效')
    # Freeze only complete sessions. Today before 18:00 is never assumed complete.
    cutoff=pd.Timestamp(now.date())-(pd.Timedelta(days=1) if now.hour<18 else pd.Timedelta(0))
    finish=min(finish,cutoff)
    provider=provider or TushareProvider(TushareSettings.from_env())
    if getattr(provider,'settings',None) and provider.settings.http_url!='https://api.tushare.pro':raise ValueError('指数仓库当前仅接受官方Tushare主源')
    fetch=SnapshotFetcher(provider,root/'reference-raw')
    left=pd.Timestamp(start)-pd.Timedelta(days=warmup*2+90)
    frames=[]
    for a,b in year_ranges(left.strftime('%Y%m%d'),(finish+pd.Timedelta(days=20)).strftime('%Y%m%d')):
        calendar=normalize_calendar(fetch.get('trade_cal',exchange='SSE',start_date=a,end_date=b))
        if not pd.DatetimeIndex(calendar.date).equals(pd.date_range(a,b)):raise ValueError('指数下载交易日历不完整')
        frames.append(calendar)
    calendar=pd.concat(frames,ignore_index=True).sort_values('date')
    sessions=pd.DatetimeIndex(calendar.loc[calendar.is_open,'date'])
    entries=sessions[(sessions>=pd.Timestamp(start))&(sessions<=finish)]
    if entries.empty:raise ValueError('指数研究区间没有完整交易日')
    begin,finish=entries[0],entries[-1]
    history=sessions[sessions<begin]
    if len(history)<warmup:raise ValueError('日历不能满足预热要求')
    data_start=history[-warmup]
    weights=[]
    # The preceding month's snapshot supplies the first research session's pool.
    preceding=begin.replace(day=1)-pd.Timedelta(days=1)
    for a,b in month_ranges(preceding.replace(day=1).strftime('%Y%m%d'),finish.strftime('%Y%m%d')):
        part=fetch.get('index_weight',index_code='399300.SZ',start_date=a,end_date=b)
        if part.empty:raise ValueError(f'{a[:6]}历史成分快照缺失，不能用未来名单填充')
        if not part.trade_date.astype(str).between(a,b).all():raise ValueError('成分快照超出请求月份')
        weights.append(part)
    intervals=normalize_memberships(pd.concat(weights,ignore_index=True),calendar)
    if len(members_on(intervals,begin))!=300:raise ValueError('缺少覆盖研究起点的历史成分')
    codes=historical_codes(intervals,begin,finish)
    metadata=[]
    for status in ['L','D','P']:
        part=fetch.get('stock_basic',exchange='',list_status=status,
            fields='ts_code,name,industry,list_date,delist_date,list_status')
        metadata.append(part)
    stocks=pd.concat(metadata,ignore_index=True)
    if stocks.ts_code.duplicated().any():raise ValueError('股票生命周期资料重复')
    stocks=stocks.set_index('ts_code')
    missing=set(codes)-set(stocks.index)
    if missing:raise ValueError('历史成分缺少上市退市资料：'+','.join(sorted(missing)))
    tasks=[]
    for code in codes:
        row=stocks.loc[code]
        listed=pd.to_datetime(str(row.list_date),format='%Y%m%d',errors='raise')
        first=max(data_start,listed)
        until=finish
        if pd.notna(row.delist_date) and str(row.delist_date).strip():
            until=min(until,pd.to_datetime(str(row.delist_date),format='%Y%m%d')-pd.Timedelta(days=1))
        active=sessions[(sessions>=first)&(sessions<=until)]
        if active.empty:raise ValueError(f'{code}生命周期与历史成分不一致')
        tasks.append({'code':code,'start':active[0].strftime('%Y%m%d'),'end':active[-1].strftime('%Y%m%d'),
            'name':str(row['name']),'industry':str(row.industry) if pd.notna(row.industry) else '未提供'})
    benchmark_parts=[normalize_index_daily(fetch.get('index_daily',ts_code='000300.SH',start_date=a,end_date=b))
        for a,b in year_ranges(data_start.strftime('%Y%m%d'),finish.strftime('%Y%m%d'))]
    benchmark=pd.concat(benchmark_parts,ignore_index=True).sort_values('date')
    expected=sessions[(sessions>=data_start)&(sessions<=finish)]
    if not pd.DatetimeIndex(benchmark.date).equals(expected):raise ValueError('基准行情覆盖不完整')
    for name,frame in [('calendar',calendar),('memberships',intervals),('benchmark',benchmark),('instruments',stocks.reset_index())]:
        _save_frame(root/'reference'/f'{name}.parquet',frame)
    plan={'schema_version':'index-download-plan-v1','index':'399300.SZ','start':begin.strftime('%Y-%m-%d'),
        'requested_start':pd.Timestamp(start).strftime('%Y-%m-%d'),'requested_end':end,
        'end':finish.strftime('%Y-%m-%d'),'data_start':data_start.strftime('%Y-%m-%d'),
        'warmup_sessions':warmup,'codes':list(codes),'tasks':tasks,'provider_id':fetch.provider_id,
        'membership_policy':'monthly_snapshot_next_session; NOT exact announcement effective dates',
        'point_in_time_certified':False,'snapshots':int(intervals.snapshot_date.nunique()),
        'reference_files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/'reference').glob('*.parquet')},
        'requests':fetch.requests,'created_at':utc_now()}
    atomic_json(root/'plan.json',plan)
    return plan

def _quality(frame,expected,eligibility,reviewed_events=()):
    values=frame[['open','high','low','close','adj_factor']].to_numpy(float)
    bad=~np.isfinite(values).all(axis=1)|(values<=0).any(axis=1)
    bad |= (frame.high<frame[['open','close','low']].max(axis=1))|(frame.low>frame[['open','close','high']].min(axis=1))
    bad |= ~np.isfinite(frame.volume)|frame.volume.lt(0)|(frame.volume.eq(0)&~frame.is_suspended)
    bad |= invalid_halt_rows(frame)
    limit_ok=np.isfinite(frame.up_limit)&np.isfinite(frame.down_limit)&frame.down_limit.gt(0)&frame.up_limit.gt(frame.down_limit)
    # Before joining the index, IPO sessions may have no price bands. Preserve raw
    # missing bands, flag them, and never invent executable limits for research.
    dates=frame.date.dt.strftime('%Y-%m-%d')
    reviewed_dates={event['date'] for event in reviewed_events}
    missing_live=(~limit_ok)&eligibility&~dates.isin(reviewed_dates)
    audits=[a for a in adjustment_discontinuities(frame) if a['date'] not in reviewed_dates]
    return {'complete':pd.DatetimeIndex(frame.date).equals(expected) and not bad.any() and not missing_live.any() and not audits,
        'invalid_price_dates':dates[bad].tolist(),'missing_limits_in_membership':dates[missing_live].tolist(),
        'unavailable_limit_dates':dates[~limit_ok].tolist(),
        'verified_event_moves':list(reviewed_events),
        'missing_positive_pe':int((~(np.isfinite(frame.pe_ttm)&frame.pe_ttm.gt(0))).sum())}


def _secondary_evidence(root,key,code,task,expected,secondary):
    """Try one alternate whole-interval basis only after an identity failure.

    Both captures remain immutable. No values are spliced or repaired in place.
    Every alternative must pass the same OHLC, status and return identities.
    """
    rejected=[]
    providers=[secondary]
    for provider in providers:
        bundle=provider.fetch(code,task['start'],task['end'])
        try:
            normalized=normalize_baostock(bundle,code,expected)
            return bundle,normalized,rejected
        except CrosscheckError as error:
            detail={'code':code,'category':'secondary_internal_validation_failed','reason':str(error),
                'source_evidence':bundle.metadata,'dates':[],'created_at':utc_now()}
            identity_failure=str(error)=='BaoStock adjusted returns disagree with its reported changes'
            if identity_failure:
                raw=bundle.raw.loc[bundle.raw.tradestatus.eq('1')].sort_values('date').set_index('date')
                adjusted=bundle.adjusted.loc[bundle.adjusted.tradestatus.eq('1')].sort_values('date').set_index('date')
                delta=(pd.to_numeric(adjusted.close).pct_change(fill_method=None)-pd.to_numeric(raw.pctChg)/100).abs()
                detail['dates']=delta.loc[delta>BAO_IDENTITY_TOLERANCE].index.tolist()
            rejected.append(detail)
            flag=bundle.metadata.get('adjusted_flag','1')
            atomic_json(root/'quarantine'/f'{key}-secondary-flag{flag}.json',detail)
            if len(providers)==1 and identity_failure and hasattr(secondary,'alternate_basis'):
                providers.append(secondary.alternate_basis())
                continue
            atomic_json(root/'quarantine'/f'{key}.json',{**detail,'rejected_bases':rejected})
            error.validation_dates=sorted({day for record in rejected for day in record['dates']})
            raise

def download_asset(root,task,calendar,intervals,fetch,secondary):
    code=task['code'];plan=json.loads((root/'plan.json').read_text())
    key=asset_key(task,fetch.provider_id,plan['reference_files']['memberships.parquet'],fetch.validation_sha256)
    directory=root/'assets'/key;receipt=directory/'receipt.json'
    if receipt.exists():
        record=json.loads(receipt.read_text(encoding='utf-8'))
        if hashlib.sha256((directory/'market.parquet').read_bytes()).hexdigest()!=record['sha256']:raise ValueError('股票缓存校验失败')
        capture_path=root/'captures'/f'{key}.json'
        if not capture_path.exists():
            captured=[]
            for api in ['daily','adj_factor','daily_basic','stk_limit']:
                params={'ts_code':code,'start_date':task['start'],'end_date':task['end']}
                specification,partition_key=fetch.key(api,params)
                previous,cache_root=fetch.cached_partition(partition_key)
                if previous and previous['status']=='succeeded':
                    path=(cache_root/previous['raw_path']).resolve()
                    if not path.is_relative_to(cache_root.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest()!=previous['sha256']:
                        raise ValueError('原始响应缓存校验失败')
                    captured.append({**specification,**{k:previous[k] for k in ['raw_path','sha256','rows','fetched_at']}})
            atomic_json(capture_path,{'code':code,'key':key,'provider_id':fetch.provider_id,
                'start':task['start'],'end':task['end'],'responses_saved':len(captured),'requests':captured,
                'status':'verified_asset_reused'})
        return {**record,'reused':True}
    expected=pd.DatetimeIndex(calendar.loc[calendar.is_open&calendar.date.between(pd.Timestamp(task['start']),pd.Timestamp(task['end'])),'date'])
    apis=['daily','adj_factor','daily_basic','stk_limit'];fetch.errors=[]
    parts=fetch.get_many([(api,{'ts_code':code,'start_date':task['start'],'end_date':task['end']}) for api in apis])
    captures=[r for r in fetch.requests if r['api'] in apis and r['params'].get('ts_code')==code
        and r['params'].get('start_date')==task['start'] and r['params'].get('end_date')==task['end']]
    atomic_json(root/'captures'/f'{key}.json',{'code':code,'key':key,'provider_id':fetch.provider_id,
        'start':task['start'],'end':task['end'],'responses_saved':len({r['api'] for r in captures}),
        'requests':captures,'status':'raw_capture_only; not independently validated'})
    if any(e['error'] in {'access_denied','rate_limit'} for e in fetch.errors):
        raise DownloadPaused('主源权限或频率限制，已保留断点，请检查后续传')
    if parts[2] is None or parts[3] is None:raise ValueError('估值或涨跌停请求失败，不能用第二源静默替换')
    frame=pd.DataFrame({'instrument':instrument(code),'date':expected})
    for part in parts:
        if part is not None:frame=frame.merge(part,on=['instrument','date'],how='left',validate='one_to_one')
    for col in ['open','high','low','close','pre_close','volume','amount','change','adj_factor']:
        if col not in frame:frame[col]=np.nan
    frame['name'],frame['industry']=task['name'],task['industry']
    frame['is_suspended']=False
    frame['valuation_carried']=False
    eligible=membership_mask(intervals,code,frame.date)&frame.date.ge(pd.Timestamp(plan['start'])).to_numpy()
    events=primary_events(frame,code)
    basic_complete=pd.DatetimeIndex(parts[2].date.sort_values()).equals(expected)
    valid=primary_valid(frame,expected,eligible,events) and basic_complete
    suspension_evidence=None
    official_status_reviews=[]
    if not valid and isinstance(fetch.provider,TushareProvider):
        # Query only months containing missing/inactive prices, never infer
        # suspension merely from a quote hole or a subsequent resume event.
        candidates=frame.loc[frame[['open','high','low','close']].isna().any(axis=1)|frame.volume.eq(0)|frame.amount.eq(0),'date']
        official=SnapshotFetcher(fetch.provider,root/'suspension-raw')
        confirmed=pd.DatetimeIndex([])
        for month in sorted(set(candidates.dt.to_period('M'))):
            a=max(month.start_time,pd.Timestamp(task['start']));b=min(month.end_time.normalize(),pd.Timestamp(task['end']))
            from finance_quant.data.providers.tushare import TushareProviderError
            try:
                raw=official.get('suspend_d',ts_code=code,start_date=a.strftime('%Y%m%d'),end_date=b.strftime('%Y%m%d'))
            except TushareProviderError:
                continue # The secondary may still supply independently valid status.
            try:
                confirmed=confirmed.union(full_day_halts(raw,code,a,b,frame))
            except AmbiguousSuspensionError:
                official_status_reviews.append({'start':a.strftime('%Y-%m-%d'),'end':b.strftime('%Y-%m-%d'),
                    'reason':'same-day suspend/resume conflict; needs independent status evidence','request':official.requests[-1]})
        if len(confirmed):
            frame=carry_halt_rows(frame.set_index('date'),confirmed).rename_axis('date').reset_index()
            events=primary_events(frame,code)
            basic_complete=not len(expected.difference(confirmed).difference(pd.DatetimeIndex(parts[2].date)))
            valid=primary_valid(frame,expected,eligible,events) and basic_complete
            suspension_evidence={'source':'tushare_suspend_d','dates':confirmed.strftime('%Y-%m-%d').tolist(),
                                 'requests':official.requests,'scope':'official full-day status; no independent price certification'}
    if not valid:
        # A broken secondary adjusted-price series need not invalidate its
        # independently matching raw/adjusted trading-status fields. Confirm
        # halts separately; never turn an unexplained quote hole into a halt.
        status_bundle=secondary.fetch(code,task['start'],task['end'])
        halted=confirmed_halts(status_bundle,code,expected)
        if len(halted):
            frame=carry_halt_rows(frame.set_index('date'),halted).rename_axis('date').reset_index()
            events=primary_events(frame,code)
            basic_complete=not len(expected.difference(halted).difference(pd.DatetimeIndex(parts[2].date)))
            valid=primary_valid(frame,expected,eligible,events) and basic_complete
            suspension_evidence={'source':status_bundle.metadata,'dates':halted.strftime('%Y-%m-%d').tolist(),
                                 'scope':'trading status only; does not certify adjusted prices'}
    trigger='anomaly' if not valid else 'sample' if sample_code(code) else 'not_selected'
    report={'policy':WAREHOUSE_POLICY,'status':'not_checked','trigger':trigger,'stocks':[],
            'conflict_codes':[],'fallback_codes':[],'primary_failures':list(fetch.errors),
            'suspension_evidence':suspension_evidence,'official_status_reviews':official_status_reviews}
    level='single_source_passed'
    if trigger!='not_selected':
        try:
            bundle,evidence,rejected=_secondary_evidence(root,key,code,task,expected,secondary)
        except (ValueError,RuntimeError) as error:
            if not valid:raise
            # Optional sampling cannot make a healthy official series depend on
            # the secondary's availability, quota, or internally broken factors.
            report.update(status='unavailable',reason=type(error).__name__,
                          validation_dates=getattr(error,'validation_dates',[]))
        else:
            halted=pd.DatetimeIndex(evidence.loc[evidence.is_suspended,'date'])
            checked=[]
            for a,b in month_ranges(task['start'],task['end']):
                dates=expected[(expected>=pd.Timestamp(a))&(expected<=pd.Timestamp(b))].difference(halted)
                section=parts[2].loc[parts[2].date.between(pd.Timestamp(a),pd.Timestamp(b))&~parts[2].date.isin(halted)]
                checked.append(repair_missing_dates(fetch,'daily_basic',code,a,b,section,dates))
            valuations=pd.concat(checked,ignore_index=True)
            if len(expected.difference(halted).difference(pd.DatetimeIndex(valuations.date))):
                raise ValueError('估值交易日记录仍缺失')
            columns=[c for c in valuations if c not in ['instrument','date']]
            frame=frame.drop(columns=columns).merge(valuations,on=['instrument','date'],how='left',validate='one_to_one')
            class Captured:
                def fetch(self,*args):return bundle
            frame,report=reconcile_market(frame,expected,(code,),'fallback',Captured(),primary_failures=fetch.errors,
                reviewed_events=REVIEWED_EVENTS)
            report.update(trigger=trigger,rejected_secondary_bases=rejected,official_status_reviews=official_status_reviews)
            if report['status']=='conflict':
                atomic_json(root/'quarantine'/f'{key}.json',{'code':code,'category':'source_conflict','cross_validation':report})
                raise ValueError('两源行情、复权或停牌校验存在冲突')
            events=report['stocks'][0].get('verified_event_moves',[])
            level='secondary_fallback_passed' if report['status']=='fallback' else 'dual_source_verified'
    if level=='single_source_passed':
        frame['price_source']='tushare';frame['adjustment_source']='tushare'
        frame['valuation_source']='tushare';frame['limit_source']='tushare'
    frame['validation_level']=level
    eligible=membership_mask(intervals,code,frame.date)&frame.date.ge(pd.Timestamp(json.loads((root/'plan.json').read_text())['start'])).to_numpy()
    frame['index_member']=eligible
    plan=json.loads((root/'plan.json').read_text())
    frame['history_sessions_before']=np.arange(len(frame))
    frame['has_required_pre_history']=frame.history_sessions_before.ge(plan['warmup_sessions'])
    frame['has_daily_price_limit']=np.isfinite(frame.up_limit)&np.isfinite(frame.down_limit)&frame.down_limit.gt(0)&frame.up_limit.gt(frame.down_limit)
    event_dates={event['date'] for event in events}
    unbounded=frame.date.dt.strftime('%Y-%m-%d').isin(event_dates)
    frame.loc[unbounded,'has_daily_price_limit']=False
    frame['reviewed_unbounded_session']=unbounded
    quality=_quality(frame,expected,eligible,events)
    quality['internal_consistency']=primary_valid(frame,expected,eligible,events)
    quality['complete']=quality['complete'] and quality['internal_consistency']
    if not quality['complete']:
        atomic_json(root/'quarantine'/f'{key}.json',{'code':code,'category':'market_quality_requires_review',
            'quality':quality,'adjustment_audits':adjustment_discontinuities(frame),'cross_validation':report,'created_at':utc_now()})
        raise ValueError('股票数据质量或成分期执行价格约束不完整，详情已隔离保存')
    if validation_signature()!=fetch.validation_sha256:raise DownloadPaused('校验代码已更新，已保留断点；请续传以使用新版本')
    directory.mkdir(parents=True,exist_ok=True);_save_frame(directory/'market.parquet',frame)
    record={'schema_version':'index-asset-v1','key':key,'code':code,'start':task['start'],'end':task['end'],
        'rows':len(frame),'file':str((directory/'market.parquet').relative_to(root)).replace('\\','/'),
        'sha256':hashlib.sha256((directory/'market.parquet').read_bytes()).hexdigest(),
        'provider_id':fetch.provider_id,'validation_sha256':fetch.validation_sha256,'validation_level':level,'validation_policy':WAREHOUSE_POLICY,'cross_validation':report,'quality':quality,'created_at':utc_now()}
    atomic_json(receipt,record);return record

def run_index_download(root,start='2020-01-01',end=None,warmup=127,*,prepare_only=False,limit=None,provider=None,secondary=None):
    if limit is not None and (isinstance(limit,bool) or not isinstance(limit,int) or limit<1):raise ValueError('本次股票数量须为正整数')
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True)
    with FileLock(root/'warehouse.lock',timeout=0):
        plan_path=root/'plan.json'
        if plan_path.exists():
            plan=json.loads(plan_path.read_text(encoding='utf-8'))
            if pd.Timestamp(plan['requested_start'])!=pd.Timestamp(start) or plan['warmup_sessions']!=warmup or (end is not None and plan['requested_end']!=end):
                raise ValueError('现有冻结下载计划与参数不同，请使用新数据目录')
            for name,digest in plan['reference_files'].items():
                if hashlib.sha256((root/'reference'/name).read_bytes()).hexdigest()!=digest:raise ValueError('参考数据校验失败')
        else:plan=prepare_index(root,start,end,warmup,provider=provider)
        if prepare_only:return plan
        provider=provider or TushareProvider(TushareSettings.from_env())
        catalog=Catalog(root)
        job='index-'+str(time.time_ns());catalog.begin_job(job,{'index':plan['index'],'codes':plan['codes']},len(plan['tasks'])*4)
        fetch=ManagedFetcher(provider,root,catalog,job,False,{'validation':{'mode':'fallback'}})
        fetch.validation_sha256=validation_signature()
        if fetch.provider_id!=plan['provider_id']:raise ValueError('续传数据源发生变化')
        secondary=secondary or BaoStockProvider(root/'secondary',timeout=90)
        calendar=pd.read_parquet(root/'reference/calendar.parquet');intervals=pd.read_parquet(root/'reference/memberships.parquet')
        progress_path=root/'progress.json'
        old=json.loads(progress_path.read_text()) if progress_path.exists() else {}
        done={code:record for code,record in old.get('assets',{}).items() if record.get('validation_sha256')==fetch.validation_sha256};failures=dict(old.get('failures',{}))
        progress={'status':'running','job_id':job,'total':len(plan['tasks']),'assets':done,'failures':failures,'updated_at':utc_now()}
        def save():
            progress['completed']=len(done);progress['updated_at']=utc_now()
            progress['validation_counts']={level:sum(r.get('validation_level')==level for r in done.values())
                for level in ['single_source_passed','dual_source_verified','secondary_fallback_passed']}
            progress['validation_policy']=WAREHOUSE_POLICY
            progress['raw_captures']=len({r['code'] for r in (json.loads(p.read_text()) for p in (root/'captures').glob('*.json')) if r['responses_saved']==4})
            atomic_json(progress_path,progress)
        try:
            save()
            consecutive_connections=0
            for task in plan['tasks'][:limit] if limit else plan['tasks']:
                if validation_signature()!=fetch.validation_sha256:raise DownloadPaused('校验代码已更新，已保留断点；请续传以使用新版本')
                if (root/'PAUSE').exists():raise DownloadPaused('用户暂停了指数下载；移除PAUSE文件后可续传')
                progress['current_code']=task['code'];save()
                try:
                    record=download_asset(root,task,calendar,intervals,fetch,secondary)
                    done[task['code']]={k:record[k] for k in ['key','code','start','end','rows','file','sha256','provider_id','validation_level','validation_sha256']}
                    failures.pop(task['code'],None)
                    consecutive_connections=0
                    print(f"INDEX {len(done)}/{len(plan['tasks'])} {task['code']} verified",flush=True)
                except DownloadPaused:raise
                except Exception as error:
                    done.pop(task['code'],None)
                    failures[task['code']]={'type':type(error).__name__,'reason':str(error)[:300] if isinstance(error,ValueError) else 'source_or_coverage_validation_failed',
                        'dates':getattr(error,'validation_dates',[])[:31]}
                    print(f"INDEX {task['code']} failed: {type(error).__name__}",flush=True)
                    from finance_quant.data.providers.baostock import BaoStockProviderError
                    consecutive_connections=consecutive_connections+1 if isinstance(error,BaoStockProviderError) and error.retryable else 0
                    if consecutive_connections>=3:raise DownloadPaused('BaoStock连续三只股票连接失败，暂停避免持续重连；稍后可续传')
                save()
            progress['status']='complete' if len(done)==len(plan['tasks']) and not failures else 'partial'
            if progress['status']=='complete':
                manifest={'schema_version':'historical-index-warehouse-v1','index':plan['index'],'start':plan['start'],'end':plan['end'],
                    'data_start':plan['data_start'],'warmup_sessions':warmup,'provider_id':fetch.provider_id,'validation_sha256':fetch.validation_sha256,
                    'membership_policy':plan['membership_policy'],'point_in_time_certified':False,
                    'reference_files':plan['reference_files'],'assets':done,'rows':sum(r['rows'] for r in done.values()),
                    'validation_policy':WAREHOUSE_POLICY,'ready_for_legacy_fixed_pool_research':False,'created_at':utc_now()}
                atomic_json(root/'manifest.json',manifest)
            save();catalog.finish(job,progress['status'],{'completed':len(done),'total':len(plan['tasks']),'failures':failures})
            return progress
        except DownloadPaused as error:
            progress.update(status='paused',message=str(error));save();catalog.finish(job,'paused',{'completed':len(done)});return progress
        except BaseException:
            progress['status']='interrupted';save();catalog.finish(job,'interrupted',{'completed':len(done)});raise
        finally:catalog.close()

def index_download_status(root):
    root=Path(root);path=root/'progress.json';plan=root/'plan.json'
    result=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'status':'not_started'}
    if plan.exists():
        p=json.loads(plan.read_text(encoding='utf-8'))
        result['plan']={k:p[k] for k in ['index','start','end','data_start','warmup_sessions','snapshots','membership_policy']}
    result.setdefault('validation_counts',{'single_source_passed':0,'dual_source_verified':0,'secondary_fallback_passed':0})
    result['budget']=NetworkBudget().status()
    return result

def index_observations_on(root,day,*,require_history=True):
    """A safe daily universe reader; never exposes a future constituent's row."""
    root=Path(root);manifest=json.loads((root/'manifest.json').read_text())
    if not pd.Timestamp(manifest['start'])<=pd.Timestamp(day)<=pd.Timestamp(manifest['end']):raise ValueError('日期不在冻结数据区间')
    for name,digest in manifest['reference_files'].items():
        if hashlib.sha256((root/'reference'/name).read_bytes()).hexdigest()!=digest:raise ValueError('参考数据校验失败')
    intervals=pd.read_parquet(root/'reference/memberships.parquet')
    selected=members_on(intervals,day);rows=[];excluded={}
    for code in selected:
        asset=manifest['assets'][code];path=(root/asset['file']).resolve()
        if not pd.Timestamp(asset['start'])<=pd.Timestamp(day)<=pd.Timestamp(asset['end']):
            excluded[code]='outside_stored_listing_lifecycle';continue
        if not path.is_relative_to(root.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest()!=asset['sha256']:
            raise ValueError('股票数据校验失败')
        frame=pd.read_parquet(path,filters=[('date','==',pd.Timestamp(day))])
        if len(frame)!=1:raise ValueError(f'{code}成分期报价缺失')
        row=frame.iloc[0]
        if require_history and not bool(row.has_required_pre_history):excluded[code]='insufficient_pre_history';continue
        rows.append(row)
    return {'date':str(pd.Timestamp(day).date()),'pool_size':len(selected),'rows':pd.DataFrame(rows),
        'excluded':excluded,'membership_policy':manifest['membership_policy']}
