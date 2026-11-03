import copy
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from finance_quant.application import market_download as downloads
from finance_quant.data import shared_market as shared
from finance_quant.data.managed import atomic_json
from finance_quant.data.market import load_market
from finance_quant.data.network_budget import DownloadPaused
from finance_quant.application.research_requests import research_options,validate_request
from finance_quant.application.task_worker import run_task
from finance_quant.api import create_app
from test_web_research import PausedService
from test_joint_data import Primary,Secondary,market_frame


class SharedProvider(Primary):
    def __init__(self):super().__init__();self.block=False;self.conflict=False
    def query(self,api,**params):
        if api=='stock_basic':
            self.calls.append((api,tuple(sorted(params.items()))))
            status=params['list_status'];codes=['600000.SH','000001.SZ'] if status=='L' else ['600519.SH'] if status=='D' else []
            return pd.DataFrame([{'ts_code':c,'name':c,'industry':'fixture','list_date':'20100101',
                                  'delist_date':'20250320' if status=='D' else '', 'list_status':status} for c in codes],
                                columns=['ts_code','name','industry','list_date','delist_date','list_status'])
        if api=='index_weight':
            self.calls.append((api,tuple(sorted(params.items()))))
            day=pd.Timestamp(params['end_date']);day-=pd.Timedelta(days=max(0,day.weekday()-4))
            return pd.DataFrame([{'index_code':'399300.SZ','con_code':c,'trade_date':day.strftime('%Y%m%d'),'weight':50.} for c in ['600000.SH','000001.SZ']])
        if self.block and api=='daily_basic':raise DownloadPaused('fixture daily budget')
        result=super().query(api,**params)
        if self.conflict and api in {'daily','index_daily'}:
            for field in ['open','high','low','close','pre_close']:result[field]*=2
        return result


@pytest.fixture
def workspace(tmp_path,monkeypatch):
    data=tmp_path/'data';root=downloads.shared_root(data);old=tmp_path/'old';reference=old/'reference';reference.mkdir(parents=True)
    provider=SharedProvider();identity=hashlib.sha256(f'offline:{type(provider).__module__}.{type(provider).__qualname__}'.encode()).hexdigest()
    dates=pd.date_range('2024-01-01','2025-12-31');calendar=pd.DataFrame({'date':dates,'is_open':dates.weekday<5})
    calendar.to_parquet(reference/'calendar.parquet',index=False)
    quotes=market_frame(pd.bdate_range('2025-01-02','2025-02-28'));quotes['adj_factor']=1.
    quotes['is_suspended']=False;quotes['validation_level']='single_source_passed'
    for field in ['price_source','adjustment_source','valuation_source','limit_source']:quotes[field]='tushare'
    assets={}
    for code in ['600000.SH','000001.SZ']:
        path=old/f'{code}.parquet';frame=quotes.copy();frame['instrument']=code[-2:]+code[:6]
        frame['index_member']=True;frame['history_sessions_before']=range(len(frame));frame.to_parquet(path,index=False)
        assets[code]={'file':path.name,'sha256':shared.digest(path),'code':code,'provider_id':identity,
                      'start':'20250102','end':'20250228','rows':len(frame),'validation_level':'single_source_passed'}
    benchmark=quotes.drop(columns=['adj_factor']).copy();benchmark['instrument']='SH000300';benchmark.to_parquet(reference/'benchmark.parquet',index=False)
    atomic_json(old/'manifest.json',{'schema_version':'historical-index-warehouse-v1','assets':assets,
                                    'data_start':'2025-01-02','start':'2025-01-06','end':'2025-02-28','membership_policy':'fixture',
                                    'reference_files':{p.name:shared.digest(p) for p in reference.glob('*.parquet')}})
    monkeypatch.setattr(downloads,'default_warehouse',lambda:old)
    original=downloads.normalize_memberships
    monkeypatch.setattr(downloads,'normalize_memberships',lambda raw,calendar:original(raw,calendar,expected_count=2))
    monkeypatch.setattr(downloads,'INDEX_SIZE',2)
    return data,root,old,provider


def spec(scope='custom',end='2025-02-28',codes=None):
    return {'scope':scope,'start':'2025-01-06','end':end,'warmup':2,'codes':codes if codes is not None else ['600000.SH','000001.SZ']}


def quote_calls(provider):return [(a,dict(p)) for a,p in provider.calls if a in ['daily','adj_factor','daily_basic','stk_limit']]


