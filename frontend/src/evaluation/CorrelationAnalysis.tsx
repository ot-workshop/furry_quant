import { FactorSelectionPanel } from "./FactorSelectionPanel";
import { useEffect, useState } from "react";
import { Button, Flex, Select, Table, Typography } from "antd";
import { api } from "../api/client";
import {
  CorrelationHeatmap,
  CorrelationHistoryChart,
} from "../components/research-charts";
import {
  EmptyState,
  ErrorState,
  Loading,
  Section,
  TableScroll,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import { correlationMatrixStats } from "../research/chart-options";
import type {
  CorrelationCell,
  EvaluationAnalysis,
  EvaluationResult,
} from "../types";
import { factorLabel, formatNumber } from "../utils/format";
import { AnalysisDates, type AnalysisRange } from "./AnalysisDates";

export function CorrelationAnalysis({
  result,
  horizon,
}: {
  result: EvaluationResult;
  horizon: string;
}) {
  const names = Object.keys(result.factors).filter(
    (key) => key !== "composite",
  );
  const [pair, setPair] = useState<[string, string]>([
    names[0],
    names[1] ?? names[0],
  ]);
  const [range, setRange] = useState<AnalysisRange>(null);
  const contextKey = `${result.metadata.id}:${horizon}:${range?.[0] ?? ""}:${range?.[1] ?? ""}`;
  const resource = useApiResource(
    names.length
      ? (signal) =>
          api.evaluationAnalysis(
            result.metadata.id,
            {
              horizon: Number(horizon),
              factor: names[0],
              first: pair[0],
              second: pair[1],
              start: range?.[0],
              end: range?.[1],
            },
            signal,
          )
      : null,
    [result.metadata.id, horizon, pair, range],
  );
  const [retained, setRetained] = useState<{
    contextKey: string;
    data: EvaluationAnalysis;
  } | null>(null);
  const [lastAction, setLastAction] = useState<{
    requestKey: string;
    first: string;
    second: string;
    switched: boolean;
  } | null>(null);

  useEffect(() => {
    if (resource.data) {
      setRetained({ contextKey, data: resource.data });
    }
  }, [contextKey, resource.data]);

  const labels = Object.fromEntries(
    names.map((key) => [
      key,
      factorLabel(key, result.metadata.factor_definitions),
    ]),
  );
  const displayData =
    resource.data ??
    (retained?.contextKey === contextKey ? retained.data : null);
  const pairLoading = Boolean(
    displayData && !resource.data && resource.refreshing,
  );
  const pairError =
    displayData && !resource.data && !resource.refreshing
      ? resource.error
      : null;
  const currentRequestKey = `${contextKey}|${pair[0]}|${pair[1]}`;
  const activeAction =
    lastAction?.requestKey === currentRequestKey ? lastAction : null;
  const activeTargetLabel = activeAction
    ? `${labels[activeAction.first] ?? activeAction.first} 与 ${labels[activeAction.second] ?? activeAction.second}`
    : null;
  const statusMessage = activeAction
    ? resource.refreshing
      ? `正在加载 ${activeTargetLabel} 历史相关性…`
      : resource.error
        ? `加载 ${activeTargetLabel} 历史相关性失败：${resource.error}`
        : resource.data
          ? activeAction.switched
            ? `已切换历史相关性至 ${activeTargetLabel}`
            : `当前已显示 ${activeTargetLabel} 历史相关性`
          : null
    : null;

  function selectPair(first: string, second: string) {
    const isSame = pair[0] === first && pair[1] === second;
    setLastAction({
      requestKey: `${contextKey}|${first}|${second}`,
      first,
      second,
      switched: !isSame || Boolean(resource.error),
    });
    if (isSame) {
      if (resource.error && !resource.refreshing) {
        resource.reload();
      }
      return;
    }
    setPair([first, second]);
  }

  const matrixNames = displayData?.correlations.names.length
    ? displayData.correlations.names
    : names;
  const stats = displayData
    ? correlationMatrixStats(matrixNames, displayData.correlations.cells)
    : null;
  return (
    <Flex vertical gap={16}>
      <Section
        title="因子相关性"
        description="方向对齐后每日横截面 Spearman 相关的平均值；行业中性化开启时使用处理后的值。与因子对未来收益的 IC 分开解释。"
      >
        <Flex gap={12} align="center" wrap>
          <AnalysisDates value={range} onChange={setRange} />
          <Typography.Text type="secondary">
            色阶固定为 −1 至 1（0
            为中间浅色）；斜纹/点阵格「—」表示无法计算相关性（有效日期不足或因子无变化），不计入色阶。点击有效单元格查看因子对。
          </Typography.Text>
        </Flex>
      </Section>
      {!names.length ? (
        <EmptyState title="没有可分析的因子" />
      ) : !displayData ? (
        resource.error ? (
          <ErrorState message={resource.error} onRetry={resource.reload} />
        ) : (
          <Loading rows={8} />
        )
      ) : (
        <>
          <Section title="平均秩相关矩阵">
            {stats?.allUncomputable ? (
              <EmptyState
                title="当前条件下所有因子均无法计算相关性"
                description="所选区间内各因子缺乏足够有效交叉日期，或因子值在区间内没有变化，请调整评估区间后重试。"
              />
            ) : (
              <>
                {stats && stats.uncomputableFactors.length > 0 ? (
                  <Typography.Text
                    type="secondary"
                    style={{ display: "block", marginBottom: 8 }}
                  >
                    {`${matrixNames.length} 个因子中 ${stats.uncomputableFactors.length} 个（${stats.uncomputableFactors.map((key) => labels[key] ?? key).join("、")}）无法计算相关性，其行列不参与比较`}
                  </Typography.Text>
                ) : null}
                <CorrelationHeatmap
                  names={matrixNames}
                  cells={displayData.correlations.cells}
                  labels={labels}
                  onSelect={(first, second) => selectPair(first, second)}
                />
                <Typography.Text
                  type="secondary"
                  style={{ fontSize: 12, display: "block", marginTop: 8 }}
                >
                  斜纹/点阵虚线格「—」表示无法计算相关性；0.00
                  为有效相关系数（中间浅色）。
                </Typography.Text>
              </>
            )}
          </Section>
          <FactorSelectionPanel
            key={`${result.metadata.id}:${horizon}:${JSON.stringify(range)}`}
            result={result}
            horizon={horizon}
            range={range}
          />
          <Section title="因子对历史相关性">
            <Flex gap={12} wrap style={{ marginBottom: 16 }}>
              {([0, 1] as const).map((index) => (
                <Select
                  key={index}
                  aria-label={index ? "相关性因子二" : "相关性因子一"}
                  value={pair[index]}
                  style={{ minWidth: 170 }}
                  options={names.map((key) => ({
                    value: key,
                    label: labels[key],
                  }))}
                  onChange={(value) =>
                    selectPair(index ? pair[0] : value, index ? value : pair[1])
                  }
                />
              ))}
            </Flex>
            {pairLoading ? (
              <Loading rows={4} />
            ) : pairError ? (
              <ErrorState message={pairError} onRetry={resource.reload} />
            ) : displayData.correlations.pair.daily.some(
                (point) =>
                  point.rank_correlation !== null &&
                  Number.isFinite(point.rank_correlation),
              ) ? (
              <CorrelationHistoryChart
                points={displayData.correlations.pair.daily}
              />
            ) : (
              <EmptyState
                title="该因子对在所选范围无法计算相关性"
                description="所选区间内缺乏足够有效交叉日期，或其中因子在区间内没有变化。"
              />
            )}
          </Section>
          <Section
            title="相关性明细"
            description="平均绝对相关保留每天的相关强度，避免不同时期的正负相关互相抵消；可通过下方每行操作用键盘切换有效因子对历史。"
          >
            <div
              role="status"
              aria-live="polite"
              style={{ marginBottom: statusMessage ? 8 : 0 }}
            >
              {statusMessage ? (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {statusMessage}
                </Typography.Text>
              ) : null}
            </div>
            <TableScroll label="相关性明细表">
              <Table<CorrelationCell>
                size="small"
                aria-label="相关性明细表"
                rowKey={(item) => `${item.first}-${item.second}`}
                dataSource={displayData.correlations.cells}
                pagination={{ pageSize: 15, showSizeChanger: false }}
                columns={[
                  {
                    title: "因子对",
                    render: (_, item) =>
                      `${labels[item.first] ?? item.first} / ${labels[item.second] ?? item.second}`,
                  },
                  {
                    title: "平均秩相关",
                    render: (_, item) =>
                      item.value !== null && Number.isFinite(item.value) ? (
                        formatNumber(item.value, 4)
                      ) : (
                        <Flex vertical gap={2}>
                          <span>—</span>
                          <Typography.Text
                            type="secondary"
                            style={{ fontSize: 12 }}
                          >
                            无法计算相关系数（有效日期不足，或因子在区间内没有变化）
                          </Typography.Text>
                        </Flex>
                      ),
                  },
                  {
                    title: "平均绝对相关",
                    render: (_, item) => formatNumber(item.mean_absolute, 4),
                  },
                  { title: "有效日期", dataIndex: "valid_dates" },
                  {
                    title: "历史走势",
                    render: (_, item) => {
                      const firstLabel = labels[item.first] ?? item.first;
                      const secondLabel = labels[item.second] ?? item.second;
                      const isValid =
                        item.value !== null && Number.isFinite(item.value);
                      return isValid ? (
                        <Button
                          type="link"
                          size="small"
                          style={{ padding: 0, height: "auto" }}
                          onClick={() => selectPair(item.first, item.second)}
                          aria-label={`查看 ${firstLabel} 与 ${secondLabel} 历史相关性`}
                        >
                          查看历史
                        </Button>
                      ) : (
                        <Typography.Text
                          type="secondary"
                          style={{ fontSize: 12 }}
                        >
                          不可切换
                        </Typography.Text>
                      );
                    },
                  },
                ]}
              />
            </TableScroll>
          </Section>
        </>
      )}
    </Flex>
  );
}
