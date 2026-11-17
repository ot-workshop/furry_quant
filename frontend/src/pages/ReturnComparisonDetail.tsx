import {
  Alert,
  Button,
  Card,
  Descriptions,
  Flex,
  Segmented,
  Space,
  Table,
  Typography,
} from "antd";
import { useState } from "react";
import { DownloadOutlined, FileTextOutlined } from "@ant-design/icons";
import {
  experimentsApi,
  type ReturnComparisonResult,
  type ReturnComparisonRow,
} from "../api/experiments";
import { api } from "../api/client";
import { paths } from "../app/routes";
import {
  AppLink,
  ErrorState,
  Loading,
  Notes,
  PageHeader,
  Signed,
} from "../components/ui";
import { Chart } from "../components/charts";
import { useApiResource } from "../hooks/useApiResource";
import {
  factorLabel,
  formatDateTime,
  formatMoney,
  formatNumber,
  formatPercent,
  REBALANCE_LABELS,
} from "../utils/format";
import { MISSING_POLICY_LABELS } from "../components/FactorMissingPolicy";

export default function ReturnComparisonDetail({
  result,
}: {
  result: ReturnComparisonResult;
}) {
  const { plan, rows } = result;
  const names = Object.fromEntries(plan.candidates.map((c) => [c.id, c.name]));
  const hasCommon = plan.comparison.sample_mode === "common";
  const [sample, setSample] = useState<"independent" | "common">(
    hasCommon ? "common" : "independent",
  );
  const points = useApiResource(
    async (signal) =>
      Promise.all(
        rows.map(async (row) => ({
          row,
          points: (await api.equity(row.backtest_run_id, signal)).items,
        })),
      ),
    [result.metadata.id],
  );
  const baselineCommon = rows.find(
    (row) =>
      row.candidate_id === plan.comparison.baseline_id &&
      row.sample === "common",
  );
  return (
    <>
      <PageHeader
        title={plan.name}
        subtitle={`收益来源对照 · 创建于 ${formatDateTime(result.metadata.created_at)}`}
        back={{ to: paths.experiments(), label: "全部方案比较" }}
        extra={
          <>
            <Button
              icon={<FileTextOutlined />}
              href={experimentsApi.reportUrl(result.metadata.id)}
            >
              Markdown 报告
            </Button>
            <Button
              icon={<DownloadOutlined />}
              href={experimentsApi.downloadUrl(result.metadata.id)}
            >
              JSON
            </Button>
          </>
        }
      />
      <Card title="对照配置">
        <Descriptions
          column={{ xs: 1, md: 3 }}
          items={[
            {
              key: "period",
              label: "研究区间",
              children: `${plan.periods.analysis.start} ~ ${plan.periods.analysis.end}`,
            },
            {
              key: "baseline",
              label: "基准方案",
              children: names[plan.comparison.baseline_id],
            },
            {
              key: "sample",
              label: "样本方式",
              children: hasCommon
                ? "各方案可用样本 + 每日共同样本"
                : "各方案可用样本",
            },
            {
              key: "cash",
              label: "初始资金",
              children: formatMoney(
                result.shared_parameters.backtest.initial_cash,
              ),
            },
            {
              key: "topk",
              label: "目标持股",
              children: `${result.shared_parameters.backtest.topk}只`,
            },
            {
              key: "rebalance",
              label: "调仓频率",
              children:
                REBALANCE_LABELS[
                  result.shared_parameters.backtest.rebalance_frequency ??
                    "daily"
                ],
            },
            {
              key: "benchmark",
              label: "评价基准",
              children: result.shared_parameters.benchmark,
            },
            {
              key: "fees",
              label: "费用口径",
              children:
                result.shared_parameters.backtest.cost_mode === "itemized"
                  ? `分项费用 · 佣金${formatPercent(result.shared_parameters.backtest.commission_rate, 3)}`
                  : `综合费率 · 买入${formatPercent(result.shared_parameters.backtest.open_cost, 3)} / 卖出${formatPercent(result.shared_parameters.backtest.close_cost, 3)}`,
            },
            {
              key: "impact",
              label: "双边冲击成本",
              children: formatPercent(
                result.shared_parameters.backtest.impact_cost,
                3,
              ),
            },
          ]}
        />
      </Card>
      <Card
        title="收益差对照"
        extra={
          <Typography.Text type="secondary">
            相对基准方案，单位：百分点
          </Typography.Text>
        }
      >
        <Table
          rowKey="candidate_id"
          pagination={false}
          dataSource={result.comparisons}
          scroll={{ x: 720 }}
          columns={[
            { title: "方案", render: (_, item) => names[item.candidate_id] },
            {
              title: "独立样本收益差",
              align: "right",
              render: (_, item) => (
                <Signed
                  value={item.independent_difference}
                  format={(value) => formatNumber(value * 100, 2)}
                />
              ),
            },
            {
              title: "共同样本收益差",
              align: "right",
              render: (_, item) => (
                <Signed
                  value={item.common_difference}
                  format={(value) => formatNumber(value * 100, 2)}
                />
              ),
            },
            {
              title: "样本变化差额",
              align: "right",
              render: (_, item) => (
                <Signed
                  value={item.sample_difference}
                  format={(value) => formatNumber(value * 100, 2)}
                />
              ),
            },
          ]}
        />
        <Typography.Paragraph
          type="secondary"
          style={{ marginTop: 12, marginBottom: 0 }}
        >
          共同样本收益差比较相同候选股票下的配置影响；样本变化差额为两种收益差之差，包含样本、排名与交易的交互影响，不能当作独立因子收益或因果归因。
        </Typography.Paragraph>
        {!hasCommon ? (
          <Alert
            type="info"
            showIcon
            title="未运行共同样本对照，不计算共同样本收益差与样本变化差额"
            style={{ marginTop: 12 }}
          />
        ) : null}
      </Card>
      <Card
        title="策略净值"
        extra={
          hasCommon ? (
            <Segmented<"independent" | "common">
              aria-label="净值样本方式"
              value={sample}
              onChange={setSample}
              options={[
                { label: "各方案可用样本", value: "independent" },
                { label: "共同样本", value: "common" },
              ]}
            />
          ) : null
        }
      >
        {points.error ? (
          <ErrorState message={points.error} onRetry={points.reload} />
        ) : !points.data ? (
          <Loading rows={3} />
        ) : (
          <Chart
            height={340}
            ariaLabel="收益对照策略净值"
            deps={[points.data, sample]}
            build={(p) => ({
              textStyle: { color: p.text },
              color: p.series,
              tooltip: { trigger: "axis" },
              legend: { type: "scroll", textStyle: { color: p.text } },
              grid: { left: 60, right: 24, top: 48, bottom: 65 },
              xAxis: {
                type: "category",
                data:
                  points
                    .data!.find((item) => item.row.sample === sample)
                    ?.points.map((point) => point.date) ?? [],
                axisLabel: { color: p.muted },
              },
              yAxis: {
                type: "value",
                scale: true,
                splitLine: { lineStyle: { color: p.split } },
              },
              dataZoom: [{ type: "inside" }, { type: "slider", bottom: 8 }],
              series: points
                .data!.filter((item) => item.row.sample === sample)
                .map((item) => ({
                  name: names[item.row.candidate_id],
                  type: "line",
                  showSymbol: false,
                  connectNulls: false,
                  data: item.points.map((point) => point.equity),
                })),
            })}
          />
        )}
      </Card>
      <Card title="运行结果">
        <Table<ReturnComparisonRow>
          rowKey={(row) => `${row.candidate_id}-${row.sample}`}
          dataSource={rows}
          pagination={false}
          scroll={{ x: 1300 }}
          columns={[
            { title: "方案", render: (_, row) => names[row.candidate_id] },
            {
              title: "样本",
              render: (_, row) =>
                row.sample === "common" ? "共同样本" : "各方案可用样本",
            },
            {
              title: "净收益",
              align: "right",
              render: (_, row) => (
                <Signed
                  value={row.metrics.total_return}
                  format={formatPercent}
                />
              ),
            },
            {
              title: "最大回撤",
              align: "right",
              render: (_, row) => (
                <Signed
                  value={row.metrics.max_drawdown}
                  format={formatPercent}
                />
              ),
            },
            {
              title: "年化波动",
              align: "right",
              render: (_, row) => formatPercent(row.metrics.volatility),
            },
            {
              title: "夏普",
              align: "right",
              render: (_, row) => formatNumber(row.metrics.sharpe_ratio, 2),
            },
            {
              title: "费用",
              align: "right",
              render: (_, row) => formatMoney(row.metrics.total_cost_amount),
            },
            {
              title: "平均换手",
              align: "right",
              render: (_, row) => formatPercent(row.metrics.avg_turnover),
            },
            {
              title: "每日可评分",
              render: (_, row) =>
                `${row.coverage.min_scored}–${row.coverage.max_scored} 只`,
            },
            {
              title: "结果",
              render: (_, row) => (
                <Space>
                  <AppLink to={paths.backtest(row.backtest_run_id)}>
                    回测
                  </AppLink>
                  <AppLink to={paths.factors({ run: row.factor_run_id })}>
                    评分
                  </AppLink>
                  <AppLink to={paths.evaluation(row.evaluation_run_id)}>
                    评估
                  </AppLink>
                </Space>
              ),
            },
          ]}
        />
      </Card>
      <Card title="方案配置">
        <Flex vertical gap={12}>
          {plan.candidates.map((c) => (
            <div key={c.id}>
              <Typography.Text strong>
                {c.name}
                {c.id === plan.comparison.baseline_id ? "（基准）" : ""}
              </Typography.Text>
              <div>
                <Typography.Text type="secondary">
                  {c.neutralization?.mode === "industry"
                    ? `行业中性化 · ${c.neutralization.industry_level ?? "L1"}`
                    : "行业中性化关闭"}
                </Typography.Text>
              </div>
              <div>
                {c.factors
                  .map(
                    (f) =>
                      `${factorLabel(f.name)}${f.id ? ` · ${f.id}` : ""}${f.window ? `（${f.window}日${f.skip ? `，剔除最近${f.skip}日` : ""}${f.baseline_window ? `，基准${f.baseline_window}日` : ""}）` : ""} ${formatPercent(f.weight, 0)}${f.missing_policy ? ` · ${MISSING_POLICY_LABELS[f.missing_policy]}` : ""}`,
                  )
                  .join("；")}
              </div>
            </div>
          ))}
        </Flex>
      </Card>
      <Card title="每日样本覆盖">
        <Table
          rowKey="date"
          size="small"
          dataSource={result.sample_days}
          pagination={{ pageSize: 10, hideOnSinglePage: true }}
          scroll={{ x: 700 }}
          columns={[
            { title: "信号日期", dataIndex: "date" },
            { title: "基础股票池", dataIndex: "pool" },
            ...plan.candidates.map((c) => ({
              title: c.name,
              render: (
                _: unknown,
                day: ReturnComparisonResult["sample_days"][number],
              ) => day.candidates[c.id],
            })),
            ...(hasCommon
              ? [
                  { title: "共同样本", dataIndex: "common" },
                  {
                    title: "查看股票",
                    render: (
                      _: unknown,
                      day: ReturnComparisonResult["sample_days"][number],
                    ) =>
                      baselineCommon && day.common >= 2 ? (
                        <AppLink
                          to={paths.factors({
                            run: baselineCommon.factor_run_id,
                            date: day.date,
                          })}
                        >
                          查看评分
                        </AppLink>
                      ) : (
                        "—"
                      ),
                  },
                ]
              : []),
          ]}
        />
        <Typography.Paragraph
          type="secondary"
          style={{ marginTop: 12, marginBottom: 0 }}
        >
          信号日期的样本用于下一交易日。样本相同指候选集合相同，不要求实际持仓相同。
        </Typography.Paragraph>
      </Card>
      <Card title="口径与限制">
        <Notes items={result.limitations} />
      </Card>
    </>
  );
}
