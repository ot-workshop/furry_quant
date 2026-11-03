"""Saved industry attribution of actual holdings, independent of trade decisions."""
from collections import defaultdict
import pandas as pd

def attach_industry_exposure(result,history,calendar,*,neutralization=None,memberships=None):
    if history is None:return
    holdings=defaultdict(list)
    for row in result['positions']:holdings[row['date']].append(row)
    days=[]
    for account in result['equity']:
        date=account['date'];positions=holdings[date]
        labels=history.at(date,[r['instrument'] for r in positions],calendar)
        grouped={}
        def add(symbol,weight,field):
            label=labels[symbol];code=label['industry_code'] or 'unknown'
            group=grouped.setdefault(code,{'industry_code':code,'industry_name':label['industry_name'] or '未知/冲突',
                'weight':0.,'count':0,'benchmark_weight':None,'active_weight':None})
            if field=='weight':group['weight']+=weight;group['count']+=1
            else:group['benchmark_weight']=(group['benchmark_weight'] or 0.)+weight
        for position in positions:add(position['instrument'],position['weight'],'weight')
        benchmark_available=False;snapshot=None
        if memberships is not None:
            day=pd.Timestamp(date)
            active=memberships.loc[memberships.effective_from.le(day)&
                (memberships.effective_until.isna()|memberships.effective_until.gt(day))&memberships.snapshot_date.lt(day)]
            if len(active):
                symbols=active.code.map(lambda c:c[-2:]+c[:6]);labels.update(history.at(date,symbols,calendar))
                for symbol,weight in zip(symbols,active.weight):add(symbol,float(weight)/100.,'benchmark_weight')
                benchmark_available=True;snapshot=str(active.snapshot_date.max().date())
        if benchmark_available:
            for group in grouped.values():
                group['benchmark_weight']=group['benchmark_weight'] or 0.
                group['active_weight']=group['weight']-group['benchmark_weight']
        weights=[v['weight'] for v in grouped.values()];invested=sum(weights)
        days.append({'date':date,'items':sorted(grouped.values(),key=lambda r:-r['weight']),
            'cash_weight':max(0.,1.-invested),'unknown_weight':grouped.get('unknown',{}).get('weight',0.),
            'max_industry_weight':max(weights,default=0.),'industry_hhi':sum(w*w for w in weights),
            'benchmark_available':benchmark_available,'benchmark_snapshot_date':snapshot})
    result['industry_exposure']={'days':days,'industry_data':{k:history.manifest[k] for k in ['id','source','policy','point_in_time_certified']},
        'neutralization':(neutralization.as_dict() if neutralization else {'mode':'off'}),
        'benchmark_note':'历史沪深300月度成分权重近似，快照后下一交易日可用，未按每日价格漂移；仅在沪深300基准下对比。' if memberships is not None else '该结果无可用的历史基准行业权重。',
        'note':'持仓按估值日历史行业归类，含仍未能卖出的调出持仓；行业中性化不限制持仓权重。未知/冲突单列；HHI含未知组、不含现金。'}
    result['metadata']['neutralization']=neutralization.as_dict() if neutralization else {'mode':'off'}
    result['metadata']['industry_data']=result['industry_exposure']['industry_data']
    result['industry_exposure']['industry_data']['data_asof']=history.data_asof
    result['industry_exposure']['note']+=' HHI按总资产行业权重平方求和，未归一到股票仓位。'
