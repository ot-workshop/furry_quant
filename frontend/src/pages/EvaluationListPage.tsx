import { Table, Tag, Typography } from "antd";
import { api } from "../api/client";
import { navigate, paths } from "../app/routes";
import { useMediaQuery } from "../app/useMediaQuery";
import {
  CollapsibleTagList,
  EmptyState,
  MobileCardList,
  PageHeader,
  Resource,
  Section,
  TableScroll,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import type { EvaluationMetadata } from "../types";
import { factorLabel, formatDateTime } from "../utils/format";

export default function EvaluationListPage() {
  const isMobile = useMediaQuery("(max-width: 767px)");
  const list = useApiResource((signal) => api.evaluations(signal), []);
  return (
    <>
      <PageHeader
        title="因子评估"
        subtitle="IC、Rank IC、分组收益与因子相关性；点击查看详情"
      />
      <Section title={`全部评估${list.data ? `（${list.data.total}）` : ""}`}>
        <Resource
          state={list}
          empty={(data) =>
            data.items.length ? null : <EmptyState title="暂无因子评估" />
          }
        >
          {(data) =>
            isMobile ? (
              <MobileCardList
                items={data.items.map((row) => ({
                  key: row.id,
                  title: row.name,
                  badges: (
                    <Tag className="numeric-token">{row.sample_size} 只</Tag>
                  ),
                  meta: [
                    <span key="range" className="numeric-token">
                      {row.start} ~ {row.end}
                    </span>,
                    <span key="horizons">
                      持有期{" "}
                      {row.evaluation.horizons.map((h) => `${h}日`).join("、")}
                    </span>,
                    <span key="created" className="numeric-token">
                      {formatDateTime(row.created_at)}
                    </span>,
                  ],
                  tags: (
                    <CollapsibleTagList
                      tags={row.factor_config.map((f, idx) => ({
                        key: f.id ?? `${f.name}-${idx}`,
                        label: `${factorLabel(f.name)}${f.window ? ` ${f.window}日` : ""}`,
                      }))}
                    />
                  ),
                  onClick: () => navigate(paths.evaluation(row.id)),
                }))}
              />
            ) : (
              <TableScroll label="因子评估列表">
                <Table<EvaluationMetadata>
                  rowKey="id"
                  dataSource={data.items}
                  pagination={{ pageSize: 20, hideOnSinglePage: true }}
                  rowClassName={() => "clickable-row"}
                  onRow={(row) => ({
                    onClick: () => navigate(paths.evaluation(row.id)),
                  })}
                  columns={[
                    {
                      title: "评估",
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
                      title: "因子",
                      render: (_, row) =>
                        row.factor_config.map((f) => (
                          <Tag key={f.id ?? f.name}>
                            {factorLabel(f.name)}
                            {f.window ? ` ${f.window}日` : ""}
                          </Tag>
                        )),
                    },
                    {
                      title: "区间",
                      width: 210,
                      render: (_, row) => `${row.start} ~ ${row.end}`,
                    },
                    {
                      title: "股票数",
                      width: 80,
                      align: "right",
                      dataIndex: "sample_size",
                    },
                    {
                      title: "持有期",
                      width: 120,
                      render: (_, row) =>
                        row.evaluation.horizons.map((h) => `${h}日`).join("、"),
                    },
                    {
                      title: "创建时间",
                      width: 170,
                      render: (_, row) => formatDateTime(row.created_at),
                    },
                  ]}
                />
              </TableScroll>
            )
          }
        </Resource>
      </Section>
    </>
  );
}
