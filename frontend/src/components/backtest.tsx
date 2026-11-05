import { useState } from "react";
import { Descriptions, Flex, Select, Table, Tag, Typography } from "antd";
import { api } from "../api/client";
import { useApiResource } from "../hooks/useApiResource";
import type {
  BacktestDetail,
  CostBreakdown,
  ExecutionEvent,
  IndustryRow,
  Position,
  Trade,
} from "../types";
import {
  formatMetricValue,
  formatMoney,
  formatNumber,
  formatPercent,
  formatWeight,
  metricLabel,
  REBALANCE_LABELS,
  SIGNED_METRICS,
  tradeSideLabel,
} from "../utils/format";
import { AnnualReturnsChart } from "./charts";
import { EmptyState, Notes, Resource, Section, Signed } from "./ui";

const PAGE = 50;

const PERFORMANCE_ROWS = [
  "total_return",
  "annualized_return",
  "volatility",
  "sharpe_ratio",
  "max_drawdown",
  "calmar_ratio",
  "win_rate",
  "profit_loss_ratio",
];

export function PerformancePanel({ detail }: { detail: BacktestDetail }) {
  const performance = detail.performance;
  const metrics = detail.metrics ?? {};
  const rows = [
    ...PERFORMANCE_ROWS.map((key) => ({
      key,
      label: metricLabel(key),
      strategy: metrics[key],
      benchmark: performance?.benchmark_metrics[key],
    })),
    {
      key: "trough",
      label: "最大回撤日",
      strategy: performance?.drawdown.trough_date ?? null,
      benchmark: performance?.benchmark_drawdown.trough_date ?? null,
    },
  ];
  const method = performance?.methodology;
  return (
    <div className="grid-2">
      <Section title="绩效指标" description="基于扣除费用后的账户净值">
        <Table
          size="small"
          pagination={false}
          rowKey="key"
          dataSource={rows}
          columns={[
            { title: "指标", dataIndex: "label" },
            {
              title: "策略",
              align: "right",
              render: (_, row) =>
                typeof row.strategy === "string" ? (
                  row.strategy
                ) : SIGNED_METRICS.has(row.key) ? (
                  <Signed
                    value={row.strategy}
                    format={(v) => formatMetricValue(row.key, v)}
                  />
                ) : (
                  <span className="numeric">
                    {formatMetricValue(row.key, row.strategy as number | null)}
                  </span>
                ),
            },
            {
              title: detail.benchmark_name
                ? `基准（${detail.benchmark_name}）`
                : "基准",
              align: "right",
              render: (_, row) =>
                typeof row.benchmark === "string" ? (
                  row.benchmark
                ) : (
                  <span className="numeric">
                    {formatMetricValue(row.key, row.benchmark as number | null)}
                  </span>
                ),
            },
          ]}
        />
        <Notes
          items={[
            method
              ? `按每年 ${method.trading_days_per_year} 个交易日年化；夏普比率使用 ${formatPercent(method.annual_risk_free_rate)} 的无风险利率；波动率为样本标准差。`
              : null,
            "卡玛比率 = 年化收益 / 最大回撤绝对值。胜率与盈亏比按日收益统计，不是逐笔交易胜率。",
            detail.benchmark
              ? `基准来自官方 Tushare，覆盖 ${detail.benchmark.start} ~ ${detail.benchmark.end}；${detail.benchmark.return_type === "gross_total_return" ? "全收益指数，计入分红再投资" : "价格指数，不含分红"}。`
              : "历史结果未保存独立基准验证信息。",
          ]}
        />
      </Section>
      <Section
        title="年度收益"
        description="各年按实际覆盖区间复利，首尾年度可能不完整"
      >
        {performance?.annual_returns.length ? (
          <AnnualReturnsChart items={performance.annual_returns} />
        ) : (
          <EmptyState title="暂无可计算的年度收益" />
        )}
      </Section>
    </div>
  );
}

