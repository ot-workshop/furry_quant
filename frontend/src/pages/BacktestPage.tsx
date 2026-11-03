import { useEffect, useMemo, useState } from "react";
import {IndustryDiagnostics} from '../components/IndustryDiagnostics';
import {TemporalAuditPanel} from '../components/TemporalAuditPanel';
import { api } from "../api/client";
import { DrawdownChart } from "../components/DrawdownChart";
import { EquityChart } from "../components/EquityChart";
import { Pagination } from "../components/Pagination";
import { PerformanceSummary } from "../components/PerformanceSummary";
import {ExecutionEvents} from '../components/ExecutionEvents';
import {TradingCostSummary,COST_LABELS} from '../components/TradingCostSummary';
import { EmptyPanel, ErrorPanel, LoadingPanel } from "../components/StatePanels";
import { useApiResource } from "../hooks/useApiResource";
import {
  formatDateTime,
  formatMetricValue,
  formatNumber,
  formatWeight,
  statusClass,
  statusLabel,
  tradeSideLabel,
} from "../utils/format";

const PAGE_SIZE = 50;

interface BacktestPageProps {
  selectedBacktestId: string | null;
  onSelectBacktest: (id: string) => void;
}

export function BacktestPage({
  selectedBacktestId,
  onSelectBacktest,
}: BacktestPageProps) {
  const [positionDate, setPositionDate] = useState<string | null>(null);
  const [positionOffset, setPositionOffset] = useState(0);
  const [tradeOffset, setTradeOffset] = useState(0);

  const list = useApiResource((signal) => api.getBacktests(signal), []);

  useEffect(() => {
    const items = list.data?.items;
    if (!items?.length) return;
    if (
      !selectedBacktestId
    ) {
      onSelectBacktest(items[0].id);
    }
  }, [list.data, selectedBacktestId, onSelectBacktest]);

  useEffect(() => {
    setPositionDate(null);
    setPositionOffset(0);
    setTradeOffset(0);
  }, [selectedBacktestId]);

  const detail = useApiResource(
    selectedBacktestId
      ? (signal) => api.getBacktest(selectedBacktestId, signal)
      : null,
    [selectedBacktestId],
  );

  const equity = useApiResource(
    selectedBacktestId
      ? (signal) => api.getBacktestEquity(selectedBacktestId, signal)
      : null,
    [selectedBacktestId],
  );

  const equityItems = equity.data?.items ?? [];
  const equityDates = useMemo(
    () => equityItems.map((item) => item.date),
    [equityItems],
  );

  useEffect(() => {
    if (!equityDates.length) {
      setPositionDate(null);
      return;
    }
    if (!positionDate || !equityDates.includes(positionDate)) {
      setPositionDate(equityDates[equityDates.length - 1]);
    }
  }, [equityDates, positionDate]);

  const positions = useApiResource(
    selectedBacktestId && positionDate
      ? (signal) =>
          api.getBacktestPositions(
            selectedBacktestId,
            {
              date: positionDate,
              offset: positionOffset,
              limit: PAGE_SIZE,
            },
            signal,
          )
      : null,
    [selectedBacktestId, positionDate, positionOffset],
  );

  const trades = useApiResource(
    selectedBacktestId
      ? (signal) =>
          api.getBacktestTrades(
            selectedBacktestId,
            { offset: tradeOffset, limit: PAGE_SIZE },
            signal,
          )
      : null,
    [selectedBacktestId, tradeOffset],
  );

  const configEntries = detail.data?.config
    ? Object.entries(detail.data.config)
    : [];
  const detailExtras = detail.data
    ? (detail.data as unknown as BacktestExtras)
    : null;
  const positionNotes = Array.from(
    new Set([
      ...toTextList(detailExtras?.position_note),
      ...toTextList(detailExtras?.metadata?.position_note),
    ]),
  );
  const limitations = Array.from(
    new Set([
      ...toTextList(detail.data?.limitations),
      ...toTextList(detailExtras?.metadata?.limitations),
    ]),
  );

  return (
    <div className="page-stack">
      <section className="card">
        <div className="card-header">
          <div>
            <h2 className="card-title">回测列表</h2>
            <p className="card-sub">
              选择一次运行，查看净值、持仓与参数。
            </p>
          </div>
          {list.data ? (
            <span className="badge">
              共 {list.data.total.toLocaleString("zh-CN")} 次运行
            </span>
          ) : null}
        </div>

        {list.loading && !list.data ? (
          <LoadingPanel label="正在加载回测列表…" />
        ) : list.error ? (
          <ErrorPanel message={list.error} onRetry={list.reload} />
        ) : !list.data?.items.length ? (
          <EmptyPanel title="暂无回测结果">
            还没有保存的回测，请先运行回测流程，再刷新页面。
          </EmptyPanel>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col">名称</th>
                  <th scope="col">策略</th>
                  <th scope="col">区间</th>
                  <th scope="col">状态</th>
                  <th scope="col" className="numeric">
                    总收益
                  </th>
                  <th scope="col" className="numeric">
                    最大回撤
                  </th>
                  <th scope="col">创建时间</th>
                  <th scope="col">操作</th>
                </tr>
              </thead>
              <tbody>
                {list.data.items.map((item) => (
                  <tr
                    key={item.id}
                    className={item.id === selectedBacktestId ? "selected-row" : ""}
                  >
                    <td>
                      <div className="cell-name">{item.name}</div>
                      <div className="cell-sub">
                        {item.is_synthetic ? "合成样本" : "真实行情"} · {item.id}
                      </div>
                    </td>
                    <td>{item.strategy}</td>
                    <td className="nowrap">
                      {item.start_date} → {item.end_date}
                    </td>
                    <td>
                      <span className={`status-pill ${statusClass(item.status)}`}>
                        {statusLabel(item.status)}
                      </span>
                    </td>
                    <td className="numeric">
                      {formatMetricValue(
                        "total_return",
                        item.metrics?.total_return,
                      )}
                    </td>
                    <td className="numeric">
                      {formatMetricValue(
                        "max_drawdown",
                        item.metrics?.max_drawdown,
                      )}
                    </td>
                    <td className="nowrap">{formatDateTime(item.created_at)}</td>
                    <td>
                      <button
                        type="button"
                        className="text-button"
                        onClick={() => onSelectBacktest(item.id)}
                        aria-label={`查看回测 ${item.name}`}
                      >
                        {item.id === selectedBacktestId ? "当前查看" : "查看"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {!selectedBacktestId ? (
        list.data?.items.length ? (
          <section className="card">
            <EmptyPanel title="请选择一次回测" />
          </section>
        ) : null
      ) : detail.loading ? (
        <section className="card">
          <LoadingPanel label="正在加载回测详情…" />
        </section>
      ) : detail.error ? (
        <section className="card">
          <ErrorPanel message={detail.error} onRetry={detail.reload} />
        </section>
      ) : detail.data ? (
        <>
          <section className="card">
            <div className="card-header">
              <div>
                <div className="detail-title-row">
                  <h2 className="card-title detail-title">{detail.data.name}</h2>
                  <span
                    className={`status-pill ${statusClass(detail.data.status)}`}
                  >
                    {statusLabel(detail.data.status)}
                  </span>
                  {detail.data.is_synthetic ? (
                    <span className="badge badge-synthetic">合成样本</span>
                  ) : (
                    <span className="badge">真实行情</span>
                  )}
                </div>
                <p className="card-sub">
                  {detail.data.strategy} · {detail.data.start_date} 至{" "}
                  {detail.data.end_date}
                </p>
              </div>
              <div className="card-actions">
                <a
                  className="button button-primary"
                  href={api.downloadUrl(detail.data.id)}
                  download={`backtest-${detail.data.id}.json`}
                >
                  下载结果 JSON
                </a>
              </div>
            </div>

            {detail.data.status !== "succeeded" ? (
              <p className="status-note">
                该运行状态为“{statusLabel(detail.data.status)}”。下方展示已保存的数据，
                未完成的结果可能不完整。
              </p>
            ) : null}

            <div className="meta-strip">
              <MetaItem label="运行 ID" value={detail.data.id} mono />
              <MetaItem label="策略" value={detail.data.strategy} />
              <MetaItem label="起始日期" value={detail.data.start_date} />
              <MetaItem label="结束日期" value={detail.data.end_date} />
              <MetaItem label="引擎" value={detail.data.engine || "—"} mono />
              <MetaItem
                label="引擎版本"
                value={detail.data.engine_version || "—"}
                mono
              />
              <MetaItem
                label="数据版本"
                value={detail.data.data_version || "—"}
                mono
              />
              <MetaItem
                label="创建时间"
                value={formatDateTime(detail.data.created_at)}
              />
            </div>

            <div className="section-divider" />

            <PerformanceSummary detail={detail.data} />
          </section>

          <IndustryDiagnostics kind="backtests" runId={selectedBacktestId} date={positionDate}/>
          <TemporalAuditPanel audit={detail.data.temporal_audit}/>
          <section className="card">
            <div className="card-header">
              <div>
                <h2 className="card-title">净值、基准与回撤</h2>
                <p className="card-sub">
                  从相同初始净值出发，对比策略与基准的表现。
                </p>
              </div>
            </div>

            {equity.loading ? (
              <LoadingPanel label="正在加载净值序列…" />
            ) : equity.error ? (
              <ErrorPanel message={equity.error} onRetry={equity.reload} />
            ) : !equityItems.length ? (
              <EmptyPanel title="暂无净值数据">
                该回测尚无可用净值。
              </EmptyPanel>
            ) : (
              <>
                <EquityChart items={equityItems} />
                <h3 className="subheading chart-heading">回撤</h3>
                <DrawdownChart items={equityItems} />
                <p className="chart-note">
                  回撤表示净值相对此前最高点的跌幅；拖动缩放条可查看局部区间。
                </p>
              </>
            )}
          </section>

          <section className="card">
            <div className="card-header">
              <div>
                <h2 className="card-title">持仓</h2>
                <p className="card-sub">
                  选择日期，查看当日收盘后的模拟持仓数量、权重与模拟账户现金。
                </p>
              </div>
              <div className="field field-inline">
                <label htmlFor="position-date">持仓日期</label>
                <select
                  id="position-date"
                  value={positionDate ?? ""}
                  disabled={!equityDates.length}
                  onChange={(event) => {
                    setPositionDate(event.target.value || null);
                    setPositionOffset(0);
                  }}
                >
                  {[...equityDates].reverse().map((date) => (
                    <option key={date} value={date}>
                      {date}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            {!positionDate ? (
              <EmptyPanel title="暂无可查询的持仓日期">
                请先确认该回测已返回净值序列。
              </EmptyPanel>
            ) : positions.loading ? (
              <LoadingPanel label="正在加载持仓…" />
            ) : positions.error ? (
              <ErrorPanel message={positions.error} onRetry={positions.reload} />
            ) : !positions.data?.items.length ? (
              <EmptyPanel title="该日期无持仓记录">
                模拟账户在所选日期没有股票持仓。
              </EmptyPanel>
            ) : (
              <>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th scope="col">股票</th>
                        <th scope="col">日期</th>
                        <th scope="col" className="numeric">
                          数量
                        </th>
                        <th scope="col" className="numeric">
                          价格
                        </th>
                        <th scope="col" className="numeric">
                          权重
                        </th>
                        <th scope="col" className="numeric">
                          现金
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {positions.data.items.map((item, index) => (
                        <tr key={`${item.date}-${item.instrument}-${index}`}>
                          <td>
                            <div className="cell-code">{item.instrument}</div>
                            <div className="cell-name">{item.name}</div>
                          </td>
                          <td>{item.date}</td>
                          <td className="numeric">
                            {formatNumber(item.amount, 2)}
                          </td>
                          <td className="numeric">
                            {formatNumber(item.price, 4)}
                          </td>
                          <td className="numeric">
                            {formatWeight(item.weight)}
                          </td>
                          <td className="numeric">
                            {formatNumber(item.cash, 2)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <Pagination
                  total={positions.data.total}
                  offset={positions.data.offset}
                  limit={positions.data.limit || PAGE_SIZE}
                  onChange={setPositionOffset}
                  loading={positions.loading}
                />
              </>
            )}

            {positionNotes.length ? (
              <p className="chart-note">
                持仓口径：{positionNotes.join("；")}
              </p>
            ) : null}
          </section>

          <section className="card">
            <div className="card-header">
              <div>
                <h2 className="card-title">成交记录</h2>
                <p className="card-sub">
                  查看本次回测模拟成交的数量、价格与费用。
                </p>
              </div>
            </div>

            {trades.loading ? (
              <LoadingPanel label="正在加载成交记录…" />
            ) : trades.error ? (
              <ErrorPanel message={trades.error} onRetry={trades.reload} />
            ) : !trades.data?.items.length ? (
              <EmptyPanel title="暂无成交记录">
                该回测尚无成交记录。
              </EmptyPanel>
            ) : (
              <>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th scope="col">日期</th>
                        <th scope="col">股票</th>
                        <th scope="col">方向</th>
                        <th scope="col" className="numeric">
                          数量
                        </th>
                        <th scope="col" className="numeric">
                          价格
                        </th>
                        <th scope="col" className="numeric">
                          金额
                        </th>
                        <th scope="col" className="numeric">
                          费用
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {trades.data.items.map((item, index) => (
                        <tr key={`${item.date}-${item.instrument}-${index}`}>
                          <td>{item.date}</td>
                          <td>
                            <div className="cell-code">{item.instrument}</div>
                            {item.name ? (
                              <div className="cell-name">{item.name}</div>
                            ) : null}
                          </td>
                          <td>
                            <span
                              className={`side-pill side-${item.side === "buy" ? "buy" : item.side === "sell" ? "sell" : "other"}`}
                            >
                              {tradeSideLabel(item.side)}
                            </span>
                          </td>
                          <td className="numeric">
                            {formatNumber(item.amount, 2)}
                          </td>
                          <td className="numeric">
                            {formatNumber(item.price, 4)}
                          </td>
                          <td className="numeric">
                            {formatNumber(item.value, 2)}
                          </td>
                          <td className="numeric">
                            {formatNumber(item.cost, 2)}
                            {item.cost_breakdown ? <details><summary>分项</summary>{COST_LABELS.filter(([key])=>key!=='total').map(([key,label])=><p key={key}>{label}：{formatNumber(item.cost_breakdown![key],2)}元</p>)}</details> : null}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <Pagination
                  total={trades.data.total}
                  offset={trades.data.offset}
                  limit={trades.data.limit || PAGE_SIZE}
                  onChange={setTradeOffset}
                  loading={trades.loading}
                />
              </>
            )}
          </section>

          {detail.data ? <TradingCostSummary detail={detail.data}/> : null}
          {detail.data?.strategy==='HistoricalIndexTopkStrategy' && selectedBacktestId ? <ExecutionEvents key={selectedBacktestId} runId={selectedBacktestId} summary={detail.data.execution_summary}/> : null}
          <section className="card">
            <div className="card-header">
              <div>
                <h2 className="card-title">运行参数与限制</h2>
                <p className="card-sub">
                  查看本次运行所用配置与模拟假设。
                </p>
              </div>
            </div>

            <div className="detail-grid">
              <div>
                <h3 className="subheading">运行参数</h3>
                {configEntries.length ? (
                  <dl className="config-list">
                    {configEntries.map(([key, value]) => (
                      <div className="config-row" key={key}>
                        <dt>{key}</dt>
                        <dd>
                          <ConfigValue value={value} />
                        </dd>
                      </div>
                    ))}
                  </dl>
                ) : (
                  <p className="muted-text">没有保存运行参数。</p>
                )}
              </div>
              <div>
                <h3 className="subheading">已知限制</h3>
                {limitations.length ? (
                  <ul className="limitation-list">
                    {limitations.map((item, index) => (
                      <li key={`${item}-${index}`}>{item}</li>
                    ))}
                  </ul>
                ) : (
                  <p className="muted-text">没有补充说明。</p>
                )}
              </div>
            </div>
          </section>
        </>
      ) : null}
    </div>
  );
}

interface BacktestExtras {
  position_note?: unknown;
  metadata?: {
    position_note?: unknown;
    limitations?: unknown;
  } | null;
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

function ConfigValue({ value }: { value: unknown }) {
  if (value === null || value === undefined) {
    return <span className="muted-text">—</span>;
  }

  if (Array.isArray(value)) {
    if (!value.length) return <span className="muted-text">[]</span>;
    const primitiveOnly = value.every(
      (entry) => entry === null || typeof entry !== "object",
    );
    if (primitiveOnly) {
      return (
        <span>{value.map((entry) => formatConfigPrimitive(entry)).join("、")}</span>
      );
    }
    return <pre>{JSON.stringify(value, null, 2)}</pre>;
  }

  if (typeof value === "object") {
    return <pre>{JSON.stringify(value, null, 2)}</pre>;
  }

  return <span>{formatConfigPrimitive(value)}</span>;
}

function formatConfigPrimitive(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value) ?? "—";
}
