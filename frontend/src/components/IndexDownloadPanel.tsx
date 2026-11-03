import {useEffect,useState,useRef} from 'react';
import {researchApi,describeResearchError,type IndexDownloadStatus} from '../api/research';
import {IndexHandlingReportPanel} from './IndexHandlingReportPanel';

const labels:Record<string,string>={not_started:'尚未开始',running:'下载中',partial:'部分通过，存在待复核数据',
  complete:'全部检查完成',paused:'已暂停',interrupted:'已中断，可续传'};
export function IndexDownloadPanel() {
  const [state,setState]=useState<IndexDownloadStatus|null>(null);
  const [error,setError]=useState<string|null>(null),[busy,setBusy]=useState(false);
  const generation=useRef(0);
  useEffect(()=>{
    let alive=true;const controller=new AbortController();
    async function refresh(){const g=generation.current;try{
      const value=await researchApi.indexDownloadStatus(controller.signal);
      if(alive && generation.current===g){setState(value);setError(null);}
    }catch(e){if(alive && generation.current===g && !controller.signal.aborted)setError(describeResearchError(e));}}
    void refresh();const timer=setInterval(()=>void refresh(),5000);
    return()=>{alive=false;controller.abort();clearInterval(timer);};
  },[]);
  async function action(command:'start'|'pause') {
    setBusy(true);generation.current++;
    try{setState(await researchApi.indexDownloadAction(command));setError(null);}
    catch(e){setError(describeResearchError(e));}finally{generation.current++;setBusy(false);}
  }
  const running=state?.worker_running;
  const bao=state?.budget.requests.filter(r=>r.source==='baostock').reduce((sum,r)=>sum+r.count,0) ?? 0;
  return <section className="card" id="index-download-panel">
    <div className="card-header"><div><h2 className="card-title">沪深300历史数据下载</h2>
      <p className="card-sub">保留历史成分变化与预热数据，下载断点和来源持久保存。状态每5秒更新。</p></div>
      <div className="card-actions">
        <button type="button" className="button button-primary" disabled={busy || running || state?.status==='complete'}
          onClick={()=>void action('start')}>开始 / 续传</button>
        <button type="button" className="button" disabled={busy || !running || state?.pause_requested}
          onClick={()=>void action('pause')}>暂停下载</button>
      </div></div>
    {error ? <p className="error-text" role="alert">{error}</p> : null}
    {state ? <div className="card-body">
      <p role="status">{running ? (state.pause_requested ? '已请求暂停，将在当前股票处理后停止' : '后台下载与核验中') : labels[state.status] ?? state.status}</p>
      <p>主源四类原始响应已保存 {state.raw_captures ?? 0} / {state.total ?? '待规划'} 只；已通过检查 {state.verified} 只；待复核 {state.failed} 只。
        {state.current_code && running ? ` 当前处理：${state.current_code}` : ''}</p>
      {state.validation_counts ? <p>单源检查通过 {state.validation_counts.single_source_passed} 只；双源核验通过 {state.validation_counts.dual_source_verified} 只；BaoStock补缺通过 {state.validation_counts.secondary_fallback_passed} 只。</p> : <p>旧版本核验结果，续传后按新规则重新检查。</p>}
      <p className="hint-text">官方Tushare为主源。按股票代码固定抽样约10%进行双源核验，异常时额外核验或补缺。单源通过仅表示内部检查通过；抽样核验不可用不会伪装为双源通过，已发现的两源冲突仍须复核。</p>
      {state.plan ? <p>研究区间：{state.plan.start} 至 {state.plan.end}；数据起点：{state.plan.data_start}；
        预热要求：{state.plan.warmup_sessions} 个交易日；历史快照：{state.plan.snapshots} 期。</p> : null}
      <p>BaoStock今日请求 {bao} / {state.limits.baostock_daily_requests} 次，全局串行、每分钟至多{state.limits.baostock_per_minute}次；登录、分页和重试均计入。缓存命中不联网。</p>
      {state.message ? <p>{state.message}</p> : null}
      {state.budget.holds.length ? <p role="alert">辅助数据源限制访问，抽样核验暂不可用；依赖该来源的异常补缺需等待解除限制。</p> : null}
      <p className="hint-text">成分快照从其日期后的下一交易日起使用，月末名单不会回填当月。该口径可能滞后实际指数调仓，不能等同精确公告生效日。待复核数据保留原始响应，不进入已核验数据集。</p>
      <p className="hint-text">历史仓库独立保存；可在“新建研究”选择“沪深300历史成分股”，按每日名单计算与回测，支持科创板日线数量规则。月度成分口径和执行假设会随结果保存。</p>
      {state.failures.length ? <details><summary>查看待复核股票（前30只）</summary>
        <ul>{state.failures.map(f=><li key={f.code}>{f.code}：{f.reason==='BaoStock adjusted returns disagree with its reported changes' ? 'BaoStock复权序列与其涨跌幅不一致，待核验' : f.reason}
          {f.dates?.length ? `；日期：${f.dates.join('、')}` : ''}</li>)}</ul></details> : null}
      <IndexHandlingReportPanel sourceUpdatedAt={state.updated_at}/>
    </div> : <p>正在读取下载状态…</p>}
  </section>;
}
