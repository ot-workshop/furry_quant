import { useMemo, useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  DatePicker,
  Descriptions,
  Flex,
  Form,
  Input,
  InputNumber,
  List,
  Popconfirm,
  Progress,
  Segmented,
  Statistic,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import {
  CloudDownloadOutlined,
  EyeOutlined,
  PauseCircleOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";
import { api } from "../api/client";
import {
  dataApi,
  type IndexHandlingReport,
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
import type { ResearchDataset, StockCoverage } from "../types";
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
          {
            key: "csi300",
            label: "沪深300历史仓库",
            children: <IndexWarehouse />,
          },
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
      description="行情下载完成后自动生成（最多 50 只、覆盖完整）；“新建研究”中可直接选择"
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
          data.snapshots.length ? null : (
            <EmptyState
              title="还没有研究数据集"
              description="在“行情下载”中选择自选股票，预览并确认下载后即会生成。"
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
          <Table<ResearchDataset>
            rowKey="id"
            dataSource={data.snapshots}
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
                render: (_, row) =>
                  `${dateText(row.start)} ~ ${dateText(row.end)}`,
              },
              { title: "交易日", align: "right", dataIndex: "trading_days" },
              {
                title: "覆盖",
                render: (_, row) => (
                  <StatusTag
                    status={row.coverage.status ?? "incomplete"}
                    label={
                      row.coverage.status === "complete" ? "完整" : "不完整"
                    }
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
        )}
      </Resource>
    </Section>
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

const INDEX_STATUS: Record<string, string> = {
  not_started: "未开始",
  running: "下载中",
  partial: "部分通过，有待复核",
  complete: "已完成",
  paused: "已暂停",
  interrupted: "已中断，可续传",
};

function IndexWarehouse() {
  const { message } = App.useApp();
  const status = useApiResource((signal) => dataApi.index(signal), [], {
    pollMs: 5000,
  });
  const [busy, setBusy] = useState(false);
  const [reportOpen, setReportOpen] = useState(false);

  async function act(kind: "start" | "pause") {
    setBusy(true);
    try {
      await (kind === "start" ? dataApi.startIndex() : dataApi.pauseIndex());
      message.success(
        kind === "start"
          ? "已加入队列，后台工作进程会开始或续传下载"
          : "已请求暂停",
      );
      status.reload();
    } catch (e) {
      message.error(describeError(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Resource state={status}>
      {(data) => {
        const total = data.total ?? 0;
        const bao = data.budget.requests
          .filter((r) => r.source === "baostock")
          .reduce((sum, r) => sum + r.count, 0);
        return (
          <Flex vertical gap={16}>
            <Card>
              <Flex justify="space-between" align="center" wrap gap={16}>
                <Flex gap={32} wrap>
                  <Statistic
                    title="状态"
                    value={
                      data.worker_running
                        ? data.pause_requested
                          ? "暂停中"
                          : "下载中"
                        : (INDEX_STATUS[data.status] ?? data.status)
                    }
                  />
                  <Statistic
                    title="已核验"
                    value={data.verified}
                    suffix={total ? `/ ${total}` : undefined}
                  />
                  <Statistic title="待复核" value={data.failed} />
                  <Statistic
                    title="BaoStock 今日请求"
                    value={bao}
                    suffix={`/ ${data.limits.baostock_daily_requests}`}
                  />
                </Flex>
                <Flex gap={8}>
                  <Popconfirm
                    title="开始或续传沪深300历史下载？"
                    description="会在后台排队执行，大约需要较长时间；已完成的股票不会重复下载。"
                    onConfirm={() => void act("start")}
                    disabled={data.worker_running || data.status === "complete"}
                  >
                    <Button
                      type="primary"
                      icon={<PlayCircleOutlined />}
                      loading={busy}
                      disabled={
                        data.worker_running || data.status === "complete"
                      }
                    >
                      {data.verified ? "续传" : "开始下载"}
                    </Button>
                  </Popconfirm>
                  <Button
                    icon={<PauseCircleOutlined />}
                    disabled={!data.worker_running || data.pause_requested}
                    onClick={() => void act("pause")}
                  >
                    暂停
                  </Button>
                </Flex>
              </Flex>
              {total ? (
                <Progress
                  style={{ marginTop: 16 }}
                  percent={Math.round((data.verified / total) * 100)}
                  status={data.worker_running ? "active" : undefined}
                />
              ) : null}
              {data.worker_running && data.current_code ? (
                <Typography.Text type="secondary">
                  当前：{data.current_code}
                </Typography.Text>
              ) : null}
              {data.budget.holds.length ? (
                <Alert
                  style={{ marginTop: 12 }}
                  type="warning"
                  showIcon
                  title="辅助数据源暂时限制访问，依赖它的抽样核验与补缺需等待"
                />
              ) : null}
            </Card>
            {data.plan ? (
              <Section title="下载计划">
                <Descriptions
                  size="small"
                  column={{ xs: 1, md: 3 }}
                  items={[
                    {
                      key: "range",
                      label: "研究区间",
                      children: `${data.plan.start} ~ ${data.plan.end}`,
                    },
                    {
                      key: "data",
                      label: "数据起点",
                      children: data.plan.data_start,
                    },
                    {
                      key: "warmup",
                      label: "预热",
                      children: `${data.plan.warmup_sessions} 个交易日`,
                    },
                    {
                      key: "snapshots",
                      label: "成分快照",
                      children: `${data.plan.snapshots} 期`,
                    },
                  ]}
                />
                <Notes
                  items={[
                    "官方 Tushare 为主源；按代码固定抽样约 10% 做 BaoStock 双源核验，异常时额外核验或补缺。",
                    "成分快照从其日期后的下一交易日起使用，不回填当月；该口径可能滞后实际调样日。",
                    data.validation_counts
                      ? `单源 ${data.validation_counts.single_source_passed} 只，双源 ${data.validation_counts.dual_source_verified} 只，补缺 ${data.validation_counts.secondary_fallback_passed} 只。`
                      : null,
                  ]}
                />
              </Section>
            ) : null}
            {data.failures.length ? (
              <Section
                title="待复核股票"
                description="前 30 只；原始响应已保留，不进入已核验数据集"
              >
                <Table
                  size="small"
                  rowKey="code"
                  pagination={false}
                  dataSource={data.failures}
                  columns={[
                    { title: "股票", dataIndex: "code" },
                    { title: "原因", dataIndex: "reason" },
                    {
                      title: "日期",
                      render: (_, row) =>
                        row.dates?.slice(0, 5).join("、") ?? "—",
                    },
                  ]}
                />
              </Section>
            ) : null}
            <Section
              title="自动处理报告"
              extra={
                <Button
                  onClick={() => setReportOpen(true)}
                  disabled={reportOpen}
                >
                  查看报告
                </Button>
              }
            >
              {reportOpen ? (
                <HandlingReport />
              ) : (
                <Typography.Text type="secondary">
                  逐只说明下载中发现的问题及处理方式；读取报告不会联网。
                </Typography.Text>
              )}
            </Section>
          </Flex>
        );
      }}
    </Resource>
  );
}

const ACTION_KIND: Record<string, { color: string; label: string }> = {
  handled: { color: "success", label: "已自动处理" },
  warning: { color: "warning", label: "已说明限制" },
  info: { color: "default", label: "信息" },
  unresolved: { color: "error", label: "待复核" },
};

function HandlingReport() {
  const report = useApiResource((signal) => dataApi.indexReport(signal), []);
  const [filter, setFilter] = useState<"issues" | "review" | "all">("issues");
  return (
    <Resource state={report}>
      {(data: IndexHandlingReport) => {
        const stocks = data.stocks.filter((s) =>
          filter === "all"
            ? true
            : filter === "review"
              ? s.outcome === "needs_review"
              : s.outcome !== "passed",
        );
        return (
          <Flex vertical gap={12}>
            <Flex gap={32} wrap>
              <Statistic
                title="已通过"
                value={data.summary.passed}
                suffix={`/ ${data.summary.total}`}
              />
              <Statistic title="自动处理" value={data.summary.auto_handled} />
              <Statistic title="待复核" value={data.summary.needs_review} />
              <Statistic title="未处理" value={data.summary.pending} />
            </Flex>
            <Flex justify="space-between" wrap gap={8}>
              <Segmented
                value={filter}
                onChange={(value) => setFilter(value as typeof filter)}
                options={[
                  { value: "issues", label: "有问题的股票" },
                  { value: "review", label: "仅待复核" },
                  { value: "all", label: "全部" },
                ]}
              />
              <Button href={dataApi.indexReportUrl}>下载完整 JSON</Button>
            </Flex>
            <Table
              size="small"
              rowKey="code"
              dataSource={stocks}
              pagination={{ pageSize: 20, hideOnSinglePage: true }}
              expandable={{
                rowExpandable: (row) => row.actions.length > 0,
                expandedRowRender: (row) => (
                  <List
                    size="small"
                    dataSource={row.actions}
                    renderItem={(action) => (
                      <List.Item>
                        <Flex vertical gap={2}>
                          <Flex gap={8}>
                            <Tag color={ACTION_KIND[action.kind]?.color}>
                              {ACTION_KIND[action.kind]?.label ?? action.kind}
                            </Tag>
                            <Typography.Text strong>
                              {action.title}
                            </Typography.Text>
                          </Flex>
                          <Typography.Text>
                            发现：{action.reason}
                          </Typography.Text>
                          <Typography.Text type="secondary">
                            处理：{action.handling}
                          </Typography.Text>
                          {action.dates.length ? (
                            <Typography.Text
                              type="secondary"
                              style={{ fontSize: 12 }}
                            >
                              涉及 {action.dates.length} 天：
                              {action.dates.slice(0, 8).join("、")}
                              {action.dates.length > 8 ? " 等" : ""}
                            </Typography.Text>
                          ) : null}
                        </Flex>
                      </List.Item>
                    )}
                  />
                ),
              }}
              columns={[
                {
                  title: "股票",
                  render: (_, row) => `${row.name} ${row.code}`,
                },
                {
                  title: "核验等级",
                  render: (_, row) =>
                    row.validation_level
                      ? (VALIDATION[row.validation_level] ??
                        row.validation_level)
                      : "—",
                },
                { title: "结果", dataIndex: "result" },
              ]}
            />
            <Notes
              title="处理规则与边界"
              items={[...data.rules, data.scope, data.job_handling]}
            />
          </Flex>
        );
      }}
    </Resource>
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
