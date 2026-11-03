import {useEffect,useRef,useState} from 'react';

type Scope='csi300'|'all_market'|'custom';
type Spec={scope:Scope;start:string;end:string;warmup:number;codes:string[]};
type Plan={id:string;spec:Spec;start:string;end:string;data_start:string;codes_count:number;reused_rows:number;missing_rows:number;
  estimated_quote_requests:number;restricted:boolean;note:string;tasks:Array<{code:string;expected_rows:number;reused_rows:number;gaps:string[][]}>};
type Job={id:string;spec:Spec;status:string;completed:number;total:number;reused_rows:number;downloaded_rows:number;current_code?:string;
  message?:string;version_id?:string;failures:Array<{code:string;reason:string}>};
type Version={id:string;scope:Scope;start:string;end:string;data_start:string;codes_count:number;restricted:boolean;snapshot_id:string|null;created_at:string;validation_counts?:Record<string,number>};
type State={worker_running:boolean;registered_codes:number;jobs:Job[];versions:Version[]};
const labels:Record<string,string>={csi300:'沪深300历史成分',all_market:'全市场（沪深A股）',custom:'自选股票',complete:'已完成',
  running:'运行中',paused:'已暂停',partial:'部分完成，待复核',interrupted:'已中断，可续传'};
async function api<T>(action:string,body?:unknown):Promise<T>{
  const response=await fetch('/api/v1/research/market-download'+action,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data=await response.json();if(!response.ok)throw new Error(data.detail??`请求失败：${response.status}`);return data as T;
}
function lastCompleteDay(){const now=new Date();if(now.getHours()<18)now.setDate(now.getDate()-1);return `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')}`;}

export function SharedMarketPanel(){
  const [scope,setScope]=useState<Scope>('csi300'),[start,setStart]=useState('2025-01-01'),[end,setEnd]=useState(lastCompleteDay),[warmup,setWarmup]=useState('127'),[codes,setCodes]=useState('');
  const [plan,setPlan]=useState<Plan|null>(null),[state,setState]=useState<State|null>(null),[error,setError]=useState<string|null>(null),[busy,setBusy]=useState(false);
  const generation=useRef(0),alive=useRef(true);
  async function refresh(){try{const value=await api<State>('');if(alive.current)setState(value);}catch(e){if(alive.current)setError(String(e));}}
  useEffect(()=>{alive.current=true;void refresh();const timer=setInterval(()=>void refresh(),5000);return()=>{alive.current=false;clearInterval(timer);};},[]);
  function edit(change:()=>void){generation.current++;setPlan(null);setError(null);change();}
  async function preview(){
    const g=++generation.current;setBusy(true);setError(null);setPlan(null);
    const parsed=codes.trim()?codes.toUpperCase().split(/[\s,，;；]+/).filter(Boolean):[];
    try{const value=await api<Plan>('/preview',{scope,start,end,warmup:Number(warmup),codes:parsed});if(alive.current&&g===generation.current)setPlan(value);}
    catch(e){if(alive.current&&g===generation.current)setError(String(e));}finally{if(alive.current)setBusy(false);void refresh();}
  }
  async function action(command:'start'|'pause',id?:string){
    setBusy(true);setError(null);
    try{await api('/'+command,command==='start'?{plan_id:id}:{});setPlan(null);await refresh();}
    catch(e){setError(String(e));}finally{if(alive.current)setBusy(false);}
  }
  return <section className="card" data-testid="shared-market-panel">
    <div className="card-header"><div><h2 className="card-title">共享行情与增量更新</h2>
      <p className="card-sub">股票池分别管理，共用已核验行情；只补缺口，发布新版本，保留旧数据和回测。</p></div>
      <button className="button" onClick={()=>void refresh()}>刷新共享状态</button></div>
    <div className="card-body">
      <div className="research-grid">
        <div className="field"><label htmlFor="shared-scope">下载范围</label><select id="shared-scope" value={scope} onChange={e=>edit(()=>setScope(e.target.value as Scope))}>
          <option value="csi300">沪深300历史成分</option><option value="all_market">全市场（沪深A股，含历史退市股）</option><option value="custom">自选股票</option></select></div>
        <div className="field"><label htmlFor="shared-start">范围开始日期</label><input id="shared-start" type="date" value={start} onChange={e=>edit(()=>setStart(e.target.value))}/></div>
        <div className="field"><label htmlFor="shared-end">范围结束日期</label><input id="shared-end" type="date" value={end} onChange={e=>edit(()=>setEnd(e.target.value))}/></div>
        <div className="field"><label htmlFor="shared-warmup">此前预热交易日</label><input id="shared-warmup" type="number" min="0" max="504" value={warmup} onChange={e=>edit(()=>setWarmup(e.target.value))}/></div>
      </div>
      <div className="field"><label htmlFor="shared-codes">限定股票代码（小范围测试可填写）</label>
        <textarea id="shared-codes" value={codes} placeholder="600036.SH, 600519.SH；全市场／沪深300留空表示全部，自选必须填写" onChange={e=>edit(()=>setCodes(e.target.value))}/></div>
      <p className="hint-text">预览只查询日历、证券及成分资料；确认后才下载行情。月度成分快照次日使用，精确公告时点未认证。全市场目前为沪深A股，北交所及历史换码映射尚未适配。修改任一参数后须重新预览。</p>
      <button className="button" disabled={busy||state?.worker_running} onClick={()=>void preview()}>预览覆盖与增量方案</button>
      {error?<p className="error-text" role="alert">{error}</p>:null}
      {plan?<div className="status-note" data-testid="shared-plan">
        <p>{labels[plan.spec.scope]}{plan.restricted?'（限定子集）':''}：{plan.codes_count}只；范围 {plan.start} 至 {plan.end}，含预热从 {plan.data_start} 开始。</p>
        <p>复用 {plan.reused_rows} 条股票日数据；需补 {plan.missing_rows} 条；预计行情请求 {plan.estimated_quote_requests} 次。{plan.note}</p>
        <details><summary>查看各股缺口</summary>{plan.tasks.map(t=><p key={t.code}>{t.code}：已覆盖{t.reused_rows}/{t.expected_rows}；{t.gaps.length?t.gaps.map(g=>g.join(' 至 ')).join('、'):'无需下载'}</p>)}</details>
        <button className="button button-primary" disabled={busy||state?.worker_running} onClick={()=>void action('start',plan.id)}>确认方案并下载 / 更新</button>
      </div>:null}
      <p>共享目录已登记 {state?.registered_codes??0} 只股票。{state?.worker_running?'正在后台下载，状态每5秒刷新。':''}</p>
      {state?.worker_running?<button className="button" disabled={busy} onClick={()=>void action('pause')}>暂停共享下载</button>:null}
      {state?.jobs.map(job=><details key={job.id} data-testid="shared-job" data-plan-id={job.id}><summary>{labels[job.spec.scope]} {job.spec.start} 至 {job.spec.end}：{labels[job.status]??job.status}，{job.completed}/{job.total}</summary>
        <p>{job.message}；复用{job.reused_rows}条，新增{job.downloaded_rows}条。{job.status==='running'?`当前：${job.current_code??'准备中'}`:''}</p>
        {job.failures.map(f=><p className="error-text" key={f.code}>{f.code}：{f.reason}</p>)}
        {['paused','partial','interrupted'].includes(job.status)?<button className="button" disabled={busy||state.worker_running} onClick={()=>void action('start',job.id)}>续传此计划</button>:null}
      </details>)}
      <h3>已发布版本</h3>
      {state?.versions.map(version=><p key={version.id}>{labels[version.scope]}{version.restricted?'（限定子集）':''} · {version.codes_count}只 · {version.start} 至 {version.end} · <code>{version.id}</code>
        {version.validation_counts?<>；单源{version.validation_counts.single_source_passed??0}只／双源{version.validation_counts.dual_source_verified??0}只／补缺{version.validation_counts.secondary_fallback_passed??0}只</>:null}
        {version.snapshot_id?<>；在“新建研究 → 已有快照”选择 <code>{version.snapshot_id}</code> 可运行固定池研究。</>:<>；归档版本。完整沪深300版本可在“新建研究 → 历史成分股”选择；全市场动态回测仍待扩展。</>}</p>)}
      <p className="hint-text">最多50只且全区间覆盖与交易字段检查通过的版本会生成研究快照；上市／退市导致覆盖不齐时仅归档，不能冒充可直接固定池回测。沪深300限定子集同样是固定池研究，不能当成完整历史指数池。旧冻结仓库入口继续保留在下方。</p>
    </div>
  </section>;
}
