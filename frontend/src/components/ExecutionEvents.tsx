import {useState} from 'react';
import {api} from '../api/client';
import {useApiResource} from '../hooks/useApiResource';
import {Pagination} from './Pagination';
const labels:Record<string,string>={index_exit:'成分调出',outside_listing_lifecycle:'生命周期结束',score_rotation:'评分轮换',
  rank_selection:'排名选入',suspended:'全天停牌',limit_down:'开盘跌停',limit_up:'开盘涨停',t_plus_one:'T+1可卖数量限制',
  missing_quote_or_before_listing:'缺少报价或尚未上市',unknown_price_limits:'涨跌停规则不明',insufficient_cash_for_board_minimum:'资金不足以达到板块最小申报数量'};
labels.candidate_shortfall='有效候选不足';labels.insufficient_valid_scores='不凑满持仓、不沿用旧评分；已有合规持仓按退出规则处理';
export function ExecutionEvents({runId,summary}:{runId:string;summary?:{blocked_exits:number;pending_exits:Array<{instrument:string;reason:string}>}}) {
  const [offset,setOffset]=useState(0);
  const events=useApiResource(signal=>api.getExecutionEvents(runId,offset,signal),[runId,offset]);
  return <section className="card" aria-label="动态池交易处理记录">
    <div className="card-header"><h2 className="card-title">动态池交易处理记录</h2></div>
    <p>调出持仓持续尝试退出；受阻时继续保留持仓与估值。已提交不等于成交，实际结果以成交明细为准。</p>
    {summary ? <p>退出受阻 {summary.blocked_exits} 次；期末仍待退出 {summary.pending_exits.length} 只。
      {summary.pending_exits.map(p=>`${p.instrument}（${labels[p.reason]??p.reason}）`).join('、')}</p> : null}
    {summary?.pending_exits.length ? <p className="warning-callout">期末仍有调出或生命周期结束的持仓未退出，继续计入账户；请结合最后可得价估值等限制解读收益，不能视作已兑现现金。</p> : null}
    {events.error ? <p role="alert">{events.error}</p> : events.loading ? <p>正在读取…</p> : !events.data?.items.length ? <p>本次没有交易处理记录。</p> : <>
      <div className="table-wrap"><table><thead><tr><th>日期</th><th>股票</th><th>操作</th><th>原因</th><th>状态</th><th>受阻原因</th></tr></thead>
        <tbody>{events.data.items.map((e,i)=><tr key={`${e.date}-${e.instrument}-${i}`}><td>{e.date}</td><td>{e.instrument}</td>
          <td>{e.action==='sell' ? '卖出' : '买入'}</td><td>{labels[e.reason]??e.reason}</td>
          <td>{e.status==='blocked' ? '受阻，保留持仓/现金' : '已提交'}</td><td>{e.block_reason ? labels[e.block_reason]??e.block_reason : '—'}</td></tr>)}</tbody></table></div>
      <Pagination total={events.data.total} offset={offset} limit={50} onChange={setOffset}/>
    </>}
  </section>;
}
