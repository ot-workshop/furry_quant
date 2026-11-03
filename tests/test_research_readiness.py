import copy
import json

import pandas as pd
import pytest

from finance_quant.application.research_readiness import validate_warmup
from finance_quant.application.research_requests import validate_request
from finance_quant.application.research_requests import research_options
from finance_quant.data.market import load_market
from finance_quant.research.registry import FactorConfig, FactorSpec
from test_web_research import workspace, make_client


@pytest.mark.parametrize('spec,required', [
    (FactorSpec('value',1.),1), (FactorSpec('momentum',1.,20,skip=5),21),
    (FactorSpec('max_drawdown',1.,60),61), (FactorSpec('medium_momentum',1.,126,skip=21),127),
    (FactorSpec('amount_anomaly',1.,20,baseline_window=60),80),
    (FactorSpec('amount_anomaly',1.,252,baseline_window=252),504),
])
def test_exact_history_boundary_uses_prior_signal_day_without_adding_skip(spec,required):
    dates = pd.date_range('2025-01-01','2027-12-31')
    calendar = pd.DataFrame({'date':dates,'is_open':(dates.weekday<5) & ~dates.isin(pd.to_datetime(['2025-01-01','2025-01-02']))})
    sessions = calendar.loc[calendar.is_open,'date'].tolist()
    config = FactorConfig((spec,))
    with pytest.raises(ValueError,match=f'至少需要{required}个'):
        validate_warmup('2025-01-01','2027-12-31',sessions[required-1],config,calendar)
    validate_warmup('2025-01-01','2027-12-31',sessions[required],config,calendar)


def test_cached_history_before_requested_data_start_is_not_borrowed():
    dates = pd.date_range('2024-01-01','2026-09-30')
    calendar = pd.DataFrame({'date':dates,'is_open':dates.weekday<5})
    with pytest.raises(ValueError,match='历史预热不足'):
        validate_warmup('2025-06-02','2026-09-30','2025-07-15',FactorConfig((FactorSpec('medium_momentum',1.),)),calendar)


def test_unknown_calendar_rejects_impossible_span_without_guessing_a_trade_date():
    config = FactorConfig((FactorSpec('medium_momentum',1.),))
    with pytest.raises(ValueError,match='127个.*61个自然日'):
        validate_warmup('2025-01-01','2026-09-30','2025-03-03',config)
    validate_warmup('2024-01-01','2026-09-30','2025-03-03',config)


def test_invalid_download_warmup_fails_api_before_queue_or_download(workspace):
    data,runs,request = workspace
    request = copy.deepcopy(request)
    request['data'] = {'mode':'download','start':'2024-12-02','end':'2025-02-28','codes':['600900.SH','000858.SZ'],
                       'benchmark':'000300.SH','validation_mode':'fallback'}
    request['factors'] = [{'name':'medium_momentum','weight':1.,'window':126,'skip':21}]
    client,service = make_client(data,runs)
    response = client.post('/api/v1/research/tasks',json=request,headers={'Idempotency-Key':'warmup-no-download'})
    assert response.status_code == 422
    assert '127个' in response.json()['detail'] and '历史预热不足' in response.json()['detail']
    assert service.store.list()['total'] == 0
    assert not runs.exists() and not (data/'research').exists()


def test_download_without_cached_calendar_rejects_before_creating_directories(tmp_path):
    request = {'name':'warmup', 'data': {'mode':'download','start':'2025-01-01','end':'2026-09-30',
               'codes':['600000.SH','000001.SZ'],'benchmark':'000300.SH','validation_mode':'fallback'},
               'backtest':{'start':'2025-03-03','end':'2026-09-30','topk':2},
               'factors':[{'name':'medium_momentum','weight':1.,'window':126}], 'evaluation':{}}
    with pytest.raises(ValueError,match='127个'):
        validate_request(request,tmp_path/'data',tmp_path/'runs')
    assert not (tmp_path/'data').exists() and not (tmp_path/'runs').exists()


def test_options_identify_current_download_without_exposing_paths(workspace):
    data,runs,_=workspace
    identifier=load_market(data)[3]['id']
    pointer=data/'research/downloads/example/manifest.json'
    pointer.parent.mkdir(parents=True)
    pointer.write_text(json.dumps({'id':identifier}),encoding='utf-8')
    options=research_options(data,runs)
    current=next(s for s in options['snapshots'] if s['id']==identifier)
    assert current['is_current_download'] is True
    assert not any('dir' in key or 'path' in key for key in current)
    pointer.write_text('{broken',encoding='utf-8')
    assert not next(s for s in research_options(data,runs)['snapshots'] if s['id']==identifier)['is_current_download']
