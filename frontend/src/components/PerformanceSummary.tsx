import type { BacktestDetail } from "../types";
import { formatMetricValue, formatPercent, metricLabel } from "../utils/format";
import { AnnualReturnsChart } from "./AnnualReturnsChart";

const ROWS = ["total_return", "annualized_return", "volatility", "sharpe_ratio", "max_drawdown", "calmar_ratio", "win_rate", "profit_loss_ratio"];
const EXTRAS = ["avg_turnover", "total_cost_amount", "total_cost", "trade_count", "total_days", "years", "excess_return", "relative_return", "tracking_error", "information_ratio", "annualized_active_return"];

export function PerformanceSummary({ detail }: { detail: BacktestDetail }) {
  const performance = detail.performance;
  const metrics = detail.metrics ?? {};
  const extras = [...EXTRAS.filter(key => key in metrics), ...Object.keys(metrics).filter(key => !ROWS.includes(key) && !EXTRAS.includes(key) && key !== "benchmark_return")];
  const method = performance?.methodology;
  return <div className="performance-summary">
    <div className="card-header compact-header">
      <div><h3 className="subheading">回测绩效汇总</h3><p className="card-sub">年度收益与绩效指标，基于扣除成交费用后的账户净值。</p></div>
    </div>
    <p className="chart-note" data-testid="benchmark-description">评价基准：{detail.benchmark_name ?? String(detail.config?.benchmark ?? '原结果保存的基准')}。
      {detail.benchmark ? <> 来源：官方Tushare；覆盖{detail.benchmark.start}至{detail.benchmark.end}（含期初锚点），{detail.benchmark.observations}条。{detail.benchmark.return_type==='gross_total_return' ? '计入分红再投资，不扣基准交易费用。' : '价格指数，不包含分红再投资。'}</> : '历史结果未保存独立基准验证信息，按原始净值计算。'}
    </p>
    {performance && !performance.available ? <p className="status-note">有效净值记录不足或数据不完整，无法计算的指标显示为“—”。</p> : null}
    <div className="performance-layout">
      <div className="performance-annual">
        <h4 className="subheading">年度收益</h4>
        {performance?.annual_returns.length ? <>
          <AnnualReturnsChart items={performance.annual_returns} />
          <div className="annual-coverage">{performance.annual_returns.map(row => <span key={row.year}>{row.year}：{row.start_date} 至 {row.end_date} · {row.trading_days}个交易日</span>)}</div>
        </> : <p className="muted-text performance-empty">暂无可计算的年度收益。</p>}
        <p className="chart-note">各年按实际覆盖区间复利计算；首尾年度可能不完整，此处未作年化。</p>
      </div>
      <div className="table-wrap performance-table">
        <table>
          <caption className="sr-only">策略与基准绩效指标</caption>
          <thead><tr><th scope="col">指标</th><th scope="col" className="numeric">策略</th><th scope="col" className="numeric">基准</th></tr></thead>
          <tbody>
            {ROWS.map(key => <tr key={key} data-metric={key}>
              <th scope="row">{metricLabel(key)}</th>
              <td className="numeric">{formatMetricValue(key, metrics[key])}</td>
              <td className="numeric">{formatMetricValue(key, performance?.benchmark_metrics[key])}</td>
            </tr>)}
            <tr data-metric="max_drawdown_date"><th scope="row">最大回撤日</th>
              <td className="numeric">{performance?.drawdown.trough_date ?? "—"}</td>
              <td className="numeric">{performance?.benchmark_drawdown.trough_date ?? "—"}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
    {extras.length ? <div className="metric-grid performance-extras">{extras.map(key => <div className="metric-card" key={key}>
      <div className="metric-label">{metricLabel(key)}</div><div className={`metric-value ${metrics[key] == null ? "is-null" : ""}`}>{formatMetricValue(key, metrics[key])}</div>
    </div>)}</div> : null}
    <p className="chart-note">{method ? <>按每年{method.trading_days_per_year}个交易日年化；无风险年利率假设{formatPercent(method.annual_risk_free_rate)}（沿用旧版），波动率采用样本标准差。</> : null}
      夏普使用日收益算术均值年化，年化收益采用复利；初始净值仅作起点，不计为收益日。</p>
    <p className="chart-note">胜率为盈利日占非零收益日比例，盈亏比为平均正日收益 / 平均负日收益绝对值，均非逐笔交易胜率。零波动、无回撤或样本不足时，相应比率显示“—”；卡玛采用（年化收益 − 无风险利率）/ 最大回撤绝对值。</p>
    <p className="chart-note">累计收益差为策略总收益减基准总收益；相对净值收益为两者期末净值之比减1。跟踪误差采用日收益差的样本标准差年化；信息比率为日收益差均值年化 / 跟踪误差，均使用相同账户估值日。</p>
  </div>;
}
