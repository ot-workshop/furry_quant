import { Table, Tag } from "antd";
import type {
  CrossValidationReport,
  CrossValidationStock,
  TemporalAuditData,
} from "../types";
import { formatPercent } from "../utils/format";
import { EmptyState, Notes, Section } from "./ui";

const AUDIT_STATUS: Record<string, { label: string; color: string }> = {
  guarded: { label: "程序约束", color: "success" },
  assumption: { label: "保守假设", color: "processing" },
  model_assumption: { label: "模拟边界", color: "processing" },
  selection_bias: { label: "固定池限制", color: "warning" },
  unverified: { label: "未认证", color: "warning" },
  diagnostic_only: { label: "仅诊断", color: "default" },
  not_used: { label: "未使用", color: "default" },
  not_modeled: { label: "未建模", color: "warning" },
};

export function TemporalAuditPanel({
  audit,
}: {
  audit?: TemporalAuditData | null;
}) {
  return (
    <Section
      title="数据时点与成交边界"
      description="哪些环节有程序保证、哪些只是假设"
    >
      {audit ? (
        <>
          <Table
            size="small"
            pagination={false}
            rowKey="key"
            dataSource={audit.items}
            columns={[
              { title: "检查项", dataIndex: "label", width: 180 },
              {
                title: "状态",
                width: 110,
                render: (_, row) => {
                  const meta = AUDIT_STATUS[row.status] ?? {
                    label: row.status,
                    color: "default",
                  };
                  return <Tag color={meta.color}>{meta.label}</Tag>;
                },
              },
              { title: "说明", dataIndex: "detail" },
            ]}
          />
          <Notes
            items={[
              audit.note,
              audit.capacity_diagnostics
                ? `成交容量未建模：成交额超过此前 20 日均额 10% 的标的交易日有 ${audit.capacity_diagnostics.above_ten_percent} 个，最大比例 ${formatPercent(audit.capacity_diagnostics.max_prior_turnover_ratio)}。${audit.capacity_diagnostics.note}`
                : null,
            ]}
          />
        </>
      ) : (
        <EmptyState
          title="旧结果未保存时点核验清单"
          description="不能据此视为已完成历史时点或成交容量认证。"
        />
      )}
    </Section>
  );
}

const CV_STATUS: Record<string, string> = {
  verified: "核验通过",
  fallback: "已回退",
  conflict: "存在冲突",
};
const SOURCE: Record<string, string> = {
  baostock: "BaoStock",
  tushare: "Tushare",
};
const ISSUE: Record<string, string> = {
  adjustment_discontinuity: "复权收益异常",
  missing_or_duplicate_sessions: "日期缺失或重复",
  missing_or_invalid_prices_or_adjustment: "价格或复权缺失/异常",
  invalid_ohlc_geometry: "开高低收不一致",
  raw_price_conflict: "两源原始价格不一致",
  plausible_adjusted_return_conflict: "复权收益冲突，需人工核实",
  primary_failed_verify_only: "主源异常，仅核验未回退",
  secondary_unavailable_invalid_or_conflicting: "第二来源不可用或冲突",
};

export function CrossValidationTable({
  report,
}: {
  report: CrossValidationReport;
}) {
  return (
    <>
      <Table<CrossValidationStock>
        size="small"
        pagination={false}
        rowKey="code"
        dataSource={report.stocks}
        columns={[
          { title: "股票", dataIndex: "code" },
          {
            title: "核验结果",
            render: (_, row) => (
              <Tag
                color={
                  row.status === "verified"
                    ? "success"
                    : row.status === "fallback"
                      ? "warning"
                      : "error"
                }
              >
                {CV_STATUS[row.status] ?? row.status}
              </Tag>
            ),
          },
          {
            title: "价格来源",
            render: (_, row) => SOURCE[row.price_source] ?? row.price_source,
          },
          {
            title: "复权来源",
            render: (_, row) =>
              SOURCE[row.adjustment_source] ?? row.adjustment_source,
          },
          {
            title: "主源检查",
            render: (_, row) =>
              row.primary_issues
                .map((issue) => ISSUE[issue] ?? issue)
                .join("、") ||
              (row.reason ? (ISSUE[row.reason] ?? row.reason) : "未发现异常"),
          },
        ]}
      />
      <Notes
        items={[
          `原始价格容差 ${report.price_absolute_tolerance_cny} 元，收益差异阈值 ${formatPercent(report.return_absolute_tolerance)}。`,
          "回退会切换整只股票区间的行情与复权序列；估值与涨跌停价格仍来自 Tushare。",
          ...report.notes,
        ]}
      />
    </>
  );
}
