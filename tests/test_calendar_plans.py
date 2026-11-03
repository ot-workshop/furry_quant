import copy
from pathlib import Path

import pandas as pd
import pytest

from finance_quant.application.calendar_plans import plan_dates
from finance_quant.application.research_readiness import validate_download_calendar
from finance_quant.configuration import DataConfig,BacktestConfig,ResearchConfig
from finance_quant.data.managed import download_dataset
from finance_quant.data.providers import TushareProviderError
from finance_quant.research.registry import FactorConfig,FactorSpec
from test_managed_data import Provider
from test_web_research import make_client


class CalendarProvider(Provider):
    # Synthetic calendar fixture; Jan 3 closure reproduces the reported boundary.
    def query(self,api,**params):
        assert api=='trade_cal','Planner must not request stock quotes'
        result=super().query(api,**params)
        result.loc[result.cal_date.eq('20220103'),'is_open']=0
        return result


def document(spec=None):
    return {'data_start':'2022-01-01','data_end':'2026-09-30',
            'backtest_start':'2022-01-03','backtest_end':'2026-09-30',
            'factors':[spec or {'name':'medium_momentum','weight':1.,'window':126,'skip':21}]}


@pytest.mark.parametrize('spec,required',[
    ({'name':'value','weight':1},1),({'name':'momentum','weight':1,'window':20,'skip':5},21),
    ({'name':'amount_anomaly','weight':1,'window':20,'baseline_window':60},80),
    ({'name':'medium_momentum','weight':1,'window':126,'skip':21},127),
    ({'name':'amount_anomaly','weight':1,'window':252,'baseline_window':252},504),
])
def test_plans_preserve_or_delay_period_using_real_session_positions_and_cache(tmp_path,spec,required):
    provider=CalendarProvider();request=document(spec);original=copy.deepcopy(request)
    plan=plan_dates(request,tmp_path,provider=provider)
    assert request==original
    assert plan['required_history']==required and plan['first_trading_day']=='2022-01-04'
    assert [p['mode'] for p in plan['proposals']]==['extend_history','delay_backtest']
    for proposal in plan['proposals']:
        calendar=next(c for c in plan['calendars'] if c['start']<=proposal['data_start'] and c['end']>=proposal['backtest_start'])
        before=[d for d in calendar['trading_dates'] if proposal['data_start']<=d<proposal['backtest_start']]
        assert len(before)==required
        if proposal['mode']=='extend_history':
            assert proposal['backtest_start']=='2022-01-04' and proposal['data_start']<'2022-01-01'
        else:
            assert proposal['data_start']=='2022-01-01' and proposal['backtest_start']>'2022-01-04'
    calls=len(provider.calls)
    assert plan_dates(request,tmp_path,provider=provider)==plan
    assert len(provider.calls)==calls and all(api=='trade_cal' for api,_ in provider.calls)


def test_incomplete_calendar_does_not_create_a_plan(tmp_path):
    class Missing(CalendarProvider):
        def query(self,api,**params):return super().query(api,**params).iloc[1:]
    with pytest.raises(ValueError,match='日历缺失或重复'):plan_dates(document(),tmp_path,provider=Missing())


@pytest.mark.parametrize('spec,required', [
    ({'name':'value','weight':1},1),
    ({'name':'momentum','weight':1,'window':30},31),
    ({'name':'medium_momentum','weight':1,'window':126,'skip':21},127),
    ({'name':'amount_anomaly','weight':1,'window':252,'baseline_window':252},504),
])
def test_automatic_plan_only_needs_backtest_dates_and_derives_both_warmup_bounds(tmp_path,spec,required):
    provider=CalendarProvider()
    request={'auto_range':True,'backtest_start':'2022-01-03','backtest_end':'2022-04-03','factors':[spec]}
    original=copy.deepcopy(request)
    plan=plan_dates(request,tmp_path,provider=provider)
    assert request==original and len(plan['proposals'])==1
    proposal=plan['proposals'][0]
    assert proposal['mode']=='auto_range'
    assert proposal['backtest_start']=='2022-01-04'
    assert proposal['backtest_end']==proposal['data_end']=='2022-04-01'
    assert proposal['warmup_end']=='2021-12-31'
    assert proposal['warmup_start']==proposal['data_start']
    assert proposal['history_days']==required
    calendar=next(c for c in plan['calendars'] if c['start']<=proposal['data_start'] and c['end']>='2022-01-04')
    history=[d for d in calendar['trading_dates'] if proposal['data_start']<=d<'2022-01-04']
    assert len(history)==required and history[0]==proposal['data_start'] and history[-1]==proposal['warmup_end']
    assert all(api=='trade_cal' for api,_ in provider.calls)