def test_import_switch_pool_no_quote_requests_and_research_roundtrip(workspace):
    data,root,old,provider=workspace;before={p:shared.digest(p) for p in old.rglob('*') if p.is_file()}
    first=downloads.prepare(root,spec('csi300'),provider=provider)
    assert first['missing_rows']==0 and first['estimated_quote_requests']==0
    assert downloads.run(root,first['id'],provider=provider,secondary=Secondary())['status']=='complete'
    second=downloads.prepare(root,spec('all_market'),provider=provider)
    assert second['missing_rows']==0
    result=downloads.run(root,second['id'],provider=provider,secondary=Secondary())
    assert result['status']=='complete' and not quote_calls(provider)
    state=downloads.status(root);assert len(state['versions'])==2
    option=next(v for v in state['versions'] if v['id']==result['version_id'])
    registry=research_options(data,data.parent/'runs')
    snapshot=next(s for s in registry['snapshots'] if s['id']==option['snapshot_id'])
    frame,_,_,manifest=load_market(data/'research/downloads'/('shared-'+option['id']))
    assert 'index_member' not in frame and 'history_sessions_before' not in frame
    assert manifest['source']=='official_tushare_shared'
    request={'name':'共享固定池验收','data':{'mode':'snapshot','snapshot_id':snapshot['id'],'codes':['600000.SH','000001.SZ']},
             'backtest':{'start':'2025-01-08','end':'2025-02-28','topk':1},
             'factors':[{'name':'momentum','window':2,'weight':1.}],
             'evaluation':{'horizons':[1],'groups':2,'min_cross_section':3}}
    normalized,_=validate_request(request,data,data.parent/'runs');assert normalized['data']['snapshot_id']==snapshot['id']
    service=PausedService(data.parent/'runs',data);service.start()
    task=service.submit(request,'shared-fixed-workflow-12345');assert service.store.claim()==task['id']
    run_task(data.parent/'runs',data,task['id']);finished=service.store.get(task['id'])
    assert finished['status']=='succeeded',finished
    assert finished['result']['factor_run_id'] and finished['result']['backtest_run_id'] and finished['result']['evaluation_run_id']
    assert {p:shared.digest(p) for p in before}==before


def test_date_extension_only_gap_plus_anchor_resume_and_old_version_pinned(workspace):
    data,root,old,provider=workspace
    p=downloads.prepare(root,spec(),provider=provider);downloads.run(root,p['id'],provider=provider,secondary=Secondary())
    immutable={p:shared.digest(p) for p in (root/'versions').rglob('*') if p.is_file()}
    p=downloads.prepare(root,spec(end='2025-03-07'),provider=provider)
    assert p['missing_rows']==10 and p['estimated_quote_requests']==8
    provider.block=True
    assert downloads.run(root,p['id'],provider=provider,secondary=Secondary())['status']=='paused'
    assert {p:shared.digest(p) for p in immutable}==immutable
    saved=len(quote_calls(provider));provider.block=False
    persisted_calls=quote_calls(provider).copy()
    assert downloads.run(root,p['id'],provider=provider,secondary=Secondary())['status']=='complete'
    for _,params in quote_calls(provider):assert params['start_date']=='20250228' and params['end_date']=='20250307'
    assert len(quote_calls(provider))>saved
    for call in persisted_calls:assert quote_calls(provider).count(call)==1
    count=len(quote_calls(provider));p2=downloads.prepare(root,spec('csi300',end='2025-03-07'),provider=provider)
    assert p2['missing_rows']==0
    assert downloads.run(root,p2['id'],provider=provider,secondary=Secondary())['status']=='complete'
    assert len(quote_calls(provider))==count
    assert {p:shared.digest(p) for p in immutable}==immutable


def test_boundary_conflict_never_poison_coverage_or_replace_pointer(workspace):
    _,root,_,provider=workspace
    p=downloads.prepare(root,spec(codes=['600000.SH']),provider=provider);downloads.run(root,p['id'],provider=provider,secondary=Secondary())
    before={p:shared.digest(p) for p in (root/'current').glob('*')};provider.conflict=True
    p=downloads.prepare(root,spec(end='2025-03-07',codes=['600000.SH']),provider=provider)
    result=downloads.run(root,p['id'],provider=provider,secondary=Secondary())
    assert result['status']=='partial' and result['failures']
    assert {p:shared.digest(p) for p in before}==before
    plan=downloads.prepare(root,spec(end='2025-03-07',codes=['600000.SH']),provider=provider)
    assert plan['missing_rows']==5


