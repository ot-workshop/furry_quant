import {useEffect,useRef,useState} from 'react';
import {describeResearchError,researchApi,type IndexHandlingReport} from '../api/research';

const kinds:Record<string,string>={handled:'已自动处理',warning:'已说明限制',info:'信息',unresolved:'待复核'};
export function IndexHandlingReportPanel({sourceUpdatedAt}:{sourceUpdatedAt?:string}) {
  const [report,setReport]=useState<IndexHandlingReport|null>(null);
  const [error,setError]=useState<string|null>(null),[loading,setLoading]=useState(false);
  const [query,setQuery]=useState(''),[filter,setFilter]=useState('handled'),[limit,setLimit]=useState(20);
  const controller=useRef<AbortController|null>(null);
  useEffect(()=>()=>controller.current?.abort(),[]);
  async function refresh() {
    controller.current?.abort();const active=new AbortController();controller.current=active;
    setLoading(true);setError(null);
    try{const next=await researchApi.indexHandlingReport(active.signal);if(!active.signal.aborted){setReport(next);setLimit(20);}}
    catch(e){if(!active.signal.aborted)setError(describeResearchError(e));}
    finally{if(!active.signal.aborted)setLoading(false);}
  }
  const selected=report?.stocks.filter(s=>(!query || `${s.code} ${s.name}`.toLowerCase().includes(query.toLowerCase())) &&
    (filter==='all' || filter==='review' ? filter==='all' || s.outcome==='needs_review' : s.outcome!=='passed')) ?? [];
  return <div className="index-handling-report">
    <button type="button" className="button" disabled={loading} onClick={()=>void refresh()}>
      {loading ? '正在读取处理报告…' : report ? '刷新自动处理报告' : '查看自动处理报告'}</button>
    {error ? <p role="alert" className="error-text">{error}</p> : null}
    {report ? <section aria-label="自动处理报告">
      <h3>自动处理报告</h3>
      <p>已通过 {report.summary.passed} / {report.summary.total} 只；自动处理或说明限制 {report.summary.auto_handled} 只；
        待复核 {report.summary.needs_review} 只；待处理 {report.summary.pending} 只。</p>
      <p className="hint-text">报告对应进度：{report.source_updated_at ?? '尚未开始'}。仅解释已有记录，读取报告不会下载行情。</p>
      {sourceUpdatedAt && report.source_updated_at && sourceUpdatedAt!==report.source_updated_at ?
        <p role="status">下载进度已变化，请刷新自动处理报告查看最新记录。</p> : null}
      {report.job_handling ? <p>{report.job_handling}</p> : null}
      <p><a href="/api/v1/research/index-download/report/download" download>下载完整 JSON 报告</a></p>
      <details><summary>查看自动处理规则与边界</summary><ul>{report.rules.map(rule=><li key={rule}>{rule}</li>)}</ul>
        <p>{report.scope}</p></details>
      <div className="card-actions">
        <label>筛选股票 <input aria-label="筛选报告股票" value={query} placeholder="股票代码或名称"
          onChange={e=>{setQuery(e.target.value.trim());setLimit(20);}} /></label>
        <label>显示范围 <select aria-label="报告显示范围" value={filter} onChange={e=>{setFilter(e.target.value);setLimit(20);}}>
          <option value="handled">自动处理与待复核</option><option value="review">仅待复核</option><option value="all">全部股票</option>
        </select></label>
      </div>
      <p>符合条件 {selected.length} 只，当前显示 {Math.min(selected.length,limit)} 只。</p>
      {selected.slice(0,limit).map(stock=><details key={stock.code} className="card" style={{padding:12,marginTop:8}}>
        <summary>{stock.code} {stock.name} — {stock.result}</summary>
        {stock.actions.length ? stock.actions.map((action,index)=><div key={`${action.rule}-${index}`} style={{marginTop:12}}>
          <strong>{action.title} · {kinds[action.kind]}</strong>
          <p>发现：{action.reason}</p><p>处理：{action.handling}</p>
          {action.dates.length ? <details><summary>涉及 {action.dates.length} 个日期</summary><p style={{overflowWrap:'anywhere'}}>{action.dates.join('、')}</p></details> : null}
          {action.evidence.length ? <details><summary>查看依据</summary><ul>{action.evidence.map((evidence,i)=><li key={i}>
            {evidence.source}{evidence.api ? ` / ${evidence.api}` : ''}
            {evidence.file ? <p style={{overflowWrap:'anywhere'}}>本地证据：{evidence.file}</p> : null}
            {evidence.url && /^https?:\/\//.test(evidence.url) ? <p><a href={evidence.url} target="_blank" rel="noreferrer">公开依据</a>
              {evidence.known_by ? `；已知日期：${evidence.known_by}` : ''}</p> : null}
            {evidence.sha256 ? <p style={{overflowWrap:'anywhere'}}>文件校验和：{typeof evidence.sha256==='string' ? evidence.sha256 : JSON.stringify(evidence.sha256)}</p> : null}
          </li>)}</ul></details> : null}
        </div>) : <p>未发现需要自动修复的问题，已按上方来源等级完成检查。</p>}
      </details>)}
      {!selected.length ? <p>没有符合条件的记录。</p> : null}
      {selected.length>limit ? <button type="button" className="button" onClick={()=>setLimit(n=>n+20)}>再显示20只</button> : null}
    </section> : null}
  </div>;
}
