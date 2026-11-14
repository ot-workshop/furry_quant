import { Descriptions, Table, Typography } from "antd";
import type { BacktestDetail } from "../types";

const REASONS: Record<string, string> = {
  board: "板块",
  listing_days: "上市天数",
  lifecycle: "上市生命周期",
  st: "ST",
  st_unknown: "ST状态未知",
  market_cap: "市值",
  liquidity: "成交额或历史不足",
  industry: "历史行业",
};
const BOARDS: Record<string, string> = {
  main: "沪深主板",
  chinext: "创业板",
  star: "科创板",
};

export function UniversePanel({ detail }: { detail: BacktestDetail }) {
  const filters = detail.universe_filters;
  if (!filters) return null;
  const amount = (value: number | null, unit: number) =>
    value === null ? "不限" : (value / unit).toLocaleString("zh-CN");
  const rows = Object.entries(detail.pool_coverage_by_date ?? {})
    .map(([date, item]) => ({ date, ...item }))
    .reverse();
  return (
    <>
      <Descriptions
        column={{ xs: 1, md: 2 }}
        items={[
          {
            key: "boards",
            label: "板块",
            children: filters.boards.map((value) => BOARDS[value]).join("、"),
          },
          {
            key: "st",
            label: "排除ST与未知状态",
            children: filters.exclude_st ? "开启" : "关闭",
          },
          {
            key: "listing",
            label: "上市至少",
            children: `${filters.min_listing_days}个自然日`,
          },
          {
            key: "cap",
            label: "总市值（亿元）",
            children: `${amount(filters.min_market_cap, 1e8)} ~ ${amount(filters.max_market_cap, 1e8)}`,
          },
          {
            key: "amount",
            label: "日均成交额下限",
            children:
              filters.min_avg_amount === null
                ? "不限"
                : `${amount(filters.min_avg_amount, 10000)}万元 / ${filters.amount_window}交易日`,
          },
          {
            key: "industry",
            label: "纳入行业",
            children: filters.industries.length
              ? `${filters.industry_level} · ${filters.industries.join("、")}`
              : "不限",
          },
        ]}
      />
      <Typography.Paragraph type="secondary">
        {detail.st_policy}。剔除原因可能重叠；因子有效性在评分阶段继续检查。
      </Typography.Paragraph>
      <Table
        rowKey="date"
        dataSource={rows}
        scroll={{ x: 700 }}
        pagination={{ pageSize: 20, showSizeChanger: true }}
        columns={[
          { title: "信号日期", dataIndex: "date" },
          { title: "当日有行情", dataIndex: "available" },
          { title: "通过筛选", dataIndex: "selected" },
          {
            title: "剔除原因与数量",
            key: "reasons",
            render: (_, row) =>
              Object.entries(row.excluded)
                .filter(([, count]) => count > 0)
                .map(
                  ([reason, count]) => `${REASONS[reason] ?? reason} ${count}`,
                )
                .join("；") || "—",
          },
        ]}
      />
    </>
  );
}
