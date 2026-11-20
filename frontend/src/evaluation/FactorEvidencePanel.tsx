import { useEffect, useState } from "react";
import {
  Alert,
  Collapse,
  DatePicker,
  Descriptions,
  Flex,
  InputNumber,
  Select,
  Table,
  Tag,
  Typography,
} from "antd";
import dayjs from "dayjs";
import { api } from "../api/client";
import { HorizontalIntervalChart } from "../components/research-charts";
import {
  EmptyState,
  ErrorState,
  Loading,
  MonoId,
  Section,
  Signed,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import type { EvaluationResult } from "../types";
import { factorLabel, formatNumber } from "../utils/format";
import { AnalysisDates, type AnalysisRange } from "./AnalysisDates";

const STATUS_COLORS: Record<string, string> = {
  ready: "success",
  no_valid_observations: "default",
  insufficient_observations: "warning",
  undefined_variance: "warning",
  missing_frozen_calendar: "error",
  no_signal_dates: "default",
};

function signedText(value: number | null, digits = 6): string {
  if (value === null || !Number.isFinite(value)) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(digits)}`;
}

export function FactorEvidencePanel({
  result,
  horizon,
}: {
  result: EvaluationResult;
  horizon: string;
}) {
  const defaultLag = Math.max(0, Math.min(251, Number(horizon) - 1));
  const [factor, setFactor] = useState(
    "composite" in result.factors
      ? "composite"
      : Object.keys(result.factors)[0],
  );
  const [lag, setLag] = useState<number>(defaultLag);
  const [range, setRange] = useState<AnalysisRange>(null);
  const [cutoff, setCutoff] = useState<string | null>(
    result.metadata.end || null,
  );

  useEffect(() => {
    setLag(Math.max(0, Math.min(251, Number(horizon) - 1)));
  }, [horizon]);

  const resource = useApiResource(
    (signal) =>
      api.evaluationEvidence(
        result.metadata.id,
        {
          horizon: Number(horizon),
          factor,
          lag,
          start: range?.[0],
          end: range?.[1],
          cutoff: cutoff ?? undefined,
        },
        signal,
      ),
    [result.metadata.id, horizon, factor, lag, range, cutoff],
  );

  const data = resource.data;
  const definitions = result.metadata.factor_definitions;
  const raw = data?.raw_availability;
  const computable =
    data?.status === "ready" &&
    data.rank_ic.mean !== null &&
    data.rank_ic.ci_low !== null &&
    data.rank_ic.ci_high !== null;

  return (
    <Flex vertical gap={16}>
      <Section
        title="证据体检筛选"
        description="按冻结信号交易日与截止日核查可用性、覆盖范围及 Newey–West 相关性调整区间（只读）。"
      >
        <Flex gap={16} wrap align="center">
          <Select
            aria-label="体检因子"
            value={factor}
            onChange={setFactor}
            style={{ minWidth: 180 }}
            options={Object.keys(result.factors).map((key) => ({
              value: key,
              label: factorLabel(key, definitions),
            }))}
          />
          <Flex gap={8} align="center">
            <Typography.Text>滞后阶数 L</Typography.Text>
            <InputNumber
              aria-label="滞后阶数"
              precision={0}
              min={0}
              max={251}
              value={lag}
              onChange={(value) => value !== null && setLag(Number(value))}
            />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              默认 {defaultLag}（h−1）
            </Typography.Text>
          </Flex>
          <AnalysisDates value={range} onChange={setRange} />
          <Flex gap={8} align="center">
            <Typography.Text>截止日</Typography.Text>
            <DatePicker
              aria-label="截止日期"
              value={cutoff ? dayjs(cutoff) : null}
              allowClear={false}
              onChange={(day) =>
                setCutoff(day ? day.format("YYYY-MM-DD") : null)
              }
            />
          </Flex>
        </Flex>
      </Section>

      {!data ? (
        resource.error ? (
          <ErrorState message={resource.error} onRetry={resource.reload} />
        ) : (
          <Loading rows={6} />
        )
      ) : (
        <>
          <Section
            title="1. 可用性与范围状态"
            extra={
              <Flex gap={8} wrap>
                <Tag color={STATUS_COLORS[data.status] ?? "default"}>
                  {data.status_label || "原因未提供"}
                </Tag>
                {data.lag.is_shorter_than_default ? (
                  <Tag color="warning">
                    短于默认滞后（L={data.lag.lag} &lt; h−1=
                    {data.lag.default_lag}）
                  </Tag>
                ) : null}
              </Flex>
            }
          >
            <Flex vertical gap={12}>
              <Alert
                type={data.status === "ready" ? "success" : "warning"}
                showIcon
                title={data.status_label || "原因未提供"}
                description={data.status_reason || "原因未提供"}
              />
              <Descriptions
                size="small"
                column={{ xs: 1, md: 3 }}
                items={[
                  {
                    key: "source",
                    label: "结果来源",
                    children: <MonoId value={result.metadata.id} />,
                  },
                  {
                    key: "signalRange",
                    label: "信号日期范围",
                    children:
                      data.range.start && data.range.end
                        ? `${data.range.start} ~ ${data.range.end}`
                        : "—",
                  },
                  {
                    key: "cutoff",
                    label: "截止日 / 最晚退出日",
                    children: `${data.range.cutoff} / ${data.range.latest_exit_date ?? "—"}`,
                  },
                  {
                    key: "lagRule",
                    label: "滞后与门槛",
                    children: `L=${data.lag.lag}（默认 ${data.lag.default_lag}）· 门槛 n ≥ ${data.lag.min_dates_required} 日`,
                  },
                  {
                    key: "common",
                    label: "共同样本证据",
                    children: data.common_sample.available
                      ? `${data.common_sample.label} (${data.common_sample.policy ?? "—"})`
                      : "未知",
                  },
                  ...(data.financials?.label
                    ? [
                        {
                          key: "financials",
                          label: "财务绑定状态",
                          children: `${data.financials.label}${data.financials.detail ? `：${data.financials.detail}` : ""}`,
                        },
                      ]
                    : []),
                ]}
              />
            </Flex>
          </Section>

          <Section
            title="2. 覆盖与样本摘要"
            description="交易日数量与股票·日数量按各自单位独立列示，不将填补评分当成真实观测。"
          >
            <Flex vertical gap={12}>
              <Descriptions
                size="small"
                column={{ xs: 1, md: 3 }}
                items={[
                  {
                    key: "signalDates",
                    label: "信号交易日",
                    children: `${data.counts.signal_dates} 交易日`,
                  },
                  {
                    key: "validIcDates",
                    label: "已成熟有效 Rank IC 日",
                    children: `${data.counts.valid_rank_ic_dates} / ${data.counts.signal_dates} 交易日`,
                  },
                  {
                    key: "unmatured",
                    label: "未成熟 / 缺退出日",
                    children: `${data.counts.unmatured_dates} / ${data.counts.missing_exit_date_dates} 交易日`,
                  },
                  {
                    key: "lagPairs",
                    label: "有效滞后配对数",
                    children: `${data.counts.lag_pairs} 对（k=1..${data.lag.lag}）`,
                  },
                  {
                    key: "rawCovRange",
                    label: "可用性记录覆盖",
                    children: raw?.available
                      ? `${raw.matched_signal_dates ?? "—"} / ${raw.total_signal_dates ?? data.counts.signal_dates} 交易日（${raw.coverage_status === "complete" ? "完整" : "仅部分日期或字段完整度不足，以下为已匹配范围"}）`
                      : `未知（${raw?.reason ?? "原因未提供"}）`,
                  },
                  {
                    key: "rawObs",
                    label:
                      factor === "composite"
                        ? "合格评分 / 股票池"
                        : "真实原始观测",
                    children: raw?.available
                      ? factor === "composite"
                        ? `${raw.eligible_stock_days ?? "未知"} / ${raw.universe_stock_days ?? "未知"} 只·日`
                        : raw.raw_valid_stock_days !== null
                          ? `${raw.raw_valid_stock_days} 只·日（缺失 ${raw.raw_missing_stock_days ?? "未知"}）`
                          : "未知（旧结果未保存原始观测字段）"
                      : `未知（${raw?.reason ?? "原因未提供"}）`,
                  },
                  {
                    key: "imputed",
                    label: "填补评分数量",
                    children: raw?.available
                      ? raw.imputed_stock_days !== null
                        ? `${raw.imputed_stock_days} 只·日（不进入单因子 IC）`
                        : "未知（未保存填补字段）"
                      : "未知",
                  },
                  {
                    key: "labelCov",
                    label: "收益标签覆盖",
                    children: data.label_coverage
                      ? `${data.label_coverage.valid_stock_days} / ${data.label_coverage.scored_stock_days} 只·日`
                      : "未知",
                  },
                  {
                    key: "pairedObs",
                    label: "进入 IC 配对观测",
                    children: `${data.counts.paired_observations_on_valid_ic_days} 只·日（仅有效 IC 日）`,
                  },
                  {
                    key: "commonSample",
                    label: "共同样本",
                    children: data.common_sample.available
                      ? data.common_sample.label
                      : "未知",
                  },
                ]}
              />

              {factor === "composite" &&
              raw?.available &&
              raw.component_imputations.length ? (
                <Table
                  size="small"
                  rowKey="factor"
                  pagination={false}
                  dataSource={raw.component_imputations}
                  columns={[
                    {
                      title: "组成因子",
                      render: (_, row) => factorLabel(row.factor, definitions),
                    },
                    {
                      title: "真实观测（只·日）",
                      align: "right",
                      render: (_, row) => row.raw_valid_stock_days ?? "未知",
                    },
                    {
                      title: "缺失（只·日）",
                      align: "right",
                      render: (_, row) => row.raw_missing_stock_days ?? "未知",
                    },
                    {
                      title: "填补评分（只·日）",
                      align: "right",
                      render: (_, row) => row.imputed_stock_days ?? "未知",
                    },
                    {
                      title: "缺失处理",
                      render: (_, row) => row.missing_policy ?? "未知",
                    },
                  ]}
                />
              ) : null}

              {data.exclusions.ic.length || data.exclusions.labels.length ? (
                <Flex gap={16} wrap>
                  {data.exclusions.ic.length ? (
                    <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                      IC 排除（分母 {data.counts.signal_dates} 交易日）：
                      {data.exclusions.ic
                        .map((item) => `${item.label} ${item.count}`)
                        .join("；")}
                    </Typography.Text>
                  ) : null}
                  {data.exclusions.labels.length && data.label_coverage ? (
                    <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                      标签排除（分母 {data.label_coverage.scored_stock_days}{" "}
                      只·日）：
                      {data.exclusions.labels
                        .map((item) => `${item.label} ${item.count}`)
                        .join("；")}
                    </Typography.Text>
                  ) : null}
                </Flex>
              ) : null}
            </Flex>
          </Section>

          <Section
            title="3. Rank IC 均值与相关性调整区间"
            description="基于 Newey–West（Bartlett 核）标准误估计历史均值的 95% 近似区间；先确认可用状态再解读区间。"
          >
            <Flex vertical gap={12}>
              <Descriptions
                size="small"
                column={{ xs: 1, md: 3 }}
                items={[
                  {
                    key: "mean",
                    label: "Rank IC 均值",
                    children: (
                      <Signed
                        value={data.rank_ic.mean}
                        format={(v) => signedText(v, 6)}
                      />
                    ),
                  },
                  {
                    key: "interval",
                    label: "95% 区间（下界 ~ 上界）",
                    children: computable ? (
                      <Flex gap={4} wrap>
                        <span style={{ whiteSpace: "nowrap" }}>
                          [{signedText(data.rank_ic.ci_low, 6)},
                        </span>
                        <span style={{ whiteSpace: "nowrap" }}>
                          {signedText(data.rank_ic.ci_high, 6)}]
                        </span>
                      </Flex>
                    ) : (
                      "—"
                    ),
                  },
                  {
                    key: "se",
                    label: "Newey–West SE",
                    children: computable
                      ? formatNumber(data.rank_ic.se, 6)
                      : "—",
                  },
                  {
                    key: "method",
                    label: "估计方法与滞后",
                    children: `Newey–West / Bartlett · L=${data.lag.lag} · n=${data.counts.valid_rank_ic_dates}`,
                  },
                  {
                    key: "conf",
                    label: "置信水平与修正",
                    children: `95%（z=${data.lag.z_critical.toFixed(6)}）· 有限样本分母 ${data.lag.finite_sample_correction}`,
                  },
                ]}
              />

              {computable ? (
                <>
                  <HorizontalIntervalChart
                    mean={data.rank_ic.mean!}
                    ciLow={data.rank_ic.ci_low!}
                    ciHigh={data.rank_ic.ci_high!}
                    ariaLabel={`${factorLabel(factor, definitions)} ${horizon}日 Rank IC 均值 ${signedText(data.rank_ic.mean, 6)}，95% 区间 [${signedText(data.rank_ic.ci_low, 6)}, ${signedText(data.rank_ic.ci_high, 6)}]`}
                  />
                  <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                    按冻结信号交易日位置计算，缺口不计作观测；区间反映该方法下历史有效日均值的估计不确定性，不代表未来收益或因果。
                  </Typography.Text>
                </>
              ) : (
                <EmptyState
                  title="当前条件不出区间"
                  description={data.status_reason || "原因未提供"}
                />
              )}

              <Collapse
                size="small"
                items={[
                  {
                    key: "method",
                    label: "方法细节、缺口处理与使用限制",
                    children: (
                      <Flex vertical gap={6}>
                        <Typography.Text style={{ fontSize: 13 }}>
                          1.
                          仅纳入截止日（cutoff）前已记录退出日且退出日不晚于截止日的有限
                          Rank IC；无退出日记录的旧观察不推定为已成熟。
                        </Typography.Text>
                        <Typography.Text style={{ fontSize: 13 }}>
                          2.
                          均值为有效且已成熟日值的等权平均；协方差按冻结信号交易日位置对齐，缺失或未成熟日仅在自协方差求和中记
                          z=0，不计作样本量 n。
                        </Typography.Text>
                        <Typography.Text style={{ fontSize: 13 }}>
                          3. Bartlett 权重为 1−k/(L+1)，有限样本标准误为
                          sqrt(S/[n(n−1)])，区间为 均值 ± 1.959964 ×
                          SE；切换不同滞后 L
                          仅作稳健性对照，不代表独立多次检验。
                        </Typography.Text>
                      </Flex>
                    ),
                  },
                ]}
              />
            </Flex>
          </Section>
        </>
      )}
    </Flex>
  );
}