export function PositionsPanel({
  runId,
  dates,
  date,
  onDate,
}: {
  runId: string;
  dates: string[];
  date: string | null;
  onDate: (date: string) => void;
}) {
  const [page, setPage] = useState(1);
  const current = date && dates.includes(date) ? date : (dates.at(-1) ?? null);
  const positions = useApiResource(
    current
      ? (signal) =>
          api.positions(
            runId,
            { date: current, offset: (page - 1) * PAGE, limit: PAGE },
            signal,
          )
      : null,
    [runId, current, page],
  );
  return (
    <Section
      title="每日持仓"
      description="所选交易日收盘后的模拟持仓"
      extra={
        <Select
          showSearch
          value={current ?? undefined}
          onChange={(value: string) => {
            setPage(1);
            onDate(value);
          }}
          options={[...dates]
            .reverse()
            .map((day) => ({ value: day, label: day }))}
          style={{ width: 160 }}
          placeholder="选择日期"
        />
      }
    >
      {!current ? (
        <EmptyState title="该回测没有净值序列" />
      ) : (
        <Resource
          state={positions}
          empty={(data) =>
            data.items.length ? null : <EmptyState title="当日无持仓" />
          }
        >
          {(data) => (
            <Table<Position>
              size="small"
              rowKey={(row) => `${row.instrument}-${row.date}`}
              dataSource={data.items}
              pagination={{
                current: page,
                pageSize: PAGE,
                total: data.total,
                onChange: setPage,
                hideOnSinglePage: true,
              }}
              columns={[
                {
                  title: "股票",
                  render: (_, row) => (
                    <Flex vertical>
                      <Typography.Text>{row.name}</Typography.Text>
                      <Typography.Text
                        type="secondary"
                        style={{ fontSize: 12 }}
                      >
                        {row.instrument}
                      </Typography.Text>
                    </Flex>
                  ),
                },
                {
                  title: "数量",
                  align: "right",
                  render: (_, row) => formatNumber(row.amount, 0),
                },
                {
                  title: "价格",
                  align: "right",
                  render: (_, row) => formatNumber(row.price, 4),
                },
                {
                  title: "权重",
                  align: "right",
                  render: (_, row) => formatWeight(row.weight),
                },
              ]}
            />
          )}
        </Resource>
      )}
    </Section>
  );
}

const COST_KEYS: Array<[keyof CostBreakdown, string]> = [
  ["commission", "券商佣金"],
  ["stamp_tax", "印花税"],
  ["transfer_fee", "过户费"],
  ["impact", "冲击成本"],
  ["legacy_fee", "综合费用"],
];

export function TradesPanel({ runId }: { runId: string }) {
  const [page, setPage] = useState(1);
  const trades = useApiResource(
    (signal) =>
      api.trades(runId, { offset: (page - 1) * PAGE, limit: PAGE }, signal),
    [runId, page],
  );
  return (
    <Section title="成交记录" description="T 日收盘后评分，T+1 开盘成交">
      <Resource
        state={trades}
        empty={(data) =>
          data.items.length ? null : <EmptyState title="没有成交" />
        }
      >
        {(data) => (
          <Table<Trade>
            size="small"
            rowKey={(row, index) => `${row.date}-${row.instrument}-${index}`}
            dataSource={data.items}
            pagination={{
              current: page,
              pageSize: PAGE,
              total: data.total,
              onChange: setPage,
              hideOnSinglePage: true,
              showSizeChanger: false,
            }}
            expandable={{
              rowExpandable: (row) => Boolean(row.cost_breakdown),
              expandedRowRender: (row) => (
                <Descriptions
                  size="small"
                  column={5}
                  items={COST_KEYS.map(([key, label]) => ({
                    key,
                    label,
                    children: formatMoney(row.cost_breakdown?.[key]),
                  }))}
                />
              ),
            }}
            columns={[
              { title: "日期", dataIndex: "date", width: 120 },
              {
                title: "股票",
                render: (_, row) =>
                  `${row.name ?? ""} ${row.instrument}`.trim(),
              },
              {
                title: "方向",
                width: 80,
                render: (_, row) => (
                  <Tag
                    color={
                      row.side === "buy"
                        ? "var(--quant-up)"
                        : row.side === "sell"
                          ? "var(--quant-down)"
                          : undefined
                    }
                  >
                    {tradeSideLabel(row.side)}
                  </Tag>
                ),
              },
              {
                title: "数量",
                align: "right",
                render: (_, row) => formatNumber(row.amount, 0),
              },
              {
                title: "价格",
                align: "right",
                render: (_, row) => formatNumber(row.price, 4),
              },
              {
                title: "金额",
                align: "right",
                render: (_, row) => formatMoney(row.value),
              },
              {
                title: "费用",
                align: "right",
                render: (_, row) => formatMoney(row.cost),
              },
            ]}
          />
        )}
      </Resource>
    </Section>
  );
}

