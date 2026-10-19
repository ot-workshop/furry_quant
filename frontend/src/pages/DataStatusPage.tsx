import { Fragment, useEffect, useMemo, useState, type ReactNode } from "react";
import { api } from "../api/client";
import { EmptyPanel, ErrorPanel, LoadingPanel } from "../components/StatePanels";
import { useApiResource } from "../hooks/useApiResource";
import type {
  Coverage,
  DataJob,
  DataJobFailureReport,
  DataJobReport,
  StockCoverage,
} from "../types";
import { formatDateTime, formatNumber } from "../utils/format";

const DASH = "—";

const JOB_STATUS_LABELS: Record<string, string> = {
  running: "运行中",
  succeeded: "已完成",
  failed: "失败",
  partial: "部分完成",
  interrupted: "已中断",
  queued: "排队中",
  pending: "待处理",
};

const JOB_STATUS_CLASSES: Record<string, string> = {
  running: "status-running",
  succeeded: "status-succeeded",
  failed: "status-failed",
  partial: "status-partial",
  interrupted: "status-interrupted",
  queued: "status-queued",
  pending: "status-queued",
};

const COVERAGE_STATUS_LABELS: Record<string, string> = {
  complete: "完整",
  incomplete: "不完整",
};

const PARTITION_STATUS_LABELS: Record<string, string> = {
  succeeded: "成功",
  failed: "失败",
  running: "运行中",
  pending: "待处理",
  partial: "部分完成",
  interrupted: "已中断",
};

const PARTITION_ORDER = [
  "succeeded",
  "failed",
  "running",
  "pending",
  "partial",
  "interrupted",
];

interface CoverageRow {
  key: string;
  item: StockCoverage;
  isBenchmark: boolean;
}

function jobStatusText(status: string | null | undefined): string {
  if (!status) return "状态未知";
  return JOB_STATUS_LABELS[status] ?? `未知状态（${status}）`;
}

function jobStatusClass(status: string): string {
  return JOB_STATUS_CLASSES[status] ?? "status-unknown";
}

function partitionStatusText(status: string): string {
  return PARTITION_STATUS_LABELS[status] ?? jobStatusText(status);
}

function coverageStatusText(status: string | null | undefined): string {
  if (!status) return "状态未知";
  return COVERAGE_STATUS_LABELS[status] ?? `未知状态（${status}）`;
}

function coverageStatusClass(status: string): string {
  if (status === "complete") return "status-complete";
  if (status === "incomplete") return "status-incomplete";
  return "status-unknown";
}

function toTimestamp(value: string | null | undefined): number {
  if (!value) return 0;
  const time = new Date(value).getTime();
  return Number.isNaN(time) ? 0 : time;
}

function partitionRank(key: string): number {
  const index = PARTITION_ORDER.indexOf(key);
  return index === -1 ? PARTITION_ORDER.length : index;
}

function hasCoverageGap(item: StockCoverage): boolean {
  return (
    !item.complete ||
    item.missing_dates.length > 0 ||
    item.invalid_dates.length > 0 ||
    (item.missing_valuation_dates?.length ?? 0) > 0
  );
}

function isCoverageReport(report: DataJobReport): report is Coverage {
  return "stocks" in report && Array.isArray(report.stocks);
}

