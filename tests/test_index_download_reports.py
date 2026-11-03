import hashlib,json,sys
from pathlib import Path
from dataclasses import replace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from finance_quant.application import index_download as download
from finance_quant.application import index_download_reports as reports
from finance_quant.application import index_download_manager as manager
from finance_quant.data.market import SnapshotFetcher
from finance_quant.data.managed import atomic_json
from finance_quant.api import create_app
from test_joint_data import Primary,Secondary,market_frame
from test_index_download import setup_plan,SparsePrimary


def warehouse(tmp_path,monkeypatch,provider=None,secondary=None,sample=False):
    monkeypatch.setattr(download,'sample_code',lambda code:sample)
    root=tmp_path/'warehouse';provider=provider or Primary();setup_plan(root,provider)
    download.run_index_download(root,provider=provider,secondary=secondary or Secondary())
    return root


def test_report_reuses_receipts_and_preserves_frozen_warehouse(tmp_path,monkeypatch):
    root=warehouse(tmp_path,monkeypatch)
    original=(root/'manifest.json').read_bytes()
    def forbidden(*args,**kwargs):raise AssertionError('Report must never query a provider')
    monkeypatch.setattr(Primary,'query',forbidden);monkeypatch.setattr(Secondary,'fetch',forbidden)
    report=reports.refresh_handling_report(root)
    assert report['summary']['passed']==2 and report['summary']['auto_handled']==0
    assert report['summary']['validation_counts']=={'single_source_passed':2}
    assert all(s['outcome']=='passed' for s in report['stocks'])
    assert (root/'manifest.json').read_bytes()==original
    assert json.loads((root/'handling-reports'/f"{report['job_id']}.json").read_text())==report
    timestamp=(root/'handling-report.json').stat().st_mtime_ns
    assert reports.refresh_handling_report(root)==report
    assert (root/'handling-report.json').stat().st_mtime_ns==timestamp
    progress=json.loads((root/'progress.json').read_text());progress.update(status='paused',updated_at='new-progress')
    atomic_json(root/'progress.json',progress)
    next_report=reports.refresh_handling_report(root)
    assert next_report['source_id']!=report['source_id'] and next_report['job_handling']


@pytest.mark.parametrize('scenario',['optional_unavailable','fallback','unresolved'])
def test_report_does_not_confuse_limits_or_failures_with_verified_repairs(tmp_path,monkeypatch,scenario):
    provider=Primary() if scenario=='optional_unavailable' else SparsePrimary()
    secondary=Secondary(error=scenario!='fallback')
    root=warehouse(tmp_path,monkeypatch,provider,secondary,sample=True)
    report=reports.refresh_handling_report(root)
    if scenario=='unresolved':
        assert report['summary']['passed']==0 and report['summary']['needs_review']==2
        assert all(s['outcome']=='needs_review' and s['actions'][0]['kind']=='unresolved' for s in report['stocks'])
    else:
        assert report['summary']['auto_handled']==2 and report['summary']['needs_review']==0
        rule='secondary_unavailable' if scenario=='optional_unavailable' else 'whole_interval_fallback'
        assert all(rule in [a['rule'] for a in s['actions']] for s in report['stocks'])
        level='single_source_passed' if scenario=='optional_unavailable' else 'secondary_fallback_passed'
        assert all(s['validation_level']==level for s in report['stocks'])


