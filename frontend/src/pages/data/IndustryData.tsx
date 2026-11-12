import { useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  Descriptions,
  Flex,
  Form,
  List,
  Progress,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
} from "antd";
import {
  CloudDownloadOutlined,
  EyeOutlined,
  PauseCircleOutlined,
} from "@ant-design/icons";
import {
  dataApi,
  type IndustryJob,
  type IndustryLevel,
  type IndustryPlan,
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

export default function IndustryData() {
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
    } catch (e) {
      message.error(describeError(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Flex vertical gap={16}>
      <div className="with-aside">
        <Card title="1. 选择下载级别">
          <Form layout="vertical" disabled={busy !== null}>
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