def test_plan_endpoint_does_not_create_task_and_hides_provider_errors(tmp_path,monkeypatch):
    from finance_quant import research_api
    provider=CalendarProvider()
    monkeypatch.setattr(research_api,'plan_dates',lambda doc,root:plan_dates(doc,root,provider=provider))
    client,service=make_client(tmp_path/'data',tmp_path/'runs')
    assert client.post('/api/v1/research/date-plan',json=document(),headers={'Origin':'https://external.test'}).status_code==403
    assert not provider.calls
    response=client.post('/api/v1/research/date-plan',json=document())
    assert response.status_code==200 and response.json()['first_trading_day']=='2022-01-04'
    assert service.store.list()['total']==0 and not (tmp_path/'runs').exists()
    def fail(*args):raise TushareProviderError('secret-not-for-response')
    monkeypatch.setattr(research_api,'plan_dates',fail)
    response=client.post('/api/v1/research/date-plan',json=document())
    assert response.status_code==503 and 'secret-not-for-response' not in response.text


def test_invalid_parameters_fail_before_any_calendar_request(tmp_path):
    provider=CalendarProvider()
    bad=document({'name':'medium_momentum','weight':1,'window':126,'skip':126})
    with pytest.raises(ValueError):plan_dates(bad,tmp_path/'data',provider=provider)
    assert not provider.calls and not (tmp_path/'data').exists()


def test_holiday_or_incomplete_warmup_stops_download_before_stock_requests(tmp_path):
    provider=CalendarProvider()
    data=DataConfig(tmp_path/'data','20210101','20220331',('600000.SH','000001.SZ'))
    config=ResearchConfig(data,BacktestConfig('2022-01-03','2022-03-31',topk=1),tmp_path/'runs',FactorConfig((FactorSpec('medium_momentum',1.),)))
    with pytest.raises(ValueError,match='实际交易日'):
        download_dataset(data,provider=provider,calendar_validator=lambda c:validate_download_calendar(config,c))
    assert provider.calls and all(api=='trade_cal' for api,_ in provider.calls)
    assert not (data.root/'manifest.json').exists()


def test_split_calendar_ranges_never_count_unknown_gap_as_history():
    from finance_quant.application.calendar_plans import make_date_plan
    old=pd.DataFrame({'date':pd.date_range('2020-01-01','2020-12-31'),'is_open':True})
    recent=pd.DataFrame({'date':pd.date_range('2022-01-01','2022-01-10'),'is_open':True})
    end=pd.DataFrame({'date':pd.date_range('2026-09-01','2026-09-30'),'is_open':True})
    with pytest.raises(ValueError,match='可用日历不足'):make_date_plan(document(),[old,recent,end])


def test_end_date_must_not_jump_over_unqueried_years_when_end_block_has_no_sessions():
    from finance_quant.application.calendar_plans import make_date_plan
    early_dates=pd.date_range('2021-01-01','2022-12-31')
    early=pd.DataFrame({'date':early_dates,'is_open':early_dates.weekday<5})
    end=pd.DataFrame({'date':pd.date_range('2026-01-01','2026-09-30'),'is_open':False})
    with pytest.raises(ValueError,match='没有可用交易日'):make_date_plan(document(),[early,end])


