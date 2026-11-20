import { useState } from "react";
import {
  Alert,
  Collapse,
  DatePicker,
  Descriptions,
  Flex,
  Grid,
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
  const [lagOverride, setLagOverride] = useState<{
    horizon: string;
    lag: number;
  } | null>(null);
  const lag =
    lagOverride && lagOverride.horizon === horizon
      ? lagOverride.lag
      : defaultLag;
  const [range, setRange] = useState<AnalysisRange>(null);
  const [cutoff, setCutoff] = useState<string | null>(
    result.metadata.end || null,
  );

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

  const screens = Grid.useBreakpoint();
  const compact =
    screens.sm === false ||
    (typeof window !== "undefined" && window.innerWidth < 576);
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
              onChange={(value) =>
                value !== null &&
                setLagOverride({ horizon, lag: Number(value) })
              }
            />
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              默认 {defaultLag}（h−1）
            </Typography.Text>
          </Flex>
          <Flex gap={8} align="center" wrap>
            <Typography.Text>信号区间</Typography.Text>
            <AnalysisDates
              value={range}
              onChange={setRange}
              compact={compact}
            />
          </Flex>
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
                layout={compact ? "vertical" : "horizontal"}
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
                      data.range.start && data.range.end ? (
                        <Flex gap={4} wrap>
                          <span style={{ whiteSpace: "nowrap" }}>
                            {data.range.start}
                          </span>
                          <span>~</span>
                          <span style={{ whiteSpace: "nowrap" }}>
                            {data.range.end}
                          </span>
                        </Flex>
                      ) : (
                        "—"
                      ),
                  },
                  {
                    key: "cutoff",
                    label: "截止日 / 最晚退出日",
                    children: (
                      <Flex gap={4} wrap>
                        <span style={{ whiteSpace: "nowrap" }}>
                          {data.range.cutoff}
                        </span>
                        <span>/</span>
                        <span style={{ whiteSpace: "nowrap" }}>
                          {data.range.latest_exit_date ?? "—"}
                        </span>
                      </Flex>
                    ),
                  },
                  {
                    key: "lagRule",
                    label: "滞后与门槛",
                    children: `L=${data.lag.lag}（默认 ${data.lag.default_lag}）· 门槛 n ≥ ${data.lag.min_dates_required} 日`,
                  },
                  {
                    key: "common",
                    label: "共同样本证据",
                    span: compact ? 1 : 2,
                    children: data.common_sample.available
                      ? `${data.common_sample.label} (${data.common_sample.policy ?? "—"})`
                      : "未知",
                  },
                ]}
              />
              {data.financials?.status_label || data.financials?.label ? (
                <Descriptions
                  size="small"
                  layout={compact ? "vertical" : "horizontal"}
                  column={1}
                  items={[
                    {
                      key: "financials",
                      label: "财务绑定状态",
                      children: (
                        <Flex gap={6} wrap align="center">
                          {data.financials.status_label ? (
                            <Tag
                              color={
                                data.financials.status === "guarded"
                                  ? "success"
                                  : data.financials.status === "missing_binding"
                                    ? "warning"
                                    : "default"
                              }
                            >
                              {data.financials.status_label}
                            </Tag>
                          ) : null}
                          <span>
                            {data.financials.detail || data.financials.label}
                          </span>
                        </Flex>
                      ),
                    },
                  ]}
                />
              ) : null}
            </Flex>
          </Section>

          <Section
            title="2. 覆盖与样本摘要"
            description="交易日数量与股票·日数量按各自单位独立列示，不将填补评分当成真实观测。"
          >
            <Flex vertical gap={12}>
              <Descriptions
                size="small"
                layout={compact ? "vertical" : "horizontal"}
                column={{ xs: 1, md: 3 }}
                items={[
                  {
                    key: "signalDates",
                    label: "信号交易日",
                    children:
                      data.counts.signal_dates !== null
                        ? `${data.counts.signal_dates} 交易日`
                        : "未知（缺少冻结日历）",
                  },
                  {
                    key: "validIcDates",
                    label: "已成熟有效 Rank IC 日",
                    children:
                      data.counts.signal_dates !== null &&
                      data.counts.valid_rank_ic_dates !== null
                        ? `${data.counts.valid_rank_ic_dates} / ${data.counts.signal_dates} 交易日`
                        : "未知",
                  },
                  {
                    key: "unmatured",
                    label: "未成熟 / 缺退出日 / 异常",
                    children:
                      data.counts.unmatured_dates !== null
                        ? `${data.counts.unmatured_dates} / ${data.counts.missing_exit_date_dates ?? "未知"} / ${data.counts.invalid_exit_date_dates ?? 0} 交易日`
                        : "未知",
                  },
                  {
                    key: "lagPairs",
                    label: "有效滞后配对数",
                    children:
                      data.counts.lag_pairs !== null
                        ? data.lag.lag === 0
                          ? "无滞后配对（L=0，常规样本标准误）"
                          : `${data.counts.lag_pairs} 对（k=1..${data.lag.lag}）`
                        : "未知",
                  },
                  {
                    key: "rawCovRange",
                    label: "可用性记录覆盖",
                    children: raw?.available
                      ? `${raw.matched_signal_dates ?? "—"} / ${raw.total_signal_dates ?? data.counts.signal_dates ?? "—"} 交易日（${raw.coverage_status === "complete" ? "完整" : "仅部分匹配，非完整区间不汇总总数"}）`
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
                        ? raw.eligible_stock_days !== null &&
                          raw.universe_stock_days !== null
                          ? `${raw.eligible_stock_days} / ${raw.universe_stock_days} 只·日`
                          : "未知（仅部分日期或字段完整度不足）"
                        : raw.raw_valid_stock_days !== null
                          ? `${raw.raw_valid_stock_days} 只·日（缺失 ${raw.raw_missing_stock_days ?? "未知"}）`
                          : "未知（未保存或非全区间字段）"
                      : `未知（${raw?.reason ?? "原因未提供"}）`,
                  },
                  {
                    key: "imputed",
                    label: "填补评分数量",
                    children:
                      factor === "composite"
                        ? raw?.available
                          ? "见下方组成因子分表（不适用与填补分列）"
                          : "未知"
                        : raw?.available
                          ? raw.imputed_stock_days !== null
                            ? `${raw.imputed_stock_days} 只·日（不进入单因子 IC）`
                            : "未知（未保存或非全区间填补字段）"
                          : "未知",
                  },
                  {
                    key: "notApplicable",
                    label: "行业不适用",
                    children:
                      factor === "composite"
                        ? raw?.available
                          ? "见下方组成因子分表"
                          : "未知"
                        : raw?.available &&
                            raw.not_applicable_stock_days !== null
                          ? `${raw.not_applicable_stock_days} 只·日（赋分 ${raw.not_applicable_scored_stock_days ?? "未知"}）`
                          : "未知",
                  },
                  {
                    key: "labelCov",
                    label: "收益标签覆盖（原评估截止日）",
                    children: data.label_coverage
                      ? data.label_coverage.valid_stock_days !== null &&
                        data.label_coverage.scored_stock_days !== null
                        ? `${data.label_coverage.valid_stock_days} / ${data.label_coverage.scored_stock_days} 只·日（信号区间内按原评估截止日；当前 cutoff 仅约束 Rank IC）`
                        : `未知（已记录 ${data.label_coverage.matched_signal_dates ?? 0} / ${data.label_coverage.total_signal_dates ?? data.counts.signal_dates ?? "—"} 日）`
                      : "未知",
                  },
                  {
                    key: "pairedObs",
                    label: "进入 IC 配对观测",
                    children: (() => {
                      const po = data.counts.paired_observations;
                      if (!po) {
                        return data.counts
                          .paired_observations_on_valid_ic_days !== null
                          ? `${data.counts.paired_observations_on_valid_ic_days} 只·日（仅有效 IC 日）`
                          : "未知";
                      }
                      if (
                        po.valid_ic_dates === null ||
                        po.recorded_dates === null
                      ) {
                        return "未知（缺少冻结信号日历）";
                      }
                      const rangeLabel =
                        po.min !== null && po.max !== null
                          ? `每日 ${po.min}–${po.max} 只`
                          : "逐日范围未知";
                      const totalLabel =
                        po.total !== null
                          ? `合计 ${po.total} 只·日`
                          : "合计未知";
                      return `${totalLabel}（${rangeLabel}；已记录 ${po.recorded_dates}/${po.valid_ic_dates} 有效日）`;
                    })(),
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
                  scroll={{ x: "max-content" }}
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
                      title: "行业不适用（只·日）",
                      align: "right",
                      render: (_, row) =>
                        row.not_applicable_stock_days !== undefined &&
                        row.not_applicable_stock_days !== null
                          ? `${row.not_applicable_stock_days}（赋分 ${row.not_applicable_scored_stock_days ?? "未知"}）`
                          : "未知",
                    },
                    {
                      title: "缺失处理",
                      render: (_, row) => row.missing_policy ?? "未知",
                    },
                  ]}
                />
              ) : null}

              {data.exclusions.ic.length ||
              (data.label_coverage &&
                (data.exclusions.labels.length ||
                  data.label_coverage.exclusions_complete === false)) ? (
                <Flex gap={16} wrap>
                  {data.exclusions.ic.length ? (
                    <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                      IC 排除（分母 {data.counts.signal_dates ?? "未知"}{" "}
                      交易日）：
                      {data.exclusions.ic
                        .map((item) => `${item.label} ${item.count}`)
                        .join("；")}
                    </Typography.Text>
                  ) : null}
                  {data.label_coverage &&
                  (data.exclusions.labels.length ||
                    data.label_coverage.exclusions_complete === false) ? (
                    <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                      {data.exclusions.labels.length
                        ? `标签排除（按原评估截止日，分母 ${data.label_coverage.scored_stock_days ?? "未知"} 只·日）：${data.exclusions.labels
                            .map((item) => `${item.label} ${item.count}`)
                            .join("；")}`
                        : `标签排除（按原评估截止日）：未知（部分日或原因明细未完整保存，已记录 ${data.label_coverage.matched_signal_dates ?? 0}/${data.label_coverage.total_signal_dates ?? data.counts.signal_dates ?? "—"} 日）`}
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
                layout={compact ? "vertical" : "horizontal"}
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
                      <Flex vertical gap={2}>
                        <span style={{ whiteSpace: "nowrap" }}>
                          下界：{signedText(data.rank_ic.ci_low, 6)}
                        </span>
                        <span style={{ whiteSpace: "nowrap" }}>
                          上界：{signedText(data.rank_ic.ci_high, 6)}
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
                    children: `Newey–West / Bartlett · L=${data.lag.lag} · n=${data.counts.valid_rank_ic_dates ?? "未知"}`,
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
                          1. 仅纳入截止日（cutoff）前已记录合法 YYYY-MM-DD
                          退出日且退出日不晚于截止日的有限 Rank
                          IC；有入场日记录时同时校验 信号日 ≤ 入场日 ≤
                          退出日，缺入场日时校验 信号日 ≤
                          退出日，无退出日或时序异常不推定为已成熟。
                        </Typography.Text>
                        <Typography.Text style={{ fontSize: 13 }}>
                          2.
                          均值为有效且已成熟日值的等权平均；协方差按冻结信号交易日位置对齐，缺失或未成熟日仅在自协方差求和中记
                          z=0，不计作样本量 n（L=0
                          时无滞后配对，退化为常规样本标准误）。
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
