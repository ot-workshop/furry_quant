import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import { Pagination } from "../components/Pagination";
import { EmptyPanel, ErrorPanel, LoadingPanel } from "../components/StatePanels";
import { useApiResource, type ApiState } from "../hooks/useApiResource";
import type { FactorRunList, Instrument } from "../types";
import { factorLabel, formatNumber, scoreUnitLabel } from "../utils/format";

const PAGE_SIZE = 50;

interface FactorRankingPageProps {
  runs: ApiState<FactorRunList>;
  selectedRunId: string | null;
  onRunChange: (id: string) => void;
  onOpenStock: (instrument: Instrument) => void;
}

export function FactorRankingPage({
  runs,
  selectedRunId,
  onRunChange,
  onOpenStock,
}: FactorRankingPageProps) {
  const [sortBy, setSortBy] = useState("composite");
  const [descending, setDescending] = useState(true);
  const [offset, setOffset] = useState(0);

  useEffect(() => {
    setSortBy("composite");
    setDescending(true);
    setOffset(0);
  }, [selectedRunId]);

  const scores = useApiResource(
    selectedRunId
      ? (signal) =>
          api.getFactorScores(
            selectedRunId,
            {
              sort_by: sortBy,
              descending,
              offset,
              limit: PAGE_SIZE,
            },
            signal,
          )
      : null,
    [selectedRunId, sortBy, descending, offset],
  );

  const selectedRun =
    runs.data?.items.find((run) => run.id === selectedRunId) ?? null;
  const metadata = scores.data?.metadata;

  const factorKeys = useMemo(() => {
    const keys = new Set<string>();
    for (const item of scores.data?.items ?? []) {
      for (const key of Object.keys(item.scores ?? {})) {
        if (key !== "composite") keys.add(key);
      }
    }
    return Array.from(keys);
  }, [scores.data]);

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
          还没有保存的因子评分，请先运行因子计算流程。
        </EmptyPanel>
      </section>
    );
  }

  const sortOptions = ["composite", ...factorKeys.filter((key) => key !== "composite")];

  return (
    <div className="page-stack">
      <section className="card">
        <div className="card-header">
          <div>
            <h2 className="card-title">排名条件</h2>
            <p className="card-sub">
              在同一计算批次中，比较不同股票的因子表现。
            </p>
          </div>
          {selectedRun?.is_synthetic ? (
            <span className="badge badge-synthetic">该批次为合成样本</span>
          ) : selectedRun ? <span className="badge">真实行情</span> : null}
        </div>

        <div className="control-grid">
          <div className="field field-grow">
            <label htmlFor="ranking-run">评分批次</label>
            <select
              id="ranking-run"
              value={selectedRunId ?? ""}
              onChange={(event) => onRunChange(event.target.value)}
            >
              {!selectedRunId ? <option value="">请选择评分批次</option> : null}
              {runs.data.items.map((run) => (
                <option key={run.id} value={run.id}>
                  {run.name} · {run.as_of}
                </option>
              ))}
            </select>
          </div>

          <div className="field">
            <label htmlFor="ranking-sort">排序字段</label>
            <select
              id="ranking-sort"
              value={sortBy}
              onChange={(event) => {
                setSortBy(event.target.value);
                setOffset(0);
              }}
            >
              {sortOptions.map((key) => (
                <option key={key} value={key}>
                  {key === "composite" ? "综合分" : factorLabel(key)}
                </option>
              ))}
            </select>
          </div>

          <div className="field">
            <label htmlFor="ranking-direction">排序方向</label>
            <select
              id="ranking-direction"
              value={descending ? "desc" : "asc"}
              onChange={(event) => {
                setDescending(event.target.value === "desc");
                setOffset(0);
              }}
            >
              <option value="desc">从高到低</option>
              <option value="asc">从低到高</option>
            </select>
          </div>
        </div>

        {selectedRun ? (
          <div className="ranking-context">
            <span>日期 {selectedRun.as_of}</span>
            <span>{selectedRun.universe}</span>
            <span>标的数 {selectedRun.sample_size}</span>
            <span>方案 {selectedRun.scheme}</span>
          </div>
        ) : null}
      </section>

      <section className="card">
        <div className="card-header">
          <div>
            <h2 className="card-title">横截面排名</h2>
            <p className="card-sub">
              展示因子百分位得分。点击“查看股票”了解各项评分及历史变化。
            </p>
          </div>
          <span className="badge">{scoreUnitLabel(metadata?.score_unit)}</span>
        </div>

        {!selectedRunId ? (
          <EmptyPanel title="尚未选择评分批次">
            请先选择评分批次以加载横截面排名。
          </EmptyPanel>
        ) : scores.loading ? (
          <LoadingPanel label="正在加载排名…" />
        ) : scores.error ? (
          <ErrorPanel message={scores.error} onRetry={scores.reload} />
        ) : !scores.data?.items.length ? (
          <EmptyPanel title="暂无排名结果">
            当前批次和筛选条件下没有评分记录。
          </EmptyPanel>
        ) : (
          <>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col" className="numeric">
                      排名
                    </th>
                    <th scope="col">股票</th>
                    <th scope="col">行业</th>
                    {factorKeys.map((key) => (
                      <th scope="col" className="numeric" key={key}>
                        {factorLabel(key)}
                      </th>
                    ))}
                    <th scope="col" className="numeric">
                      综合分
                    </th>
                    <th scope="col">评分日期</th>
                    <th scope="col">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {scores.data.items.map((item) => (
                    <tr key={`${item.instrument}-${item.date}`}>
                      <td className="numeric rank-cell">
                        {formatNumber(item.rank, 0)}
                      </td>
                      <td>
                        <div className="cell-code">{item.instrument}</div>
                        <div className="cell-name">{item.name}</div>
                      </td>
                      <td>{item.industry || "—"}</td>
                      {factorKeys.map((key) => (
                        <td className="numeric" key={key}>
                          {formatNumber(item.scores?.[key], 2)}
                        </td>
                      ))}
                      <td className="numeric composite-cell">
                        {formatNumber(item.composite, 2)}
                      </td>
                      <td>{item.date}</td>
                      <td>
                        <button
                          type="button"
                          className="text-button"
                          onClick={() =>
                            onOpenStock({
                              instrument: item.instrument,
                              name: item.name,
                            })
                          }
                          aria-label={`查看 ${item.instrument} ${item.name} 的股票因子`}
                        >
                          查看股票
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {scores.data.total > PAGE_SIZE ? (
              <Pagination
                total={scores.data.total}
                offset={scores.data.offset}
                limit={scores.data.limit || PAGE_SIZE}
                onChange={setOffset}
                loading={scores.loading}
              />
            ) : (
              <p className="table-footnote">
                共 {scores.data.total.toLocaleString("zh-CN")} 条评分记录。
              </p>
            )}
          </>
        )}
      </section>
    </div>
  );
}
