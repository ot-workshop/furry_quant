"""Independently audit live ten-factor UI tasks from frozen files, offline."""
from __future__ import annotations
import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
import numpy as np
import pandas as pd
from finance_quant.application.research_requests import snapshot_registry
from finance_quant.storage import ResultStore


def raw_at(section, index, spec):
    name, window = spec['name'], spec.get('window')
    p = section['adjusted']
    if name in {'momentum','reversal','medium_momentum'}:
        return (p[index-spec.get('skip',0)]/p[index-window]-1)*(-1 if name=='reversal' else 1)
    if name == 'value': return 1/section['pe_ttm'][index]
    if name == 'book_to_price': return 1/section['pb'][index]
    if name == 'size': return -math.log(section['total_mv'][index])
    if name in {'volatility','downside_volatility'}:
        values = p[index-window:index+1]
        r = values[1:]/values[:-1]-1
        if name == 'downside_volatility': r = r[r<0]
        return np.std(r,ddof=1)*np.sqrt(252)
    if name == 'max_drawdown':
        values = p[index-window:index+1]
        return np.min(values/np.maximum.accumulate(values)-1)
    a,base=section['amount'],spec['baseline_window']
    recent,prior=a[index-window+1:index+1],a[index-window-base+1:index-window+1]
    assert np.isfinite(recent).all() and np.isfinite(prior).all() and (recent>=0).all() and (prior>=0).all()
    assert np.count_nonzero(recent>0)>=math.ceil(.8*window) and np.count_nonzero(prior>0)>=math.ceil(.8*base)
    assert a[index]>0 and prior.mean()>0
    return recent.mean()/prior.mean()


