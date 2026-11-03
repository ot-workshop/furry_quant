from dataclasses import replace
import copy
import numpy as np
import pandas as pd
import pytest
from finance_quant.configuration import BacktestConfig
from finance_quant.research.execution_policy import ExecutionCosts,rebalance_dates


def test_schedule_is_first_available_session_of_period_and_first_entry_even_mid_period():
    calendar=pd.to_datetime(['2023-09-27','2023-09-28','2023-10-09','2023-10-10','2023-10-13','2023-10-16','2023-11-01'])
    assert rebalance_dates(calendar,'2023-09-28','2023-11-01','monthly')==['2023-09-28','2023-10-09','2023-11-01']
    assert rebalance_dates(calendar,'2023-09-28','2023-11-01','weekly')==['2023-09-28','2023-10-09','2023-10-16','2023-11-01']
    assert rebalance_dates(calendar,'2023-09-28','2023-11-01','daily')==calendar[1:].strftime('%Y-%m-%d').tolist()
    assert rebalance_dates(calendar[:-1],'2023-09-28','2023-10-16','monthly')==rebalance_dates(calendar,'2023-09-28','2023-10-16','monthly')
    year=pd.to_datetime(['2024-12-27','2024-12-30','2024-12-31','2025-01-02','2025-01-06'])
    assert rebalance_dates(year,'2024-12-27','2025-01-06','weekly')==['2024-12-27','2024-12-30','2025-01-06']


def test_historical_tax_boundaries_minimum_commission_and_independent_impact():
    costs=ExecutionCosts(cost_mode='itemized',impact_cost=.002)
    a=costs.breakdown(1000.,False,'2022-04-28')
    assert a==pytest.approx(dict(commission=5.,stamp_tax=0.,transfer_fee=.02,impact=2.,legacy_fee=0.,total=7.02))
    assert costs.breakdown(1000.,False,'2022-04-29')['transfer_fee']==pytest.approx(.01)
    assert costs.breakdown(1000.,True,'2023-08-25')['stamp_tax']==pytest.approx(1.)
    assert costs.breakdown(1000.,True,'2023-08-28')['stamp_tax']==pytest.approx(.5)
    assert costs.breakdown(0.,True,'2023-08-28')['total']==0
    fixed=replace(costs,tax_mode='fixed',stamp_tax_rate=.003,transfer_fee_rate=.00004)
    assert fixed.breakdown(1000.,True,'2020-01-02')['total']==pytest.approx(10.04)
    legacy=replace(costs,cost_mode='legacy',open_cost=.001,close_cost=.002)
    assert legacy.breakdown(1000.,True,'2020-01-02')==pytest.approx(dict(commission=0.,stamp_tax=0.,transfer_fee=0.,impact=2.,legacy_fee=5.,total=7.))
    # An additional yuan for tax/impact must not get swallowed by minimum commission.
    assert costs.max_buy_value(1007.02,'2022-04-28')==pytest.approx(1000.)


@pytest.mark.parametrize('change',[
    {'rebalance_frequency':'quarterly'},{'rebalance_frequency':True},{'cost_mode':'combined'},
    {'tax_mode':'today'},{'commission_rate':True},{'stamp_tax_rate':-.001},{'transfer_fee_rate':float('nan')},
    {'impact_cost':float('inf')},{'impact_cost':1.},{'cost_mode':'itemized','tax_mode':'fixed','commission_rate':.7,'stamp_tax_rate':.4},
])
def test_invalid_execution_settings_are_rejected(change):
    with pytest.raises(ValueError):BacktestConfig('2020-01-02','2020-01-31',**change)


def test_historical_fee_table_rejects_unsupported_earlier_range():
    with pytest.raises(ValueError,match='2015-08-01'):BacktestConfig('2015-07-31','2015-08-31',cost_mode='itemized')
    BacktestConfig('2015-07-31','2015-08-31',cost_mode='itemized',tax_mode='fixed')


