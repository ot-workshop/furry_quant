import { useState } from "react";
import {
  Alert,
  Descriptions,
  Flex,
  InputNumber,
  Segmented,
  Select,
  Table,
  Typography,
} from "antd";
import { api } from "../api/client";
import { ICSeriesChart } from "../components/research-charts";
import { EmptyState, ErrorState, Loading, Section } from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import type { EvaluationResult, RollingICPoint } from "../types";
import { factorLabel, formatNumber } from "../utils/format";
import { AnalysisDates, type AnalysisRange } from "./AnalysisDates";

export function RollingAnalysis({
  result,
  horizon,
}: {
  result: EvaluationResult;
  horizon: string;
}) {
  const [factor, setFactor] = useState(
    "composite" in result.factors
      ? "composite"
      : Object.keys(result.factors)[0],
  );
  const [window, setWindow] = useState(60);
  const [minimum, setMinimum] = useState(60);
  const [range, setRange] = useState<AnalysisRange>(null);
  const resource = useApiResource(
    (signal) =>
      api.evaluationAnalysis(
        result.metadata.id,
        {
          factor,
          horizon: Number(horizon),
          window,
          min_periods: minimum,
          start: range?.[0],
          end: range?.[1],
        },
        signal,
      ),
    [result.metadata.id, factor, horizon, window, minimum, range],
  );
  const changeWindow = (value: number) => {
    setMinimum(minimum === window ? value : Math.min(minimum, value));
    setWindow(value);
  };
  const data = resource.data;
  return (
    <Flex vertical gap={16}>
      <Section
        title="时序分析"
        description="回看窗口按信号交易日计算；持有期由上方选择，两者独立。"
      >
        <Flex gap={16} wrap align="center">
          <Select
            aria-label="分析因子"
            value={factor}
            onChange={setFactor}
            style={{ minWidth: 180 }}
            options={Object.keys(result.factors).map((key) => ({
              value: key,
              label: factorLabel(key, result.metadata.factor_definitions),
            }))}
          />
          <Typography.Text>回看窗口</Typography.Text>
          <Segmented
            aria-label="滚动窗口预设"
            value={window}
            onChange={(value) => changeWindow(Number(value))}
            options={[20, 60, 120].map((value) => ({
              value,
              label: `${value} 日`,
            }))}
          />
          <InputNumber
            aria-label="滚动窗口交易日"
            precision={0}
            min={2}
            max={2520}
            value={window}
            onChange={(value) => value !== null && changeWindow(Number(value))}
          />
          <Typography.Text>最少有效日期</Typography.Text>
          <InputNumber
            aria-label="最少有效日期"
            precision={0}
            min={2}
            max={window}
            value={minimum}
            onChange={(value) => value !== null && setMinimum(Number(value))}
          />
          <AnalysisDates value={range} onChange={setRange} />
        </Flex>
      </Section>
      <Alert
        type="info"
        showIcon
        title="横轴为信号日；IC 使用之后实现的收益，仅用于历史诊断。"
        description="滚动 IR 为均值 / 样本标准差，未年化。多日标签可能重叠；统计实际可知日期见明细，不代表信号日已经知道未来收益。"
      />
      {!data ? (
        resource.error ? (
          <ErrorState message={resource.error} onRetry={resource.reload} />
        ) : (
          <Loading rows={8} />
        )
      ) : !data.series.length ? (
        <EmptyState title="该日期范围没有诊断记录" />
      ) : (
        <>
          <Section title="所选区间汇总">
            <Descriptions
              size="small"
              column={{ xs: 1, md: 3 }}
              items={[
                {
                  key: "ic",
                  label: "IC 均值",
                  children: formatNumber(data.summary.ic.mean, 4),
                },
                {
                  key: "ric",
                  label: "Rank IC 均值",
                  children: formatNumber(data.summary.rank_ic.mean, 4),
                },
                {
                  key: "valid",
                  label: "有效日期",
                  children: data.summary.rank_ic.valid_dates,
                },
                {
                  key: "ir",
                  label: "IC IR",
                  children: formatNumber(data.summary.ic.ir, 3),
                },
                {
                  key: "rir",
                  label: "Rank IC IR",
                  children: formatNumber(data.summary.rank_ic.ir, 3),
                },
                {
                  key: "window",
                  label: "滚动口径",
                  children: `${data.window} 日窗口 / 至少 ${data.min_periods} 个有效日期`,
                },
              ]}
            />
          </Section>
          <div className="grid-2">
            {(
              [
                ["daily", "每日 IC / Rank IC"],
                ["mean", "滚动均值"],
                ["ir", "滚动 IR（未年化）"],
                ["cumulative", "累计 IC（非策略收益）"],
              ] as const
            ).map(([metric, title]) => (
              <Section key={metric} title={title}>
                <ICSeriesChart
                  points={data.series}
                  metric={metric}
                  group={`${result.metadata.id}-ic-analysis`}
                />
              </Section>
            ))}
          </div>
          <Section
            title="诊断明细"
            description="缺失日期占用滚动窗口；样本不足或标准差为零时 IR 为空。选择的展示范围之前的记录仍可用于滚动预热。"
          >
            <Table<RollingICPoint>
              size="small"
              rowKey="date"
              dataSource={data.series}
              pagination={{ pageSize: 10, showSizeChanger: false }}
              scroll={{ x: 950 }}
              columns={[
                { title: "信号日", dataIndex: "date" },
                { title: "股票数", dataIndex: "observations" },
                ...(
                  [
                    "ic",
                    "rank_ic",
                    "ic_mean",
                    "rank_ic_mean",
                    "ic_ir",
                    "rank_ic_ir",
                  ] as const
                ).map((key, index) => ({
                  title: [
                    "IC",
                    "Rank IC",
                    "滚动 IC",
                    "滚动 Rank IC",
                    "IC IR",
                    "Rank IC IR",
                  ][index],
                  render: (_: unknown, point: RollingICPoint) =>
                    formatNumber(point[key], 4),
                })),
                { title: "窗口有效日期", dataIndex: "rank_ic_count" },
                {
                  title: "最早可知日期",
                  render: (_, point) => point.available_on ?? "—",
                },
              ]}
            />
          </Section>
        </>
      )}
    </Flex>
  );
}
