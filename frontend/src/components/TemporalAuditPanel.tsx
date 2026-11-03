import type {TemporalAuditData} from '../types';
import {formatPercent} from '../utils/format';

const labels:Record<string,string>={assumption:'保守假设',selection_bias:'固定池限制',unverified:'未认证',
  diagnostic_only:'仅作诊断',not_used:'未使用',guarded:'已设程序约束',model_assumption:'模拟边界',not_modeled:'未建模'};

export function TemporalAuditPanel({audit}:{audit?:TemporalAuditData|null}) {
  return <section className="card" data-testid="temporal-audit">
    <div className="card-header"><div><h2 className="card-title">数据时点与成交边界</h2>
      <p className="card-sub">{audit?'历史公开时点尚未完全认证；程序校验通过不能替代历史发布与可成交性认证。':'此旧结果未保存本轮核验清单，不能据此视为已完成历史时点或成交容量认证；原来源与限制说明仍适用。'}</p></div></div>
    {audit?<><div className="table-wrap"><table><thead><tr><th>检查项</th><th>状态</th><th>本次采用的口径与限制</th></tr></thead><tbody>
      {audit.items.map(row=><tr key={row.key}><td>{row.label}</td><td>{labels[row.status]??row.status}</td><td>{row.detail}</td></tr>)}
    </tbody></table></div>
      {audit.capacity_diagnostics?<p className="hint-text">成交容量未建模。可核对此前20日成交额的标的交易日：{audit.capacity_diagnostics.valid_stock_days}；最大比例 {formatPercent(audit.capacity_diagnostics.max_prior_turnover_ratio)}；超过10%的有{audit.capacity_diagnostics.above_ten_percent}个；缺少预热的有{audit.capacity_diagnostics.missing_lookback_stock_days}个。{audit.capacity_diagnostics.note}</p>:null}
      <p className="hint-text">{audit.note}</p></>:null}
  </section>;
}
