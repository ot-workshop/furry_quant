import { poolLabel } from "../utils/format";
import ReturnComparisonDetail from "./ReturnComparisonDetail";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Flex,
  Steps,
  Table,
  Tag,
  Typography,
} from "antd";
import {
  DownloadOutlined,
  FileTextOutlined,
  TrophyOutlined,
} from "@ant-design/icons";
import {
  experimentsApi,
  type ExperimentCandidate,
  type ExperimentPeriodName,
  type ExperimentResult,
  type ExperimentRow,
} from "../api/experiments";
import { paths } from "../app/routes";
import {
  AppLink,
  ErrorState,
  Loading,
  MonoId,
  Notes,
  PageHeader,
  Section,
  Signed,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import {
  factorLabel,
  formatDateTime,
  formatMoney,
  formatPercent,
  REBALANCE_LABELS,
} from "../utils/format";

const PERIODS: Array<{
  key: ExperimentPeriodName;
  label: string;
  note: string;
}> = [
  { key: "development", label: "开发段", note: "仅供观察，不参与选择" },
  { key: "validation", label: "验证段", note: "按收益差选出方案并锁定" },
  { key: "test", label: "测试段", note: "只运行锁定的方案" },
];

export default function ExperimentDetailPage({ id }: { id: string }) {
  const detail = useApiResource(
    (signal) => experimentsApi.get(id, signal),
    [id],
  );
  if (detail.data === null)
    return detail.error ? (
      <>
        <PageHeader
          title="方案比较"
          back={{ to: paths.experiments(), label: "全部方案比较" }}
        />
        <ErrorState message={detail.error} onRetry={detail.reload} />
      </>
    ) : (
      <Loading rows={10} />
    );
  if ("comparisons" in detail.data)
    return <ReturnComparisonDetail result={detail.data} />;
  return <ExperimentView result={detail.data} />;
}

function row(
  rows: ExperimentRow[],
  candidate: string,
  period: ExperimentPeriodName,
) {
  return (
    rows.find(
      (item) => item.candidate_id === candidate && item.period === period,
    ) ?? null
  );
}

function ExperimentView({ result }: { result: ExperimentResult }) {
  const { metadata, plan, selection, rows, shared_parameters: shared } = result;
  const chosen = plan.candidates.find((c) => c.id === selection.candidate_id);
  const test = row(rows, selection.candidate_id, "test");
  return (
    <>
      <PageHeader
        back={{ to: paths.experiments(), label: "全部方案比较" }}
        title={plan.name}
        subtitle={`创建于 ${formatDateTime(metadata.created_at)} · 选择于 ${formatDateTime(selection.locked_at)}`}
        tags={
          <Tag color="success" icon={<TrophyOutlined />}>
            选定：{chosen?.name ?? selection.candidate_id}
          </Tag>
        }
        extra={
          <>
            <Button
              icon={<FileTextOutlined />}
              href={experimentsApi.reportUrl(metadata.id)}
            >
              Markdown 报告
            </Button>
            <Button
              icon={<DownloadOutlined />}
              href={experimentsApi.downloadUrl(metadata.id)}
            >
              JSON
            </Button>
          </>
        }
      />
      {metadata.factor_selection ? (
        <Card title="因子组合筛选来源">
          <Descriptions
            column={{ xs: 1, md: 3 }}
            items={[
              {
                key: "source",
                label: "来源评估",
                children: (
                  <AppLink
                    to={paths.evaluation(
                      metadata.factor_selection.evaluation_id,
                    )}
                  >
                    {metadata.factor_selection.evaluation_id}
                  </AppLink>
                ),
              },
              {
                key: "cutoff",
                label: "推荐截止日",
                children: metadata.factor_selection.parameters.cutoff,
              },
              {
                key: "quality",
                label: "质量门槛",
                children: `Rank IC ≥ ${metadata.factor_selection.parameters.min_ic}；IR ≥ ${metadata.factor_selection.parameters.min_ir}`,
              },
              {
                key: "counts",
                label: "候选数量",
                children: `${metadata.factor_selection.stages.input} → 质量通过 ${metadata.factor_selection.stages.quality_passed} → MILP ${metadata.factor_selection.stages.milp_candidates}`,
              },
              {
                key: "corr",
                label: "相关性上限",
                children: metadata.factor_selection.parameters.max_correlation,
              },
              {
                key: "combo",
                label: "推荐组合",
                children: metadata.factor_selection.selected_keys
                  .map((key) => factorLabel(key))
                  .join("、"),
              },
            ]}
          />
        </Card>
      ) : null}
      <Card>
        <Steps
          current={3}
          items={PERIODS.map((period) => ({
            title: period.label,
            content: `${plan.periods[period.key].start} ~ ${plan.periods[period.key].end}`,
            subTitle: period.note,
          }))}
        />
      </Card>
      {selection.score <= 0 ? (
        <Alert
          type="warning"
          showIcon
          title="验证段没有方案跑赢基准"
          description="选定方案只是候选中收益差最高的一个，不代表已通过有效性检验。"
        />
      ) : null}
      <Section
        title="候选方案对比"
        description="收益均已扣除费用；每段从相同资金和空仓开始，不能拼接净值"
      >
        <Table<ExperimentCandidate>
          rowKey="id"
          pagination={false}
          dataSource={plan.candidates}
          scroll={{ x: "max-content" }}
          rowClassName={(candidate) =>
            candidate.id === selection.candidate_id
              ? "ant-table-row-selected"
              : ""
          }
          columns={[
            {
              title: "方案",
              fixed: "left",
              render: (_, candidate) => (
                <Flex vertical>
                  <Typography.Text strong>
                    {candidate.name}{" "}
                    {candidate.id === selection.candidate_id ? (
                      <TrophyOutlined />
                    ) : null}
                  </Typography.Text>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    {candidate.factors
                      .map(
                        (f) =>
                          `${factorLabel(f.name)}${f.window ? `${f.window}日` : ""} ${formatPercent(f.weight, 0)}`,
                      )
                      .join(" + ")}
                  </Typography.Text>
                </Flex>
              ),
            },
            ...PERIODS.flatMap((period) => [
              {
                title: `${period.label}收益`,
                align: "right" as const,
                render: (_: unknown, candidate: ExperimentCandidate) => {
                  const item = row(rows, candidate.id, period.key);
                  return item ? (
                    <AppLink to={paths.backtest(item.backtest_run_id)}>
                      <Signed
                        value={item.metrics.total_return}
                        format={(v) => formatPercent(v)}
                      />
                    </AppLink>
                  ) : (
                    <Typography.Text type="secondary">未运行</Typography.Text>
                  );
                },
              },
            ]),
            {
              title: "验证段收益差",
              align: "right",
              render: (_, candidate) => {
                const item = row(rows, candidate.id, "validation");
                return item ? (
                  <Signed
                    value={item.metrics.excess_return}
                    format={(v) => formatPercent(v)}
                  />
                ) : (
                  "—"
                );
              },
            },
            {
              title: "验证段回撤",
              align: "right",
              render: (_, candidate) =>
                formatPercent(
                  row(rows, candidate.id, "validation")?.metrics.max_drawdown,
                ),
            },
            {
              title: "验证段费用",
              align: "right",
              render: (_, candidate) =>
                formatMoney(
                  row(rows, candidate.id, "validation")?.metrics
                    .total_cost_amount,
                ),
            },
          ]}
        />
      </Section>
      <Section
        title="分段结果明细"
        description="各分段独立从空仓开始。股票池按当时资格变化；有效评分差异单独列出，测试段只运行已锁定方案。"
      >
        <Table<ExperimentRow>
          size="small"
          rowKey={(item) => `${item.candidate_id}-${item.period}`}
          dataSource={rows}
          pagination={false}
          scroll={{ x: 950 }}
          columns={[
            {
              title: "方案",
              render: (_, item) =>
                plan.candidates.find(
                  (candidate) => candidate.id === item.candidate_id,
                )?.name ?? item.candidate_id,
            },
            {
              title: "分段",
              render: (_, item) =>
                PERIODS.find((period) => period.key === item.period)?.label,
            },
            { title: "交易日", dataIndex: "trading_days" },
            {
              title: "逐日股票池范围",
              render: (_, item) =>
                item.coverage
                  ? `${item.coverage.min_pool}–${item.coverage.max_pool} 只`
                  : `${shared.codes.length} 只`,
            },
            {
              title: "逐日有效评分范围",
              render: (_, item) =>
                item.coverage
                  ? `${item.coverage.min_scored}–${item.coverage.max_scored} 只`
                  : "—",
            },
            {
              title: "有评分的决策日",
              render: (_, item) =>
                item.coverage
                  ? `${item.coverage.scored_days}/${item.coverage.signal_days}`
                  : "—",
            },
            {
              title: "结果",
              render: (_, item) => (
                <Flex gap={10} wrap>
                  <AppLink to={paths.backtest(item.backtest_run_id)}>
                    回测
                  </AppLink>
                  <AppLink to={paths.factors({ run: item.factor_run_id })}>
                    评分
                  </AppLink>
                  <AppLink to={paths.evaluation(item.evaluation_run_id)}>
                    评估
                  </AppLink>
                </Flex>
              ),
            },
          ]}
        />
      </Section>
      {test ? (
        <Section
          title="测试段（选定方案）"
          description="测试段结果不参与选择，仅用于复核"
        >
          <Descriptions
            size="small"
            column={{ xs: 1, md: 3 }}
            items={[
              {
                key: "ret",
                label: "总收益",
                children: (
                  <Signed
                    value={test.metrics.total_return}
                    format={(v) => formatPercent(v)}
                  />
                ),
              },
              {
                key: "bench",
                label: "基准收益",
                children: (
                  <Signed
                    value={test.metrics.benchmark_return}
                    format={(v) => formatPercent(v)}
                  />
                ),
              },
              {
                key: "excess",
                label: "收益差",
                children: (
                  <Signed
                    value={test.metrics.excess_return}
                    format={(v) => formatPercent(v)}
                  />
                ),
              },
              {
                key: "dd",
                label: "最大回撤",
                children: formatPercent(test.metrics.max_drawdown),
              },
              {
                key: "cost",
                label: "费用（元）",
                children: formatMoney(test.metrics.total_cost_amount),
              },
              {
                key: "turnover",
                label: "日均换手",
                children: formatPercent(test.metrics.avg_turnover),
              },
              {
                key: "bt",
                label: "回测",
                children: (
                  <AppLink to={paths.backtest(test.backtest_run_id)}>
                    查看回测
                  </AppLink>
                ),
              },
              {
                key: "ev",
                label: "评估",
                children: (
                  <AppLink to={paths.evaluation(test.evaluation_run_id)}>
                    查看因子评估
                  </AppLink>
                ),
              },
            ]}
          />
        </Section>
      ) : null}
      <Section title="共用参数">
        <Descriptions
          size="small"
          column={{ xs: 1, md: 3 }}
          items={[
            {
              key: "codes",
              label: "股票池",
              children:
                shared.universe_label ??
                (shared.universe
                  ? poolLabel(shared.universe)
                  : `${shared.codes.length} 只`),
            },
            { key: "benchmark", label: "基准", children: shared.benchmark },
            {
              key: "topk",
              label: "目标持股",
              children: `${shared.backtest.topk} 只`,
            },
            {
              key: "cash",
              label: "初始资金",
              children: `${shared.backtest.initial_cash.toLocaleString("zh-CN")} 元`,
            },
            {
              key: "freq",
              label: "调仓",
              children:
                REBALANCE_LABELS[
                  shared.backtest.rebalance_frequency ?? "daily"
                ],
            },
            {
              key: "impact",
              label: "冲击成本",
              children: formatPercent(shared.backtest.impact_cost, 3),
            },
            {
              key: "data",
              label: "数据版本",
              children: <MonoId value={metadata.data_version} />,
            },
          ]}
        />
        <Notes defaultOpen title="已知限制" items={result.limitations} />
      </Section>
    </>
  );
}
