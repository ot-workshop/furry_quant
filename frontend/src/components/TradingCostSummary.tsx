import type {BacktestDetail} from '../types';
import {formatNumber} from '../utils/format';
export const COST_LABELS=[['commission','券商佣金'],['stamp_tax','卖出印花税'],['transfer_fee','过户费'],['impact','冲击成本'],['legacy_fee','旧版综合费用'],['total','合计']] as const;
export function TradingCostSummary({detail}:{detail:BacktestDetail}) {
  const {execution_costs:costs,cost_summary:summary,rebalance_policy:rebalance}=detail;
  if(!costs&&!rebalance)return null;
  return <section className="card" aria-label="调仓与交易费用">
    <div className="card-header"><h2 className="card-title">调仓与交易费用</h2></div>
    {rebalance ? <><p>调仓频率：{({daily:'每日',weekly:'每周首个交易日',monthly:'每月首个交易日'})[rebalance.frequency]}；计划调仓 {rebalance.scheduled_count} 次，每日估值。</p>
      <p className="hint-text">{rebalance.timing}；{rebalance.exit_policy}。调仓机会不保证发生交易，替换数量仍受配置限制。</p></> : null}
    {costs ? <><p>{costs.cost_mode==='itemized' ? `分项模式：佣金${100*costs.commission_rate}%，单笔最低${costs.min_cost}元；${costs.tax_mode==='historical' ? '印花税与过户费按成交日历史费率' : `卖出印花税${100*costs.stamp_tax_rate}%，双边过户费${100*costs.transfer_fee_rate}%`}` : '旧版模式：沿用综合费率与最低费用，不额外叠加分项税费'}。</p>
      <p className="hint-text">双边冲击成本为成交额的 {100*costs.impact_cost}%，作为额外费用计入净值；不是按当日最终成交量估计的开盘滑点或容量模型。</p></> : null}
    {summary ? <div className="table-wrap"><table><thead><tr><th>费用项目</th><th>累计金额（元）</th></tr></thead><tbody>
      {COST_LABELS.map(([key,label])=><tr key={key}><td>{label}</td><td>{formatNumber(summary[key],2)}</td></tr>)}
    </tbody></table></div> : null}
  </section>;
}
