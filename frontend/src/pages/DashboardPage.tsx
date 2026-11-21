import {
  Alert,
  Button,
  Card,
  Flex,
  Space,
  Statistic,
  Table,
  Typography,
} from "antd";
import { DatabaseOutlined, PlusCircleOutlined } from "@ant-design/icons";
import { api } from "../api/client";
import { researchApi } from "../api/research";
import { navigate, paths } from "../app/routes";
import { useMediaQuery } from "../app/useMediaQuery";
import { TaskTable } from "../components/tasks";
import {
  AppLink,
  EmptyState,
  MobileCardList,
  PageHeader,
  Resource,
  Section,
  Signed,
  TableScroll,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import type { BacktestSummary } from "../types";
import { formatDateTime, formatPercent } from "../utils/format";

function CardLoadError({
  onRetry,
  retrying,
}: {
  onRetry: () => void;
  retrying: boolean;
}) {
  return (
    <Flex align="center" gap={6} style={{ marginTop: 4 }}>
      <Typography.Text type="danger" style={{ fontSize: 13 }}>
        状态读取失败
      </Typography.Text>
      <Button
        type="link"
        size="small"
        style={{ padding: 0, height: "auto" }}
        loading={retrying}
        disabled={retrying}
        onClick={onRetry}
      >
        {retrying ? "重试中…" : "重试"}
      </Button>
    </Flex>
  );
}

export default function DashboardPage() {
  const isMobile = useMediaQuery("(max-width: 767px)");
  const summary = useApiResource((signal) => api.dashboardSummary(signal), [], {
    retryOnErrorMs: 5000,
  });
  const tasks = useApiResource((signal) => researchApi.tasks(signal), [], {
    pollMs: 5000,
  });
  const backtests = useApiResource((signal) => api.backtests(signal), [], {
    retryOnErrorMs: 5000,
  });

  const counts = summary.data?.counts;
  const datasetCount = counts
    ? counts.fixed + counts.historical + counts.market
    : 0;
  const database = summary.data?.database ?? null;
  const active =
    tasks.data?.items.filter(
      (t) => t.status === "queued" || t.status === "running",
    ) ?? [];
  const noData = summary.data !== null && datasetCount === 0;

  return (
    <>
      <PageHeader
        title="工作台"
        subtitle="数据、任务与最近结果一览"
        extra={
          <>
            <Button
              icon={<DatabaseOutlined />}
              onClick={() => navigate(paths.data())}
            >
              数据管理
            </Button>
            <Button
              type="primary"
              icon={<PlusCircleOutlined />}
              onClick={() => navigate(paths.newResearch())}
            >
              新建研究
            </Button>
          </>
        }
      />

      {noData ? (
        <Alert
          type="info"
          showIcon
          title="还没有可用于研究的数据集"
          description="先在数据管理中下载行情（自选股票或沪深300），下载完成后即可新建研究。"
          action={
            <Button
              type="primary"
              onClick={() => navigate(paths.data("market"))}
            >
              去下载行情
            </Button>
          }
        />
      ) : null}

      <div className="grid-4">
        <Card>
          <Statistic
            title="研究数据集"
            value={counts ? datasetCount : "—"}
            suffix={counts ? "个" : undefined}
            loading={summary.loading}
          />
          {summary.error && !counts ? (
            <CardLoadError
              onRetry={summary.reload}
              retrying={summary.refreshing}
            />
          ) : counts ? (
            <div className="dashboard-dataset-breakdown">
              <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                <span style={{ whiteSpace: "nowrap" }}>
                  固定股票池 {counts.fixed}
                </span>
                {" · "}
                <span style={{ whiteSpace: "nowrap" }}>
                  沪深300历史成分 {counts.historical}
                </span>
                {" · "}
                <span style={{ whiteSpace: "nowrap" }}>
                  条件筛选 {counts.market}
                </span>
              </Typography.Text>
              {(counts.invalid ?? 0) > 0 ? (
                <div>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    已跳过 {counts.invalid} 个无效清单
                  </Typography.Text>
                </div>
              ) : null}
              <div>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  按发布清单统计（未重验数据文件）
                </Typography.Text>
              </div>
            </div>
          ) : null}
        </Card>
        <Card>
          <Statistic
            title="行情覆盖股票"
            value={database ? database.securities : "—"}
            suffix={database ? "只" : undefined}
            loading={summary.loading}
          />
          {summary.error && !summary.data ? (
            <CardLoadError
              onRetry={summary.reload}
              retrying={summary.refreshing}
            />
          ) : summary.data ? (
            <Typography.Text type="secondary" style={{ fontSize: 13 }}>
              {database === null
                ? "数据库未连接"
                : database.coverage_start
                  ? `${database.coverage_start} ~ ${database.coverage_end}`
                  : "暂无行情"}
            </Typography.Text>
          ) : null}
        </Card>
        <Card>
          <Statistic
            title="进行中的任务"
            value={tasks.data ? active.length : "—"}
            suffix={tasks.data ? "个" : undefined}
            loading={tasks.loading}
          />
          {tasks.error && !tasks.data ? (
            <CardLoadError onRetry={tasks.reload} retrying={tasks.refreshing} />
          ) : null}
        </Card>
        <Card>
          <Statistic
            title="已保存回测"
            value={backtests.data?.total ?? "—"}
            loading={backtests.loading}
          />
          {backtests.error && !backtests.data ? (
            <CardLoadError
              onRetry={backtests.reload}
              retrying={backtests.refreshing}
            />
          ) : null}
        </Card>
      </div>

      <Section
        title="最近任务"
        extra={<AppLink to={paths.tasks()}>全部任务</AppLink>}
      >
        <Resource
          state={tasks}
          empty={(data) =>
            data.items.length ? null : (
              <EmptyState
                title="还没有提交过研究"
                action={
                  <Button
                    type="primary"
                    onClick={() => navigate(paths.newResearch())}
                  >
                    新建研究
                  </Button>
                }
              />
            )
          }
        >
          {(data) => (
            <TaskTable
              tasks={data.items.slice(0, 6)}
              onChanged={tasks.reload}
              compact
            />
          )}
        </Resource>
      </Section>

      <Section
        title="最近回测"
        extra={<AppLink to={paths.backtests()}>全部回测</AppLink>}
      >
        <Resource
          state={backtests}
          empty={(data) =>
            data.items.length ? null : <EmptyState title="暂无回测结果" />
          }
        >
          {(data) =>
            isMobile ? (
              <MobileCardList
                paginate={false}
                items={data.items.slice(0, 6).map((row) => ({
                  key: row.id,
                  title: row.name,
                  meta: [
                    <span key="ret">
                      总收益{" "}
                      <Signed
                        value={row.metrics?.total_return}
                        format={(v) => formatPercent(v)}
                      />
                    </span>,
                    <span key="dd">
                      回撤{" "}
                      <span className="numeric-token">
                        {formatPercent(row.metrics?.max_drawdown)}
                      </span>
                    </span>,
                    <span key="range" className="numeric-token">
                      {row.start_date} ~ {row.end_date}
                    </span>,
                    <span key="created" className="numeric-token">
                      {formatDateTime(row.created_at)}
                    </span>,
                  ],
                  onClick: () => navigate(paths.backtest(row.id)),
                }))}
              />
            ) : (
              <TableScroll label="最近回测表">
                <Table<BacktestSummary>
                  rowKey="id"
                  size="small"
                  pagination={false}
                  dataSource={data.items.slice(0, 6)}
                  rowClassName={() => "clickable-row"}
                  onRow={(row) => ({
                    onClick: () => navigate(paths.backtest(row.id)),
                  })}
                  columns={[
                    {
                      title: "回测",
                      key: "name",
                      render: (_, row) => (
                        <Space orientation="vertical" size={0}>
                          <Typography.Text strong>{row.name}</Typography.Text>
                          <Typography.Text
                            type="secondary"
                            style={{ fontSize: 12 }}
                          >
                            {row.start_date} ~ {row.end_date}
                          </Typography.Text>
                        </Space>
                      ),
                    },
                    {
                      title: "总收益",
                      key: "ret",
                      align: "right",
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
                      align: "right",
                      render: (_, row) =>
                        formatPercent(row.metrics?.max_drawdown),
                    },
                    {
                      title: "创建时间",
                      key: "created",
                      width: 160,
                      render: (_, row) => (
                        <Flex>{formatDateTime(row.created_at)}</Flex>
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