def test_native_exchange_accounts_for_minimum_tax_impact_and_cash_before_fill(tmp_path):
    import qlib
    from qlib.backtest.position import Position
    from qlib.backtest.decision import Order
    from finance_quant.integrations.china_a import ChinaAExchange
    from finance_quant.integrations.qlib_runner import write_features
    days=pd.to_datetime(['2022-04-28','2022-04-29','2022-05-05'])
    quotes=pd.DataFrame({'date':days,'instrument':'SH688001','open':10.,'close':10.,'high':10.,'low':10.,
        'volume':[100.,1e9,100.],'factor':1.,'change':0.,'up_limit':12.,'down_limit':8.})
    write_features(quotes,days,tmp_path/'qlib')
    qlib.init(provider_uri=str(tmp_path/'qlib'),region='cn',kernels=1,expression_cache=None,dataset_cache=None)
    states={('SH688001',d):{'execution_available':True,'execution_state':'active','has_daily_price_limit':True,'open':10.,'up_limit':12.,'down_limit':8.} for d in days}
    costs=ExecutionCosts(cost_mode='itemized',impact_cost=.002)
    exchange=ChinaAExchange(market_states=states,execution_costs=costs,start_time=days[0],end_time=days[-1],
        codes=['SH688001'],deal_price='$open',limit_threshold=('$open < 0','$open < 0'),open_cost=.001,close_cost=.001,min_cost=5.,impact_cost=.002)
    def order(amount,side,day):return Order(stock_id='SH688001',amount=amount,direction=side,start_time=day,end_time=day)
    cash=2010+5+.0402+4.02
    position=Position(cash=cash)
    buy=order(201,Order.BUY,days[0]);value,fee,price=exchange.deal_order(buy,position=position)
    assert buy.deal_amount==201 and value==2010 and fee==pytest.approx(9.0602)
    assert position.get_cash()==pytest.approx(0.,abs=1e-8)
    position.add_count_all('day')
    sell=order(201,Order.SELL,days[1]);value,fee,_=exchange.deal_order(sell,position=position)
    assert fee==pytest.approx(5+2.01+.0201+4.02)
    assert position.get_cash()==pytest.approx(2010-fee)
    # Cash one cent short reduces STAR by one share; it cannot go negative.
    small=Position(cash=cash-.01);buy=order(201,Order.BUY,days[0]);exchange.deal_order(buy,position=small)
    assert buy.deal_amount==200 and small.get_cash()>=0
    # Future/full-day volume never drives our fixed-notional opening impact.
    large=Position(cash=10000.);buy=order(201,Order.BUY,days[1]);_,fee,_=exchange.deal_order(buy,position=large)
    assert fee==pytest.approx(5+.0201+4.02)
    assert costs.breakdown(2010.,False,days[0])['impact']==costs.breakdown(2010.,False,days[1])['impact']