def test_all_market_retains_delisted_and_does_not_claim_complete_for_subset(workspace):
    _,root,_,provider=workspace
    plan=downloads.prepare(root,spec('all_market',end='2025-03-28',codes=[]),provider=provider)
    assert plan['codes_count']==3 and not plan['restricted']
    dead=next(t for t in plan['tasks'] if t['code']=='600519.SH')
    assert dead['end']=='20250319'
    limited=downloads.prepare(root,spec('all_market',codes=['600000.SH']),provider=provider)
    assert limited['restricted'] and limited['codes_count']==1


def test_complete_csi_version_selectable_without_modifying_original_warehouse(workspace):
    data,root,old,provider=workspace
    old_hash=shared.digest(old/'manifest.json')
    plan=downloads.prepare(root,spec('csi300',codes=[]),provider=provider)
    result=downloads.run(root,plan['id'],provider=provider,secondary=Secondary())
    assert result['status']=='complete'
    view=root/'versions'/result['version_id']/'index-view'
    from finance_quant.data.historical_warehouse import warehouse_option,load_historical_input
    option=warehouse_option(view)
    market,_,_,meta=load_historical_input(view,'2025-01-08','2025-02-28',2)
    assert market.instrument.nunique()==2 and set(meta['codes'])=={'600000.SH','000001.SZ'}
    options=research_options(data,data.parent/'runs')
    assert any(o['id']==option['id'] for o in options['historical_indices'])
    body={'name':'共享动态版本','data':{'mode':'index_warehouse','warehouse_id':option['id']},
          'backtest':{'start':'2025-01-08','end':'2025-02-28','topk':1},
          'factors':[{'name':'momentum','window':2,'weight':1.}],
          'evaluation':{'horizons':[1],'groups':2,'min_cross_section':3}}
    normalized,execution=validate_request(body,data,data.parent/'runs')
    assert Path(execution['warehouse_dir'])==view.resolve()
    assert normalized['data']['warehouse_id']==option['id']
    assert shared.digest(old/'manifest.json')==old_hash


def test_year_boundary_splits_requests_and_reuses_archive(workspace):
    _,root,_,provider=workspace
    plan=downloads.prepare(root,spec(end='2026-01-06',codes=['600000.SH']),provider=provider)
    assert plan['estimated_quote_requests']==8
    assert downloads.run(root,plan['id'],provider=provider,secondary=Secondary())['status']=='complete'
    calls=quote_calls(provider)
    assert all(pd.Timestamp(p['end_date'])-pd.Timestamp(p['start_date'])<pd.Timedelta(days=370) for _,p in calls)
    count=len(calls)
    assert downloads.run(root,plan['id'],provider=provider,secondary=Secondary())['status']=='complete'
    assert len(quote_calls(provider))==count


def test_preview_again_after_success_reuses_same_data_version(workspace):
    _,root,_,provider=workspace
    plan=downloads.prepare(root,spec(end='2025-03-07',codes=['600000.SH']),provider=provider)
    first=downloads.run(root,plan['id'],provider=provider,secondary=Secondary());calls=len(quote_calls(provider))
    updated=downloads.prepare(root,spec(end='2025-03-07',codes=['600000.SH']),provider=provider)
    assert updated['id']!=plan['id'] and updated['missing_rows']==0
    second=downloads.run(root,updated['id'],provider=provider,secondary=Secondary())
    assert first['version_id']==second['version_id'] and len(quote_calls(provider))==calls
    assert len(downloads.status(root)['versions'])==1


def test_partial_month_uses_only_prior_snapshot_and_missing_complete_month_rejected(workspace):
    _,root,_,provider=workspace
    partial=downloads.prepare(root,{**spec('csi300',codes=[]),'end':'2025-02-14'},provider=provider)
    path=root/'plans'/partial['id']/'reference/memberships.parquet'
    intervals=pd.read_parquet(path)
    assert intervals.snapshot_date.max()<=pd.Timestamp('2025-02-14')
    assert partial['codes_count']==2


def test_unsupported_fixed_pool_securities_are_archived_not_offered_as_executable(workspace,monkeypatch):
    _,root,_,provider=workspace
    original=provider.query
    def query(api,**params):
        if api=='stock_basic' and params['list_status']=='L':
            frame=original(api,**params)
            row=frame.iloc[0].copy();row.ts_code='688001.SH';return pd.concat([frame,pd.DataFrame([row])],ignore_index=True)
        return original(api,**params)
    monkeypatch.setattr(provider,'query',query)
    plan=downloads.prepare(root,spec(codes=['688001.SH']),provider=provider)
    result=downloads.run(root,plan['id'],provider=provider,secondary=Secondary())
    assert result['status']=='complete'
    version=next(v for v in downloads.status(root)['versions'] if v['id']==result['version_id'])
    assert version['snapshot_id'] is None