function firstParam(
  params: Record<string, string> | undefined,
  keys: string[],
): string | null {
  if (!params) return null;
  for (const key of keys) {
    const value = params[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return null;
}

function failureRange(params: Record<string, string> | undefined): string {
  const start = firstParam(params, ["start", "start_date", "begin", "from"]);
  const end = firstParam(params, ["end", "end_date", "finish", "to"]);
  if (!start && !end) return DASH;
  return `${start ?? DASH} 至 ${end ?? DASH}`;
}

export function DataStatusPage() {
  const status = useApiResource((signal) => api.getDataStatus(signal), []);
  const [selectedSnapshotId, setSelectedSnapshotId] = useState<string | null>(
    null,
  );
  const [expandedStockKeys, setExpandedStockKeys] = useState<Set<string>>(
    () => new Set<string>(),
  );
  const [expandedJobId, setExpandedJobId] = useState<string | null>(null);

  const snapshots = useMemo(
    () =>
      [...(status.data?.snapshots ?? [])].sort(
        (left, right) =>
          toTimestamp(right.created_at) - toTimestamp(left.created_at),
      ),
    [status.data],
  );

  const jobs = useMemo(() => status.data?.jobs ?? [], [status.data]);

  const partitionEntries = useMemo(() => {
    const partitions = status.data?.partitions;
    if (!partitions) return [];
    return Object.entries(partitions).sort(
      (left, right) => partitionRank(left[0]) - partitionRank(right[0]),
    );
  }, [status.data]);

  useEffect(() => {
    if (!snapshots.length) {
      setSelectedSnapshotId(null);
      return;
    }
    setSelectedSnapshotId((current) =>
      current && snapshots.some((snapshot) => snapshot.id === current)
        ? current
        : status.data?.active_snapshot_id ?? snapshots[0].id,
    );
  }, [snapshots, status.data?.active_snapshot_id]);

  useEffect(() => {
    setExpandedStockKeys(new Set());
  }, [selectedSnapshotId]);

  useEffect(() => {
    setExpandedJobId((current) =>
      current && jobs.some((job) => job.id === current) ? current : null,
    );
  }, [jobs]);

  const selectedSnapshot =
    snapshots.find((snapshot) => snapshot.id === selectedSnapshotId) ?? null;
  const coverage = selectedSnapshot?.coverage ?? null;

  const coverageRows = useMemo<CoverageRow[]>(() => {
    if (!coverage) return [];
    const rows: CoverageRow[] = (coverage.stocks ?? []).map((item, index) => ({
      key: `stock-${item.code}-${index}`,
      item,
      isBenchmark: false,
    }));
    if (coverage.benchmark) {
      rows.push({
        key: `benchmark-${coverage.benchmark.code}`,
        item: coverage.benchmark,
        isBenchmark: true,
      });
    }
    return rows;
  }, [coverage]);

  const toggleStock = (key: string) => {
    setExpandedStockKeys((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const toggleJob = (id: string) => {
    setExpandedJobId((current) => (current === id ? null : id));
  };

  return (
    <div className="page-stack">
      <section className="card">
        <div className="card-header">
          <div>
            <h2 className="card-title">数据概览</h2>
            <p className="card-sub">
              只读查看本地行情分区、快照覆盖率与下载任务记录。
            </p>
          </div>
          <div className="card-actions">
            <button
              type="button"
              className="button"
              onClick={status.reload}
              disabled={status.loading}
            >
              {status.loading ? "读取中…" : "刷新状态"}
            </button>
          </div>
        </div>

        {status.loading ? (
          <LoadingPanel label="正在读取数据状态…" />
        ) : status.error ? (
          <ErrorPanel message={status.error} onRetry={status.reload} />
        ) : status.data ? (
          <>
            <h3 className="subheading">分区状态</h3>
            {partitionEntries.length ? (
              <div className="badge-row">
                {partitionEntries.map(([key, count]) => (
                  <span
                    className={`badge${
                      key === "succeeded"
                        ? " partition-ok"
                        : key === "failed"
                          ? " partition-bad"
                          : ""
                    }`}
                    key={key}
                  >
                    {partitionStatusText(key)} {formatNumber(count, 0)}
                  </span>
                ))}
              </div>
            ) : (
              <p className="muted-text">暂无分区统计。</p>
            )}
            <p className="hint-text">
              累计包含历史请求；当前数据以所选快照校验状态为准。本页只查询状态。
            </p>
          </>
        ) : (
          <EmptyPanel title="暂无数据状态">
            当前没有可显示的数据信息，点击“刷新状态”重试。
          </EmptyPanel>
        )}
      </section>

      {status.data ? (
        <section className="card">
          <div className="card-header">
            <div>
              <h2 className="card-title">数据快照</h2>
              <p className="card-sub">
                查看最近一次快照的请求区间与股票覆盖情况；存在多个快照时可切换。
              </p>
            </div>
            {snapshots.length > 1 ? (
              <div className="field field-inline">
                <label htmlFor="snapshot-select">选择快照</label>
                <select
                  id="snapshot-select"
                  value={selectedSnapshotId ?? ""}
                  onChange={(event) =>
                    setSelectedSnapshotId(event.target.value || null)
                  }
                >
                  {snapshots.map((snapshot) => (
                    <option key={snapshot.id} value={snapshot.id}>
                      {formatDateTime(snapshot.created_at)} · {snapshot.start} 至{" "}
                      {snapshot.end}
                    </option>
                  ))}
                </select>
              </div>
            ) : selectedSnapshot ? (
              <span className="badge">{selectedSnapshot.id === status.data?.active_snapshot_id ? "当前快照" : "历史快照"}</span>
            ) : null}
          </div>

          {!snapshots.length ? (
            <EmptyPanel title="暂无数据快照">
              请使用已配置的下载命令获取数据，然后点击“刷新状态”。
            </EmptyPanel>
          ) : !selectedSnapshot || !coverage ? (
            <EmptyPanel title="请选择数据快照" />
          ) : (
            <>
              <div className="meta-strip">
                <MetaItem
                  label="快照 ID"
                  value={selectedSnapshot.id}
                  mono
                />
                <MetaItem
                  label="请求区间"
                  value={`${selectedSnapshot.start} 至 ${selectedSnapshot.end}`}
                />
                <MetaItem
                  label="股票数量"
                  value={`${formatNumber(selectedSnapshot.codes.length, 0)} 只`}
                />
                <MetaItem
                  label="交易日"
                  value={`${formatNumber(selectedSnapshot.trading_days, 0)} 天`}
                />
                <MetaItem
                  label="数据行数"
                  value={formatNumber(selectedSnapshot.rows, 0)}
                />
                <MetaItem label="基准" value={selectedSnapshot.benchmark} />
                <MetaItem
                  label="状态"
                  value={
                    <span
                      className={`status-pill ${coverageStatusClass(coverage.status)}`}
                    >
                      {coverageStatusText(coverage.status)}
                    </span>
                  }
                />
                <MetaItem
                  label="创建时间"
                  value={formatDateTime(selectedSnapshot.created_at)}
                />
              </div>

              <div className="section-divider" />

              <div className="card-header compact-header">
                <div>
                  <h3 className="subheading">股票覆盖情况</h3>
                  <p className="card-sub">
                    逐只代码对比预期与实际交易日；点击“详情”查看缺失和无效日期。
                  </p>
                </div>
                <span className="badge">
                  预期交易日{" "}
                  {formatNumber(coverage.expected_sessions, 0)} · 股票{" "}
                  {formatNumber(coverage.stock_count, 0)}
                </span>
              </div>

              {coverageRows.length ? (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th scope="col">代码</th>
                        <th scope="col" className="numeric">
                          预期
                        </th>
                        <th scope="col" className="numeric">
                          实际
                        </th>
                        <th scope="col" className="numeric">
                          缺失
                        </th>
                        <th scope="col" className="numeric">
                          无效
                        </th>
                        <th scope="col" className="numeric">
                          缺失估值
                        </th>
                        <th scope="col">状态</th>
                        <th scope="col">操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      {coverageRows.map((row) => {
                        const { item } = row;
                        const expanded = expandedStockKeys.has(row.key);
                        const detailId = `coverage-detail-${row.key}`;
                        const valuationCount =
                          item.missing_valuation_dates?.length ?? 0;

                        return (
                          <Fragment key={row.key}>
                            <tr className={expanded ? "selected-row" : ""}>
                              <td>
                                <div className="cell-code">{item.code}</div>
                                {row.isBenchmark ? (
                                  <div className="cell-sub">基准</div>
                                ) : null}
                              </td>
                              <td className="numeric">
                                {formatNumber(item.expected, 0)}
                              </td>
                              <td className="numeric">
                                {formatNumber(item.actual, 0)}
                              </td>
                              <td className="numeric">
                                <CountValue
                                  value={item.missing_dates.length}
                                  tone="bad"
                                />
                              </td>
                              <td className="numeric">
                                <CountValue
                                  value={item.invalid_dates.length}
                                  tone="bad"
                                />
                              </td>
                              <td className="numeric">
                                <CountValue value={valuationCount} tone="warn" />
                              </td>
                              <td>
                                <span
                                  className={`status-pill ${coverageStatusClass(
                                    item.complete ? "complete" : "incomplete",
                                  )}`}
                                >
                                  {item.complete ? "完整" : "不完整"}
                                </span>
                              </td>
                              <td>
                                <button
                                  type="button"
                                  className="text-button"
                                  aria-expanded={expanded}
                                  aria-controls={detailId}
                                  onClick={() => toggleStock(row.key)}
                                >
                                  {expanded ? "收起" : "详情"}
                                </button>
                              </td>
                            </tr>
                            {expanded ? (
                              <tr
                                className="coverage-detail-row"
                                id={detailId}
                              >
                                <td colSpan={8}>
                                  <div className="coverage-detail">
                                    <div className="coverage-detail-block">
                                      <span className="coverage-detail-label">
                                        缺失日期
                                      </span>
                                      <DateList
                                        dates={item.missing_dates}
                                        tone="bad"
                                        empty="无缺失日期"
                                      />
                                    </div>
                                    <div className="coverage-detail-block">
                                      <span className="coverage-detail-label">
                                        无效日期
                                      </span>
                                      <DateList
                                        dates={item.invalid_dates}
                                        tone="bad"
                                        empty="无无效日期"
                                      />
                                    </div>
                                    <div className="coverage-detail-block">
                                      <span className="coverage-detail-label">
                                        缺失估值日期（提示）
                                      </span>
                                      <DateList
                                        dates={item.missing_valuation_dates}
                                        tone="warn"
                                        empty="无缺失估值日期"
                                      />
                                    </div>
                                  </div>
                                </td>
                              </tr>
                            ) : null}
                          </Fragment>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              ) : (
                <EmptyPanel title="暂无覆盖明细" />
              )}

              <p className="hint-text">
                快照使用固定的股票池代码；该股票池不代表历史指数成分股。
              </p>
              {coverage.note ? (
                <p className="hint-callout">{coverage.note}</p>
              ) : null}
              <p className="warning-callout">
                缺失估值仅作提示，不代表行情数据错误；本页不会补零。
              </p>
            </>
          )}
        </section>
      ) : null}

      {status.data ? (
        <section className="card">
          <div className="card-header">
            <div>
              <h2 className="card-title">下载任务记录</h2>
              <p className="card-sub">
                查看每次任务的进度与结果；点击“详情”展开覆盖缺口或请求失败信息。
              </p>
            </div>
            {jobs.length ? (
              <span className="badge">共 {jobs.length} 条</span>
            ) : null}
          </div>

          {!jobs.length ? (
            <EmptyPanel title="暂无下载任务记录">
              完成一次已配置的数据下载后，任务记录会显示在这里。
            </EmptyPanel>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">时间</th>
                    <th scope="col">区间</th>
                    <th scope="col">状态</th>
                    <th scope="col" className="numeric">
                      完成 / 总数
                    </th>
                    <th scope="col" className="numeric">
                      复用缓存
                    </th>
                    <th scope="col" className="numeric">
                      失败
                    </th>
                    <th scope="col">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {jobs.map((job) => {
                    const expanded = expandedJobId === job.id;
                    const detailId = `job-detail-${job.id}`;

                    return (
                      <Fragment key={job.id}>
                        <tr className={expanded ? "selected-row" : ""}>
                          <td>
                            <div className="cell-name">
                              {formatDateTime(job.started_at)}
                            </div>
                            <div className="cell-sub">
                              结束{" "}
                              {job.finished_at
                                ? formatDateTime(job.finished_at)
                                : DASH}
                            </div>
                          </td>
                          <td>
                            <div className="nowrap">
                              {job.config.start} 至 {job.config.end}
                            </div>
                            <div className="cell-sub">
                              {formatNumber(job.config.codes.length, 0)} 只 ·
                              基准 {job.config.benchmark}
                            </div>
                          </td>
                          <td>
                            <span
                              className={`status-pill ${jobStatusClass(job.status)}`}
                            >
                              {jobStatusText(job.status)}
                            </span>
                          </td>
                          <td className="numeric">
                            {formatNumber(job.completed, 0)} /{" "}
                            {formatNumber(job.total, 0)}
                          </td>
                          <td className="numeric">
                            {formatNumber(job.cached, 0)}
                          </td>
                          <td className="numeric">
                            <CountValue value={job.failed} tone="bad" />
                          </td>
                          <td>
                            <button
                              type="button"
                              className="text-button"
                              aria-expanded={expanded}
                              aria-controls={detailId}
                              onClick={() => toggleJob(job.id)}
                            >
                              {expanded ? "收起" : "详情"}
                            </button>
                          </td>
                        </tr>
                        {expanded ? (
                          <tr className="coverage-detail-row" id={detailId}>
                            <td colSpan={7}>
                              <JobDetail job={job} />
                            </td>
                          </tr>
                        ) : null}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>
      ) : null}
    </div>
  );
}

function JobDetail({ job }: { job: DataJob }) {
  if (!job.report) {
    return (
      <div className="job-detail">
        <p className="muted-text">
          {job.status === "running"
            ? "任务仍在运行，暂未生成覆盖报告。"
            : "该任务没有保存覆盖报告。"}
        </p>
      </div>
    );
  }

  if (isCoverageReport(job.report)) {
    return <JobCoverageReport report={job.report} />;
  }

  return <JobFailureReportView report={job.report} />;
}

function JobCoverageReport({ report }: { report: Coverage }) {
  const gapRows = useMemo(() => {
    const rows: Array<{
      key: string;
      item: StockCoverage;
      isBenchmark: boolean;
    }> = [];

    (report.stocks ?? []).forEach((item, index) => {
      if (hasCoverageGap(item)) {
        rows.push({
          key: `stock-${item.code}-${index}`,
          item,
          isBenchmark: false,
        });
      }
    });

    if (report.benchmark && hasCoverageGap(report.benchmark)) {
      rows.push({
        key: `benchmark-${report.benchmark.code}`,
        item: report.benchmark,
        isBenchmark: true,
      });
    }

    return rows;
  }, [report]);

  return (
    <div className="job-detail">
      <div className="job-detail-head">
        <h4 className="job-detail-title">覆盖报告</h4>
        <span
          className={`status-pill ${coverageStatusClass(report.status)}`}
        >
          {coverageStatusText(report.status)}
        </span>
      </div>
      <div className="job-report-meta">
        <span>预期交易日 {formatNumber(report.expected_sessions, 0)}</span>
        <span>股票数 {formatNumber(report.stock_count, 0)}</span>
      </div>
      {report.note ? <p className="job-reason">{report.note}</p> : null}
      {report.adjustment_anomalies?.length ? (
        <div className="table-wrap">
          <table>
            <thead><tr><th>代码</th><th>前一交易日</th><th>异常日期</th><th>复权收益率</th><th>接口涨跌幅</th></tr></thead>
            <tbody>{report.adjustment_anomalies.map((row) => (
              <tr key={`${row.instrument}-${row.date}`}>
                <td>{row.instrument}</td><td>{row.previous_date}</td><td>{row.date}</td>
                <td className="count-bad">{formatNumber(row.adjusted_return * 100, 2)}%</td>
                <td>{row.reported_change === null ? DASH : `${formatNumber(row.reported_change * 100, 2)}%`}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      ) : null}

      {gapRows.length ? (
        <>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">代码</th>
                  <th scope="col" className="numeric">
                    缺失
                  </th>
                  <th scope="col" className="numeric">
                    无效
                  </th>
                  <th scope="col" className="numeric">
                    缺失估值
                  </th>
                </tr>
              </thead>
              <tbody>
                {gapRows.map((row) => (
                  <tr key={row.key}>
                    <td>
                      <div className="cell-code">{row.item.code}</div>
                      {row.isBenchmark ? (
                        <div className="cell-sub">基准</div>
                      ) : null}
                    </td>
                    <td className="numeric">
                      <CountValue
                        value={row.item.missing_dates.length}
                        tone="bad"
                      />
                    </td>
                    <td className="numeric">
                      <CountValue
                        value={row.item.invalid_dates.length}
                        tone="bad"
                      />
                    </td>
                    <td className="numeric">
                      <CountValue
                        value={row.item.missing_valuation_dates?.length ?? 0}
                        tone="warn"
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {gapRows.map(({key, item}) => (
            <details key={key} className="coverage-detail">
              <summary>{item.code} 日期详情</summary>
              <p>缺失：<DateList dates={item.missing_dates} tone="bad" empty="无" /></p>
              <p>无效：<DateList dates={item.invalid_dates} tone="bad" empty="无" /></p>
              <p>多余：<DateList dates={item.extra_dates} tone="bad" empty="无" /></p>
              <p>重复：<DateList dates={item.duplicate_dates} tone="bad" empty="无" /></p>
              <p>缺失或非正估值：<DateList dates={item.missing_valuation_dates} tone="warn" empty="无" /></p>
            </details>
          ))}
        </>
      ) : (
        <p className="muted-text">未发现覆盖率缺口。</p>
      )}

      <p className="warning-callout">
        缺失估值仅作提示，不代表行情数据错误；本页不会补零。
      </p>
    </div>
  );
}

function JobFailureReportView({ report }: { report: DataJobFailureReport }) {
  const failures = report.failures ?? [];

  return (
    <div className="job-detail">
      <div className="job-detail-head">
        <h4 className="job-detail-title">请求失败</h4>
        <span className={`status-pill ${jobStatusClass(report.status)}`}>
          {jobStatusText(report.status)}
        </span>
      </div>
      <p className="job-reason">{{request_partitions_failed:"部分请求未完成，可重跑补抓",validation_or_publication_failed:"数据校验或快照发布未完成",download_interrupted:"下载中断，可重跑续传"}[report.reason] || report.reason || "任务未提供失败原因。"}</p>

      {failures.length ? (
        <>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">接口</th>
                  <th scope="col">代码</th>
                  <th scope="col">区间</th>
                  <th scope="col">错误</th>
                </tr>
              </thead>
              <tbody>
                {failures.map((failure, index) => (
                  <tr key={`${failure.api}-${index}`}>
                    <td className="cell-code">{failure.api || DASH}</td>
                    <td className="cell-code">
                      {firstParam(failure.params, [
                        "ts_code",
                        "codes",
                        "code",
                        "symbol",
                      ]) ?? DASH}
                    </td>
                    <td className="nowrap">{failureRange(failure.params)}</td>
                    <td className="failure-error">
                      {{request_failed:"供应商请求失败，可重试",rate_limit:"供应商频率限制，请稍后重试",access_denied:"供应商权限不足",unsupported:"供应商不支持该接口",network_failed:"网络请求未完成，可重试",missing_sessions:"交易日记录缺失",invalid_response:"返回内容未通过校验",cache_corrupt:"缓存校验失败，可重试重新获取"}[failure.error] || failure.error || DASH}
                      {failure.missing_dates?.length ? <DateList dates={failure.missing_dates} tone="bad" empty="无" /> : null}
                      {failure.extra_dates?.length ? <DateList dates={failure.extra_dates} tone="bad" empty="无" /> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="table-footnote">
            仅展示后端返回的接口、参数与错误摘要。
          </p>
        </>
      ) : (
        <p className="muted-text">没有可展示的请求失败详情。</p>
      )}
    </div>
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
        {value}
      </div>
    </div>
  );
}

function CountValue({
  value,
  tone,
}: {
  value: number;
  tone?: "warn" | "bad";
}) {
  const text = formatNumber(value, 0);
  if (!value) return <span className="coverage-zero">{text}</span>;
  const className =
    tone === "bad" ? "count-bad" : tone === "warn" ? "count-warn" : undefined;
  return <span className={className}>{text}</span>;
}

function DateList({
  dates,
  tone,
  empty,
}: {
  dates?: string[];
  tone: "bad" | "warn";
  empty: string;
}) {
  if (!dates?.length) {
    return <span className="date-list is-muted">{empty}</span>;
  }

  return (
    <span className="date-list">
      {dates.map((date, index) => (
        <span className={`date-chip is-${tone}`} key={`${date}-${index}`}>
          {date}
        </span>
      ))}
    </span>
  );
}
