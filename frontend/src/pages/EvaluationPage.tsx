import { useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "../api/client";
import {TemporalAuditPanel} from '../components/TemporalAuditPanel';
import { EmptyPanel, ErrorPanel, LoadingPanel } from "../components/StatePanels";
import { useApiResource } from "../hooks/useApiResource";
import type {
  EvaluationMetadata,
  EvaluationResult,
  FactorDiagnostic,
  ICStats,
  LabelDiagnostic,
} from "../types";
import {
  factorLabel,
  formatDateTime,
  formatNumber,
  formatPercent,
  formatWeight,
} from "../utils/format";

const DASH = "—";
const NO_EVALUATIONS: EvaluationMetadata[] = [];

const LABEL_NAMES: Record<string, string> = {
  momentum: "动量",
  value: "盈利收益率",
  volatility: "低波动",
  composite: "综合分",
};

function labelName(key: string): string {
  return LABEL_NAMES[key] ?? factorLabel(key);
}

function exclusionText(exclusions: Record<string, number>): string {
  const reasons: Record<string, string> = {
    outside_evaluation_end: "后续交易日不足或离场超出评估区间",
    missing_entry_or_exit: "缺少入场或离场价格",
    non_trading_entry_or_exit: "入场或离场日停牌",
  };
  const entries = Object.entries(exclusions).filter(([, count]) => count > 0);
  if (!entries.length) return DASH;
  return entries.map(([reason, count]) => `${reasons[reason] ?? reason} ${formatNumber(count, 0)}`).join(" · ");
}

export function EvaluationPage({ selectedEvaluationId = null }: { selectedEvaluationId?: string | null }) {
  const [selectedRunId, setSelectedRunId] = useState<string | null>(selectedEvaluationId);
  const [selectedHorizon, setSelectedHorizon] = useState<number | null>(null);

  const list = useApiResource((signal) => api.getEvaluations(signal), []);
  const evaluations = list.data?.items ?? NO_EVALUATIONS;

  const selectedMetadata =
    evaluations.find((item) => item.id === selectedRunId) ?? null;

  const horizons = useMemo(
    () => selectedMetadata?.evaluation.horizons ?? [],
    [selectedMetadata],
  );

  useEffect(() => {
    if (!selectedRunId && !selectedEvaluationId && evaluations.length) setSelectedRunId(evaluations[0].id);
  }, [evaluations, selectedRunId, selectedEvaluationId]);

  useEffect(() => { if (selectedEvaluationId) setSelectedRunId(selectedEvaluationId); }, [selectedEvaluationId]);

  useEffect(() => {
    setSelectedHorizon(horizons[0] ?? null);
  }, [horizons]);

  const detail = useApiResource(
    selectedRunId
      ? (signal) => api.getEvaluation(selectedRunId, signal)
      : null,
    [selectedRunId],
  );

  const result = detail.data;
  const horizonKey = selectedHorizon === null ? null : String(selectedHorizon);
  const factorEntries = useMemo(
    () => Object.entries(result?.factors ?? {}),
    [result],
  );

  return (
    <div className="page-stack">
      <section className="card">
        <div className="card-header">
          <div>
            <h2 className="card-title">评估结果</h2>
            <p className="card-sub">
              只读查看已保存的因子评估，包括 IC、分组收益、相关性与覆盖情况。
            </p>
          </div>
          <div className="card-actions">
            {result ? (
              <>
                <a
                  className="button button-compact"
                  href={api.evaluationDownloadUrl(result.metadata.id)}
                  download
                >
                  下载 JSON
                </a>
                <a
                  className="button button-compact"
                  href={api.evaluationReportUrl(result.metadata.id)}
                  download
                >
                  下载 Markdown
                </a>
              </>
            ) : null}
          </div>
        </div>

        {list.loading ? (
          <LoadingPanel label="正在读取评估列表…" />
        ) : list.error ? (
          <ErrorPanel message={list.error} onRetry={list.reload} />
        ) : !evaluations.length ? (
          <EmptyPanel title="暂无评估结果">
            完成一次评估并保存结果后，记录会显示在这里。
          </EmptyPanel>
        ) : (
          <>
            <div className="card-actions">
              <div className="field field-inline">
                <label htmlFor="evaluation-select">评估运行</label>
                <select
                  id="evaluation-select"
                  value={selectedRunId ?? ""}
                  onChange={(event) => setSelectedRunId(event.target.value)}
                >
                  {evaluations.map((item) => (
                    <option key={item.id} value={item.id}>
                      {formatDateTime(item.created_at)} · {item.name}
                    </option>
                  ))}
                </select>
              </div>
              <div className="field field-inline">
                <label htmlFor="horizon-select">持有期</label>
                <select
                  id="horizon-select"
                  value={selectedHorizon ?? ""}
                  onChange={(event) =>
                    setSelectedHorizon(
                      event.target.value ? Number(event.target.value) : null,
                    )
                  }
                  disabled={!horizons.length}
                >
                  {horizons.map((horizon) => (
                    <option key={horizon} value={horizon}>
                      {horizon} 个交易日
                    </option>
                  ))}
                </select>
              </div>
            </div>

            {detail.loading ? (
              <LoadingPanel label="正在读取评估结果…" />
            ) : detail.error ? (
              <ErrorPanel message={detail.error} onRetry={detail.reload} />
            ) : !result ? (
              <EmptyPanel title="请选择评估运行" />
            ) : (
              <EvaluationDetail
                result={result}
                horizon={selectedHorizon}
                factorEntries={factorEntries}
                horizonKey={horizonKey}
              />
            )}
          </>
        )}
      </section>

      {result ? <TemporalAuditPanel audit={result.metadata.temporal_audit}/> : null}
      {result ? <CoverageSections result={result} /> : null}
    </div>
  );
}

function EvaluationDetail({
  result,
  horizon,
  factorEntries,
  horizonKey,
}: {
  result: EvaluationResult;
  horizon: number | null;
  factorEntries: Array<[string, { horizons: Record<string, FactorDiagnostic> }]>;
  horizonKey: string | null;
}) {
  const { metadata, methodology } = result;
  const directionEntries = Object.entries(methodology.directions);

  return (
    <>
      <div className="section-divider" />

      <div className="card-header compact-header">
        <div>
          <h3 className="subheading">{metadata.name}</h3>
          <p className="card-sub">
            {metadata.start} 至 {metadata.end} · 样本{" "}
            {formatNumber(metadata.sample_size, 0)} 只
          </p>
        </div>
        <span className="badge">
          {metadata.is_synthetic ? "合成样本" : "真实数据"}
        </span>
      </div>

      <div className="meta-strip">
        <MetaItem label="评估 ID" value={metadata.id} mono />
        <MetaItem label="创建时间" value={formatDateTime(metadata.created_at)} />
        <MetaItem label="因子批次" value={metadata.factor_run_id} mono />
        <MetaItem label="数据版本" value={metadata.data_version} mono />
        <MetaItem
          label="分组数"
          value={formatNumber(metadata.evaluation.groups, 0)}
        />
        <MetaItem
          label="最小横截面"
          value={formatNumber(metadata.evaluation.min_cross_section, 0)}
        />
      </div>

      <div className="badge-row">
        {metadata.factor_config.map((item) => (
            <span className="badge" key={item.id ?? item.name}>
              {labelName(item.name)}{item.id ? ` [${item.id}]` : ''} · 权重 {formatWeight(item.weight)}
            {item.window !== null ? ` · ${item.window} 日窗口` : ""}
              {item.skip != null ? ` · 剔除${item.skip}日` : ''}{item.baseline_window != null ? ` · 基准${item.baseline_window}日` : ''}
          </span>
        ))}
      </div>

      <div className="section-divider" />

      <h3 className="subheading">方法说明</h3>
      <p className="hint-text">
        IC 衡量因子与后续收益的相关性，Rank IC 比较两者的排名。各日期等权，IR 为均值除以样本标准差，不作年化；无法计算时显示为 —。
      </p>
      <details>
      <summary>查看计算定义与来源</summary>
      <div className="meta-strip">
        <MetaItem label="IC 引擎" value={methodology.ic_engine} />
        <MetaItem label="标签" value={methodology.label} />
        <MetaItem label="信号区间" value={methodology.signal_range} />
        <MetaItem
          label="统计口径"
          value={methodology.statistics}
        />
      </div>
      {directionEntries.length ? (
        <p className="hint-text">
          方向对齐：
          {directionEntries
            .map(([key, value]) => `${factorLabel(key,metadata.factor_definitions)}（${value === "lower" ? "原始值越低越好" : "原始值越高越好"}）`)
            .join(" · ")}
        </p>
      ) : null}
      </details>

      <p className="warning-callout">
        评分在 T 日收盘后生成，下一交易日开盘入场，持有 h 个交易日后开盘离场；收益标签仅用于事后评估。
      </p>
      <p className="warning-callout">
        本页不展示分组净值或年化 IR。评估基于规模很小的固定股票池，无法据此验证策略有效性，也不构成收益或可交易性判断。
      </p>

      {methodology.limitations.length ? (
        <div className="hint-text">
          {methodology.limitations.map((item, index) => (
            <div key={`${item}-${index}`}>· {item}</div>
          ))}
        </div>
      ) : null}

      <div className="section-divider" />

      <h3 className="subheading">因子诊断</h3>
      <p className="card-sub">
        {horizon === null
          ? "请选择持有期。"
          : `持有期 ${horizon} 个交易日；Q1 为方向对齐后的低分组，QG 为高分组。`}
      </p>

      {!factorEntries.length || horizonKey === null ? (
        <EmptyPanel title="暂无该持有期的诊断结果" />
      ) : (
        factorEntries.map(([name, factor], index) => (
          <div key={name}>
            {index > 0 ? <div className="section-divider" /> : null}
            <h4 className="subheading">{factorLabel(name, metadata.factor_definitions)}</h4>
            {factor.horizons[horizonKey] ? (
              <FactorDiagnosticView diagnostic={factor.horizons[horizonKey]} />
            ) : (
              <EmptyPanel title="该因子在此持有期没有诊断结果" />
            )}
          </div>
        ))
      )}

      <CorrelationSection result={result} />
    </>
  );
}

function FactorDiagnosticView({ diagnostic }: { diagnostic: FactorDiagnostic }) {
  const { summary, groups } = diagnostic;

  return (
    <>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th scope="col">指标</th>
              <th scope="col" className="numeric">
                均值
              </th>
              <th scope="col" className="numeric">
                标准差
              </th>
              <th scope="col" className="numeric">
                IR
              </th>
              <th scope="col" className="numeric">
                正值占比
              </th>
              <th scope="col" className="numeric">
                有效日期
              </th>
            </tr>
          </thead>
          <tbody>
            <ICRow label="IC" stats={summary.ic} />
            <ICRow label="Rank IC" stats={summary.rank_ic} />
          </tbody>
        </table>
      </div>

      <div className="badge-row">
        <span className="badge">
          平均高减低 {formatPercent(summary.mean_high_minus_low)}
        </span>
        <span className="badge">
          价差日期 {formatNumber(summary.spread_dates, 0)}
        </span>
        <span className="badge">
          信号日期 {formatNumber(summary.signal_dates, 0)}
        </span>
        <span className="badge">
          标签观测 {formatNumber(summary.label_observations, 0)}
        </span>
      </div>

      {groups.length ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">分组</th>
                <th scope="col" className="numeric">
                  平均收益
                </th>
                <th scope="col" className="numeric">
                  日期数
                </th>
                <th scope="col" className="numeric">
                  观测数
                </th>
              </tr>
            </thead>
            <tbody>
              {groups.map((group) => (
                <tr key={group.group}>
                  <td>Q{group.group}</td>
                  <td className="numeric">{formatPercent(group.mean_return)}</td>
                  <td className="numeric">{formatNumber(group.dates, 0)}</td>
                  <td className="numeric">
                    {formatNumber(group.observations, 0)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted-text">暂无分组收益。</p>
      )}
      <p className="table-footnote">
        分组可能因得分并列而不等或为空；按方向对齐后的得分排序，Q1 为低分位、QG 为高分位。
      </p>
    </>
  );
}

function ICRow({ label, stats }: { label: string; stats: ICStats }) {
  return (
    <tr>
      <td>{label}</td>
      <td className="numeric">{formatNumber(stats.mean, 4)}</td>
      <td className="numeric">{formatNumber(stats.std, 4)}</td>
      <td className="numeric">{formatNumber(stats.ir, 4)}</td>
      <td className="numeric">{formatPercent(stats.positive_fraction)}</td>
      <td className="numeric">{formatNumber(stats.valid_dates, 0)}</td>
    </tr>
  );
}

function CorrelationSection({ result }: { result: EvaluationResult }) {
  const { correlations } = result;

  return (
    <>
      <div className="section-divider" />
      <h3 className="subheading">因子相关性</h3>
      <p className="card-sub">对齐因子方向后，计算每日横截面秩相关，再对日期取平均。</p>
      {correlations.pairs.length ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">因子对</th>
                <th scope="col" className="numeric">
                  平均秩相关
                </th>
                <th scope="col" className="numeric">
                  有效日期
                </th>
              </tr>
            </thead>
            <tbody>
              {correlations.pairs.map((pair, index) => (
                <tr key={`${pair.first}-${pair.second}-${index}`}>
                  <td>
                    {factorLabel(pair.first,result.metadata.factor_definitions)} / {factorLabel(pair.second,result.metadata.factor_definitions)}
                  </td>
                  <td className="numeric">
                    {formatNumber(pair.mean_rank_correlation, 4)}
                  </td>
                  <td className="numeric">
                    {formatNumber(pair.valid_dates, 0)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted-text">暂无可展示的因子对相关性。</p>
      )}
    </>
  );
}

function CoverageSections({ result }: { result: EvaluationResult }) {
  const labelEntries = Object.entries(result.labels);

  return (
    <>
      <section className="card">
        <div className="card-header">
          <div>
            <h2 className="card-title">信号覆盖</h2>
            <p className="card-sub">
              评分日与对应入场日；评分在收盘后生成，下一交易日开盘执行。
            </p>
          </div>
          <span className="badge">
            共 {formatNumber(result.signal_coverage.length, 0)} 个信号日
          </span>
        </div>

        {result.signal_coverage.length ? (
          <details>
          <summary>查看逐日信号覆盖</summary>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">信号日</th>
                  <th scope="col">入场日</th>
                  <th scope="col" className="numeric">
                    已评分
                  </th>
                  <th scope="col" className="numeric">
                    股票池
                  </th>
                </tr>
              </thead>
              <tbody>
                {result.signal_coverage.map((row) => (
                  <tr key={`${row.signal_date}-${row.entry_date}`}>
                    <td>{row.signal_date}</td>
                    <td>{row.entry_date}</td>
                    <td className="numeric">
                      {formatNumber(row.scored_instruments, 0)}
                    </td>
                    <td className="numeric">{formatNumber(row.universe, 0)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          </details>
        ) : (
          <EmptyPanel title="暂无信号覆盖记录" />
        )}
      </section>

      <section className="card">
        <div className="card-header">
          <div>
            <h2 className="card-title">标签覆盖</h2>
            <p className="card-sub">
              标签是事后收益，仅用于评估，不作为模型输入。
            </p>
          </div>
        </div>

        {labelEntries.length ? (
          labelEntries.map(([key, label], index) => (
            <div key={key}>
              {index > 0 ? <div className="section-divider" /> : null}
              <LabelCoverageView name={key} label={label} />
              {result.tables?.[`labels/${key}/observations`] ? <p><a href={`/api/v1/evaluations/${encodeURIComponent(result.metadata.id)}/labels/${key}/download`}>
                下载完整收益标签明细（Parquet）</a> · 共 {result.tables[`labels/${key}/observations`].rows} 条，JSON报告保留文件索引。</p> : null}
            </div>
          ))
        ) : (
          <EmptyPanel title="暂无标签覆盖记录" />
        )}
      </section>
    </>
  );
}

function LabelCoverageView({
  name,
  label,
}: {
  name: string;
  label: LabelDiagnostic;
}) {
  return (
    <>
      <div className="card-header compact-header">
        <h3 className="subheading">持有期 {name} 个交易日</h3>
        <span className="badge">
          有效观测 {formatNumber(label.valid_observations, 0)} · 排除{" "}
          {formatNumber(label.excluded_observations, 0)}
        </span>
      </div>

      {label.coverage.length ? (
        <details>
        <summary>查看逐日标签覆盖与排除原因</summary>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">日期</th>
                <th scope="col" className="numeric">
                  已评分
                </th>
                <th scope="col" className="numeric">
                  有效标签
                </th>
                <th scope="col">排除原因</th>
              </tr>
            </thead>
            <tbody>
              {label.coverage.map((row) => (
                <tr key={row.date}>
                  <td>{row.date}</td>
                  <td className="numeric">{formatNumber(row.scored, 0)}</td>
                  <td className="numeric">
                    {formatNumber(row.valid_labels, 0)}
                  </td>
                  <td>{exclusionText(row.exclusions)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        </details>
      ) : (
        <p className="muted-text">暂无覆盖明细。</p>
      )}
    </>
  );
}

function MetaItem({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="meta-item">
      <div className="meta-label">{label}</div>
      <div className={mono ? "meta-value meta-mono" : "meta-value"}>
        {value || DASH}
      </div>
    </div>
  );
}
