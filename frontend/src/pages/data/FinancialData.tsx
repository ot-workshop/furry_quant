import { useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  Descriptions,
  Flex,
  Form,
  InputNumber,
  List,
  Progress,
  Select,
  Switch,
  Table,
  Typography,
} from "antd";
import {
  CloudDownloadOutlined,
  EyeOutlined,
  PauseCircleOutlined,
} from "@ant-design/icons";
import { dataApi, type FinancialJob, type FinancialPlan } from "../../api/data";
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
export default function FinancialData() {
  const { message } = App.useApp();
  const status = useApiResource((signal) => dataApi.financial(signal), [], {
    pollMs: 5000,
  });
  const [marketVersion, setMarketVersion] = useState("");
  const [refreshDays, setRefreshDays] = useState(7);
  const [fullRefresh, setFullRefresh] = useState(false);
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
          full_refresh: fullRefresh,
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
      message.info("已请求暂停，当前报表处理完后停止；已完成的报表会保留");
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
        <Card title="1. 选择更新范围">
          <Form layout="vertical" disabled={busy !== null}>
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
              extra="间隔内已更新的股票会跳过；0 表示立即增量更新。默认回看最近400天公告，每90天自动全量校订。"
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
            <Form.Item
              label="全量校订"
              extra="重新检查全部历史公告，发现旧公告上的就地修订；忽略更新间隔。"
            >
              <Switch checked={fullRefresh} onChange={edit(setFullRefresh)} />
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
