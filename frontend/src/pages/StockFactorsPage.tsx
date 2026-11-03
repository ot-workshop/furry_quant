import { useEffect, useMemo, useState } from "react";
import {IndustryDiagnostics} from '../components/IndustryDiagnostics';
import {TemporalAuditPanel} from '../components/TemporalAuditPanel';
import { api } from "../api/client";
import { FactorHistoryChart } from "../components/FactorHistoryChart";
import { FactorDetails } from "../components/FactorDetails";
import { EmptyPanel, ErrorPanel, LoadingPanel } from "../components/StatePanels";
import { useApiResource, type ApiState } from "../hooks/useApiResource";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import type { FactorRunList, Instrument } from "../types";
import {
  factorLabel,
  formatNumber,
  formatWeight,
  scoreUnitLabel,
} from "../utils/format";

type DisplayMode = "score" | "raw";

interface StockFactorsPageProps {
  runs: ApiState<FactorRunList>;
  selectedRunId: string | null;
  onRunChange: (id: string) => void;
  selectedInstrument: Instrument | null;
  onInstrumentChange: (instrument: Instrument) => void;
  requestedDate?: string;
}

export function StockFactorsPage({
  runs,
  selectedRunId,
  onRunChange,
  selectedInstrument,
  onInstrumentChange,
  requestedDate,
}: StockFactorsPageProps) {
  const [query, setQuery] = useState("");
  const [displayMode, setDisplayMode] = useState<DisplayMode>("score");
  const [selectedDate, setSelectedDate] = useState(requestedDate ?? "");
  const debouncedQuery = useDebouncedValue(query, 250);

  const instruments = useApiResource(
    selectedRunId ? (signal) => api.getInstruments(debouncedQuery.trim(), signal, selectedRunId) : null,
    [debouncedQuery, selectedRunId],
  );

  useEffect(() => {
    if (
      (!selectedInstrument || !instruments.data?.items.some(item => item.instrument === selectedInstrument.instrument)) &&
      !debouncedQuery.trim() &&
      instruments.data?.items.length
    ) {
      onInstrumentChange(instruments.data.items[0]);
    }
  }, [
    selectedInstrument,
    debouncedQuery,
    instruments.data,
    onInstrumentChange,
  ]);

  useEffect(() => { setQuery(""); }, [selectedRunId]);
  useEffect(() => { setSelectedDate(requestedDate ?? ""); }, [selectedRunId, requestedDate]);

  const selectedRun =
    runs.data?.items.find((run) => run.id === selectedRunId) ?? null;
  const availableDates = selectedRun?.available_dates ?? (selectedRun ? [selectedRun.as_of] : []);
  const effectiveDate = availableDates.includes(selectedDate) ? selectedDate
    : requestedDate ? availableDates.filter(date => date <= requestedDate).at(-1) : selectedRun?.as_of;

  const scores = useApiResource(
    selectedRunId && selectedRun && selectedInstrument && effectiveDate
      ? (signal) =>
          api.getFactorScores(
            selectedRunId,
            {
              instrument: selectedInstrument.instrument,
              date: effectiveDate,
              offset: 0,
              limit: 1,
            },
            signal,
          )
      : null,
    [selectedRunId, selectedInstrument?.instrument, effectiveDate],
  );

  const history = useApiResource(
    selectedInstrument && selectedRun?.scheme
      ? (signal) =>
          api.getFactorHistory(
            selectedInstrument.instrument,
            selectedRun.scheme,
            signal,
            selectedRunId ?? undefined,
          )
      : null,
    [selectedInstrument?.instrument, selectedRun?.scheme, selectedRunId],
  );

  const score = scores.data?.items[0] ?? null;
  const historyItems = useMemo(() => effectiveDate
    ? (history.data?.items ?? []).filter(row => row.date <= effectiveDate) : [], [history.data, effectiveDate]);
  const metadata = scores.data?.metadata;
  const raw = score?.raw ?? null;
  const scoreValues = score?.scores ?? null;
  const metadataPositionNotes = toTextList(metadata?.position_note);
  const metadataLimitations = toTextList(metadata?.limitations);

  const factorKeys = useMemo(() => {
    const keys = new Set<string>();
    for (const key of Object.keys(scoreValues ?? {})) {
      if (key !== "composite") keys.add(key);
    }
    for (const key of Object.keys(raw ?? {})) {
      if (key !== "composite") keys.add(key);
    }
    return Array.from(keys);
  }, [raw, scoreValues]);

  const instrumentItems = instruments.data?.items ?? [];
  const pickerItems =
    selectedInstrument &&
    !instrumentItems.some(
      (item) => item.instrument === selectedInstrument.instrument,
    )
      ? [selectedInstrument, ...instrumentItems]
      : instrumentItems;

  if (runs.loading && !runs.data) {
    return (
      <section className="card">
        <LoadingPanel label="正在加载评分批次…" />
      </section>
    );
  }

  if (runs.error) {
    return (
      <section className="card">
        <ErrorPanel message={runs.error} onRetry={runs.reload} />
      </section>
    );
  }

  if (!runs.data?.items.length) {
    return (
      <section className="card">
        <EmptyPanel title="暂无评分批次">
          还没有保存的因子评分。请先运行因子评分流程，再刷新页面。
        </EmptyPanel>
      </section>
    );
  }

  if (selectedRunId && !selectedRun) {
    return <section className="card"><ErrorPanel message="指定任务的评分批次不存在或暂时不可读取；没有切换到其他批次。" onRetry={runs.reload} /></section>;
  }

  return (
    <div className="page-stack">
      <IndustryDiagnostics kind="factors" runId={selectedRunId} date={effectiveDate}/>
      {scores.data?.metadata ? <TemporalAuditPanel audit={scores.data.metadata.temporal_audit}/>:null}
      <section className="card">
        <div className="card-header">
          <div>
            <h2 className="card-title">查询条件</h2>
            <p className="card-sub">
              选择股票、计算批次与日期，查看同一方案下的评分。
            </p>
          </div>
          {selectedRun ? (
            selectedRun.is_synthetic ? (
              <span className="badge badge-synthetic">该批次为合成样本</span>
            ) : (
              <span className="badge">真实行情</span>
            )
          ) : null}
        </div>

        <div className="control-grid">
          <div className="field field-grow">
            <label htmlFor="factor-run">评分批次</label>
            <select
              id="factor-run"
              value={selectedRunId ?? ""}
              onChange={(event) => onRunChange(event.target.value)}
            >
              {!selectedRunId ? <option value="">请选择评分批次</option> : null}
              {runs.data.items.map((run) => (
                <option key={run.id} value={run.id}>
                  {run.name} · {run.as_of} ·{" "}
                  {run.is_synthetic ? "合成样本" : "真实行情"}
                </option>
              ))}
            </select>
          </div>

          <div className="field field-grow">
            <label htmlFor="score-date">评分日期</label>
            <select id="score-date" value={effectiveDate ?? ""} onChange={(event) => setSelectedDate(event.target.value)}>
              {[...availableDates].reverse().map((date) => <option key={date} value={date}>{date}</option>)}
            </select>
          </div>
          <div className="field field-grow">
            <label htmlFor="instrument-search">搜索股票</label>
            <input
              id="instrument-search"
              type="search"
              value={query}
              placeholder="输入代码或名称"
              autoComplete="off"
              onChange={(event) => setQuery(event.target.value)}
            />
          </div>

          <div className="field field-grow">
            <label htmlFor="instrument-select">股票</label>
            <select
              id="instrument-select"
              value={selectedInstrument?.instrument ?? ""}
              disabled={!pickerItems.length}
              onChange={(event) => {
                const next = pickerItems.find(
                  (item) => item.instrument === event.target.value,
                );
                if (next) onInstrumentChange(next);
              }}
            >
              {!selectedInstrument ? <option value="">请选择股票</option> : null}
              {pickerItems.map((item) => (
                <option key={item.instrument} value={item.instrument}>
                  {item.instrument} · {item.name}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div className="control-footnote">
          {instruments.loading
            ? "正在搜索股票…"
            : instruments.error
              ? `股票列表加载失败：${instruments.error}`
              : `找到 ${instruments.data?.total ?? 0} 只股票`}
          {instruments.error ? (
            <button
              type="button"
              className="text-button inline-retry"
              onClick={instruments.reload}
            >
              重试
            </button>
          ) : null}
        </div>
      </section>

      {selectedRun ? (
        <section className="card">
          <div className="card-header">
            <div>
              <h2 className="card-title">评分口径</h2>
              <p className="card-sub">
                分数与排名表示所选股票池内的相对位置。
              </p>
            </div>
            <div className="badge-row">
              <span className="badge">{selectedRun.scheme}</span>
              <span className="badge">数据版本 {selectedRun.data_version}</span>
            </div>
          </div>

          <div className="meta-strip">
            <MetaItem label="批次名称" value={selectedRun.name} />
            <MetaItem label="批次 ID" value={selectedRun.id} mono />
            <MetaItem label="评分日期" value={effectiveDate ?? selectedRun.as_of} />
            <MetaItem label="股票池" value={selectedRun.universe} />
            <MetaItem label="标的数" value={String(selectedRun.sample_size)} />
            <MetaItem label="方案" value={selectedRun.scheme} mono />
            <MetaItem label="数据版本" value={selectedRun.data_version} mono />
          </div>

          {metadata?.weights && Object.keys(metadata.weights).length ? (
            <div className="weight-row" role="group" aria-label="因子权重">
              <span className="weight-row-label">因子权重</span>
              {Object.entries(metadata.weights).map(([key, weight]) => (
                <span key={key} className="weight-chip">
                  {factorLabel(key)}
                  <strong>{formatWeight(weight)}</strong>
                </span>
              ))}
            </div>
          ) : null}

          {metadata?.direction ? (
            <p className="hint-callout">
              {metadata.direction}；分数越高表示越符合该因子方向的偏好，不代表上涨概率。
            </p>
          ) : null}

          {metadataPositionNotes.map((note, index) => (
            <p className="hint-callout" key={`position-note-${index}`}>
              {note}
            </p>
          ))}

          {metadataLimitations.length ? (
            <ul className="limitation-list">
              {metadataLimitations.map((item, index) => (
                <li key={`metadata-limitation-${index}`}>{item}</li>
              ))}
            </ul>
          ) : null}
        </section>
      ) : null}

      <section className="card">
        <div className="card-header">
          <div>
            <h2 className="card-title">个股因子明细</h2>
            <p className="card-sub">
              {selectedInstrument
                ? `${selectedInstrument.instrument} · ${selectedInstrument.name}`
                : "先选择一只股票"}
            </p>
          </div>
          <fieldset className="toggle-field">
            <legend>展示口径</legend>
            <div className="toggle-group">
              <label>
                <input
                  type="radio"
                  name="factor-display"
                  value="score"
                  checked={displayMode === "score"}
                  onChange={() => setDisplayMode("score")}
                />
                百分位得分
              </label>
              <label>
                <input
                  type="radio"
                  name="factor-display"
                  value="raw"
                  checked={displayMode === "raw"}
                  onChange={() => setDisplayMode("raw")}
                />
                原始值
              </label>
            </div>
          </fieldset>
        </div>

        {!selectedRunId ? (
          <EmptyPanel title="尚未选择评分批次">
            请先在上方选择评分批次。
          </EmptyPanel>
        ) : !selectedInstrument ? (
          <EmptyPanel title="尚未选择股票">
            使用上方搜索框或下拉列表选择股票后查看评分。
          </EmptyPanel>
        ) : scores.loading ? (
          <LoadingPanel label="正在加载个股评分…" />
        ) : scores.error ? (
          <ErrorPanel message={scores.error} onRetry={scores.reload} />
        ) : !score ? (
          <EmptyPanel title="该批次没有此股票的评分记录">
            请确认所选股票属于该评分批次的股票池，或切换其他批次。
          </EmptyPanel>
        ) : (
          <>
            <div className="stock-head">
              <div className="stock-identity">
                <span className="stock-code">{score.instrument}</span>
                <div>
                  <h3 className="stock-name">{score.name || selectedInstrument.name}</h3>
                  <p className="stock-sub">
                    {score.industry_status ? `历史行业：${score.industry_name ?? '未知/冲突'}` : score.industry ? `行业（下载时）：${score.industry}` : "行业：暂无资料"}
                    <span aria-hidden="true"> · </span>
                    评分日期：{score.date}
                  </p>
                </div>
              </div>
              <div className="summary-inline">
                <div className="summary-item">
                  <span className="summary-label">综合分</span>
                  <div className="summary-value-row">
                    <strong className="summary-value">
                      {formatNumber(score.composite, 2)}
                    </strong>
                    <span className="summary-unit">
                      百分位加权分
                    </span>
                  </div>
                </div>
                <div className="summary-item">
                  <span className="summary-label">排名</span>
                  <div className="summary-value-row">
                    <strong className="summary-value">
                      {formatNumber(score.rank, 0)}
                    </strong>
                    <span className="summary-unit">股票池内排名</span>
                  </div>
                </div>
              </div>
            </div>

            {factorKeys.length ? (
              <div className="factor-grid">
                {factorKeys.map((key) => {
                  const weight = metadata?.weights?.[key];
                  const value =
                    displayMode === "raw" ? raw?.[key] : scoreValues?.[key];
                  return (
                    <div className="factor-card" key={key}>
                      <div className="factor-card-head">
                        <span>{factorLabel(key, metadata?.factor_definitions)}</span>
                        {typeof weight === "number" ? (
                          <span className="weight-tag">
                            权重 {formatWeight(weight)}
                          </span>
                        ) : null}
                      </div>
                      <div className="factor-value">
                        {formatNumber(value, displayMode === "raw" ? 4 : 2)}
                      </div>
                      <div className="factor-unit">
                        {displayMode === "raw"
                          ? "原始因子值"
                          : scoreUnitLabel(metadata?.score_unit)}
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <EmptyPanel title="该股票没有可展示的因子值">
                当前记录中没有可用的因子值。
              </EmptyPanel>
            )}

            <p className="hint-text">
              综合分由各因子的百分位加权计算，不随原始值/得分切换而改变。
              分数越高表示越符合该因子方向的偏好，不代表上涨概率或买入建议。
            </p>
          </>
        )}
      </section>

      {scores.data?.metadata?.factor_definitions && !scores.loading && selectedInstrument ? <FactorDetails response={scores.data} score={score} instrument={selectedInstrument.instrument} /> : null}

      <section className="card">
        <div className="card-header">
          <div>
            <h2 className="card-title">因子历史走势</h2>
            <p className="card-sub">
              {selectedInstrument
                ? `股票 ${selectedInstrument.instrument} · 截至 ${effectiveDate ?? "—"} · 方案 ${selectedRun?.scheme ?? "—"}`
                : "选择股票后加载历史评分"}
            </p>
          </div>
          {history.data?.is_synthetic === true ? (
            <span className="badge badge-synthetic">合成样本</span>
          ) : history.data?.is_synthetic === false ? (
            <span className="badge">真实行情</span>
          ) : null}
        </div>

        {!selectedRunId ? (
          <EmptyPanel title="尚未选择评分批次" />
        ) : !selectedInstrument ? (
          <EmptyPanel title="尚未选择股票" />
        ) : !selectedRun?.scheme ? (
          <EmptyPanel title="缺少评分方案">
            该批次未提供 scheme，无法按方案查询历史评分。
          </EmptyPanel>
        ) : history.loading ? (
          <LoadingPanel label="正在加载历史评分…" />
        ) : history.error ? (
          <ErrorPanel message={history.error} onRetry={history.reload} />
        ) : !historyItems.length ? (
          <EmptyPanel title="暂无历史评分">
            此股票在所选方案下没有历史评分。
          </EmptyPanel>
        ) : (
          <>
            <FactorHistoryChart
              items={historyItems}
              scheme={selectedRun.scheme}
              scoreUnit={metadata?.score_unit}
              definitions={metadata?.factor_definitions}
            />
            <p className="chart-note">
              评分方案：<code>{selectedRun.scheme}</code>；
              当前图表口径：{scoreUnitLabel(metadata?.score_unit)}。
            </p>
          </>
        )}
      </section>
    </div>
  );
}

function toTextList(value: unknown): string[] {
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed ? [trimmed] : [];
  }
  if (Array.isArray(value)) {
    return value
      .filter((item): item is string => typeof item === "string")
      .map((item) => item.trim())
      .filter(Boolean);
  }
  return [];
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
      <div className={mono ? "meta-value meta-mono" : "meta-value"}>{value}</div>
    </div>
  );
}
