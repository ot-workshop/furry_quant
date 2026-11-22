import { Table, Tag } from "antd";
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
import {
  isResultRowBackground,
  ResultDateRange,
  ResultTitle,
} from "../components/resultList";
import { useApiResource } from "../hooks/useApiResource";
import type { EvaluationMetadata } from "../types";
import { factorLabel, formatDateTime } from "../utils/format";

export default function EvaluationListPage() {
  const isMobile = useMediaQuery("(max-width: 767px)");
  const narrowDesktop = useMediaQuery(
    "(min-width: 1024px) and (max-width: 1199px)",
  );
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
                  className="result-list-table"
                  tableLayout="fixed"
                  dataSource={data.items}
                  pagination={{ pageSize: 20, hideOnSinglePage: true }}
                  rowClassName={() => "clickable-row"}
                  onRow={(row) => ({
                    onClick: (event) => {
                      if (isResultRowBackground(event))
                        navigate(paths.evaluation(row.id));
                    },
                  })}
                  columns={[
                    {
                      title: "评估",
                      render: (_, row) => (
                        <ResultTitle
                          to={paths.evaluation(row.id)}
                          name={row.name}
                          meta={
                            <span className="result-list-id" title={row.id}>
                              {row.id}
                            </span>
                          }
                        />
                      ),
                    },
                    {
                      title: "因子",
                      width: narrowDesktop ? 172 : 224,
                      render: (_, row) => (
                        <CollapsibleTagList
                          desktop
                          tags={row.factor_config.map((f, idx) => ({
                            key: f.id ?? `${f.name}-${idx}`,
                            label: `${factorLabel(f.name)}${f.window ? ` ${f.window}日` : ""}`,
                          }))}
                        />
                      ),
                    },
                    {
                      title: "区间",
                      width: 176,
                      align: "right",
                      hidden: narrowDesktop,
                      render: (_, row) => (
                        <ResultDateRange start={row.start} end={row.end} />
                      ),
                    },
                    {
                      title: "股票数",
                      width: 74,
                      align: "right",
                      dataIndex: "sample_size",
                      className: "result-list-number",
                    },
                    {
                      title: "持有期",
                      width: narrowDesktop ? 96 : 110,
                      render: (_, row) => (
                        <span className="result-list-horizons result-list-number">
                          {row.evaluation.horizons
                            .map((h) => `${h}日`)
                            .join("、")}
                        </span>
                      ),
                    },
                    {
                      title: narrowDesktop ? "区间 / 创建时间" : "创建时间",
                      width: narrowDesktop ? 139 : 136,
                      align: "right",
                      render: (_, row) => (
                        <div className="result-list-dates">
                          {narrowDesktop ? (
                            <ResultDateRange start={row.start} end={row.end} />
                          ) : null}
                          <span className="result-list-secondary result-list-number">
                            {formatDateTime(row.created_at)}
                          </span>
                        </div>
                      ),
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
