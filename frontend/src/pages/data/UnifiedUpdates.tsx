import { useEffect, useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  DatePicker,
  Flex,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Switch,
  Table,
  Typography,
} from "antd";
import dayjs from "dayjs";
import {
  dataApi,
  type MarketScope,
  type UpdateJob,
  type UpdatePlan,
  type UpdateSpec,
} from "../../api/data";
import { describeError } from "../../api/http";
import { Resource, Section } from "../../components/ui";
import { useApiResource } from "../../hooks/useApiResource";
import { formatDateTime } from "../../utils/format";
import { SCOPES } from "./shared";

const LABELS: Record<string, string> = {
  market: "行情与参考数据",
  financial: "财务报表",
  industry: "历史行业",
  benchmarks: "评价基准",
};
function defaults(): UpdateSpec {
  const end = dayjs()
    .subtract(new Date().getHours() < 18 ? 1 : 0, "day")
    .format("YYYY-MM-DD");
  return {
    market: {
      scope: "csi300",
      start: "2020-01-01",
      end,
      warmup: 127,
      codes: [],
      refresh_sessions: 20,
    },
    market_version: null,
    financial: { refresh_days: 7 },
    industry: { levels: ["L1", "L2", "L3"], refresh: true },
    benchmarks: ["H00300.CSI"],
  };
}

