import type { FactorScore, FactorScoresResponse } from '../types';
import { formatNumber, formatPercent } from '../utils/format';

const REASONS: Record<string,string> = {
  insufficient_history:'历史预热不足', missing_or_nonpositive_pe_ttm:'PE缺失或非正',
  missing_or_nonpositive_pb:'PB缺失或非正', missing_or_nonpositive_total_mv:'市值缺失或非正',
  insufficient_negative_returns_or_history:'窗口未预热或负收益不足2次',
  insufficient_history_or_trading_activity:'窗口未预热、成交额异常或有效成交日不足80%', suspended_auxiliary_value:'停牌沿用的辅助值不参与',
};

export function FactorDetails({response, score, instrument}: {response: FactorScoresResponse; score: FactorScore | null; instrument: string}) {
  const metadata = response.metadata;
  const definitions = metadata?.factor_definitions ?? {};
  const diagnostic = response.diagnostics?.find(row => row.instrument === instrument);
  const coverage = metadata?.coverage;
  const neutralized=Boolean(score?.processed);
  return <section className="card" data-testid="factor-calculation-details">
    <div className="card-header"><div><h2 className="card-title">计算明细与数据覆盖</h2>
      <p className="card-sub">{coverage ? `当日股票池${coverage.universe}只，全部入选因子均有效${coverage.eligible}只。` : ''}分项贡献 = 百分位得分 × 权重；缺失项不会补零或自动改权重。</p>
    </div></div>
    {score?.industry_status ? <p className="hint-text">历史行业：{score.industry_name ?? '未知/冲突'}（{score.classification}，{score.industry_code ?? '无代码'}）；{neutralized?`处理前综合分 ${formatNumber(score.before_composite,2)}，排名 ${score.before_rank}；处理后综合分 ${formatNumber(score.composite,2)}，排名 ${score.rank}。`:'中性化关闭。'}</p>:null}
    {diagnostic?.reasons._industry ? <p className="status-note">历史行业缺失或冲突，未参与中性化评分；不会使用当前行业回填。</p>:null}
    {diagnostic?.reasons._cross_section ? <p className="status-note">该股因子值有效，但当日共同有效股票不足2只，未生成百分位与综合分。</p> : null}
    <div className="table-wrap"><table>
      <thead><tr><th>指标 / 实例</th><th>参数</th><th>方向</th><th className="numeric">原始值</th><th className="numeric">得分</th>{neutralized?<><th>处理前得分</th><th>标准化行业残差</th><th>处理前贡献</th></>:null}<th className="numeric">权重</th><th className="numeric">综合分贡献</th><th>有效覆盖 / 状态</th></tr></thead>
      <tbody>{Object.entries(definitions).map(([key, definition]) => {
        const raw = score?.raw?.[key] ?? diagnostic?.raw?.[key];
        const reason = diagnostic?.reasons[key];
        const count = coverage?.factors[key];
        const weight = metadata?.weights?.[key];
        return <tr key={key} data-factor-id={key}>
          <td><strong>{definition.label}</strong><div className="cell-sub">{key}</div><details><summary>公式与来源字段</summary><p>{definition.formula}</p><p>{definition.required_fields.join('、')}</p><p>{definition.data_note}</p></details></td>
          <td>{Object.entries(definition.params).map(([name,value]) => `${({window:'窗口',skip:'剔除',baseline_window:'基准'} as Record<string,string>)[name] ?? name}${value}日`).join(' · ') || '无窗口'}<div className="cell-sub">至少回看{definition.lookback}日</div></td>
          <td>{definition.direction === 'lower' ? '越低越好' : '越高越好'}</td>
          <td className="numeric">{formatNumber(raw,6)}</td><td className="numeric">{formatNumber(score?.scores?.[key],2)}</td>
          {neutralized?<><td>{formatNumber(score?.before_scores?.[key],2)}</td><td>{formatNumber(score?.processed?.[key],6)}</td><td>{formatNumber(score?.before_contributions?.[key],2)}</td></>:null}
          <td className="numeric">{formatPercent(weight)}</td><td className="numeric">{formatNumber(score?.contributions?.[key],2)}</td>
          <td>{count ? `${count.valid}/${coverage?.universe}只 · ` : ''}{reason ? REASONS[reason] ?? reason : score ? '可用' : '无综合评分'}</td>
        </tr>;
      })}</tbody>
    </table></div>
    {score ? <p className="hint-text">综合分：{formatNumber(score.composite,2)}；行情/估值可用时间按当日18:00假设。{score.is_suspended ? '该股当日停牌，评分不代表可成交。' : ''}{score.valuation_asof && score.valuation_asof !== score.date ? `PE沿用日期：${score.valuation_asof}。` : ''}</p> : null}
  </section>;
}
