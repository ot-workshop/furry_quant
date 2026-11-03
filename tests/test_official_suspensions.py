import hashlib,json
import pandas as pd
import pytest
from finance_quant.data.official_suspensions import full_day_halts
from finance_quant.data.providers import TushareProvider,TushareSettings
from finance_quant.application import index_download as module
from test_joint_data import market_frame,Primary,Secondary,bundle
from dataclasses import replace
from test_index_download import setup_plan


def status(day='20250106',kind='S',timing=None,code='600000.SH'):
    return {'ts_code':code,'trade_date':day,'suspend_type':kind,'suspend_timing':timing}


def test_intraday_and_future_resume_cannot_create_daily_halts():
    frame=market_frame()
    raw=pd.DataFrame([status(timing='13:00-15:00'),status('20250107','R')])
    assert full_day_halts(raw,'600000.SH','20250102','20250108',frame).empty
    frame.loc[2,['volume','amount']]=float('nan')
    raw=pd.DataFrame([status(),status('20250107','R')])
    assert full_day_halts(raw,'600000.SH','20250102','20250108',frame).tolist()==[pd.Timestamp('2025-01-06')]


@pytest.mark.parametrize('case',['wrong_code','outside_date','unknown_type','activity','ambiguous','schema'])
def test_invalid_status_evidence_is_rejected(case):
    frame=market_frame();frame.loc[2,['volume','amount']]=float('nan')
    row=status()
    if case=='wrong_code':row['ts_code']='000001.SZ'
    if case=='outside_date':row['trade_date']='20250109'
    if case=='unknown_type':row['suspend_type']='X'
    if case=='activity':frame.loc[2,'amount']=123
    raw=pd.DataFrame([row])
    if case=='ambiguous':raw=pd.DataFrame([row,status(kind='R')])
    if case=='schema':raw=raw.drop(columns='suspend_timing')
    with pytest.raises(ValueError):full_day_halts(raw,'600000.SH','20250102','20250108',frame)


@pytest.mark.parametrize('ambiguous',[False,True])
def test_official_halts_complete_warehouse_without_baostock_and_keep_intraday_trades(tmp_path,monkeypatch,ambiguous):
    monkeypatch.setattr(module,'sample_code',lambda code:False)
    class Official(TushareProvider):
        def __init__(self):
            super().__init__(TushareSettings('offline-test'))
            self.fixture=Primary()
        def query(self,api,**params):
            if api=='suspend_d':
                return pd.DataFrame([status('20200106',code=params['ts_code']),
                    status('20200107',timing='13:00-15:00',code=params['ts_code'])]+
                    ([status('20200106','R',code=params['ts_code'])] if ambiguous else []))
            raw=self.fixture.query(api,**params)
            return raw.loc[raw.trade_date.ne('20200106')] if api in ['daily','daily_basic'] else raw
    root=tmp_path/'warehouse';provider=Official();plan=setup_plan(root,provider)
    plan['provider_id']=hashlib.sha256(provider.settings.http_url.encode()).hexdigest()
    (root/'plan.json').write_text(json.dumps(plan),encoding='utf-8')
    class HaltSecondary(Secondary):
        def fetch(self,code,start,end):
            self.calls.append((code,start,end))
            item=bundle(market_frame(pd.bdate_range(start,end),code),code)
            raw,adjusted=item.raw.copy(),item.adjusted.copy()
            for part in [raw,adjusted]:
                part.loc[part.date.eq('2020-01-06'),'tradestatus']='0'
                part.loc[part.date.eq('2020-01-06'),['volume','amount']]='0'
            return replace(item,raw=raw,adjusted=adjusted)
    secondary=HaltSecondary() if ambiguous else Secondary(error=True)
    result=module.run_index_download(root,provider=provider,secondary=secondary)
    assert result['status']=='complete'
    assert bool(secondary.calls)==ambiguous
    assert result['validation_counts']['single_source_passed']==2
    for asset in result['assets'].values():
        frame=pd.read_parquet(root/asset['file'])
        halted=frame.loc[frame.date.eq('2020-01-06')].iloc[0]
        active=frame.loc[frame.date.eq('2020-01-07')].iloc[0]
        assert halted.is_suspended and halted.volume==0 and halted.close==10
        assert halted.valuation_asof=='2020-01-03'
        assert not active.is_suspended and active.volume>0
        receipt=json.loads((root/asset['file']).with_name('receipt.json').read_text())
        evidence=receipt['cross_validation']['suspension_evidence']
        assert evidence['dates']==['2020-01-06']
        if ambiguous:
            assert evidence['source']['source']=='baostock'
            assert receipt['cross_validation']['official_status_reviews']
        else:
            assert evidence['source']=='tushare_suspend_d'
            assert evidence['requests'][0]['sha256']
    requests=len(provider.fixture.calls)
    again=module.run_index_download(root,provider=provider,secondary=secondary)
    assert again['status']=='complete' and len(provider.fixture.calls)==requests
