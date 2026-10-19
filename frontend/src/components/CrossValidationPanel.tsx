import type { CrossValidationReport } from "../types";
import { formatPercent } from "../utils/format";

const statusName = (status: string) => ({ verified: "核验通过", fallback: "已回退", conflict: "存在冲突" }[status] ?? status);
const sourceName = (source: string) => source === "baostock" ? "BaoStock" : source === "tushare" ? "Tushare" : source;
const issueName = (issue: string) => ({
  adjustment_discontinuity: "复权收益异常",
  missing_or_duplicate_sessions: "日期缺失或重复",
  missing_or_invalid_prices_or_adjustment: "价格或复权缺失/异常",
  invalid_ohlc_geometry: "开高低收不一致",
  raw_price_conflict: "两源原始价格不一致",
  plausible_adjusted_return_conflict: "复权收益冲突，需人工核实",
  primary_failed_verify_only: "主源异常，当前仅核验、不回退",
  secondary_unavailable_invalid_or_conflicting: "第二来源不可用、无效或存在冲突",
}[issue] ?? issue);

export function CrossValidationPanel({ report }: { report?: CrossValidationReport }) {
  if (!report) return null;
  return (
    <section className="page-stack">
      <div className="card-header compact-header">
        <h3 className="subheading">跨源核验</h3>
        <span className="badge">{statusName(report.status)} · 回退 {report.fallback_codes.length} 只 · 冲突 {report.conflict_codes.length} 只</span>
      </div>
      <p className="hint-text">对比未复权开高低收与复权后的收益。回退会切换整只股票的区间行情与复权序列；估值、涨跌停价格仍来自 Tushare。</p>
      <div className="table-wrap">
        <table>
          <thead><tr><th>股票</th><th>核验结果</th><th>价格来源</th><th>复权来源</th><th>主源检查</th></tr></thead>
          <tbody>{report.stocks.map(stock => (
            <tr key={stock.code}>
              <td>{stock.code}</td><td>{statusName(stock.status)}</td>
              <td>{sourceName(stock.price_source)}</td><td>{sourceName(stock.adjustment_source)}</td>
              <td>{stock.primary_issues.map(issueName).join("、") || (stock.reason ? issueName(stock.reason) : "未发现异常")}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
      <details>
        <summary>查看差异日期与核验范围</summary>
        <p className="hint-text">原始价格容差 {report.price_absolute_tolerance_cny} 元，收益差异阈值 {formatPercent(report.return_absolute_tolerance)}；不比较两家的因子绝对值。除权日的算法差异单独列出，不直接拼接因子。</p>
        {report.stocks.map(stock => (
          <div key={stock.code}>
            <p>{stock.code}：原始价格差异 {stock.raw_price_differences.length} 天；复权收益差异 {stock.return_differences.length} 天；除权日口径差异 {stock.corporate_action_differences.length} 天。</p>
            {stock.reason ? <p className="hint-text">{issueName(stock.reason)}</p> : null}
            {stock.raw_price_differences.length ? <p>{stock.raw_price_differences.join("、")}</p> : null}
            {stock.return_differences.length ? (
              <div className="table-wrap"><table>
                <thead><tr><th>日期</th><th>主源复权收益</th><th>BaoStock 复权收益</th></tr></thead>
                <tbody>{stock.return_differences.map(row => <tr key={row.date}><td>{row.date}</td><td>{formatPercent(row.primary_return)}</td><td>{formatPercent(row.secondary_return)}</td></tr>)}</tbody>
              </table></div>
            ) : null}
          </div>
        ))}
        {report.notes.map(note => <p className="hint-text" key={note}>{note}</p>)}
      </details>
    </section>
  );
}
