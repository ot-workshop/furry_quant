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

export default function DashboardPage() {
  const isMobile = useMediaQuery("(max-width: 767px)");
  const status = useApiResource((signal) => api.dataStatus(signal), []);
  const tasks = useApiResource((signal) => researchApi.tasks(signal), [], {
    pollMs: 5000,
  });
  const backtests = useApiResource((signal) => api.backtests(signal), []);

  const fixed = status.data?.snapshots.filter((item) => !item.is_subset) ?? [];
  const historical = status.data?.historical_indices ?? [];
  const market = status.data?.market_universes ?? [];
  const datasetCount = fixed.length + historical.length + market.length;
  const database = status.data?.database;
  const active =
    tasks.data?.items.filter(
      (t) => t.status === "queued" || t.status === "running",
    ) ?? [];
  const noData = status.data !== null && datasetCount === 0;

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
            value={datasetCount}
            suffix="个"
            loading={status.loading}
          />
          {status.data ? (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              固定股票池 {fixed.length} · 沪深300历史成分 {historical.length} ·
              条件筛选 {market.length}
            </Typography.Text>
          ) : null}
        </Card>
        <Card>
          <Statistic
            title="行情覆盖股票"
            value={database?.securities ?? "—"}
            suffix={database ? "只" : undefined}
            loading={status.loading}
          />
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {database?.coverage_start
              ? `${database.coverage_start} ~ ${database.coverage_end}`
              : "数据库未连接或暂无行情"}
          </Typography.Text>
        </Card>
        <Card>
          <Statistic
            title="进行中的任务"
            value={active.length}
            suffix="个"
            loading={tasks.loading}
          />
        </Card>
        <Card>
          <Statistic
            title="已保存回测"
            value={backtests.data?.total ?? "—"}
            loading={backtests.loading}
          />
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
                  badges: (
                    <Signed
                      value={row.metrics?.total_return}
                      format={(v) => formatPercent(v)}
                    />
                  ),
                  meta: [
                    <span key="range" className="numeric-token">
                      {row.start_date} ~ {row.end_date}
                    </span>,
                    <span key="dd">
                      回撤{" "}
                      <span className="numeric-token">
                        {formatPercent(row.metrics?.max_drawdown)}
                      </span>
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
