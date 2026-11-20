import { RollingAnalysis } from "../evaluation/RollingAnalysis";
import { CorrelationAnalysis } from "../evaluation/CorrelationAnalysis";
import { FactorEvidencePanel } from "../evaluation/FactorEvidencePanel";
import { useState } from "react";
import {
  Button,
  Card,
  Descriptions,
  Flex,
  Segmented,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import { DownloadOutlined, FileTextOutlined } from "@ant-design/icons";
import { api } from "../api/client";
import { paths } from "../app/routes";
import { TemporalAuditPanel } from "../components/audit";
import { GroupReturnsChart } from "../components/charts";
import {
  EmptyState,
  ErrorState,
  Loading,
  MonoId,
  Notes,
  PageHeader,
  Section,
  Signed,
  TableScroll,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import type {
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
} from "../utils/format";

export default function EvaluationDetailPage({ id }: { id: string }) {
  const detail = useApiResource((signal) => api.evaluation(id, signal), [id]);
  if (detail.data === null)
    return detail.error ? (
      <>
        <PageHeader
          title="因子评估"
          back={{ to: paths.evaluations(), label: "全部评估" }}
        />
        <ErrorState message={detail.error} onRetry={detail.reload} />
      </>
    ) : (
      <Loading rows={10} />
    );
  return <EvaluationView result={detail.data} />;
}

function EvaluationView({ result }: { result: EvaluationResult }) {
  const { metadata, methodology } = result;
  const [horizon, setHorizon] = useState(
    String(metadata.evaluation.horizons[0] ?? ""),
  );
  const definitions = metadata.factor_definitions;
  const factors = Object.entries(result.factors);

  return (
    <>
      <PageHeader
        back={{ to: paths.evaluations(), label: "全部评估" }}
        title={metadata.name}
        subtitle={`${metadata.start} ~ ${metadata.end} · ${metadata.sample_size} 只股票 · ${formatDateTime(metadata.created_at)}`}
        tags={
          metadata.is_synthetic ? (
            <Tag color="warning">合成样本</Tag>
          ) : undefined
        }
        extra={
          <>
            <Button
              icon={<FileTextOutlined />}
              href={api.evaluationReportUrl(metadata.id)}
            >
              Markdown 报告
            </Button>
            <Button
              icon={<DownloadOutlined />}
              href={api.evaluationDownloadUrl(metadata.id)}
            >
              JSON
            </Button>
          </>
        }
      />
      <Card>
        <Flex gap={16} align="center" wrap>
          <Typography.Text type="secondary">持有期</Typography.Text>
          <Segmented
            value={horizon}
            onChange={(value) => setHorizon(String(value))}
            options={metadata.evaluation.horizons.map((h) => ({
              value: String(h),
              label: `${h} 个交易日`,
            }))}
          />
          <Typography.Text type="secondary" style={{ fontSize: 13 }}>
            T 日收盘后评分，T+1 开盘入场，持有 h 日后开盘离场
          </Typography.Text>
        </Flex>
      </Card>
      <Tabs
        items={[
          {
            key: "factors",
            label: "因子诊断",
            children: (
              <Flex vertical gap={16}>
                <Section
                  title="IC 汇总"
                  description="各因子在所选持有期的信息系数；IR 为均值 / 标准差，未年化"
                >
                  <TableScroll label="因子 IC 汇总表">
                    <Table<DiagnosticRow>
                      size="small"
                      rowKey="key"
                      pagination={false}
                      dataSource={factors.flatMap(([key, value]) => {
                        const diagnostic = value.horizons[horizon];
                        return diagnostic ? [{ key, diagnostic }] : [];
                      })}
                      columns={[
                        {
                          title: "因子",
                          render: (_, row) => factorLabel(row.key, definitions),
                        },
                        ...icColumns<DiagnosticRow>(
                          "IC",
                          (row) => row.diagnostic.summary.ic,
                        ),
                        ...icColumns<DiagnosticRow>(
                          "Rank IC",
                          (row) => row.diagnostic.summary.rank_ic,
                        ),
                        {
                          title: "高减低",
                          align: "right" as const,
                          render: (_: unknown, row: DiagnosticRow) => (
                            <Signed
                              value={row.diagnostic.summary.mean_high_minus_low}
                              format={(v) => formatPercent(v)}
                            />
                          ),
                        },
                      ]}
                    />
                  </TableScroll>
                </Section>
                <div className="grid-2">
                  {factors.map(([key, value]) => {
                    const diagnostic = value.horizons[horizon];
                    return (
                      <Section
                        key={key}
                        title={`${factorLabel(key, definitions)} · 分组平均收益`}
                        description="Q1 为方向对齐后的低分组"
                      >
                        {diagnostic?.groups.length ? (
                          <GroupReturnsChart groups={diagnostic.groups} />
                        ) : (
                          <EmptyState title="没有分组收益" />
                        )}
                      </Section>
                    );
                  })}
                </div>
              </Flex>
            ),
          },
          {
            key: "evidence",
            label: "证据体检",
            children: <FactorEvidencePanel result={result} horizon={horizon} />,
          },
          {
            key: "rolling",
            label: "时序分析",
            children: <RollingAnalysis result={result} horizon={horizon} />,
          },
          {
            key: "correlation",
            label: "因子相关性",
            children: <CorrelationAnalysis result={result} horizon={horizon} />,
          },
          {
            key: "coverage",
            label: "覆盖情况",
            children: <Coverage result={result} horizon={horizon} />,
          },
          {
            key: "method",
            label: "方法与口径",
            children: (
              <Flex vertical gap={16}>
                <Section title="方法">
                  <Descriptions
                    size="small"
                    column={1}
                    items={[
                      {
                        key: "engine",
                        label: "IC 计算",
                        children: methodology.ic_engine,
                      },
                      {
                        key: "label",
                        label: "收益标签",
                        children: methodology.label,
                      },
                      {
                        key: "range",
                        label: "信号区间",
                        children: methodology.signal_range,
                      },
                      {
                        key: "groups",
                        label: "分组",
                        children: methodology.groups,
                      },
                      {
                        key: "stats",
                        label: "统计口径",
                        children: methodology.statistics,
                      },
                      {
                        key: "factor",
                        label: "因子批次",
                        children: <MonoId value={metadata.factor_run_id} />,
                      },
                      {
                        key: "data",
                        label: "数据版本",
                        children: <MonoId value={metadata.data_version} />,
                      },
                    ]}
                  />
                  <Notes
                    defaultOpen
                    items={methodology.limitations}
                    title="已知限制"
                  />
                </Section>
                <TemporalAuditPanel audit={metadata.temporal_audit} />
              </Flex>
            ),
          },
        ]}
      />
    </>
  );
}

interface DiagnosticRow {
  key: string;
  diagnostic: FactorDiagnostic;
}

function icColumns<T>(label: string, pick: (row: T) => ICStats) {
  return [
    {
      title: `${label} 均值`,
      align: "right" as const,
      render: (_: unknown, row: T) => formatNumber(pick(row).mean, 4),
    },
    {
      title: `${label} IR`,
      align: "right" as const,
      render: (_: unknown, row: T) => formatNumber(pick(row).ir, 3),
    },
    {
      title: `${label} >0 占比`,
      align: "right" as const,
      render: (_: unknown, row: T) =>
        formatPercent(pick(row).positive_fraction, 0),
    },
  ];
}

const EXCLUSIONS: Record<string, string> = {
  outside_evaluation_end: "后续交易日不足",
  missing_entry_or_exit: "缺少入场或离场价格",
  non_trading_entry_or_exit: "入场或离场日停牌",
};

function Coverage({
  result,
  horizon,
}: {
  result: EvaluationResult;
  horizon: string;
}) {
  const label: LabelDiagnostic | undefined = result.labels[horizon];
  return (
    <div className="grid-2">
      <Section
        title="信号覆盖"
        description={`${result.signal_coverage.length} 个信号日`}
      >
        <TableScroll label="信号覆盖表">
          <Table
            size="small"
            rowKey="signal_date"
            dataSource={result.signal_coverage}
            pagination={{ pageSize: 10, hideOnSinglePage: true, size: "small" }}
            columns={[
              { title: "信号日", dataIndex: "signal_date" },
              { title: "入场日", dataIndex: "entry_date" },
              {
                title: "已评分 / 股票池",
                align: "right",
                render: (_, row) =>
                  `${row.scored_instruments} / ${row.universe}`,
              },
            ]}
          />
        </TableScroll>
      </Section>
      <Section
        title={`收益标签（${horizon} 日）`}
        description={
          label
            ? `有效 ${label.valid_observations} · 排除 ${label.excluded_observations}`
            : undefined
        }
        extra={
          result.tables?.[`labels/${horizon}/observations`] ? (
            <Button
              size="small"
              href={api.evaluationLabelsUrl(result.metadata.id, horizon)}
            >
              下载明细
            </Button>
          ) : undefined
        }
      >
        {label ? (
          <Table
            size="small"
            rowKey="date"
            dataSource={label.coverage}
            pagination={{ pageSize: 10, hideOnSinglePage: true, size: "small" }}
            columns={[
              { title: "日期", dataIndex: "date" },
              {
                title: "有效 / 已评分",
                align: "right",
                render: (_, row) => `${row.valid_labels} / ${row.scored}`,
              },
              {
                title: "排除原因",
                render: (_, row) =>
                  Object.entries(row.exclusions)
                    .filter(([, count]) => count > 0)
                    .map(
                      ([reason, count]) =>
                        `${EXCLUSIONS[reason] ?? reason} ${count}`,
                    )
                    .join("；") || "—",
              },
            ]}
          />
        ) : (
          <EmptyState title="没有该持有期的标签" />
        )}
      </Section>
    </div>
  );
}
