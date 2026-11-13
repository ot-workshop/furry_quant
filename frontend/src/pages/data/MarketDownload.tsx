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
  Progress,
  Segmented,
  Table,
  Tag,
  Typography,
} from "antd";
import {
  CloudDownloadOutlined,
  EyeOutlined,
  PauseCircleOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";
import {
  dataApi,
  type MarketJob,
  type MarketPlan,
  type MarketScope,
  type MarketVersion,
} from "../../api/data";
import { describeError } from "../../api/http";
import {
  EmptyState,
  Notes,
  Resource,
  Section,
  StatusTag,
} from "../../components/ui";
import { useApiResource } from "../../hooks/useApiResource";
import { formatDateTime, formatNumber } from "../../utils/format";
import { SCOPES } from "./shared";
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

export default function MarketDownload() {
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
    } catch (e) {
      message.error(describeError(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Flex vertical gap={16}>
      <div className="with-aside">
        <Card title="1. 选择下载范围">
          <Form layout="vertical" disabled={busy !== null}>
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
