import { Descriptions, Table } from "antd";
import type { FactorScore } from "../types";
import { formatNumber } from "../utils/format";
import { MISSING_POLICY_LABELS } from "./FactorMissingPolicy";

type Detail = NonNullable<FactorScore["factor_details"]>[string];
const COMPANY_TYPES: Record<string, string> = {
  "1": "一般工商业",
  "2": "银行",
  "3": "保险",
  "4": "证券",
  "7": "多元金融",
};

export function FactorCalculationDetails({
  label,
  detail,
  scored,
}: {
  label: string;
  detail: Detail;
  scored: boolean;
}) {
  const gross = detail.status !== undefined;
  const actual = !scored
    ? "未参与本日评分；详见排除原因"
    : detail.status === "not_applicable"
      ? "行业不适用，该项记50分；原始值为空"
      : detail.imputed
        ? `${MISSING_POLICY_LABELS[detail.missing_policy]}；原始值仍为空`
        : gross
          ? "有效毛利率，正常参与排名"
          : detail.ttm_net_profit == null
            ? "缺失"
            : detail.ttm_net_profit < 0
              ? "亏损，正常参与排名"
              : detail.ttm_net_profit === 0
                ? "零利润，正常参与排名"
                : "盈利，正常参与排名";
  return (
    <>
      <Descriptions
        title={`${label}计算明细`}
        size="small"
        column={{ xs: 1, md: 2 }}
        style={{ marginTop: 16 }}
        items={[
          ...(gross
            ? [
                {
                  key: "revenue",
                  label: "TTM营业收入（元）",
                  children: formatNumber(detail.revenue_ttm, 2),
                },
                {
                  key: "cost",
                  label: "TTM营业成本（元）",
                  children: formatNumber(detail.operating_cost_ttm, 2),
                },
                {
                  key: "type",
                  label: "当时报表类型",
                  children:
                    COMPANY_TYPES[detail.company_type ?? ""] ?? "未确认",
                },
                {
                  key: "industry",
                  label: "历史行业依据",
                  children:
                    detail.historical_industry?.industry_name ??
                    "以当时可用报表类型判断",
                },
              ]
            : [
                {
                  key: "profit",
                  label: "TTM归母净利润（元）",
                  children: formatNumber(detail.ttm_net_profit, 2),
                },
                {
                  key: "cap",
                  label: "当日总市值（元）",
                  children: formatNumber(detail.total_mv, 2),
                },
              ]),
          {
            key: "period",
            label: "报告期",
            children: detail.report_period ?? "—",
          },
          {
            key: "available",
            label: "报告可用时间",
            children: detail.financial_available_at ?? "—",
          },
          {
            key: "policy",
            label: "真正缺失处理",
            children: MISSING_POLICY_LABELS[detail.missing_policy],
          },
          { key: "status", label: "实际处理", children: actual },
          ...(gross
            ? [
                {
                  key: "reason",
                  label: "口径 / 原因",
                  children:
                    detail.reason_label ?? "（营业收入 − 营业成本）÷ 营业收入",
                },
              ]
            : []),
        ]}
      />
      {gross && detail.components?.length ? (
        <Table
          size="small"
          pagination={false}
          rowKey="report_period"
          dataSource={detail.components}
          scroll={{ x: 760 }}
          columns={[
            { title: "TTM组成报告期", dataIndex: "report_period" },
            { title: "系数", dataIndex: "coefficient" },
            {
              title: "报告可用时间",
              dataIndex: "available_at",
              render: (value: string | null) => value ?? "—",
            },
            {
              title: "报表类型",
              dataIndex: "company_type",
              render: (value: string | null) =>
                COMPANY_TYPES[value ?? ""] ?? "未确认",
            },
            {
              title: "营业收入（元）",
              dataIndex: "revenue",
              align: "right",
              render: (value: number | null) => formatNumber(value, 2),
            },
            {
              title: "营业成本（元）",
              dataIndex: "operating_cost",
              align: "right",
              render: (value: number | null) => formatNumber(value, 2),
            },
          ]}
        />
      ) : null}
    </>
  );
}