def check(evidence,data_root,runs):
    assert evidence.get('status') == 'passed'
    for rejection in evidence.get('preflight_rejections', []):
        assert rejection['status'] == 422
        assert '成交额异动[amount_anomaly] 1/2只' in rejection['detail']
    registry,store=snapshot_registry(data_root),ResultStore(runs)
    raw_checks=score_checks=account_days=trades=labels=0
    histories={}; results={}; backtests={}; halt_checks=0
    for case in evidence['cases']:
        request,task=case['request'],case['task']; assert task['status']=='succeeded'
        result=task['result']; results[case['key']]=result
        market,_,calendar,manifest=registry[result['data_version']]
        assert set(manifest['codes'])==set(request['data']['codes'])
        factors=store.get('factors',result['factor_run_id'])
        backtest=store.get('backtests',result['backtest_run_id'])
        backtests[case['key']]={row['date']:row['equity'] for row in backtest['equity']}
        evaluation=store.get('evaluations',result['evaluation_run_id'])
        specs=sorted(request['factors'],key=lambda s:s.get('id',s['name']))
        assert specs==factors['metadata']['factor_config']
        assert backtest['metadata']['factor_run_id']==evaluation['metadata']['factor_run_id']==result['factor_run_id']
        data={}
        for instrument,section in market.sort_values(['instrument','date']).groupby('instrument'):
            data[instrument]={field:section[field].to_numpy() for field in ['close','adj_factor','pe_ttm','pb','total_mv','amount']}
            data[instrument]['adjusted']=data[instrument]['close']*data[instrument]['adj_factor']
            data[instrument]['index']={date.strftime('%Y-%m-%d'):i for i,date in enumerate(section.date)}
        histories[case['key']]={(r['date'],r['instrument']):r for r in factors['history']}
        for date,observations in pd.DataFrame(factors['history']).groupby('date'):
            expected={}
            for row in observations.to_dict('records'):
                section=data[row['instrument']]; index=section['index'][date]
                expected[row['instrument']]={s.get('id',s['name']):raw_at(section,index,s) for s in specs}
            for row in observations.to_dict('records'):
                weighted=0.
                for spec in specs:
                    key=spec.get('id',spec['name']); value=expected[row['instrument']][key]
                    assert math.isclose(value,row['raw'][key],abs_tol=1e-10), (case['key'],date,row['instrument'],key,value,row['raw'][key])
                    population=[v[key] for v in expected.values()]
                    lower=spec['name'] in {'volatility','downside_volatility'}
                    better=sum(v>value if lower else v<value for v in population); tied=sum(v==value for v in population)
                    score=(better+(tied+1)/2)/len(population)*100
                    assert math.isclose(score,row['scores'][key],abs_tol=1e-8)
                    assert math.isclose(score*spec['weight'],row['contributions'][key],abs_tol=1e-8)
                    raw_checks+=1; score_checks+=2; weighted+=score*spec['weight']
                assert math.isclose(weighted,row['composite'],abs_tol=1e-8); score_checks+=1
        positions=defaultdict(list); fills=defaultdict(list)
        quotes=market.set_index(['instrument','date'])
        for p in backtest['positions']:
            positions[p['date']].append(p)
            quote=quotes.loc[(p['instrument'],pd.Timestamp(p['date']))]
            assert math.isclose(p['price'],quote.close,rel_tol=2e-6,abs_tol=2e-5)
        for row in factors.get('diagnostics',[]):
            quote=quotes.loc[(row['instrument'],pd.Timestamp(row['date']))]
            if quote.get('is_suspended',False):
                for spec in specs:
                    if spec['name'] in {'book_to_price','size'}:
                        key=spec.get('id',spec['name'])
                        assert row['raw'][key] is None and row['reasons'][key]=='suspended_auxiliary_value'
                        halt_checks+=1
        for t in backtest['trades']:
            quote=quotes.loc[(t['instrument'],pd.Timestamp(t['date']))]
            assert not quote.get('is_suspended',False)
            assert math.isclose(t['price'],quote.open,rel_tol=2e-6,abs_tol=2e-5)
            rate=request['backtest']['open_cost' if t['side']=='buy' else 'close_cost']
            assert math.isclose(t['cost'],max(request['backtest']['min_cost'],t['value']*rate),abs_tol=1e-6)
            fills[t['date']].append(t); trades+=1
        cash=request['backtest']['initial_cash']
        for point in backtest['equity'][1:]:
            cash+=sum((t['value'] if t['side']=='sell' else -t['value'])-t['cost'] for t in fills[point['date']])
            assert math.isclose(cash,point['cash'],abs_tol=1e-5)
            assets=cash+sum(p['price']*p['amount'] for p in positions[point['date']])
            assert math.isclose(assets,point['account_value'],abs_tol=1e-5)
            assert math.isclose(assets/request['backtest']['initial_cash'],point['equity'],abs_tol=1e-10)
            account_days+=1
        sessions=pd.DatetimeIndex(calendar.loc[calendar.is_open,'date'])
        for horizon,record in evaluation['labels'].items():
            for row in record['observations']:
                signal,entry,exit_=map(pd.Timestamp,(row['date'],row['entry_date'],row['exit_date']))
                assert sessions.get_loc(entry)==sessions.get_loc(signal)+1
                assert sessions.get_loc(exit_)==sessions.get_loc(entry)+int(horizon)
                assert row['exit_date']<=request['backtest']['end']
                a,b=quotes.loc[(row['instrument'],entry)],quotes.loc[(row['instrument'],exit_)]
                assert not a.get('is_suspended',False) and not b.get('is_suspended',False)
                assert math.isclose(row['forward_return'],b.open*b.adj_factor/(a.open*a.adj_factor)-1,abs_tol=1e-10)
                labels+=1
    baseline=histories['baseline']
    def changed(other,field,key=None):
        return sum(not math.isclose(baseline[k][field][key] if key else baseline[k][field],other[k][field][key] if key else other[k][field],abs_tol=1e-10)
                   for k in baseline.keys()&other.keys())
    assert all(baseline[k]['raw']==histories['weights'][k]['raw'] for k in baseline.keys()&histories['weights'].keys())
    changed_weights=changed(histories['weights'],'composite'); assert changed_weights>0
    changed_window=changed(histories['window'],'raw','momentum'); assert changed_window>0
    changed_skip=changed(histories['skip'],'raw','medium_momentum'); assert changed_skip>0
    changed_baseline=changed(histories['baseline-window'],'raw','amount_anomaly'); assert changed_baseline>0
    pool=histories['pool']; changed_pool=sum(baseline[k]['scores']!=pool[k]['scores'] for k in baseline.keys()&pool.keys()); assert changed_pool>0
    identities = {}
    for case in evidence['cases']:
        result = results[case['key']]
        identity = (result['data_version'], json.dumps(sorted(case['request']['factors'], key=lambda s:s.get('id',s['name'])), sort_keys=True))
        if identity in identities:
            assert identities[identity] == result['factor_run_id']
        identities[identity] = result['factor_run_id']
    assert len(set(identities.values())) == len(identities)
    if 'halt-window' in results:
        assert results['halt-window']['factor_run_id'] == results['baseline']['factor_run_id']
        assert results['halt-window']['backtest_run_id'] != results['baseline']['backtest_run_id']
        assert halt_checks > 0
    changed_backtests = {key:sum(not math.isclose(backtests['baseline'][d],row[d],abs_tol=1e-10)
                                  for d in backtests['baseline'].keys() & row.keys())
                         for key,row in backtests.items() if key not in {'baseline','halt-window'}}
    assert all(count > 0 for count in changed_backtests.values())
    return {'status':'passed','cases':len(results),'preflight_rejections':len(evidence.get('preflight_rejections', [])),
            'raw_factor_checks':raw_checks,'score_and_contribution_checks':score_checks,
            'account_days':account_days,'trade_checks':trades,'forward_label_checks':labels,
            'changed_weight_scores':changed_weights,'changed_momentum_windows':changed_window,'changed_skip_values':changed_skip,
            'changed_amount_baselines':changed_baseline,'changed_pool_percentiles':changed_pool,
            'changed_backtest_days':changed_backtests,'suspended_auxiliary_exclusions':halt_checks}


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--data-root',type=Path,required=True); parser.add_argument('--runs-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path); args=parser.parse_args()
    result=check(json.loads(args.evidence.read_text(encoding='utf-8')),args.data_root,args.runs_dir)
    encoded=json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:args.output.write_text(encoded,encoding='utf-8')
    print(encoded)