export default function UnifiedUpdates() {
  const { message } = App.useApp();
  const status = useApiResource((signal) => dataApi.updates(signal), [], {
    pollMs: 5000,
  });
  const [spec, setSpec] = useState<UpdateSpec>(defaults);
  const [marketDraft, setMarketDraft] = useState(defaults().market!);
  const [plan, setPlan] = useState<UpdatePlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [enabled, setEnabled] = useState(false);
  const [hour, setHour] = useState(19);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    if (!loaded && status.data) {
      const schedule = status.data.schedule;
      if (schedule) {
        setSpec(schedule.spec);
        if (schedule.spec.market) setMarketDraft(schedule.spec.market);
        setEnabled(schedule.enabled);
        setHour(schedule.hour);
      }
      setLoaded(true);
    }
  }, [loaded, status.data]);
  function edit(patch: Partial<UpdateSpec>) {
    setSpec((value) => ({ ...value, ...patch }));
    setPlan(null);
    setError(null);
  }
  function market(patch: Partial<typeof marketDraft>) {
    const value = { ...marketDraft, ...patch };
    setMarketDraft(value);
    edit({ market: value });
  }
  async function action(work: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (e) {
      setError(describeError(e));
    } finally {
      setBusy(false);
    }
  }
  async function start(id: string) {
    await dataApi.startUpdates(id);
    setPlan(null);
    status.reload();
    message.success("统一更新已入队");
  }
  return (
    <Flex vertical gap={16}>
      <Alert
        type="info"
        showIcon
        title="一个任务串行更新各类数据"
        description="先发布行情，再按这批行情的股票范围更新财务，随后更新行业和基准。成功项目保留；失败后续传不会重跑已完成项目。"
      />
      <Card title="更新范围">
        <Form layout="vertical" disabled={busy}>
          <Space wrap>
            <Checkbox
              checked={spec.market !== null}
              onChange={(e) =>
                edit({ market: e.target.checked ? marketDraft : null })
              }
            >
              行情与参考数据
            </Checkbox>
            <Checkbox
              checked={spec.financial !== null}
              onChange={(e) =>
                edit({
                  financial: e.target.checked ? { refresh_days: 7 } : null,
                })
              }
            >
              财务报表
            </Checkbox>
            <Checkbox
              checked={spec.industry !== null}
              onChange={(e) =>
                edit({
                  industry: e.target.checked
                    ? { levels: ["L1", "L2", "L3"], refresh: true }
                    : null,
                })
              }
            >
              历史行业
            </Checkbox>
            <Checkbox
              checked={spec.benchmarks.length > 0}
              onChange={(e) =>
                edit({ benchmarks: e.target.checked ? ["H00300.CSI"] : [] })
              }
            >
              评价基准
            </Checkbox>
          </Space>
          {spec.market ? (
            <div className="grid-4" style={{ marginTop: 16 }}>
              <Form.Item label="股票范围">
                <Select
                  value={marketDraft.scope}
                  onChange={(scope: MarketScope) =>
                    market({ scope, codes: [] })
                  }
                  options={Object.entries(SCOPES).map(([value, label]) => ({
                    value,
                    label,
                  }))}
                />
              </Form.Item>
              <Form.Item label="研究开始日期">
                <DatePicker
                  value={dayjs(marketDraft.start)}
                  onChange={(value) =>
                    value && market({ start: value.format("YYYY-MM-DD") })
                  }
                />
              </Form.Item>
              <Form.Item label="结束日期">
                <DatePicker
                  value={dayjs(marketDraft.end)}
                  onChange={(value) =>
                    value && market({ end: value.format("YYYY-MM-DD") })
                  }
                />
              </Form.Item>
              <Form.Item label="预热交易日">
                <InputNumber
                  min={0}
                  max={504}
                  value={marketDraft.warmup}
                  onChange={(value) => market({ warmup: Number(value) })}
                />
              </Form.Item>
              {marketDraft.scope === "custom" ? (
                <Form.Item label="股票代码（逗号或空格分隔）">
                  <Input
                    value={marketDraft.codes.join(", ")}
                    onChange={(event) =>
                      market({
                        codes: event.target.value
                          .split(/[\s,，]+/)
                          .filter(Boolean),
                      })
                    }
                  />
                </Form.Item>
              ) : null}
              <Form.Item label="行情修订回看交易日">
                <InputNumber
                  min={0}
                  max={504}
                  value={marketDraft.refresh_sessions ?? 20}
                  onChange={(value) =>
                    market({ refresh_sessions: Number(value) })
                  }
                />
              </Form.Item>
            </div>
          ) : (
            <Form.Item label="使用已有行情版本" style={{ marginTop: 16 }}>
              <Select
                value={spec.market_version}
                onChange={(market_version) => edit({ market_version })}
                options={(status.data?.market_versions ?? []).map((v) => ({
                  value: v.id,
                  label: `${SCOPES[v.scope]} · ${v.codes_count}只 · ${v.start} ~ ${v.end}`,
                }))}
              />
            </Form.Item>
          )}
          {spec.financial ? (
            <Form.Item
              label="财务更新间隔（天）"
              extra="最近400天公告增量检查，每90天自动全量校订。0表示立即更新。"
            >
              <InputNumber
                min={0}
                max={3650}
                value={spec.financial.refresh_days}
                onChange={(value) =>
                  edit({
                    financial: {
                      ...spec.financial!,
                      refresh_days: Number(value),
                    },
                  })
                }
              />
            </Form.Item>
          ) : null}
          {spec.industry ? (
            <Form.Item
              label="行业级别"
              extra="刷新历史分类与区间；相同任务失败后复用已完成分区。"
            >
              <Checkbox.Group
                value={spec.industry.levels}
                options={["L1", "L2", "L3"]}
                onChange={(levels) =>
                  edit({
                    industry: {
                      ...spec.industry!,
                      levels: levels as ("L1" | "L2" | "L3")[],
                    },
                  })
                }
              />
            </Form.Item>
          ) : null}
          {spec.benchmarks.length ? (
            <Form.Item label="评价基准">
              <Select
                mode="multiple"
                value={spec.benchmarks}
                onChange={(benchmarks) => edit({ benchmarks })}
                options={[
                  { value: "H00300.CSI", label: "沪深300全收益" },
                  { value: "000300.SH", label: "沪深300价格指数" },
                  { value: "000905.SH", label: "中证500" },
                  { value: "000852.SH", label: "中证1000" },
                ]}
              />
            </Form.Item>
          ) : null}
          <Button
            onClick={() =>
              void action(async () =>
                setPlan(await dataApi.previewUpdates(spec)),
              )
            }
          >
            预览统一更新
          </Button>
        </Form>
      </Card>
      {error ? <Alert type="error" showIcon title={error} /> : null}
      {plan ? (
        <Card title="确认计划">
          <Flex vertical gap={12}>
            <Typography.Text>
              {plan.stages.map((key) => LABELS[key]).join(" → ")}
            </Typography.Text>
            {plan.plans.market ? (
              <Typography.Text>
                股票 {plan.plans.market.codes_count} 只，数据从{" "}
                {plan.plans.market.data_start} 开始；复用{" "}
                {plan.plans.market.reused_rows} 行，缺口{" "}
                {plan.plans.market.missing_rows} 行。
              </Typography.Text>
            ) : null}
            <Typography.Text type="secondary">
              {plan.note} 预览会查询参考数据，不下载个股行情。
            </Typography.Text>
            <Button
              type="primary"
              loading={busy}
              onClick={() => void action(() => start(plan.id))}
            >
              确认统一更新
            </Button>
          </Flex>
        </Card>
      ) : null}
      <Card title="自动更新（北京时间）">
        <Space wrap>
          <Switch checked={enabled} onChange={setEnabled} />
          <Typography.Text>每天</Typography.Text>
          <InputNumber
            min={0}
            max={23}
            value={hour}
            onChange={(value) => setHour(Number(value))}
            suffix="点"
          />
          <Button
            loading={busy}
            onClick={() =>
              void action(async () => {
                await dataApi.scheduleUpdates({ enabled, hour, spec });
                status.reload();
                message.success("自动更新计划已保存");
              })
            }
          >
            保存自动更新计划
          </Button>
        </Space>
        <Typography.Paragraph type="secondary">
          使用上方股票范围和起始日期；行情结束日期自动推到最近已完成的数据日。需要
          worker 持续运行，每天最多入队一次；恢复备份后自动计划默认关闭。
        </Typography.Paragraph>
      </Card>
      <Section
        title="统一更新记录"
        extra={
          <Button
            onClick={() =>
              void action(async () => {
                await dataApi.pauseUpdates();
                status.reload();
              })
            }
          >
            暂停统一更新
          </Button>
        }
      >
        <Resource state={status}>
          {(data) => (
            <Table<UpdateJob>
              rowKey="id"
              dataSource={data.jobs}
              pagination={{ pageSize: 10 }}
              columns={[
                { title: "状态", dataIndex: "status" },
                {
                  title: "完成",
                  render: (_, job) => `${job.completed}/${job.total}`,
                },
                {
                  title: "各项结果",
                  render: (_, job) => (
                    <Flex vertical>
                      {Object.entries(job.steps).map(([key, step]) => (
                        <Typography.Text key={key}>
                          {LABELS[key]}：{step.status} {step.message}{" "}
                          {step.result?.version_id ?? ""}
                        </Typography.Text>
                      ))}
                    </Flex>
                  ),
                },
                {
                  title: "时间",
                  render: (_, job) => formatDateTime(job.updated_at),
                },
                {
                  title: "操作",
                  render: (_, job) =>
                    ["partial", "paused", "interrupted"].includes(
                      job.status,
                    ) ? (
                      <Button
                        disabled={busy}
                        onClick={() => void action(() => start(job.id))}
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