export function CostsPanel({ detail }: { detail: BacktestDetail }) {
  const {
    execution_costs: costs,
    cost_summary: summary,
    rebalance_policy: rebalance,
  } = detail;
  if (!costs && !rebalance)
    return (
      <Section title="调仓与费用">
        <EmptyState title="该结果未保存费用明细" />
      </Section>
    );
  return (
    <div className="grid-2">
      <Section title="费用构成" description="累计金额（元）">
        {summary ? (
          <Table
            size="small"
            pagination={false}
            rowKey="key"
            dataSource={[
              ...COST_KEYS,
              ["total", "合计"] as [keyof CostBreakdown, string],
            ].map(([key, label]) => ({
              key,
              label,
              value: summary[key],
            }))}
            columns={[
              { title: "项目", dataIndex: "label" },
              {
                title: "金额",
                align: "right",
                render: (_, row) => (
                  <span className="numeric">{formatMoney(row.value)}</span>
                ),
              },
            ]}
          />
        ) : (
          <EmptyState title="未保存费用汇总" />
        )}
      </Section>
      <Section title="费用与调仓规则">
        <Descriptions
          size="small"
          column={1}
          items={[
            ...(rebalance
              ? [
                  {
                    key: "freq",
                    label: "调仓频率",
                    children: REBALANCE_LABELS[rebalance.frequency],
                  },
                  {
                    key: "count",
                    label: "计划调仓次数",
                    children: rebalance.scheduled_count,
                  },
                  {
                    key: "timing",
                    label: "执行时点",
                    children: rebalance.timing,
                  },
                ]
              : []),
            ...(costs
              ? [
                  {
                    key: "mode",
                    label: "费用模式",
                    children:
                      costs.cost_mode === "itemized"
                        ? `分项：佣金 ${formatPercent(costs.commission_rate, 3)}，${costs.tax_mode === "historical" ? "印花税与过户费按成交日历史费率" : `印花税 ${formatPercent(costs.stamp_tax_rate, 3)}、过户费 ${formatPercent(costs.transfer_fee_rate, 4)}`}`
                        : "综合费率（不另计分项税费）",
                  },
                  {
                    key: "impact",
                    label: "冲击成本",
                    children: `成交额的 ${formatPercent(costs.impact_cost, 3)}（双边）`,
                  },
                  {
                    key: "min",
                    label: "单笔最低",
                    children: `${costs.min_cost} 元`,
                  },
                ]
              : []),
          ]}
        />
        {rebalance?.exit_policy ? (
          <Notes
            items={[
              rebalance.exit_policy,
              "冲击成本是按成交额比例扣除的压力假设，不是成交量容量模型。",
            ]}
          />
        ) : null}
      </Section>
    </div>
  );
}

export function IndustryPanel({
  runId,
  date,
  kind,
}: {
  runId: string;
  date?: string | null;
  kind: "backtests" | "factor-runs";
}) {
  const report = useApiResource(
    (signal) => api.industries(kind, runId, date, signal),
    [kind, runId, date],
  );
  const [metric, setMetric] = useState<string>("");
  return (
    <Section
      title={kind === "backtests" ? "持仓行业暴露" : "行业分布与中性化"}
      description={report.data?.date}
    >
      <Resource
        state={report}
        empty={(data) =>
          data.industry_data ? null : (
            <EmptyState
              title="该结果未保存历史行业数据"
              description={data.note ?? "可复用配置重新运行以生成行业诊断。"}
            />
          )
        }
      >
        {(data) => {
          const keys = Object.keys(data.items[0]?.before_mean ?? {});
          const selected = keys.includes(metric) ? metric : keys[0];
          const neutral = data.neutralization?.mode === "industry";
          return (
            <Flex vertical gap={12}>
              {kind === "backtests" ? (
                <Descriptions
                  size="small"
                  column={{ xs: 1, md: 4 }}
                  items={[
                    {
                      key: "max",
                      label: "最大行业权重",
                      children: formatPercent(data.max_industry_weight),
                    },
                    {
                      key: "hhi",
                      label: "行业集中度 HHI",
                      children: formatNumber(data.industry_hhi, 4),
                    },
                    {
                      key: "cash",
                      label: "现金",
                      children: formatPercent(data.cash_weight),
                    },
                    {
                      key: "unknown",
                      label: "未知行业",
                      children: formatPercent(data.unknown_weight),
                    },
                  ]}
                />
              ) : keys.length ? (
                <Select
                  value={selected}
                  onChange={setMetric}
                  options={keys.map((key) => ({ value: key, label: key }))}
                  style={{ width: 220 }}
                />
              ) : null}
              <Table<IndustryRow>
                size="small"
                rowKey="industry_code"
                pagination={false}
                dataSource={data.items}
                columns={[
                  { title: "行业", render: (_, row) => row.industry_name },
                  {
                    title: "股票数",
                    align: "right",
                    render: (_, row) =>
                      `${row.count}${row.singleton ? "（单只）" : ""}`,
                  },
                  ...(kind === "backtests"
                    ? [
                        {
                          title: "持仓权重",
                          align: "right" as const,
                          render: (_: unknown, row: IndustryRow) =>
                            formatPercent(row.weight),
                        },
                        {
                          title: "基准权重",
                          align: "right" as const,
                          render: (_: unknown, row: IndustryRow) =>
                            data.benchmark_available
                              ? formatPercent(row.benchmark_weight)
                              : "—",
                        },
                        {
                          title: "主动偏离",
                          align: "right" as const,
                          render: (_: unknown, row: IndustryRow) =>
                            data.benchmark_available ? (
                              <Signed
                                value={row.active_weight}
                                format={(v) => formatPercent(v)}
                              />
                            ) : (
                              "—"
                            ),
                        },
                      ]
                    : [
                        {
                          title: "处理前平均分",
                          align: "right" as const,
                          render: (_: unknown, row: IndustryRow) =>
                            formatNumber(row.before_mean?.[selected], 2),
                        },
                        {
                          title: "处理后平均分",
                          align: "right" as const,
                          render: (_: unknown, row: IndustryRow) =>
                            formatNumber(row.after_mean?.[selected], 2),
                        },
                        {
                          title: "残差均值",
                          align: "right" as const,
                          render: (_: unknown, row: IndustryRow) =>
                            neutral
                              ? formatNumber(row.residual_mean?.[selected], 6)
                              : "未处理",
                        },
                      ]),
                ]}
              />
              <Notes
                items={[
                  data.industry_data?.policy,
                  data.note,
                  data.benchmark_note,
                  data.coverage
                    ? `当日股票池 ${data.coverage.universe} 只：行业已知 ${data.coverage.known}，缺失 ${data.coverage.missing}，冲突 ${data.coverage.conflict}。`
                    : null,
                  "行业公告时点未认证。",
                ]}
              />
            </Flex>
          );
        }}
      </Resource>
    </Section>
  );
}