@pytest.mark.parametrize('size',[2,7])
def test_extended_history_plan_completes_download_and_native_backtest(tmp_path,monkeypatch,size):
    from test_pool_workflow_matrix import codes_for,request_for,run_request
    from finance_quant.storage import ResultStore
    class CalendarQuotes(Provider):
        def query(self,api,**params):
            result=super().query(api,**params)
            if api=='trade_cal':result.loc[result.cal_date.eq('20220103'),'is_open']=0
            elif 'trade_date' in result:result=result.loc[~result.trade_date.eq('20220103')]
            return result
    doc=document();doc['data_end']=doc['backtest_end']='2022-03-31'
    plan=plan_dates(doc,tmp_path/'calendar',provider=CalendarProvider())['proposals'][0]
    request=request_for(codes_for(size,79))
    request['data'].update(start=plan['data_start'],end=plan['data_end'],validation_mode='off')
    request['backtest'].update(start=plan['backtest_start'],end=plan['backtest_end'])
    request['factors']=doc['factors']
    task=run_request(request,tmp_path/'data',tmp_path/'runs',monkeypatch,CalendarQuotes())
    assert task['status']=='succeeded',task['error']
    store=ResultStore(tmp_path/'runs')
    backtest=store.get('backtests',task['result']['backtest_run_id'])
    evaluation=store.get('evaluations',task['result']['evaluation_run_id'])
    assert backtest['summary']['start_date']=='2022-01-04'
    assert backtest['metadata']['factor_run_id']==evaluation['metadata']['factor_run_id']==task['result']['factor_run_id']
    assert all(t['date']>='2022-01-04' for t in backtest['trades'])


@pytest.mark.parametrize('size',[2,7,15])
def test_automatic_range_sparse_factors_and_secondary_reconnect_complete_native_workflow(tmp_path,monkeypatch,size):
    from finance_quant.application import task_worker
    from finance_quant.application.research_requests import validate_request
    from finance_quant.application.tasks import TaskStore
    from finance_quant.data.market import load_market
    from finance_quant.data.providers.baostock import BaoStockProvider
    from finance_quant.storage import ResultStore
    from test_pool_workflow_matrix import codes_for,request_for,DiversePrimary,DiverseSecondary
    codes=codes_for(size,177)
    request=request_for(codes)
    request['backtest'].update(start='2025-02-03',end='2025-03-28')
    request['factors'][0]['window']=30+size
    request['factors'][0]['weight']=.6
    request['factors'][1]['weight']=.1
    automatic={'auto_range':True,'backtest_start':request['backtest']['start'],
               'backtest_end':request['backtest']['end'],'factors':request['factors']}
    plan=plan_dates(automatic,tmp_path/'calendar',provider=CalendarProvider())['proposals'][0]
    request['data'].update(start=plan['data_start'],end=plan['data_end'])
    class Sparse(DiversePrimary):
        def query(self,api,**params):
            frame=super().query(api,**params)
            return frame.head(1) if api=='adj_factor' else frame
    primary=Sparse()
    calls={}
    secondary=DiverseSecondary()
    def worker(parameters):
        code=parameters['code'];calls[code]=calls.get(code,0)+1
        if code==codes[-1] and calls[code]==1:raise TimeoutError('private network body')
        bundle=secondary.fetch(code,parameters['start'],parameters['end'])
        return {'raw':bundle.raw.to_dict('records'),'adjusted':bundle.adjusted.to_dict('records')}
    adapter=BaoStockProvider(tmp_path/'secondary',worker=worker)
    original=download_dataset
    monkeypatch.setattr(task_worker,'download_dataset',lambda data,**kwargs:original(data,provider=primary,secondary_provider=adapter,**kwargs))
    data,runs=tmp_path/'data',tmp_path/'runs'
    normalized,execution=validate_request(request,data,runs)
    store=TaskStore(runs);store.initialize()
    task=store.create(normalized,execution,f'auto-sparse-pool-{size}')
    assert store.claim()==task['id']
    task_worker.run_task(runs,data,task['id'])
    result=store.get(task['id'])
    assert result['status']=='succeeded',result['error']
    assert result['request']['factors']==normalized['factors']
    market,_,calendar,manifest=load_market(Path(execution['download_root']))
    assert len(market.instrument.unique())==size
    assert market.adjustment_source.eq('baostock').all()
    assert set(manifest['cross_validation']['fallback_codes'])==set(codes)
    assert calls[codes[-1]]==2 and all(n==1 for code,n in calls.items() if code!=codes[-1])
    assert not any('trade_date' in dict(params) for api,params in primary.calls if api=='adj_factor')
    artifacts=ResultStore(runs)
    for kind,key in [('backtests','backtest_run_id'),('evaluations','evaluation_run_id')]:
        artifact=artifacts.get(kind,result['result'][key])
        assert artifact['metadata']['factor_run_id']==result['result']['factor_run_id']
    backtest=artifacts.get('backtests',result['result']['backtest_run_id'])
    assert backtest['summary']['start_date']==plan['backtest_start']
    assert all(t['date']>=plan['backtest_start'] for t in backtest['trades'])
