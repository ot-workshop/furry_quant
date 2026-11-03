import { useEffect, useState } from "react";
import {
  backtestDownloadUrl,
  evaluationReportUrl,
  experimentsApi,
  type ExperimentMetadata,
  type ExperimentPeriod,
  type ExperimentPeriodName,
  type ExperimentResult,
  type ExperimentRow,
} from "../api/experiments";
import { EmptyPanel, ErrorPanel, LoadingPanel } from "../components/StatePanels";
import { useApiResource } from "../hooks/useApiResource";
import {
  formatDateTime,
  formatNumber,
  formatPercent,
  formatWeight,
  factorLabel,
} from "../utils/format";

const DASH = "—";
const NO_EXPERIMENTS: ExperimentMetadata[] = [];

const PERIODS: Array<{
  key: ExperimentPeriodName;
  label: string;
  note: string;
}> = [
  { key: "development", label: "开发期", note: "仅作信息参考，不参与方案选择" },
  { key: "validation", label: "验证期", note: "用于比较并选定方案" },
  { key: "test", label: "测试期", note: "仅运行选定候选，不据此宣布胜出" },
];

function periodRange(period: ExperimentPeriod | undefined): string {
  if (!period || !period.start || !period.end) return DASH;
  return `${period.start} → ${period.end}`;
}

function findRow(
  rows: ExperimentRow[],
  candidateId: string,
  period: ExperimentPeriodName,
): ExperimentRow | null {
  return (
    rows.find(
      (row) => row.candidate_id === candidateId && row.period === period,
    ) ?? null
  );
}

export function ExperimentPage() {
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const list = useApiResource((signal) => experimentsApi.list(signal), []);
  const experiments = list.data?.items ?? NO_EXPERIMENTS;

  useEffect(() => {
    if (!experiments.length) {
      setSelectedId(null);
      return;
    }
    setSelectedId((current) =>
      current && experiments.some((item) => item.id === current)
        ? current
        : experiments[0].id,
    );
  }, [experiments]);

  const detail = useApiResource(
    selectedId ? (signal) => experimentsApi.get(selectedId, signal) : null,
    [selectedId],
  );

  const result = detail.data;

  return (
    <div className="page-stack">
      <section className="card">
        <div className="card-header">
          <div>
            <h2 className="card-title">历史分段方案比较</h2>
            <p className="card-sub">
              只读查看已保存的分段验证结果；方案选择只使用验证期，测试期仅复核选定候选。
            </p>
          </div>
          <div className="card-actions">
            {result ? (
              <>
                <a
                  className="button button-compact"
                  href={experimentsApi.downloadUrl(result.metadata.id)}
                  download={`experiment-${result.metadata.id}.json`}
                >
                  下载结果 JSON
                </a>
                <a
                  className="button button-compact"
                  href={experimentsApi.reportUrl(result.metadata.id)}
                  download={`experiment-${result.metadata.id}.md`}
                >
                  下载 Markdown
                </a>
              </>
            ) : null}
          </div>
        </div>

        {list.loading ? (
          <LoadingPanel label="正在读取方案列表…" />
        ) : list.error ? (
          <ErrorPanel message={list.error} onRetry={list.reload} />
        ) : !experiments.length ? (
          <EmptyPanel title="暂无方案比较结果">
            完成一次历史分段验证并保存结果后，记录会显示在这里。
          </EmptyPanel>
        ) : (
          <>
            <div className="card-actions">
              <div className="field field-inline">
                <label htmlFor="experiment-select">方案结果</label>
                <select
                  id="experiment-select"
                  value={selectedId ?? ""}
                  onChange={(event) => setSelectedId(event.target.value)}
                >
                  {experiments.map((item) => (
                    <option key={item.id} value={item.id}>
                      {formatDateTime(item.created_at)} · {item.name}
                    </option>
                  ))}
                </select>
              </div>
              <span className="badge">
              共 {formatNumber(list.data?.total, 0)} 次实验
              </span>
            </div>

            {detail.loading ? (
              <LoadingPanel label="正在读取方案详情…" />
            ) : detail.error ? (
              <ErrorPanel message={detail.error} onRetry={detail.reload} />
            ) : !result ? (
              <EmptyPanel title="请选择方案" />
            ) : (
              <ExperimentOverview result={result} />
            )}
          </>
        )}
      </section>

      {result ? <ExperimentComparisons result={result} /> : null}
    </div>
  );
}

