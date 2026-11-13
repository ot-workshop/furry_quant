import { useEffect, useMemo, useRef, useState } from "react";
import { Alert, App, Button, Card, Divider, Flex, Typography } from "antd";
import {
  CheckCircleFilled,
  ExclamationCircleFilled,
  RocketOutlined,
  UndoOutlined,
} from "@ant-design/icons";
import { describeError, newIdempotencyKey } from "../api/http";
import {
  researchApi,
  type ResearchOptions,
  type ResearchRequest,
} from "../api/research";
import { navigate, paths } from "../app/routes";
import {
  BacktestSection,
  CostSection,
  DataSection,
  EvaluationSection,
  FactorSection,
} from "../research/sections";
import {
  buildRequest,
  clearDraft,
  dataRange,
  defaultForm,
  earliestStart,
  formFromRequest,
  mergeDraft,
  poolSize,
  pruneFactors,
  readDraft,
  saveDraft,
  weightTotal,
  type FormState,
} from "../research/form";
import { EmptyState, ErrorState, Loading, PageHeader } from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";

export default function ResearchNewPage({
  fromTask,
}: {
  fromTask: string | null;
}) {
  const options = useApiResource((signal) => researchApi.options(signal), []);
  if (options.data === null)
    return options.error ? (
      <ErrorState message={options.error} onRetry={options.reload} />
    ) : (
      <Loading rows={10} />
    );
  return <ResearchForm options={options.data} fromTask={fromTask} />;
}

function ResearchForm({
  options,
  fromTask,
}: {
  options: ResearchOptions;
  fromTask: string | null;
}) {
  const { message } = App.useApp();
  const [form, setForm] = useState<FormState>(() => {
    const draft = fromTask ? null : readDraft();
    const initial = draft
      ? mergeDraft(defaultForm(options), draft, options)
      : defaultForm(options);
    return pruneFactors(initial, options);
  });
  const [notice, setNotice] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const idempotency = useRef<{ fingerprint: string; key: string } | null>(null);

  useEffect(() => {
    if (!fromTask) return;
    const controller = new AbortController();
    researchApi
      .task(fromTask, controller.signal)
      .then((task) => {
        if (task.kind === "experiment") return;
        const restored = formFromRequest(
          task.request as ResearchRequest,
          options,
        );
        setForm(pruneFactors(restored.form, options));
        setNotice([
          `已载入任务「${task.name}」的配置，可修改后重新提交。`,
          ...restored.warnings,
        ]);
      })
      .catch((error) => {
        if (!controller.signal.aborted)
          setNotice([`无法载入任务配置：${describeError(error)}`]);
      });
    return () => controller.abort();
  }, [fromTask, options]);

  useEffect(() => saveDraft(form), [form]);

  const built = useMemo(() => buildRequest(form, options), [form, options]);
  const update = (patch: Partial<FormState>) => {
    setSubmitError(null);
    setForm((current) => pruneFactors({ ...current, ...patch }, options));
  };

  async function submit() {
    if (!built.request || submitting) return;
    const fingerprint = JSON.stringify(built.request);
    if (idempotency.current?.fingerprint !== fingerprint)
      idempotency.current = { fingerprint, key: newIdempotencyKey("research") };
    setSubmitting(true);
    setSubmitError(null);
    try {
      const task = await researchApi.submit(
        built.request,
        idempotency.current.key,
      );
      idempotency.current = null;
      message.success(`已提交「${task.name}」，正在排队运行`);
      navigate(paths.tasks(task.id));
    } catch (error) {
      setSubmitError(describeError(error));
    } finally {
      setSubmitting(false);
    }
  }

  if (!options.snapshots.length && !options.historical_indices?.length)
    return (
      <>
        <PageHeader title="新建研究" />
        <Card>
          <EmptyState
            title="还没有可用于研究的数据集"
            description="研究需要先下载行情。到数据管理中选择自选股票或沪深300，预览后确认下载；完成后回到这里即可选择。"
            action={
              <Button
                type="primary"
                onClick={() => navigate(paths.data("market"))}
              >
                去下载行情
              </Button>
            }
          />
        </Card>
      </>
    );

  const range = dataRange(form, options);
  const earliest = earliestStart(form, options);
  const total = weightTotal(form);
  return (
    <>
      <PageHeader
        title="新建研究"
        subtitle="选择数据与股票池，设置因子和回测参数；提交后在后台计算因子、回测与评估"
        extra={
          <Button
            icon={<UndoOutlined />}
            onClick={() => {
              clearDraft();
              setForm(defaultForm(options));
              setNotice([]);
            }}
          >
            恢复默认
          </Button>
        }
      />
      {notice.map((text) => (
        <Alert key={text} type="info" showIcon closable title={text} />
      ))}
      <div className="with-aside">
        <Flex vertical gap={16}>
          <DataSection form={form} options={options} update={update} />
          <FactorSection
            form={form}
            options={options}
            update={update}
            setForm={setForm}
          />
          <BacktestSection
            form={form}
            options={options}
            update={update}
            earliest={earliest}
          />
          <CostSection form={form} update={update} />
          <EvaluationSection form={form} update={update} />
        </Flex>
        <div className="aside-sticky">
          <Card title="研究摘要">
            <Flex vertical gap={8}>
              <Summary label="名称" value={form.name || "—"} />
              <Summary
                label="数据"
                value={
                  form.sourceMode === "index_warehouse"
                    ? "沪深300历史成分（动态）"
                    : `${poolSize(form, options)} 只股票`
                }
              />
              <Summary
                label="数据范围"
                value={range ? `${range.start} ~ ${range.end}` : "—"}
              />
              <Summary
                label="回测区间"
                value={
                  form.start && form.end ? `${form.start} ~ ${form.end}` : "—"
                }
              />
              <Summary label="最早可回测" value={earliest ?? "—"} />
              <Summary
                label="因子权重合计"
                value={
                  <Typography.Text
                    type={Math.abs(total - 100) < 1e-6 ? "success" : "danger"}
                  >
                    {total}%
                  </Typography.Text>
                }
              />
              <Summary label="目标持股" value={`${form.topk} 只`} />
            </Flex>
            <Divider style={{ margin: "16px 0" }} />
            {built.errors.length ? (
              <div>
                <Typography.Text type="warning">
                  <ExclamationCircleFilled /> 还有 {built.errors.length}{" "}
                  项需要处理
                </Typography.Text>
                {built.errors.map((item) => (
                  <div key={item} style={{ paddingBlock: 8 }}>
                    <Typography.Text style={{ fontSize: 13 }}>
                      {item}
                    </Typography.Text>
                  </div>
                ))}
              </div>
            ) : (
              <Typography.Text type="success">
                <CheckCircleFilled /> 配置检查通过，可以提交
              </Typography.Text>
            )}
            {submitError ? (
              <Alert
                type="error"
                showIcon
                title="提交失败"
                description={submitError}
                style={{ marginTop: 12 }}
              />
            ) : null}
            <Button
              type="primary"
              block
              size="large"
              icon={<RocketOutlined />}
              style={{ marginTop: 16 }}
              disabled={!built.request}
              loading={submitting}
              onClick={() => void submit()}
            >
              提交研究
            </Button>
          </Card>
        </div>
      </div>
    </>
  );
}

function Summary({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <Flex justify="space-between" gap={12}>
      <Typography.Text type="secondary">{label}</Typography.Text>
      <Typography.Text style={{ textAlign: "right" }}>{value}</Typography.Text>
    </Flex>
  );
}
