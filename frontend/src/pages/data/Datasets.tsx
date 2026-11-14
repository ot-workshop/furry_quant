import { Button, Flex, Table, Tag, Typography } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { api } from "../../api/client";
import { navigate, paths, setSearch } from "../../app/routes";
import { CrossValidationTable } from "../../components/audit";
import {
  EmptyState,
  Notes,
  Resource,
  Section,
  StatusTag,
} from "../../components/ui";
import { useApiResource } from "../../hooks/useApiResource";
import type {
  HistoricalDataset,
  ResearchDataset,
  StockCoverage,
} from "../../types";
import { dateText } from "../../utils/format";
import { CROSS_STATUS, VALIDATION } from "./shared";
export default function Datasets() {
  const status = useApiResource((signal) => api.dataStatus(signal), []);
  return (
    <Section
      title="研究数据集"
      description="行情下载完成后可用于固定股票池、沪深300历史成分或全市场条件筛选。不同研究复用共享行情，回测绑定所选数据版本。"
      extra={
        <Button
          icon={<ReloadOutlined />}
          onClick={status.reload}
          loading={status.refreshing}
        >
          刷新
        </Button>
      }
    >
      <Resource
        state={status}
        empty={(data) =>
          data.snapshots.length ||
          data.historical_indices?.length ||
          data.market_universes?.length ? null : (
            <EmptyState
              title="还没有研究数据集"
              description="在“行情下载”中选择自选股票或沪深300，预览并确认下载后即会生成。"
              action={
                <Button
                  type="primary"
                  onClick={() => setSearch({ tab: "market" })}
                >
                  去下载行情
                </Button>
              }
            />
          )
        }
      >
        {(data) => (
          <Flex vertical gap={24}>
            {data.market_universes?.length ? (
              <HistoricalDatasets items={data.market_universes} market />
            ) : null}
            {data.historical_indices?.length ? (
              <HistoricalDatasets items={data.historical_indices} />
            ) : null}
            {data.snapshots.length ? (
              <SnapshotDatasets items={data.snapshots} />
            ) : null}
          </Flex>
        )}
      </Resource>
    </Section>
  );
}

function HistoricalDatasets({
  items,
  market = false,
}: {
  items: HistoricalDataset[];
  market?: boolean;
}) {
  return (
    <Flex vertical gap={8}>
      <Typography.Text strong>
        {market ? "共享行情仓库" : "沪深300历史成分（动态股票池）"}
      </Typography.Text>
      <Table<HistoricalDataset>
        rowKey="id"
        dataSource={items}
        pagination={false}
        columns={[
          {
            title: "数据集",
            render: (_, row) => (
              <Flex vertical>
                <Typography.Text strong>
                  {market ? row.label : "沪深300历史成分"} · 覆盖{" "}
                  {row.codes_count} 只
                </Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {row.shared_version ?? row.id}
                </Typography.Text>
              </Flex>
            ),
          },
          {
            title: "研究范围",
            render: (_, row) => `${row.start} ~ ${row.end}`,
          },
          {
            title: "行情起点（含预热）",
            render: (_, row) => row.data_start,
          },
          { title: "交易日", align: "right", dataIndex: "trading_days" },
          {
            title: "说明",
            render: (_, row) => (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {row.note}
              </Typography.Text>
            ),
          },
          {
            title: "",
            align: "right",
            render: (_, row) => (
              <Button
                size="small"
                type="link"
                onClick={() =>
                  navigate(
                    market
                      ? `${paths.newResearch()}?market=${encodeURIComponent(row.id)}`
                      : paths.newResearch(),
                  )
                }
              >
                用于研究
              </Button>
            ),
          },
        ]}
      />
    </Flex>
  );
}

function SnapshotDatasets({ items }: { items: ResearchDataset[] }) {
  return (
    <Flex vertical gap={8}>
      <Typography.Text strong>固定股票池</Typography.Text>
      <Table<ResearchDataset>
        rowKey="id"
        dataSource={items}
        pagination={{ pageSize: 20, hideOnSinglePage: true }}
        expandable={{
          expandedRowRender: (row) => <DatasetDetail dataset={row} />,
        }}
        columns={[
          {
            title: "数据集",
            render: (_, row) => (
              <Flex vertical>
                <Typography.Text strong>
                  {row.is_subset ? "研究股票子集" : "行情下载"} ·{" "}
                  {row.codes.length} 只
                </Typography.Text>
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {row.id}
                </Typography.Text>
              </Flex>
            ),
          },
          {
            title: "范围",
            render: (_, row) => `${dateText(row.start)} ~ ${dateText(row.end)}`,
          },
          { title: "交易日", align: "right", dataIndex: "trading_days" },
          {
            title: "覆盖",
            render: (_, row) => (
              <StatusTag
                status={row.coverage.status ?? "incomplete"}
                label={row.coverage.status === "complete" ? "完整" : "不完整"}
              />
            ),
          },
          {
            title: "核验",
            render: (_, row) =>
              row.validation_counts ? (
                Object.entries(row.validation_counts)
                  .filter(([, count]) => count)
                  .map(([level, count]) => (
                    <Tag key={level}>
                      {VALIDATION[level] ?? level} {count}
                    </Tag>
                  ))
              ) : row.cross_validation ? (
                <Tag>
                  {CROSS_STATUS[row.cross_validation.status] ??
                    row.cross_validation.status}
                </Tag>
              ) : (
                "—"
              ),
          },
          {
            title: "",
            align: "right",
            render: () => (
              <Button
                size="small"
                type="link"
                onClick={() => navigate(paths.newResearch())}
              >
                用于研究
              </Button>
            ),
          },
        ]}
      />
    </Flex>
  );
}

function DatasetDetail({ dataset }: { dataset: ResearchDataset }) {
  const stocks = [
    ...(dataset.coverage.stocks ?? []),
    ...(dataset.coverage.benchmark
      ? [
          {
            ...dataset.coverage.benchmark,
            code: `${dataset.coverage.benchmark.code}（基准）`,
          },
        ]
      : []),
  ];
  return (
    <Flex vertical gap={12}>
      <Table<StockCoverage>
        size="small"
        rowKey="code"
        pagination={{ pageSize: 10, hideOnSinglePage: true, size: "small" }}
        dataSource={stocks}
        columns={[
          { title: "代码", dataIndex: "code" },
          {
            title: "实际 / 应有交易日",
            align: "right",
            render: (_, row) => `${row.actual} / ${row.expected}`,
          },
          {
            title: "缺失",
            align: "right",
            render: (_, row) => row.missing_dates.length || "—",
          },
          {
            title: "异常",
            align: "right",
            render: (_, row) => row.invalid_dates.length || "—",
          },
          {
            title: "停牌",
            align: "right",
            render: (_, row) => row.suspended_dates?.length || "—",
          },
          {
            title: "状态",
            render: (_, row) =>
              row.complete ? (
                <Tag color="success">完整</Tag>
              ) : (
                <Tag color="warning">有缺口</Tag>
              ),
          },
        ]}
      />
      {dataset.cross_validation ? (
        <CrossValidationTable report={dataset.cross_validation} />
      ) : null}
      <Notes items={[dataset.coverage.note, ...dataset.notes]} />
    </Flex>
  );
}
