import {useState} from 'react';
import {useApiResource} from '../hooks/useApiResource';
import {ErrorPanel,LoadingPanel} from './StatePanels';
import {formatNumber,formatPercent} from '../utils/format';

interface IndustryRow {
  industry_code:string;industry_name:string;count:number;singleton?:boolean;
  weight?:number;benchmark_weight?:number|null;active_weight?:number|null;
  before_mean?:Record<string,number>;after_mean?:Record<string,number>;residual_mean?:Record<string,number>|null;
}
interface IndustryReport {
  items:IndustryRow[];available?:boolean;date?:string;note?:string;benchmark_note?:string;
  benchmark_available?:boolean;benchmark_snapshot_date?:string|null;
  cash_weight?:number;unknown_weight?:number;max_industry_weight?:number;industry_hhi?:number;
  coverage?:{universe:number;known:number;missing:number;conflict:number;singleton_stocks?:number};
  industry_data?:{id:string;policy:string;point_in_time_certified:boolean;data_asof?:string|null};
  neutralization?:{mode:string};
}

export function IndustryDiagnostics({kind,runId,date}:{kind:'factors'|'backtests';runId:string|null;date?:string|null}) {
  const [factor,setFactor]=useState('');
  const resource=useApiResource<IndustryReport>(runId ? async signal=>{
    const url=`/api/v1/${kind==='factors'?'factor-runs':'backtests'}/${encodeURIComponent(runId)}/industries${date?`?date=${date}`:''}`;
    const response=await fetch(url,{signal});
    if(!response.ok)throw new Error('无法读取该任务的行业诊断，请重试');
    return response.json();
  }:null,[kind,runId,date]);
  const data=resource.data,keys=Object.keys(data?.items[0]?.before_mean ?? {});
  const selected=keys.includes(factor)?factor:keys[0];
  const enabled=data?.neutralization?.mode==='industry';
  return <section className="card" data-testid="industry-diagnostics">
    <div className="card-header"><div><h2 className="card-title">{kind==='factors'?'行业处理前后诊断':'持仓行业暴露'}</h2>
      <p className="card-sub">{data?.date} · 行业中性化：{enabled?'开启':'关闭'}</p></div></div>
    {resource.loading?<LoadingPanel label="正在读取历史行业诊断…"/>:resource.error?<ErrorPanel message={resource.error} onRetry={resource.reload}/>:data ? <>
      {!data.industry_data ? <p className="status-note">{data.note ?? '此旧批次未保存历史行业数据，请复用配置重新运行。'}</p>:<>
        <p className="hint-text">{data.industry_data.policy}。版本 {data.industry_data.id.slice(0,12)}{data.industry_data.data_asof?`，来源核验截至${data.industry_data.data_asof}`:''}；公告时点未认证。</p>
        {data.coverage?<p className="status-note">当日股票池 {data.coverage.universe} 只；行业已知 {data.coverage.known}；缺失 {data.coverage.missing}；冲突 {data.coverage.conflict}；单股票行业 {data.coverage.singleton_stocks ?? '—'} 只。</p>:null}
        {kind==='factors'?<label>查看指标 <select aria-label="行业诊断指标" value={selected ?? ''} onChange={e=>setFactor(e.target.value)}>{keys.map(key=><option key={key} value={key}>{key}</option>)}</select></label>:<p className="status-note">最大行业权重 {formatPercent(data.max_industry_weight)} · 行业HHI {formatNumber(data.industry_hhi,4)} · 现金 {formatPercent(data.cash_weight)} · 未知行业持仓 {formatPercent(data.unknown_weight)}</p>}
        <div className="table-wrap"><table><thead><tr><th>历史行业</th><th>股票数</th>{kind==='factors'?<><th>处理前平均分</th><th>处理后平均分</th><th>标准化残差均值</th></>:<><th>持仓权重</th><th>基准近似权重</th><th>主动偏离</th></>}</tr></thead><tbody>
          {data.items.map(row=><tr key={row.industry_code}><td>{row.industry_name}<div className="cell-sub">{row.industry_code}</div></td><td>{row.count}{row.singleton?'（单股票）':''}</td>
            {kind==='factors'?<><td>{formatNumber(row.before_mean?.[selected],2)}</td><td>{formatNumber(row.after_mean?.[selected],2)}</td><td>{enabled?formatNumber(row.residual_mean?.[selected],6):'未处理'}</td></>:<><td>{formatPercent(row.weight)}</td><td>{data.benchmark_available?formatPercent(row.benchmark_weight):'无可用数据'}</td><td>{data.benchmark_available?formatPercent(row.active_weight):'—'}</td></>}
          </tr>)}
        </tbody></table></div>
        <p className="hint-text">{data.note} {data.benchmark_note}{data.benchmark_snapshot_date?` 快照：${data.benchmark_snapshot_date}`:''}</p>
      </>}
    </>:null}
  </section>;
}