function ExperimentOverview({ result }: { result: ExperimentResult }) {
  const { metadata, plan, selection, shared_parameters } = result;
  const selectedCandidate =
    plan.candidates.find(
      (candidate) => candidate.id === selection.candidate_id,
    ) ?? null;
  const selectedLabel = selectedCandidate
    ? `${selectedCandidate.name}（${selectedCandidate.id}）`
    : selection.candidate_id;

  return (
    <>
      <div className="section-divider" />

      <div className="card-header compact-header">
        <div>
          <div className="detail-title-row">
            <h3 className="subheading detail-title">{plan.name}</h3>
            <span className="badge">历史分段验证</span>
            <span className="badge">
              {metadata.is_synthetic ? "合成样本" : "非合成数据"}
            </span>
          </div>
          <p className="card-sub">
            创建于 {formatDateTime(metadata.created_at)} · 选定候选{" "}
            {selectedLabel}
          </p>
        </div>
      </div>

      <div className="meta-strip">
        <MetaItem label="方案 ID" value={metadata.id} mono />
        <MetaItem label="数据版本" value={metadata.data_version} mono />
        <MetaItem label="锁定时间" value={formatDateTime(selection.locked_at)} />
      </div>

      <p className="warning-callout">
        历史数据此前已用于研究；本次为回溯分段检验，不代表全新未见样本。
      </p>
      <p className="hint-text">使用手工因子与权重，无模型训练。按验证期累计净收益减基准累计收益选方案，同分按候选 ID 升序。</p>

      <h3 className="subheading">历史分段</h3>
      <div className="metric-grid">
        {PERIODS.map((period) => (
          <div className="metric-card" key={period.key}>
            <div className="metric-label">{period.label}</div>
            <div className="subheading">
              {periodRange(plan.periods[period.key])}
            </div>
            <div className="muted-text">{period.note}</div>
          </div>
        ))}
      </div>
      <p className="hint-text">
        每个分段均从新的现金与持仓开始；各分段收益与净值不能首尾拼接。
      </p>

      <div className="section-divider" />

      <h3 className="subheading">公共参数</h3>
      <div className="meta-strip">
        <MetaItem label="数据版本" value={metadata.data_version} mono />
        <MetaItem label="基准" value={shared_parameters.benchmark || DASH} />
        <MetaItem
          label="股票池数量"
          value={formatNumber(shared_parameters.codes.length, 0)}
        />
        <MetaItem
          label="初始资金（元）"
          value={formatNumber(shared_parameters.backtest.initial_cash, 2)}
        />
        <MetaItem
          label="持仓数 topk"
          value={formatNumber(shared_parameters.backtest.topk, 0)}
        />
        <MetaItem
          label="剔除数 n_drop"
          value={formatNumber(shared_parameters.backtest.n_drop, 0)}
        />
        <MetaItem
          label="风险度 risk_degree"
          value={formatNumber(shared_parameters.backtest.risk_degree, 4)}
        />
        <MetaItem label="调仓频率" value={({daily:'每日',weekly:'每周首个交易日',monthly:'每月首个交易日'})[shared_parameters.backtest.rebalance_frequency ?? 'daily']}/>
        {shared_parameters.backtest.cost_mode==='itemized' ? <>
          <MetaItem label="券商综合佣金" value={formatPercent(shared_parameters.backtest.commission_rate,4)}/>
          <MetaItem label="税费模式" value={shared_parameters.backtest.tax_mode==='fixed' ? '自定义固定费率' : '成交日历史费率'}/>
          {shared_parameters.backtest.tax_mode==='fixed' ? <>
            <MetaItem label="卖出印花税" value={formatPercent(shared_parameters.backtest.stamp_tax_rate,4)}/>
            <MetaItem label="双边过户费" value={formatPercent(shared_parameters.backtest.transfer_fee_rate,4)}/>
          </> : null}
        </> : <><MetaItem
          label="买入综合费率"
          value={formatPercent(shared_parameters.backtest.open_cost, 4)}
        />
        <MetaItem
          label="卖出费率"
          value={formatPercent(shared_parameters.backtest.close_cost, 4)}
        />
        </>}
        <MetaItem label="双边冲击成本" value={formatPercent(shared_parameters.backtest.impact_cost ?? 0,4)}/>
        <MetaItem
          label="最低费用（元）"
          value={formatNumber(shared_parameters.backtest.min_cost, 2)}
        />
        <MetaItem
          label="评估持有期"
          value={
            shared_parameters.evaluation.horizons.length
              ? shared_parameters.evaluation.horizons
                  .map((horizon) => `${formatNumber(horizon, 0)} 日`)
                  .join("、")
              : DASH
          }
        />
        <MetaItem
          label="分组数"
          value={formatNumber(shared_parameters.evaluation.groups, 0)}
        />
        <MetaItem
          label="最小横截面"
          value={formatNumber(
            shared_parameters.evaluation.min_cross_section,
            0,
          )}
        />
      </div>
      <p className="hint-text">
        股票池：{shared_parameters.codes.length
          ? shared_parameters.codes.join("、")
          : DASH}
      </p>
    </>
  );
}

