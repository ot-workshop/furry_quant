import hashlib,json
from pathlib import Path
import pandas as pd
import pytest
from finance_quant.application import index_download as module
from finance_quant.data.index_universe import normalize_memberships
from finance_quant.data.network_budget import DownloadPaused
from test_joint_data import Primary,Secondary

def setup_plan(root,provider):
    root.mkdir(parents=True);reference=root/'reference';reference.mkdir()
    dates=pd.date_range('2019-01-01','2020-04-30')
    calendar=pd.DataFrame({'date':dates,'is_open':dates.weekday<5})
    raw=pd.DataFrame([{'index_code':'399300.SZ','trade_date':'20191231','con_code':code,'weight':50.} for code in ['600000.SH','000001.SZ']])
    intervals=normalize_memberships(raw,calendar,expected_count=2)
    calendar.to_parquet(reference/'calendar.parquet',index=False);intervals.to_parquet(reference/'memberships.parquet',index=False)
    identity=f'offline:{type(provider).__module__}.{type(provider).__qualname__}'
    plan={'requested_start':'2020-01-01','requested_end':None,'index':'399300.SZ','start':'2020-01-01',
        'end':'2020-03-31','data_start':'2019-06-28','warmup_sessions':127,'codes':['600000.SH','000001.SZ'],
        'tasks':[{'code':code,'start':start,'end':'20200331','name':code,'industry':'fixture'} for code,start in
            [('600000.SH','20190628'),('000001.SZ','20191220')]],
        'reference_files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in reference.glob('*.parquet')},
        'provider_id':hashlib.sha256(identity.encode()).hexdigest(),'snapshots':1,'membership_policy':'monthly_snapshot_next_session'}
    (root/'plan.json').write_text(json.dumps(plan),encoding='utf-8')
    return plan

class SparsePrimary(Primary):
    def query(self,api,**params):
        result=super().query(api,**params)
        return result.head(1) if api=='adj_factor' else result

def test_download_resume_lifecycle_history_and_daily_reader(tmp_path):
    root=tmp_path/'warehouse';provider=SparsePrimary();setup_plan(root,provider)
    first=module.run_index_download(root,provider=provider,secondary=Secondary(),limit=1)
    assert first['status']=='partial' and first['completed']==1 and not (root/'manifest.json').exists()
    count=len(provider.calls)
    final=module.run_index_download(root,provider=provider,secondary=Secondary())
    assert final['status']=='complete' and final['completed']==2 and len(provider.calls)==count+4
    manifest=json.loads((root/'manifest.json').read_text())
    for code,asset in manifest['assets'].items():
        frame=pd.read_parquet(root/asset['file'])
        assert frame.adjustment_source.eq('baostock').all()
        assert frame.date.min()>=pd.Timestamp(asset['start'])
    day=module.index_observations_on(root,'2020-01-02')
    assert day['pool_size']==2 and day['excluded']=={'000001.SZ':'insufficient_pre_history'}
    assert day['rows'].instrument.tolist()==['SH600000']
    assert module.index_observations_on(root,'2020-01-02',require_history=False)['rows'].shape[0]==2
    again=module.run_index_download(root,provider=provider,secondary=Secondary())
    assert again['status']=='complete' and len(provider.calls)==count+4

def test_pause_keeps_raw_partitions_and_resume_does_not_redownload_them(tmp_path):
    class Pausing(Primary):
        pause=True
        def query(self,api,**params):
            if self.pause and api=='daily_basic':raise DownloadPaused('budget')
            return super().query(api,**params)
    root=tmp_path/'warehouse';provider=Pausing();setup_plan(root,provider)
    result=module.run_index_download(root,provider=provider,secondary=Secondary())
    assert result['status']=='paused' and not (root/'manifest.json').exists()
    provider.pause=False
    result=module.run_index_download(root,provider=provider,secondary=Secondary())
    assert result['status']=='complete'

def test_corrupt_receipt_is_not_counted_as_completed(tmp_path):
    root=tmp_path/'warehouse';provider=Primary();setup_plan(root,provider)
    result=module.run_index_download(root,provider=provider,secondary=Secondary())
    record=result['assets']['600000.SH'];(root/record['file']).write_bytes(b'corrupt')
    result=module.run_index_download(root,provider=provider,secondary=Secondary())
    assert result['status']=='partial' and result['completed']==1 and '600000.SH' in result['failures']

def test_frozen_plan_rejects_silent_start_or_history_changes(tmp_path):
    root=tmp_path/'warehouse';provider=Primary();setup_plan(root,provider)
    with pytest.raises(ValueError):module.run_index_download(root,start='2019-01-01',provider=provider,secondary=Secondary())
    with pytest.raises(ValueError):module.run_index_download(root,warmup=21,provider=provider,secondary=Secondary())

def test_lifecycle_end_does_not_create_prices_or_use_a_future_replacement(tmp_path):
    root=tmp_path/'warehouse';provider=Primary();plan=setup_plan(root,provider)
    plan['tasks'][0]['end']='20200115'
    (root/'plan.json').write_text(json.dumps(plan),encoding='utf-8')
    result=module.run_index_download(root,provider=provider,secondary=Secondary())
    assert result['status']=='complete'
    view=module.index_observations_on(root,'2020-01-16',require_history=False)
    assert view['pool_size']==2 and view['rows'].instrument.tolist()==['SZ000001']
    assert view['excluded']=={'600000.SH':'outside_stored_listing_lifecycle'}