def test_exact_halt_intraday_and_status_conflict_explanations_use_saved_evidence(tmp_path):
    code='600000.SH';root=tmp_path.resolve();directory=root/'assets/stock';directory.mkdir(parents=True)
    frame=market_frame(pd.bdate_range('20250102',periods=5))
    frame['is_suspended']=frame.date.eq('2025-01-06')
    frame.loc[frame.is_suspended,['volume','amount']]=0
    frame.to_parquet(directory/'market.parquet',index=False)
    class StatusSource:
        def query(self,*args,**kwargs):
            return pd.DataFrame([{'ts_code':code,'trade_date':'20250106','suspend_type':'S','suspend_timing':None},
                {'ts_code':code,'trade_date':'20250107','suspend_type':'S','suspend_timing':'13:00-15:00'}])
    fetch=SnapshotFetcher(StatusSource(),root/'suspension-raw')
    fetch.get('suspend_d',ts_code=code,start_date='20250101',end_date='20250131')
    request=fetch.requests[0]
    receipt={'code':code,'file':'assets/stock/market.parquet','sha256':hashlib.sha256((directory/'market.parquet').read_bytes()).hexdigest(),
             'validation_level':'single_source_passed','quality':{},'cross_validation':{'suspension_evidence':{
                 'source':'tushare_suspend_d','dates':['2025-01-06'],'requests':[request]}}}
    record=reports.describe_receipt(root,receipt)
    actions={a['rule']:a for a in record['actions']}
    assert actions['official_full_day_halt']['dates']==['2025-01-06']
    assert actions['intraday_halt']['dates']==['2025-01-07']
    assert actions['intraday_halt']['evidence'][0]['sha256']==request['sha256']
    # A failed or unobservable intraday record must not be described as a
    # preserved real trade merely because its provider annotation says so.
    frame.loc[frame.date.eq('2025-01-07'),['volume','amount']]=0
    frame.to_parquet(directory/'market.parquet',index=False)
    receipt['sha256']=hashlib.sha256((directory/'market.parquet').read_bytes()).hexdigest()
    assert 'intraday_halt' not in [a['rule'] for a in reports.describe_receipt(root,receipt)['actions']]
    # Older dual-source receipts carry halts inside the stock crosscheck.
    receipt['cross_validation']={'stocks':[{'suspended_dates':['2025-01-06'],'secondary':{'source':'baostock','files':{}}}]}
    assert reports.describe_receipt(root,receipt)['actions'][0]['rule']=='independent_halt_status'


def test_api_and_download_are_read_only_and_expose_same_report(tmp_path,monkeypatch):
    root=warehouse(tmp_path,monkeypatch)
    monkeypatch.setattr(manager,'warehouse_root',lambda:root)
    client=TestClient(create_app(tmp_path/'runs',data_root=tmp_path/'data'),base_url='http://127.0.0.1')
    result=client.get('/api/v1/research/index-download/report')
    assert result.status_code==200 and result.json()['summary']['passed']==2
    attachment=client.get('/api/v1/research/index-download/report/download')
    assert attachment.status_code==200 and attachment.json()==result.json()
    assert 'attachment' in attachment.headers['content-disposition']
    assert attachment.headers['cache-control']=='no-store'


@pytest.mark.parametrize('status',['complete','paused','interrupted'])
def test_cli_saves_reports_for_success_pause_and_interruption(tmp_path,monkeypatch,status,capsys):
    root=tmp_path/'warehouse';root.mkdir()
    def execute(*args,**kwargs):
        progress={'status':status,'assets':{},'failures':{},'total':0,'job_id':'test-job'}
        atomic_json(root/'progress.json',progress)
        if status=='interrupted':raise RuntimeError('simulated interruption')
        return progress
    monkeypatch.setattr(download,'run_index_download',execute)
    monkeypatch.setattr(sys,'argv',['finance-quant','index-download','--data-dir',str(root)])
    from finance_quant.cli import main
    if status=='interrupted':
        with pytest.raises(RuntimeError,match='simulated interruption'):main()
    else:main()
    assert json.loads((root/'handling-report.json').read_text())['status']==status


def test_unknown_failures_do_not_echo_provider_messages_or_secrets():
    failed=reports._failure('600000.SH',{'reason':'secret-provider-token-or-body','dates':['2025-01-06']},'fixture')
    assert 'secret-provider' not in json.dumps(failed)
    assert failed['outcome']=='needs_review' and failed['actions'][0]['dates']==['2025-01-06']