function ExperimentComparisons({ result }: { result: ExperimentResult }) {
  const { plan, selection, rows, limitations } = result;
  const selectedTest = findRow(rows, selection.candidate_id, "test");
  const selectedCandidate =
    plan.candidates.find(
      (candidate) => candidate.id === selection.candidate_id,
    ) ?? null;
  const selectedLabel = selectedCandidate
    ? selectedCandidate.name
    : selection.candidate_id;

  const candidateName = (candidateId: string): string =>
    plan.candidates.find((candidate) => candidate.id === candidateId)?.name ??
    candidateId;

  return (
    <>
      <section className="card">
        <div className="card-header">
          <div>
            <h2 className="card-title">方案比较</h2>
            <p className="card-sub">
              开发期供观察，验证期用于选方案。费用已计入净收益。
            </p>
          </div>
          <span className="badge">按验证期收益差选择</span>
        </div>

        <div className="meta-strip">
          <MetaItem
            label="选择指标"
            value={plan.selection_metric || selection.metric}
            mono
          />
          <MetaItem
            label="选定候选"
            value={selectedLabel}
          />
          <MetaItem
            label="锁定时间"
            value={formatDateTime(selection.locked_at)}
          />
        </div>

        {selection.score <= 0 ? (
          <p className="hint-text">验证期没有候选跑赢基准；选定方案只是在这些候选中收益差最高，不代表已通过有效性验证。</p>
        ) : null}

        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">候选方案</th>
                <th scope="col">因子配置</th>
                <th scope="col" className="numeric">
                  开发期收益
                </th>
                <th scope="col" className="numeric">
                  验证期收益
                </th>
                <th scope="col" className="numeric">
                  验证期基准
                </th>
                <th scope="col" className="numeric">
                  验证期超额
                </th>
                <th scope="col" className="numeric">
                  验证期回撤
                </th>
                <th scope="col" className="numeric">
                  验证期费用（元）
                </th>
                <th scope="col">测试分段</th>
              </tr>
            </thead>
            <tbody>
              {plan.candidates.map((candidate) => {
                const chosen = candidate.id === selection.candidate_id;
                const development = findRow(
                  rows,
                  candidate.id,
                  "development",
                );
                const validation = findRow(rows, candidate.id, "validation");
                const test = findRow(rows, candidate.id, "test");

                return (
                  <tr
                    key={candidate.id}
                    className={chosen ? "selected-row" : ""}
                  >
                    <td>
                      <div className="cell-name">{candidate.name}</div>
                      <div className="cell-sub">{candidate.id}</div>
                      {chosen ? <span className="badge">选定</span> : null}
                    </td>
                    <td>
                      {candidate.factors.length ? (
                        <div className="badge-row">
                          {candidate.factors.map((factor, index) => (
                            <span
                              className="badge"
                              key={`${factor.name}-${index}`}
                            >
                              {factor.name === "value" ? "盈利收益率" : factorLabel(factor.name)} · 权重{" "}
                              {formatWeight(factor.weight)}
                              {factor.window !== null &&
                              Number.isFinite(factor.window)
                                ? ` · ${formatNumber(factor.window, 0)} 日窗口`
                                : ""}
                            </span>
                          ))}
                        </div>
                      ) : (
                        <span className="muted-text">{DASH}</span>
                      )}
                    </td>
                    <td className="numeric">
                      {development
                        ? formatPercent(development.metrics.total_return)
                        : DASH}
                    </td>
                    <td className="numeric">
                      {validation
                        ? formatPercent(validation.metrics.total_return)
                        : DASH}
                    </td>
                    <td className="numeric">
                      {validation
                        ? formatPercent(validation.metrics.benchmark_return)
                        : DASH}
                    </td>
                    <td className="numeric">
                      {validation
                        ? formatPercent(validation.metrics.excess_return)
                        : DASH}
                    </td>
                    <td className="numeric">
                      {validation
                        ? formatPercent(validation.metrics.max_drawdown)
                        : DASH}
                    </td>
                    <td className="numeric">
                      {validation
                        ? formatNumber(
                            validation.metrics.total_cost_amount,
                            2,
                          )
                        : DASH}
                    </td>
                    <td>{chosen && test ? "已运行" : "未运行"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        <p className="table-footnote">
          测试分段按设计只运行选定候选；其他候选显示“未运行”，其收益不会按 0
          计入比较。
        </p>

        <div className="section-divider" />

        <h3 className="subheading">验证期排名</h3>
        {selection.ranking.length ? (
          <div className="badge-row">
            {selection.ranking.map((entry, index) => (
              <span
                className="badge"
                key={`${entry.candidate_id}-${index}`}
              >
                {index + 1}. {candidateName(entry.candidate_id)} ·{" "}
                {formatNumber(entry.score, 6)}
              </span>
            ))}
          </div>
        ) : (
          <p className="muted-text">没有保存验证期排名。</p>
        )}
      </section>

      <section className="card">
        <div className="card-header">
          <div>
            <h2 className="card-title">选定候选：测试分段</h2>
            <p className="card-sub">
              {selectedLabel} · 仅展示已保存的测试分段结果，不据此宣布新的胜出方案。
            </p>
          </div>
          {selectedTest ? (
            <div className="card-actions">
              <a
                className="button button-compact"
                href={backtestDownloadUrl(selectedTest.backtest_run_id)}
                download={`backtest-${selectedTest.backtest_run_id}.json`}
              >
                下载回测 JSON
              </a>
              <a
                className="button button-compact"
                href={evaluationReportUrl(selectedTest.evaluation_run_id)}
                download={`evaluation-${selectedTest.evaluation_run_id}.md`}
              >
                下载评估 Markdown
              </a>
            </div>
          ) : null}
        </div>

        {!selectedTest ? (
          <EmptyPanel title="测试分段未运行">
            选定候选没有测试分段运行记录；缺失结果不按 0 收益处理。
          </EmptyPanel>
        ) : (
          <>
            <div className="meta-strip">
              <MetaItem
                label="回测运行 ID"
                value={selectedTest.backtest_run_id}
                mono
              />
              <MetaItem
                label="因子运行 ID"
                value={selectedTest.factor_run_id}
                mono
              />
              <MetaItem
                label="评估运行 ID"
                value={selectedTest.evaluation_run_id}
                mono
              />
              <MetaItem
                label="交易日数"
                value={formatNumber(selectedTest.trading_days, 0)}
              />
            </div>

            <div className="metric-grid">
              <div className="metric-card">
                <div className="metric-label">总收益</div>
                <div className="metric-value">
                  {formatPercent(selectedTest.metrics.total_return)}
                </div>
              </div>
              <div className="metric-card">
                <div className="metric-label">基准收益</div>
                <div className="metric-value">
                  {formatPercent(selectedTest.metrics.benchmark_return)}
                </div>
              </div>
              <div className="metric-card">
                <div className="metric-label">算术超额收益</div>
                <div className="metric-value">
                  {formatPercent(selectedTest.metrics.excess_return)}
                </div>
              </div>
              <div className="metric-card">
                <div className="metric-label">最大回撤</div>
                <div className="metric-value">
                  {formatPercent(selectedTest.metrics.max_drawdown)}
                </div>
              </div>
              <div className="metric-card">
                <div className="metric-label">费用金额（元）</div>
                <div className="metric-value">
                  {formatNumber(selectedTest.metrics.total_cost_amount, 2)}
                </div>
              </div>
              <div className="metric-card">
                <div className="metric-label">日均换手率</div>
                <div className="metric-value">
                  {formatPercent(selectedTest.metrics.avg_turnover)}
                </div>
              </div>
            </div>

            <p className="hint-text">
              超额为收益的算术差，未做年化、风险调整或显著性检验；测试分段仅运行选定候选，各分段从新的现金与持仓开始，不能拼接净值。
            </p>
          </>
        )}
      </section>

      <section className="card">
        <div className="card-header">
          <div>
            <h2 className="card-title">已知限制</h2>
            <p className="card-sub">以下限制由方案结果原样提供。</p>
          </div>
        </div>
        {limitations.length ? (
          <ul className="limitation-list">
            {limitations.map((item, index) => (
              <li key={`${item}-${index}`}>{item}</li>
            ))}
          </ul>
        ) : (
          <p className="muted-text">结果未提供额外限制说明。</p>
        )}
      </section>
    </>
  );
}

function MetaItem({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
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