def test_corrupt_import_and_plan_rejected_without_rewriting_old(workspace):
    _,root,old,provider=workspace
    p=downloads.prepare(root,spec(),provider=provider)
    path=root/'plans'/p['id']/'plan.json';doc=json.loads(path.read_text());doc['end']='2025-03-07';atomic_json(path,doc)
    with pytest.raises(ValueError,match='计划校验'):downloads.run(root,p['id'],provider=provider,secondary=Secondary())
    (old/'600000.SH.parquet').write_bytes(b'corrupt')
    with pytest.raises(ValueError,match='校验失败'):shared.read_asset(root,'600000.SH',pd.bdate_range('2025-01-02','2025-01-10'),p['provider_id'])


def test_api_local_origin_strict_parameters_and_no_get_downloads(workspace,monkeypatch):
    data,root,_,provider=workspace
    monkeypatch.setattr(downloads,'official_provider',lambda:provider)
    service=PausedService(data.parent/'runs',data)
    client=TestClient(create_app(data.parent/'runs',data_root=data,task_service=service),base_url='http://127.0.0.1')
    assert client.get('/api/v1/research/market-download').status_code==200 and not provider.calls
    assert client.post('/api/v1/research/market-download/preview',json=spec(),headers={'Origin':'https://outside.test'}).status_code==403
    assert client.post('/api/v1/research/market-download/preview',json={**spec(),'root':'outside'}).status_code==422
    response=client.post('/api/v1/research/market-download/preview',json=spec());assert response.status_code==200,response.text
    assert not quote_calls(provider)
    assert client.post('/api/v1/research/market-download/start',json={'plan_id':'../../outside'}).status_code==422


@pytest.mark.parametrize('patch',[{'warmup':True},{'warmup':-1},{'end':'2099-01-01'},{'codes':['600000.SH','600000.SH']},{'codes':['000001.BJ']},{'start':'2025-02-30'}])
def test_invalid_dates_scope_and_codes_rejected(patch):
    with pytest.raises((ValueError,TypeError)):downloads.validate_spec({**spec(),**patch})


def test_atomic_progress_retries_windows_read_handle_and_never_truncates(tmp_path,monkeypatch):
    from finance_quant.data import managed
    path=tmp_path/'progress.json';atomic_json(path,{'completed':298})
    original=Path.replace;attempts=[]
    def replace(source,target):
        attempts.append(True)
        assert json.loads(path.read_text())=={'completed':298}
        if len(attempts)<4:raise PermissionError('transient Windows reader handle')
        return original(source,target)
    monkeypatch.setattr(Path,'replace',replace);monkeypatch.setattr(managed.time,'sleep',lambda _:None)
    atomic_json(path,{'completed':301})
    assert json.loads(path.read_text())=={'completed':301} and len(attempts)==4


def test_atomic_progress_persistent_denial_preserves_previous_document(tmp_path,monkeypatch):
    from finance_quant.data import managed
    path=tmp_path/'progress.json';atomic_json(path,{'completed':298})
    def denied(*_):raise PermissionError('persistent denial')
    monkeypatch.setattr(Path,'replace',denied);monkeypatch.setattr(managed.time,'sleep',lambda _:None)
    with pytest.raises(PermissionError):atomic_json(path,{'completed':301})
    assert json.loads(path.read_text())=={'completed':298}
    assert list(tmp_path.iterdir())==[path]


def test_progress_reader_retries_transient_windows_replace_lock(tmp_path,monkeypatch):
    path=tmp_path/'progress.json';atomic_json(path,{'status':'complete'})
    original=Path.read_text;attempts=[]
    def read(source,*args,**kwargs):
        if source==path:
            attempts.append(True)
            if len(attempts)<4:raise PermissionError('transient replace lock')
        return original(source,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',read);monkeypatch.setattr(downloads.time,'sleep',lambda _:None)
    assert downloads._json(path)=={'status':'complete'} and len(attempts)==4


def test_mixed_segment_validation_never_upgrades_single_source_rows():
    frame=pd.DataFrame({'instrument':['SH600000','SH600000','SZ000001'],
                        'validation_level':['dual_source_verified','single_source_passed','secondary_fallback_passed']})
    assert shared.validation_counts(frame)=={'single_source_passed':1,'dual_source_verified':0,'secondary_fallback_passed':1}
