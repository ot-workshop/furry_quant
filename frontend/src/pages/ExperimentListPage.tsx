import { Alert, Button, Table, Tag, Typography } from "antd";
import { PlusOutlined } from "@ant-design/icons";
import { experimentsApi, type ExperimentMetadata } from "../api/experiments";
import { navigate, paths } from "../app/routes";
import { useMediaQuery } from "../app/useMediaQuery";
import {
  EmptyState,
  MobileCardList,
  PageHeader,
  Resource,
  Section,
  TableScroll,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import { formatDateTime } from "../utils/format";

export default function ExperimentListPage() {
  const isMobile = useMediaQuery("(max-width: 767px)");
  const list = useApiResource((signal) => experimentsApi.list(signal), []);
  return (
    <>
      <PageHeader
        title="方案比较"
        subtitle="分段验证因子方案，或比较因子、行业处理与样本变化对收益的影响"
        extra={
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => navigate(paths.newExperiment())}
          >
            新建方案比较
          </Button>
        }
      />
      <Alert
        type="info"
        showIcon
        title="为什么要分段？"
        description="同一段历史上反复挑选因子组合会高估效果。方案比较在验证段锁定选择后，才在测试段运行一次，用来检查选择是否稳健。"
        closable
      />
      <Section
        title={`全部方案比较${list.data ? `（${list.data.total}）` : ""}`}
      >
        <Resource
          state={list}
          empty={(data) =>
            data.items.length ? null : (
              <EmptyState
                title="还没有方案比较"
                action={
                  <Button
                    type="primary"
                    onClick={() => navigate(paths.newExperiment())}
                  >
                    新建方案比较
                  </Button>
                }
              />
            )
          }
        >
          {(data) =>
            isMobile ? (
              <MobileCardList
                items={data.items.map((row) => ({
                  key: row.id,
                  title: row.name,
                  badges: (
                    <Tag>
                      {row.validation_kind === "return_comparison"
                        ? "收益来源对照"
                        : "分段比较"}
                    </Tag>
                  ),
                  meta: [
                    <span key="candidate">
                      选定方案 {row.selected_candidate ?? "—"}
                    </span>,
                    <span key="created" className="numeric-token">
                      {formatDateTime(row.created_at)}
                    </span>,
                  ],
                  onClick: () => navigate(paths.experiment(row.id)),
                }))}
              />
            ) : (
              <TableScroll label="方案比较列表">
                <Table<ExperimentMetadata>
                  rowKey="id"
                  dataSource={data.items}
                  pagination={{ pageSize: 20, hideOnSinglePage: true }}
                  rowClassName={() => "clickable-row"}
                  onRow={(row) => ({
                    onClick: () => navigate(paths.experiment(row.id)),
                  })}
                  columns={[
                    {
                      title: "名称",
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
                      render: (_, row) =>
                        row.validation_kind === "return_comparison"
                          ? "收益来源对照"
                          : "分段比较",
                    },
                    {
                      title: "选定方案",
                      render: (_, row) => row.selected_candidate ?? "—",
                    },
                    {
                      title: "数据版本",
                      dataIndex: "data_version",
                      ellipsis: true,
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
