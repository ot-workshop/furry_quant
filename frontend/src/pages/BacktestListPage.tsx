import { useMemo, useState } from "react";
import { Input, Table, Tag, Typography } from "antd";
import { SearchOutlined } from "@ant-design/icons";
import { api } from "../api/client";
import { navigate, paths } from "../app/routes";
import {
  EmptyState,
  PageHeader,
  Resource,
  Section,
  Signed,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import type { BacktestSummary } from "../types";
import { formatDateTime, formatPercent } from "../utils/format";

const STRATEGIES: Record<string, string> = {
  TopkDropoutStrategy: "固定股票池",
  HistoricalIndexTopkStrategy: "历史成分动态池",
};

export default function BacktestListPage() {
  const list = useApiResource((signal) => api.backtests(signal), []);
  const [query, setQuery] = useState("");
  const items = useMemo(
    () =>
      (list.data?.items ?? []).filter((item) =>
        `${item.name} ${item.id} ${item.start_date} ${item.end_date}`
          .toLowerCase()
          .includes(query.toLowerCase()),
      ),
    [list.data, query],
  );
  return (
    <>
      <PageHeader
        title="回测"
        subtitle="点击任一行查看净值、持仓、成交与费用"
      />
      <Section
        title={`全部回测${list.data ? `（${list.data.total}）` : ""}`}
        extra={
          <Input
            allowClear
            prefix={<SearchOutlined />}
            placeholder="按名称、编号或日期筛选"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            style={{ width: 260 }}
          />
        }
      >
        <Resource
          state={list}
          empty={(data) =>
            data.items.length ? null : (
              <EmptyState
                title="暂无回测"
                description="提交研究后，回测结果会出现在这里。"
              />
            )
          }
        >
          {() => (
            <Table<BacktestSummary>
              rowKey="id"
              dataSource={items}
              pagination={{
                pageSize: 20,
                hideOnSinglePage: true,
                showSizeChanger: false,
              }}
              rowClassName={() => "clickable-row"}
              onRow={(row) => ({
                onClick: () => navigate(paths.backtest(row.id)),
              })}
              columns={[
                {
                  title: "回测",
                  key: "name",
                  render: (_, row) => (
                    <div>
                      <Typography.Text strong>{row.name}</Typography.Text>
                      <div>
                        <Typography.Text
                          type="secondary"
                          style={{ fontSize: 12 }}
                        >
                          {row.id}
                        </Typography.Text>
                      </div>
                    </div>
                  ),
                },
                {
                  title: "类型",
                  key: "strategy",
                  width: 140,
                  render: (_, row) => (
                    <Tag>{STRATEGIES[row.strategy] ?? row.strategy}</Tag>
                  ),
                },
                {
                  title: "区间",
                  key: "range",
                  width: 210,
                  sorter: (a, b) => a.start_date.localeCompare(b.start_date),
                  render: (_, row) => `${row.start_date} ~ ${row.end_date}`,
                },
                {
                  title: "总收益",
                  key: "ret",
                  width: 110,
                  align: "right",
                  sorter: (a, b) =>
                    (a.metrics?.total_return ?? -Infinity) -
                    (b.metrics?.total_return ?? -Infinity),
                  render: (_, row) => (
                    <Signed
                      value={row.metrics?.total_return}
                      format={(v) => formatPercent(v)}
                    />
                  ),
                },
                {
                  title: "最大回撤",
                  key: "dd",
                  width: 110,
                  align: "right",
                  render: (_, row) => (
                    <span className="numeric">
                      {formatPercent(row.metrics?.max_drawdown)}
                    </span>
                  ),
                },
                {
                  title: "创建时间",
                  key: "created",
                  width: 170,
                  defaultSortOrder: "descend",
                  sorter: (a, b) => a.created_at.localeCompare(b.created_at),
                  render: (_, row) => formatDateTime(row.created_at),
                },
              ]}
            />
          )}
        </Resource>
      </Section>
    </>
  );
}