const EVENT_LABELS: Record<string, string> = {
  index_exit: "成分调出",
  outside_listing_lifecycle: "生命周期结束",
  score_rotation: "评分轮换",
  rank_selection: "排名选入",
  suspended: "全天停牌",
  limit_down: "开盘跌停",
  limit_up: "开盘涨停",
  t_plus_one: "T+1 限制",
  missing_quote_or_before_listing: "缺少报价或未上市",
  unknown_price_limits: "涨跌停规则不明",
  insufficient_cash_for_board_minimum: "资金不足最小申报数量",
  candidate_shortfall: "有效候选不足",
  insufficient_valid_scores: "有效评分不足，不凑满持仓",
};

export function EventsPanel({
  runId,
  summary,
}: {
  runId: string;
  summary?: BacktestDetail["execution_summary"];
}) {
  const [page, setPage] = useState(1);
  const events = useApiResource(
    (signal) =>
      api.executionEvents(
        runId,
        { offset: (page - 1) * PAGE, limit: PAGE },
        signal,
      ),
    [runId, page],
  );
  return (
    <Section
      title="动态池执行记录"
      description={
        summary
          ? `退出受阻 ${summary.blocked_exits} 次；期末待退出 ${summary.pending_exits.length} 只`
          : undefined
      }
    >
      <Resource
        state={events}
        empty={(data) =>
          data.items.length ? null : <EmptyState title="没有执行记录" />
        }
      >
        {(data) => (
          <Table<ExecutionEvent>
            size="small"
            rowKey={(row, index) => `${row.date}-${row.instrument}-${index}`}
            dataSource={data.items}
            pagination={{
              current: page,
              pageSize: PAGE,
              total: data.total,
              onChange: setPage,
              hideOnSinglePage: true,
            }}
            columns={[
              { title: "日期", dataIndex: "date", width: 120 },
              { title: "股票", dataIndex: "instrument" },
              {
                title: "操作",
                render: (_, row) => (row.action === "sell" ? "卖出" : "买入"),
              },
              {
                title: "原因",
                render: (_, row) => EVENT_LABELS[row.reason] ?? row.reason,
              },
              {
                title: "结果",
                render: (_, row) =>
                  row.status === "blocked" ? (
                    <Tag color="warning">
                      受阻：
                      {EVENT_LABELS[row.block_reason ?? ""] ?? row.block_reason}
                    </Tag>
                  ) : (
                    <Tag>已提交</Tag>
                  ),
              },
            ]}
          />
        )}
      </Resource>
    </Section>
  );
}
