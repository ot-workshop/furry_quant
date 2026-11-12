import { useMemo, useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  DatePicker,
  Descriptions,
  Flex,
  Form,
  Input,
  InputNumber,
  List,
  Progress,
  Segmented,
  Select,
  Space,
  Statistic,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import {
  CloudDownloadOutlined,
  EyeOutlined,
  PauseCircleOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";
import { api } from "../api/client";
import {
  dataApi,
  type FinancialJob,
  type FinancialPlan,
  type IndustryJob,
  type IndustryLevel,
  type IndustryPlan,
  type MarketJob,
  type MarketPlan,
  type MarketScope,
  type MarketVersion,
} from "../api/data";
import { describeError } from "../api/http";
import { navigate, paths, setSearch } from "../app/routes";
import { CrossValidationTable } from "../components/audit";
import {
  EmptyState,
  Notes,
  PageHeader,
  Resource,
  Section,
  StatusTag,
} from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import type {
  HistoricalDataset,
  ResearchDataset,
  StockCoverage,
} from "../types";
import { dateText, formatDateTime, formatNumber } from "../utils/format";

const SCOPES: Record<MarketScope, string> = {
  custom: "自选股票",
  csi300: "沪深300历史成分",
  all_market: "全市场（沪深 A 股）",
};
const CROSS_STATUS: Record<string, string> = {
  verified: "双源核验通过",
  fallback: "部分回退",
  conflict: "存在冲突",
};
const VALIDATION: Record<string, string> = {
  single_source_passed: "单源检查",
  dual_source_verified: "双源核验",
  secondary_fallback_passed: "补缺通过",
};

export default function DataPage({ tab }: { tab: string | null }) {
  return (
    <>
      <PageHeader
        title="数据管理"
        subtitle="下载并核验行情，生成可用于研究的数据集"
      />
      <Tabs
        activeKey={tab ?? "datasets"}
        onChange={(value) =>
          setSearch({ tab: value === "datasets" ? null : value })
        }
        items={[
          { key: "datasets", label: "研究数据集", children: <Datasets /> },
          { key: "market", label: "行情下载", children: <MarketDownload /> },
          { key: "financial", label: "财务数据", children: <FinancialData /> },
          { key: "industry", label: "行业数据", children: <IndustryData /> },
          { key: "database", label: "数据库", children: <Database /> },
        ]}
      />
    </>
  );
}

function Datasets() {
  const status = useApiResource((signal) => api.dataStatus(signal), []);
  return (
    <Section
      title="研究数据集"
      description="自选股票下载完成后生成固定股票池（最多 50 只、覆盖完整）；沪深300下载完成后生成历史成分动态股票池。“新建研究”中可直接选择"
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
          data.snapshots.length || data.historical_indices?.length ? null : (
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

function HistoricalDatasets({ items }: { items: HistoricalDataset[] }) {
  return (
    <Flex vertical gap={8}>
      <Typography.Text strong>沪深300历史成分（动态股票池）</Typography.Text>
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
                  沪深300历史成分 · 累计 {row.codes_count} 只
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

function lastCompleteDay() {
  const now = new Date();
  if (now.getHours() < 18) now.setDate(now.getDate() - 1);
  return dayjs(now).format("YYYY-MM-DD");
}

function parseCodes(text: string) {
  return text
    .toUpperCase()
    .split(/[\s,，;；、]+/)
    .filter(Boolean)
    .map((code) => code.replace(/^(SH|SZ)(\d{6})$/, "$2.$1"));
}

function MarketDownload() {
  const { message } = App.useApp();
  const status = useApiResource((signal) => dataApi.market(signal), [], {
    pollMs: 5000,
  });
  const [scope, setScope] = useState<MarketScope>("custom");
  const [range, setRange] = useState<[string, string]>([
    dayjs().subtract(1, "year").startOf("year").format("YYYY-MM-DD"),
    lastCompleteDay(),
  ]);
  const [warmup, setWarmup] = useState(127);
  const [codes, setCodes] = useState("");
  const [plan, setPlan] = useState<MarketPlan | null>(null);
  const [busy, setBusy] = useState<"preview" | "start" | "pause" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const running = status.data?.worker_running ?? false;
  const parsed = useMemo(() => parseCodes(codes), [codes]);
  const edit =
    <T,>(setter: (value: T) => void) =>
    (value: T) => {
      setter(value);
      setPlan(null);
      setError(null);
    };

  async function preview() {
    setBusy("preview");
    setError(null);
    try {
      setPlan(
        await dataApi.previewMarket({
          scope,
          start: range[0],
          end: range[1],
          warmup,
          codes: parsed,
        }),
      );
    } catch (e) {
      setError(describeError(e));
    } finally {
      setBusy(null);
    }
  }

  async function start(planId: string) {
    setBusy("start");
    try {
      await dataApi.startMarket(planId);
      message.success("已加入下载队列，后台工作进程会依次处理");
      setPlan(null);
      status.reload();
    } catch (e) {
      message.error(describeError(e));
    } finally {
      setBusy(null);
    }
  }

  async function pause() {
    setBusy("pause");
    try {
      await dataApi.pauseMarket();
      message.info("已请求暂停，当前股票处理完后停止；已核验的数据会保留");
      status.reload();
    } finally {
      setBusy(null);
    }
  }

  return (
    <Flex vertical gap={16}>
      <div className="with-aside">
        <Card title="1. 选择下载范围">
          <Form layout="vertical">
            <Form.Item label="范围">
              <Segmented<MarketScope>
                value={scope}
                onChange={edit(setScope)}
                options={(Object.keys(SCOPES) as MarketScope[]).map(
                  (value) => ({ value, label: SCOPES[value] }),
                )}
              />
            </Form.Item>
            <div className="grid-2">
              <Form.Item
                label="研究区间"
                extra="结束日期最晚为最近一个已收盘交易日（18:00 后可取当天）"
              >
                <DatePicker.RangePicker
                  value={[dayjs(range[0]), dayjs(range[1])]}
                  allowClear={false}
                  disabledDate={(day) => day.isAfter(dayjs(lastCompleteDay()))}
                  onChange={(values) =>
                    values?.[0] &&
                    values[1] &&
                    edit(setRange)([
                      values[0].format("YYYY-MM-DD"),
                      values[1].format("YYYY-MM-DD"),
                    ])
                  }
                  style={{ width: "100%" }}
                />
              </Form.Item>
              <Form.Item
                label="向前预热"
                tooltip="研究区间开始前额外下载的交易日，用于计算长窗口因子"
              >
                <InputNumber
                  min={0}
                  max={504}
                  value={warmup}
                  onChange={(v) => edit(setWarmup)(Number(v ?? 0))}
                  suffix="个交易日"
                  style={{ width: "100%" }}
                />
              </Form.Item>
            </div>
            <Form.Item
              label={scope === "custom" ? "股票代码" : "限定股票（可选）"}
              required={scope === "custom"}
              extra={`支持 600000.SH / SH600000，逗号、空格或换行分隔；已识别 ${parsed.length} 个。${scope === "custom" ? "不超过 50 只时会自动生成研究数据集。" : "留空表示范围内全部股票。"}`}
            >
              <Input.TextArea
                rows={3}
                value={codes}
                onChange={(e) => edit(setCodes)(e.target.value)}
                placeholder="600000.SH, 000001.SZ, 600519.SH"
              />
            </Form.Item>
            <Button
              icon={<EyeOutlined />}
              loading={busy === "preview"}
              disabled={running || (scope === "custom" && !parsed.length)}
              onClick={() => void preview()}
            >
              预览下载方案
            </Button>
            <Typography.Text
              type="secondary"
              style={{ marginLeft: 12, fontSize: 12 }}
            >
              预览只查询交易日历与证券资料，不下载行情
            </Typography.Text>
            {error ? (
              <Alert
                type="error"
                showIcon
                title={error}
                style={{ marginTop: 12 }}
              />
            ) : null}
          </Form>
        </Card>
        <div className="aside-sticky">
          <Card title="2. 确认并下载">
            {plan ? (
              <Flex vertical gap={12}>
                <Descriptions
                  size="small"
                  column={1}
                  items={[
                    {
                      key: "scope",
                      label: "范围",
                      children: `${SCOPES[plan.spec.scope]}${plan.restricted ? "（限定子集）" : ""}`,
                    },
                    {
                      key: "codes",
                      label: "股票",
                      children: `${plan.codes_count} 只`,
                    },
                    {
                      key: "range",
                      label: "区间",
                      children: `${plan.start} ~ ${plan.end}`,
                    },
                    {
                      key: "data",
                      label: "含预热自",
                      children: plan.data_start,
                    },
                    {
                      key: "reuse",
                      label: "可复用",
                      children: `${formatNumber(plan.reused_rows, 0)} 条`,
                    },
                    {
                      key: "missing",
                      label: "需下载",
                      children: `${formatNumber(plan.missing_rows, 0)} 条`,
                    },
                    {
                      key: "requests",
                      label: "预计请求",
                      children: `${formatNumber(plan.estimated_quote_requests, 0)} 次`,
                    },
                  ]}
                />
                <Notes
                  items={[
                    plan.note,
                    plan.excluded_unsupported
                      ? `已排除 ${plan.excluded_unsupported} 只暂不支持的证券。`
                      : null,
                  ]}
                />
                <Button
                  type="primary"
                  icon={<CloudDownloadOutlined />}
                  loading={busy === "start"}
                  disabled={running}
                  onClick={() => void start(plan.id)}
                >
                  {plan.missing_rows ? "确认下载" : "无需下载，直接发布版本"}
                </Button>
              </Flex>
            ) : (
              <Typography.Text type="secondary">
                先在左侧设置范围并点击“预览下载方案”。修改任何参数后需要重新预览。
              </Typography.Text>
            )}
          </Card>
        </div>
      </div>
      <Section
        title="下载记录"
        description={running ? "后台正在下载，每 5 秒刷新" : undefined}
        extra={
          running ? (
            <Button
              icon={<PauseCircleOutlined />}
              loading={busy === "pause"}
              onClick={() => void pause()}
            >
              暂停
            </Button>
          ) : null
        }
      >
        <Resource
          state={status}
          empty={(data) =>
            data.jobs.length ? null : <EmptyState title="还没有下载记录" />
          }
        >
          {(data) => (
            <Table<MarketJob>
              rowKey="id"
              size="small"
              dataSource={data.jobs}
              pagination={{ pageSize: 10, hideOnSinglePage: true }}
              expandable={{
                rowExpandable: (job) => job.failures.length > 0,
                expandedRowRender: (job) => (
                  <List
                    size="small"
                    dataSource={job.failures}
                    renderItem={(failure) => (
                      <List.Item>
                        <Typography.Text type="danger">
                          {failure.code}：{failure.reason}
                        </Typography.Text>
                      </List.Item>
                    )}
                  />
                ),
              }}
              columns={[
                {
                  title: "范围",
                  render: (_, job) =>
                    `${SCOPES[job.spec.scope]} · ${job.spec.start} ~ ${job.spec.end}`,
                },
                {
                  title: "状态",
                  render: (_, job) => <StatusTag status={job.status} />,
                },
                {
                  title: "进度",
                  width: 220,
                  render: (_, job) => (
                    <Progress
                      size="small"
                      percent={
                        job.total
                          ? Math.round((job.completed / job.total) * 100)
                          : 0
                      }
                      format={() => `${job.completed}/${job.total}`}
                      status={
                        job.status === "running"
                          ? "active"
                          : job.failures.length
                            ? "exception"
                            : undefined
                      }
                    />
                  ),
                },
                {
                  title: "说明",
                  render: (_, job) => (
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      {job.status === "running"
                        ? `当前 ${job.current_code ?? "准备中"}`
                        : job.message}
                    </Typography.Text>
                  ),
                },
                {
                  title: "",
                  align: "right",
                  render: (_, job) =>
                    ["paused", "partial", "interrupted"].includes(
                      job.status,
                    ) ? (
                      <Button
                        size="small"
                        disabled={running}
                        onClick={() => void start(job.id)}
                      >
                        续传
                      </Button>
                    ) : null,
                },
              ]}
            />
          )}
        </Resource>
      </Section>
      <Section
        title="已发布版本"
        description="每次下载完成后发布不可变版本，旧版本和已有回测不受影响"
      >
        <Resource
          state={status}
          empty={(data) =>
            data.versions.length ? null : <EmptyState title="还没有发布版本" />
          }
        >
          {(data) => (
            <Table<MarketVersion>
              rowKey="id"
              size="small"
              dataSource={data.versions}
              pagination={{ pageSize: 10, hideOnSinglePage: true }}
              columns={[
                {
                  title: "范围",
                  render: (_, v) =>
                    `${SCOPES[v.scope]}${v.restricted ? "（子集）" : ""} · ${v.codes_count} 只`,
                },
                { title: "区间", render: (_, v) => `${v.start} ~ ${v.end}` },
                {
                  title: "发布时间",
                  render: (_, v) => formatDateTime(v.created_at),
                },
                {
                  title: "可用于",
                  render: (_, v) =>
                    v.snapshot_id ? (
                      <Tag color="success">固定股票池研究</Tag>
                    ) : v.scope === "csi300" && !v.restricted ? (
                      <Tag color="processing">历史成分研究</Tag>
                    ) : (
                      <Tag>仅归档</Tag>
                    ),
                },
              ]}
            />
          )}
        </Resource>
      </Section>
    </Flex>
  );
}

function FinancialData() {
  const { message } = App.useApp();
  const status = useApiResource((signal) => dataApi.financial(signal), [], {
    pollMs: 5000,
  });
  const [marketVersion, setMarketVersion] = useState("");
  const [refreshDays, setRefreshDays] = useState(7);
  const [plan, setPlan] = useState<FinancialPlan | null>(null);
  const [busy, setBusy] = useState<"preview" | "start" | "pause" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const running = status.data?.worker_running ?? false;
  const versions = status.data?.market_versions ?? [];
  const selected =
    versions.find((item) => item.id === marketVersion) ?? versions[0];
  const version = status.data?.version ?? null;

  function edit<T>(setter: (value: T) => void) {
    return (value: T) => {
      setter(value);
      setPlan(null);
      setError(null);
    };
  }

  async function preview() {
    if (!selected) return;
    setBusy("preview");
    setError(null);
    try {
      setPlan(
        await dataApi.previewFinancial({
          market_version: selected.id,
          refresh_days: refreshDays,
        }),
      );
    } catch (e) {
      setError(describeError(e));
    } finally {
      setBusy(null);
    }
  }

  async function start(planId: string) {
    setBusy("start");
    try {
      await dataApi.startFinancial(planId);
      message.success("已加入下载队列，后台工作进程会依次处理");
      setPlan(null);
      status.reload();
    } catch (e) {
      message.error(describeError(e));
    } finally {
      setBusy(null);
    }
  }

  async function pause() {
    setBusy("pause");
    try {
      await dataApi.pauseFinancial();
      message.info("已请求暂停，当前股票处理完后停止；已完成的数据会保留");
      status.reload();
    } finally {
      setBusy(null);
    }
  }

  return (
    <Flex vertical gap={16}>
      <div className="with-aside">
        <Card title="1. 选择更新范围">
          <Form layout="vertical">
            <Form.Item
              label="行情版本"
              extra="财务数据的股票清单来自已发布的行情版本；先下载行情，再下载这些股票的财报。"
            >
              <Select
                value={selected?.id}
                placeholder="选择已发布的行情版本"
                onChange={(value) => edit(setMarketVersion)(value)}
                options={versions.map((item) => ({
                  value: item.id,
                  label: `${SCOPES[item.scope]}${item.restricted ? "（子集）" : ""} · ${item.codes_count} 只 · ${item.start} ~ ${item.end}`,
                }))}
              />
            </Form.Item>
            <Form.Item
              label="更新间隔"
              extra="间隔内已抓取过的股票会跳过；0 表示全部重新抓取（用于补抓更正公告）。"
            >
              <InputNumber
                min={0}
                max={3650}
                value={refreshDays}
                onChange={(v) => edit(setRefreshDays)(Number(v ?? 0))}
                suffix="天"
                style={{ width: "100%" }}
              />
            </Form.Item>
            <Button
              icon={<EyeOutlined />}
              loading={busy === "preview"}
              disabled={running || !selected}
              onClick={() => void preview()}
            >
              预览要下载的股票
            </Button>
            <Typography.Text
              type="secondary"
              style={{ marginLeft: 12, fontSize: 12 }}
            >
              预览只读取本地记录，不调用接口
            </Typography.Text>
            {error ? (
              <Alert
                type="error"
                showIcon
                title={error}
                style={{ marginTop: 12 }}
              />
            ) : null}
          </Form>
        </Card>
        <div className="aside-sticky">
          <Card title="2. 确认并下载">
            {plan ? (
              <Flex vertical gap={12}>
                <Descriptions
                  size="small"
                  column={1}
                  items={[
                    {
                      key: "codes",
                      label: "待下载股票",
                      children: `${plan.codes.length} 只`,
                    },
                    {
                      key: "skipped",
                      label: "跳过",
                      children: `${plan.skipped} 只（近期已更新）`,
                    },
                    {
                      key: "requests",
                      label: "预计请求",
                      children: `${formatNumber(plan.estimated_requests, 0)} 次（每只 ${plan.requests_per_stock} 次）`,
                    },
                  ]}
                />
                <Notes items={[plan.note]} />
                <Button
                  type="primary"
                  icon={<CloudDownloadOutlined />}
                  loading={busy === "start"}
                  disabled={running}
                  onClick={() => void start(plan.id)}
                >
                  {plan.codes.length ? "确认下载" : "无需下载，直接发布版本"}
                </Button>
              </Flex>
            ) : (
              <Typography.Text type="secondary">
                先在左侧选择行情版本并点击“预览要下载的股票”。
              </Typography.Text>
            )}
          </Card>
        </div>
      </div>
      <Section
        title="当前版本"
        description="研究提交时会绑定当前版本；新版本不影响已有结果"
      >
        {version ? (
          <Descriptions
            size="small"
            column={{ xs: 1, md: 3 }}
            items={[
              { key: "id", label: "版本", children: version.id.slice(0, 12) },
              {
                key: "codes",
                label: "覆盖股票",
                children: `${formatNumber(version.codes, 0)} 只`,
              },
              {
                key: "rows",
                label: "报表行数",
                children: formatNumber(version.rows, 0),
              },
              {
                key: "period",
                label: "最新报告期",
                children: version.latest_report_period ?? "—",
              },
              {
                key: "quarantine",
                label: "隔离行",
                children: formatNumber(version.quarantined, 0),
              },
              {
                key: "created",
                label: "发布时间",
                children: formatDateTime(version.created_at),
              },
            ]}
          />
        ) : (
          <EmptyState
            title="还没有财务数据"
            description="选择已发布的行情版本下载财报；银行、保险等缺毛利率字段的行业会在诊断中注明。"
          />
        )}
      </Section>
      <Section
        title="下载记录"
        description={running ? "后台正在下载，每 5 秒刷新" : undefined}
        extra={
          running ? (
            <Button
              icon={<PauseCircleOutlined />}
              loading={busy === "pause"}
              onClick={() => void pause()}
            >
              暂停
            </Button>
          ) : null
        }
      >
        <Resource
          state={status}
          empty={(data) =>
            data.jobs.length ? null : <EmptyState title="还没有下载记录" />
          }
        >
          {(data) => (
            <Table<FinancialJob>
              rowKey="id"
              size="small"
              dataSource={data.jobs}
              pagination={{ pageSize: 10, hideOnSinglePage: true }}
              expandable={{
                rowExpandable: (job) => job.failures.length > 0,
                expandedRowRender: (job) => (
                  <List
                    size="small"
                    dataSource={job.failures}
                    renderItem={(failure) => (
                      <List.Item>
                        <Typography.Text type="danger">
                          {failure.code}：{failure.reason}
                        </Typography.Text>
                      </List.Item>
                    )}
                  />
                ),
              }}
              columns={[
                {
                  title: "状态",
                  render: (_, job) => <StatusTag status={job.status} />,
                },
                {
                  title: "进度",
                  width: 220,
                  render: (_, job) => (
                    <Progress
                      size="small"
                      percent={
                        job.total
                          ? Math.round((job.completed / job.total) * 100)
                          : 0
                      }
                      format={() => `${job.completed}/${job.total}`}
                      status={
                        job.status === "running"
                          ? "active"
                          : job.failures.length
                            ? "exception"
                            : undefined
                      }
                    />
                  ),
                },
                {
                  title: "新增行",
                  align: "right",
                  render: (_, job) => formatNumber(job.downloaded_rows, 0),
                },
                {
                  title: "说明",
                  render: (_, job) => (
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      {job.status === "running"
                        ? `当前 ${job.current_code ?? "准备中"}`
                        : job.message}
                    </Typography.Text>
                  ),
                },
                {
                  title: "",
                  align: "right",
                  render: (_, job) =>
                    ["paused", "partial", "interrupted"].includes(
                      job.status,
                    ) ? (
                      <Button
                        size="small"
                        disabled={running}
                        onClick={() => void start(job.id)}
                      >
                        续传
                      </Button>
                    ) : null,
                },
              ]}
            />
          )}
        </Resource>
      </Section>
    </Flex>
  );
}

const INDUSTRY_LEVEL_LABELS: Record<IndustryLevel, string> = {
  L1: "一级",
  L2: "二级",
  L3: "三级",
};
const INDUSTRY_LEVEL_OPTIONS = (["L1", "L2", "L3"] as IndustryLevel[]).map(
  (level) => ({ value: level, label: INDUSTRY_LEVEL_LABELS[level] }),
);

function levelTags(
  levels:
    | Partial<
        Record<
          IndustryLevel,
          { label: string; sectors: number; instruments: number }
        >
      >
    | undefined,
) {
  return Object.entries(levels ?? {}).map(([level, value]) => (
    <Tag key={level}>
      {value?.label ?? INDUSTRY_LEVEL_LABELS[level as IndustryLevel]} ·{" "}
      {value?.sectors ?? 0} 板块 / {value?.instruments ?? 0} 只
    </Tag>
  ));
}

function IndustryData() {
  const { message } = App.useApp();
  const status = useApiResource((signal) => dataApi.industry(signal), [], {
    pollMs: 5000,
  });
  const [levels, setLevels] = useState<IndustryLevel[]>(["L1", "L2", "L3"]);
  const [refresh, setRefresh] = useState(false);
  const [plan, setPlan] = useState<IndustryPlan | null>(null);
  const [busy, setBusy] = useState<"preview" | "start" | "pause" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const running = status.data?.worker_running ?? false;
  const version = status.data?.version ?? null;
  const edit =
    <T,>(setter: (value: T) => void) =>
    (value: T) => {
      setter(value);
      setPlan(null);
      setError(null);
    };

  async function preview() {
    if (!levels.length) {
      setError("请至少选择一个行业级别。");
      return;
    }
    setBusy("preview");
    setError(null);
    try {
      setPlan(await dataApi.previewIndustry({ levels, refresh }));
    } catch (e) {
      setError(describeError(e));
    } finally {
      setBusy(null);
    }
  }

  async function start(planId: string) {
    setBusy("start");
    try {
      await dataApi.startIndustry(planId);
      message.success("已加入下载队列，后台工作进程会依次处理");
      setPlan(null);
      status.reload();
    } catch (e) {
      message.error(describeError(e));
    } finally {
      setBusy(null);
    }
  }

  async function pause() {
    setBusy("pause");
    try {
      await dataApi.pauseIndustry();
      message.info("已请求暂停，当前板块处理完后停止；已完成的数据会保留");
      status.reload();
    } finally {
      setBusy(null);
    }
  }

  return (
    <Flex vertical gap={16}>
      <div className="with-aside">
        <Card title="1. 选择下载级别">
          <Form layout="vertical">
            <Form.Item
              label="申万行业级别"
              extra="包含申万2014与2021历史分类；默认全部级别。行业中性化可任选一级、二级或三级。"
            >
              <Checkbox.Group
                options={INDUSTRY_LEVEL_OPTIONS}
                value={levels}
                onChange={(values) =>
                  edit(setLevels)(values as IndustryLevel[])
                }
              />
            </Form.Item>
            <Form.Item
              label="刷新缓存"
              extra="勾选后重新抓取分类与全部成分；不勾选会复用已下载分区。"
            >
              <Switch checked={refresh} onChange={edit(setRefresh)} />
            </Form.Item>
            <Button
              icon={<EyeOutlined />}
              loading={busy === "preview"}
              disabled={running || !levels.length}
              onClick={() => void preview()}
            >
              预览下载
            </Button>
            <Typography.Text
              type="secondary"
              style={{ marginLeft: 12, fontSize: 12 }}
            >
              预览只读取分类目录，不下载成分
            </Typography.Text>
            {error ? (
              <Alert
                type="error"
                showIcon
                title={error}
                style={{ marginTop: 12 }}
              />
            ) : null}
          </Form>
        </Card>
        <div className="aside-sticky">
          <Card title="2. 确认并下载">
            {plan ? (
              <Flex vertical gap={12}>
                <Descriptions
                  size="small"
                  column={1}
                  items={[
                    {
                      key: "levels",
                      label: "级别",
                      children: plan.spec.levels
                        .map((level) => INDUSTRY_LEVEL_LABELS[level])
                        .join("、"),
                    },
                    {
                      key: "sectors",
                      label: "板块",
                      children: Object.entries(plan.sectors)
                        .map(([key, count]) => `${key} ${count}`)
                        .join(" · "),
                    },
                    {
                      key: "requests",
                      label: "预计请求",
                      children: `${formatNumber(plan.estimated_requests, 0)} 次`,
                    },
                    {
                      key: "minutes",
                      label: "预计耗时",
                      children: `约 ${plan.estimated_minutes} 分钟`,
                    },
                    {
                      key: "current",
                      label: "当前版本",
                      children: plan.current_version
                        ? plan.current_version.slice(0, 12)
                        : "无",
                    },
                  ]}
                />
                <Notes items={[plan.note]} />
                <Button
                  type="primary"
                  icon={<CloudDownloadOutlined />}
                  loading={busy === "start"}
                  disabled={running}
                  onClick={() => void start(plan.id)}
                >
                  确认下载
                </Button>
              </Flex>
            ) : (
              <Typography.Text type="secondary">
                先在左侧选择级别并点击“预览下载”。修改任何参数后需要重新预览。
              </Typography.Text>
            )}
          </Card>
        </div>
      </div>
      <Section
        title="当前版本"
        description="研究提交时会绑定当前版本；新版本不影响已有结果"
      >
        {version ? (
          <Descriptions
            size="small"
            column={{ xs: 1, md: 3 }}
            items={[
              { key: "id", label: "版本", children: version.id.slice(0, 12) },
              {
                key: "levels",
                label: "级别",
                children: levelTags(version.levels),
              },
              {
                key: "instruments",
                label: "股票",
                children: `${formatNumber(version.instruments, 0)} 只`,
              },
              {
                key: "rows",
                label: "区间记录",
                children: formatNumber(version.rows, 0),
              },
              {
                key: "asof",
                label: "核验日期",
                children: version.data_asof ?? "—",
              },
              {
                key: "created",
                label: "发布时间",
                children: formatDateTime(version.created_at),
              },
            ]}
          />
        ) : (
          <EmptyState
            title="还没有行业数据"
            description="选择级别后下载；行业中性化会使用这里发布的版本。"
          />
        )}
      </Section>
      <Section
        title="下载记录"
        description={running ? "后台正在下载，每 5 秒刷新" : undefined}
        extra={
          running ? (
            <Button
              icon={<PauseCircleOutlined />}
              loading={busy === "pause"}
              onClick={() => void pause()}
            >
              暂停
            </Button>
          ) : null
        }
      >
        <Resource
          state={status}
          empty={(data) =>
            data.jobs.length ? null : <EmptyState title="还没有下载记录" />
          }
        >
          {(data) => (
            <Table<IndustryJob>
              rowKey="id"
              size="small"
              dataSource={data.jobs}
              pagination={{ pageSize: 10, hideOnSinglePage: true }}
              expandable={{
                rowExpandable: (job) => job.failures.length > 0,
                expandedRowRender: (job) => (
                  <List
                    size="small"
                    dataSource={job.failures}
                    renderItem={(failure) => (
                      <List.Item>
                        <Typography.Text type="danger">
                          {failure.taxonomy} {failure.level} {failure.code}：
                          {failure.reason}
                        </Typography.Text>
                      </List.Item>
                    )}
                  />
                ),
              }}
              columns={[
                {
                  title: "级别",
                  render: (_, job) =>
                    job.levels
                      .map((level) => INDUSTRY_LEVEL_LABELS[level])
                      .join("、"),
                },
                {
                  title: "状态",
                  render: (_, job) => <StatusTag status={job.status} />,
                },
                {
                  title: "进度",
                  width: 220,
                  render: (_, job) => (
                    <Progress
                      size="small"
                      percent={
                        job.total
                          ? Math.round((job.completed / job.total) * 100)
                          : 0
                      }
                      format={() => `${job.completed}/${job.total}`}
                      status={
                        job.status === "running"
                          ? "active"
                          : job.failures.length
                            ? "exception"
                            : undefined
                      }
                    />
                  ),
                },
                {
                  title: "说明",
                  render: (_, job) => (
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      {job.message}
                    </Typography.Text>
                  ),
                },
                {
                  title: "",
                  align: "right",
                  render: (_, job) =>
                    ["paused", "partial", "interrupted"].includes(
                      job.status,
                    ) ? (
                      <Button
                        size="small"
                        disabled={running}
                        onClick={() => void start(job.id)}
                      >
                        续传
                      </Button>
                    ) : null,
                },
              ]}
            />
          )}
        </Resource>
      </Section>
      <Section title="已发布版本">
        <Resource
          state={status}
          empty={(data) =>
            data.versions.length ? null : <EmptyState title="还没有发布版本" />
          }
        >
          {(data) => (
            <Table
              rowKey="id"
              size="small"
              dataSource={data.versions}
              pagination={{ pageSize: 10, hideOnSinglePage: true }}
              columns={[
                {
                  title: "版本",
                  render: (_, row: (typeof data.versions)[number]) => (
                    <Space>
                      <Typography.Text code>
                        {row.id.slice(0, 12)}
                      </Typography.Text>
                      {row.current ? <Tag color="success">当前</Tag> : null}
                    </Space>
                  ),
                },
                {
                  title: "级别",
                  render: (_, row: (typeof data.versions)[number]) =>
                    levelTags(row.levels),
                },
                {
                  title: "记录 / 股票",
                  render: (_, row: (typeof data.versions)[number]) =>
                    `${formatNumber(row.rows, 0)} / ${formatNumber(row.instruments, 0)}`,
                },
                {
                  title: "核验日期",
                  render: (_, row: (typeof data.versions)[number]) =>
                    row.data_asof ?? "—",
                },
                {
                  title: "发布时间",
                  render: (_, row: (typeof data.versions)[number]) =>
                    formatDateTime(row.created_at),
                },
              ]}
            />
          )}
        </Resource>
      </Section>
    </Flex>
  );
}

function Database() {
  const status = useApiResource((signal) => api.dataStatus(signal), []);
  return (
    <Resource state={status}>
      {(data) =>
        data.database ? (
          <Flex vertical gap={16}>
            <div className="grid-4">
              <Card>
                <Statistic title="股票" value={data.database.securities} />
              </Card>
              <Card>
                <Statistic title="数据发布" value={data.database.releases} />
              </Card>
              <Card>
                <Statistic title="修订" value={data.database.revisions} />
              </Card>
              <Card>
                <Statistic title="冻结数据集" value={data.database.datasets} />
              </Card>
            </div>
            <Section title="行情表">
              <Descriptions
                size="small"
                column={{ xs: 1, md: 3 }}
                items={[
                  {
                    key: "range",
                    label: "覆盖区间",
                    children: data.database.coverage_start
                      ? `${data.database.coverage_start} ~ ${data.database.coverage_end}`
                      : "—",
                  },
                  {
                    key: "schema",
                    label: "结构版本",
                    children: data.database.schema_version,
                  },
                  {
                    key: "latest",
                    label: "最近发布",
                    children: data.database.latest_release
                      ? `${formatDateTime(data.database.latest_release.created_at)} · 变更 ${data.database.latest_release.changed_rows} 行`
                      : "—",
                  },
                  ...Object.entries(data.database.row_counts).map(
                    ([table, count]) => ({
                      key: table,
                      label: table,
                      children: formatNumber(count, 0),
                    }),
                  ),
                ]}
              />
            </Section>
            <Section title="数据检查">
              <Flex gap={8} wrap>
                {Object.entries(data.database.checks).map(([key, count]) => (
                  <Tag key={key}>
                    {key}：{count}
                  </Tag>
                ))}
              </Flex>
            </Section>
          </Flex>
        ) : (
          <EmptyState
            title="未连接数据库"
            description="开发环境或测试环境中没有配置 PostgreSQL 时不显示。"
          />
        )
      }
    </Resource>
  );
}