def test_fixed_pool_frequency_changes_orders_and_exact_costs_flow_into_daily_account(tmp_path):
    from finance_quant.integrations.qlib_runner import run_qlib
    days=pd.bdate_range('2023-08-21','2023-10-03');codes=['SH600000','SZ000001']
    quotes=pd.concat([pd.DataFrame({'date':days,'instrument':code,'open':10.,'close':10.,'high':10.,'low':10.,
        'volume':1e6,'factor':1.,'change':0.,'up_limit':11.,'down_limit':9.}) for code in [*codes,'SH000300']],ignore_index=True)
    history=[{'date':d.strftime('%Y-%m-%d'),'instrument':code,'available_at':d.tz_localize('Asia/Shanghai').isoformat(),
              'composite':100. if i%2==j else 0.} for i,d in enumerate(days) for j,code in enumerate(codes)]
    factors={'metadata':{'id':'alternate'},'history':history};results={}
    for frequency in ['daily','weekly','monthly']:
        result=run_qlib(quotes,factors,tmp_path/frequency,calendar=days,start='2023-08-22',end='2023-09-29',
            initial_date='2023-08-21',names={code:code for code in codes},initial_cash=100000.,topk=1,n_drop=1,
            rebalance_frequency=frequency,cost_mode='itemized',commission_rate=.0002,impact_cost=.001,require_availability=True)
        schedule=result['metadata']['rebalance_policy']['dates'];results[frequency]=result
        assert result['trades'] and all(t['date'] in schedule for t in result['trades'])
        assert schedule[0]=='2023-08-22'
        cash=100000.;positions={};by_day={}
        for trade in result['trades']:by_day.setdefault(trade['date'],[]).append(trade)
        for point in result['equity'][1:]:
            for trade in by_day.get(point['date'],[]):
                value=trade['value'];sell=trade['side']=='sell';tax=(.001 if trade['date']<'2023-08-28' else .0005) if sell else 0.
                fee=max(value*.0002,5.)+value*(tax+.00001+.001)
                assert trade['cost']==pytest.approx(fee)
                positions[trade['instrument']]=positions.get(trade['instrument'],0)+(trade['amount']*(-1 if sell else 1))
                cash+=value-fee if sell else -value-fee
            assert cash>=0 and point['cash']==pytest.approx(cash)
            assert point['account_value']==pytest.approx(cash+sum(positions.values())*10.)
        assert len(result['equity'])==len(days[(days>='2023-08-22')&(days<='2023-09-29')])+1
    assert len(results['daily']['trades'])>len(results['weekly']['trades'])>len(results['monthly']['trades'])
    assert results['daily']['metadata']['cost_summary']['total']>results['monthly']['metadata']['cost_summary']['total']
    changed=quotes.copy();changed['volume']*=10000.
    changed.loc[changed.date.gt('2023-09-05'),['open','close','high','low']]*=2.
    other=run_qlib(changed,factors,tmp_path/'future',calendar=days,start='2023-08-22',end='2023-09-29',
        initial_date='2023-08-21',names={code:code for code in codes},initial_cash=100000.,topk=1,n_drop=1,
        rebalance_frequency='weekly',cost_mode='itemized',commission_rate=.0002,impact_cost=.001,require_availability=True)
    for key in ['trades','equity']:
        assert [r for r in other[key] if r['date']<='2023-09-05']==[r for r in results['weekly'][key] if r['date']<='2023-09-05']


@pytest.mark.parametrize('frequency',['weekly','monthly'])
def test_dynamic_forced_exits_retry_on_non_rebalance_days_and_cash_waits(tmp_path,frequency):
    from test_historical_workflow import warehouse_fixture,request
    from finance_quant.application.historical_workflow import run_historical_research
    from finance_quant.storage import ResultStore
    warehouse=tmp_path/'warehouse';warehouse_fixture(warehouse,blocked=True)
    body=request();body['backtest'].update(end='2024-02-01',rebalance_frequency=frequency,cost_mode='itemized',impact_cost=.001)
    output=run_historical_research(body,warehouse,tmp_path/'runs');store=ResultStore(tmp_path/'runs')
    result=store.get('backtests',output['backtest_run_id'])
    dates=result['metadata']['rebalance_policy']['dates']
    assert result['trades'] and all(t['date'] in dates for t in result['trades'] if t['side']=='buy')
    exits=[t for t in result['trades'] if t.get('reason')=='index_exit']
    assert exits and exits[0]['date']=='2024-01-16' and exits[0]['date'] not in dates
    assert all(t['date'] in dates or t.get('reason')=='index_exit' for t in result['trades'])
    assert len(result['equity'])==len(pd.bdate_range('2024-01-08','2024-02-01'))+1
    if frequency=='monthly':assert next(t for t in result['trades'] if t['instrument']=='SH688001' and t['side']=='buy')['date']=='2024-02-01'
    for trade in result['trades']:
        b=trade['cost_breakdown'];assert b['total']==pytest.approx(trade['cost'])
        assert b['commission']==pytest.approx(max(trade['value']*.0003,5))
        assert b['stamp_tax']==pytest.approx(trade['value']*.0005 if trade['side']=='sell' else 0.)
        assert b['impact']==pytest.approx(trade['value']*.001)
    assert result['metadata']['cost_summary']['total']==pytest.approx(sum(t['cost'] for t in result['trades']))
    assert result['metadata']['execution_summary']['pending_exits']==[]
    assert run_historical_research(body,warehouse,tmp_path/'runs')['backtest_reused']
    changed=copy.deepcopy(body);changed['backtest']['commission_rate']=.002
    altered=run_historical_research(changed,warehouse,tmp_path/'runs')
    assert altered['factor_run_id']==output['factor_run_id'] and altered['backtest_run_id']!=output['backtest_run_id']
