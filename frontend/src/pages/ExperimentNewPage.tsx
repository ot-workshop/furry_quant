import { CandidateEditor } from "../components/CandidateEditor";
import {
  ExperimentParameters,
  ExperimentSubmission,
} from "../components/ExperimentSubmission";
import { useExperimentSubmission } from "../hooks/useExperimentSubmission";
import ReturnComparisonNew from "./ReturnComparisonNew";
import type { ExperimentCandidateDraft as Candidate } from "../research/experiments";
import {
  selectionApi,
  parseSelectionReference,
  type SelectionDraft,
} from "../api/factorSelection";
import { selectionOptions } from "../research/selection-draft";
import { buildExperiment } from "../research/experiments";
import { NeutralizationControls } from "../research/sections/NeutralizationControls";
import { useMemo, useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  ConfigProvider,
  DatePicker,
  Flex,
  Form,
  List,
  Space,
  Typography,
  Tabs,
} from "antd";
import { PlusOutlined } from "@ant-design/icons";
import dayjs, { type Dayjs } from "dayjs";
import {
  researchApi,
  type FactorDefinition,
  type FinancialCoverage,
  type ResearchOptions,
  type ResearchRequest,
} from "../api/research";
import { navigate, paths } from "../app/routes";
import { EmptyState, ErrorState, Loading, PageHeader } from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import {
  defaultForm,
  formFromRequest,
  factorAvailable,
  factorRows,
  financialCoverage,
  tradingDates,
  type FormState,
} from "../research/form";
import {
  BacktestSection,
  CostSection,
  DataSection,
  EvaluationSection,
  UniverseFilters,
} from "../research/sections";

function candidate(
  index: number,
  names: string[],
  definitions: FactorDefinition[],
  coverage: FinancialCoverage | null,
): Candidate {
  const rows = factorRows([], definitions).map((row) => {
    const definition = definitions.find((item) => item.name === row.name);
    return names.includes(row.name) &&
      definition &&
      factorAvailable(definition, coverage)
      ? {
          ...row,
          enabled: true,
          weight: Math.round(10000 / names.length) / 100,
        }
      : row;
  });
  return {
    id: `plan_${index}`,
    name: `方案 ${String.fromCharCode(65 + index)}`,
    factors: rows,
  };
}

export default function ExperimentNewPage({
  selection = null,
}: {
  selection?: string | null;
}) {
  const fromTask = new URLSearchParams(window.location.search).get("from");
  const [tab, setTab] = useState(() =>
    new URLSearchParams(window.location.search).get("mode") === "comparison"
      ? "comparison"
      : "split",
  );
  const resource = useApiResource(
    async (signal) => {
      const reference = selection ? parseSelectionReference(selection) : null;
      const [options, seed, baseline] = await Promise.all([
        researchApi.options(signal),
        reference
          ? selectionApi.draft(reference, signal)
          : Promise.resolve(null),
        fromTask ? researchApi.task(fromTask, signal) : Promise.resolve(null),
      ]);
      return {
        options: seed ? selectionOptions(options, seed) : options,
        seed,
        baseline:
          baseline?.kind === "research"
            ? (baseline.request as ResearchRequest)
            : null,
      };
    },
    [selection, fromTask],
  );
  const options = { ...resource, data: resource.data?.options ?? null };
  if (options.data === null)
    return options.error ? (
      <ErrorState message={options.error} onRetry={options.reload} />
    ) : (
      <Loading rows={10} />
    );
  if (
    !options.data.snapshots.length &&
    !options.data.historical_indices?.length &&
    !options.data.market_universes?.length
  )
    return (
      <>
        <PageHeader
          title="新建方案比较"
          back={{ to: paths.experiments(), label: "全部方案比较" }}
        />
        <Card>
          <EmptyState
            title="还没有可用于比较的数据集"
            description="请先在数据管理中下载自选股票、沪深300或全市场行情。"
            action={
              <Space>
                <Button
                  type="primary"
                  onClick={() => navigate(paths.data("market"))}
                >
                  去下载行情
                </Button>
                {options.data.historical_indices?.length ? (
                  <Button onClick={() => navigate(paths.newResearch())}>
                    新建研究
                  </Button>
                ) : null}
              </Space>
            }
          />
        </Card>
      </>
    );
  const splitForm = (
    <ExperimentForm
      key={selection ?? "manual"}
      options={options.data}
      seed={resource.data?.seed ?? null}
      active={Boolean(selection) || tab === "split"}
    />
  );
  if (selection) return splitForm;
  return (
    <Tabs
      activeKey={tab}
      onChange={setTab}
      items={[
        { key: "split", label: "分段比较", children: splitForm },
        {
          key: "comparison",
          label: "收益来源对照",
          children: (
            <ReturnComparisonNew
              options={options.data}
              research={resource.data?.baseline ?? null}
              active={tab === "comparison"}
            />
          ),
        },
      ]}
    />
  );
}

function ExperimentForm({
  options,
  seed,
  active,
}: {
  options: ResearchOptions;
  seed: SelectionDraft | null;
  active: boolean;
}) {
  const { message } = App.useApp();
  const definitions = options.factor_definitions;
  const [form, setForm] = useState<FormState>(() =>
    seed
      ? formFromRequest(seed.research, options).form
      : {
          ...defaultForm(options),
          name: "方案比较",
        },
  );
  const coverage = financialCoverage(form, options);
  const [candidates, setCandidates] = useState<Candidate[]>(() =>
    seed
      ? seed.candidates.map((item) => ({
          ...item,
          factors: factorRows(item.factors, definitions),
        }))
      : [
          candidate(0, ["momentum"], definitions, coverage),
          candidate(1, ["value"], definitions, coverage),
        ],
  );
  const [splits, setSplits] = useState<{ validation: string; test: string }>({
    validation: seed?.periods.validation.start ?? "",
    test: seed?.periods.test.start ?? "",
  });
  const submission = useExperimentSubmission(
    "experiment",
    (task) => {
      message.success(`已提交「${task.name || form.name}」`);
      navigate(paths.tasks(task.id));
    },
    active,
  );
  const waiting = submission.state.kind === "waiting";
  const update = (patch: Partial<FormState>) =>
    setForm((current) => ({ ...current, ...patch }));

  const dates = tradingDates(form, options);
  const built = useMemo(
    () => buildExperiment(form, options, candidates, splits, seed?.reference),
    [form, options, candidates, splits, seed],
  );
  const periods = built.periods;

  const allowed = new Set(dates);
  const disabled = (day: Dayjs) => {
    const text = day.format("YYYY-MM-DD");
    return (
      (allowed.size > 0 && !allowed.has(text)) ||
      (Boolean(form.start) && text <= form.start) ||
      (Boolean(form.end) && text >= form.end)
    );
  };

  return (
    <>
      <PageHeader
        title="新建方案比较"
        back={{ to: paths.experiments(), label: "全部方案比较" }}
        subtitle="在同一股票池和交易参数下比较多个因子方案"
      />
      <div className="with-aside">
        <ExperimentParameters waiting={waiting}>
          {seed ? (
            <Alert
              type="info"
              showIcon
              title={`组合筛选截止日：${seed.reference.parameters.cutoff}`}
              description={seed.warnings.join(" ")}
            />
          ) : null}
          <ConfigProvider componentDisabled={Boolean(seed) || waiting}>
            <DataSection form={form} options={options} update={update} />
            <UniverseFilters form={form} options={options} update={update} />
          </ConfigProvider>
          <BacktestSection
            form={form}
            options={options}
            update={update}
            earliest={built.earliest}
          />
          <Card title="分段">
            <Typography.Paragraph type="secondary">
              上方回测区间为总区间。开发段 = 总区间开始 ~
              验证段前一交易日；验证段 = 验证开始 ~ 测试段前一交易日；测试段 =
              测试开始 ~ 总区间结束。
            </Typography.Paragraph>
            <Space size={24} wrap>
              <Form.Item label="验证段开始" style={{ marginBottom: 0 }}>
                <DatePicker
                  value={splits.validation ? dayjs(splits.validation) : null}
                  disabledDate={disabled}
                  onChange={(day) =>
                    setSplits((current) => ({
                      ...current,
                      validation: day?.format("YYYY-MM-DD") ?? "",
                    }))
                  }
                />
              </Form.Item>
              <Form.Item label="测试段开始" style={{ marginBottom: 0 }}>
                <DatePicker
                  value={splits.test ? dayjs(splits.test) : null}
                  disabledDate={(day) =>
                    disabled(day) ||
                    (Boolean(splits.validation) &&
                      day.format("YYYY-MM-DD") <= splits.validation)
                  }
                  onChange={(day) =>
                    setSplits((current) => ({
                      ...current,
                      test: day?.format("YYYY-MM-DD") ?? "",
                    }))
                  }
                />
              </Form.Item>
            </Space>
            {periods ? (
              <Typography.Paragraph style={{ marginTop: 12, marginBottom: 0 }}>
                开发 {periods.development.start} ~ {periods.development.end}
                ；验证 {periods.validation.start} ~ {periods.validation.end}
                ；测试 {periods.test.start} ~ {periods.test.end}
              </Typography.Paragraph>
            ) : null}
          </Card>
          <Card
            title="候选方案"
            extra={
              <Button
                icon={<PlusOutlined />}
                disabled={candidates.length >= 8}
                onClick={() =>
                  setCandidates((current) => [
                    ...current,
                    candidate(
                      current.length,
                      ["momentum"],
                      definitions,
                      coverage,
                    ),
                  ])
                }
              >
                添加方案
              </Button>
            }
          >
            <Flex vertical gap={16}>
              {candidates.map((item, index) => (
                <ConfigProvider
                  key={item.id}
                  componentDisabled={
                    Boolean(seed && item.id === "milp") || waiting
                  }
                >
                  <CandidateEditor
                    value={item}
                    definitions={definitions}
                    form={form}
                    options={options}
                    coverage={coverage}
                    removable={
                      candidates.length > 2 && !(seed && item.id === "milp")
                    }
                    onChange={(next) =>
                      setCandidates((current) =>
                        current.map((c, i) => (i === index ? next : c)),
                      )
                    }
                    onRemove={() =>
                      setCandidates((current) =>
                        current.filter((_, i) => i !== index),
                      )
                    }
                  />
                </ConfigProvider>
              ))}
            </Flex>
          </Card>
          <Card title="行业处理">
            <ConfigProvider componentDisabled={Boolean(seed) || waiting}>
              <NeutralizationControls
                form={form}
                options={options}
                update={update}
              />
            </ConfigProvider>
          </Card>
          <CostSection form={form} update={update} />
          <EvaluationSection form={form} update={update} />
        </ExperimentParameters>
        <div className="aside-sticky">
          <Card title="检查">
            {built.errors.length ? (
              <List
                size="small"
                dataSource={built.errors}
                renderItem={(text) => (
                  <List.Item style={{ paddingInline: 0 }}>
                    <Typography.Text style={{ fontSize: 13 }}>
                      {text}
                    </Typography.Text>
                  </List.Item>
                )}
              />
            ) : (
              <Typography.Text type="success">
                配置检查通过；服务器会再检查每个方案的历史预热。
              </Typography.Text>
            )}
            <ExperimentSubmission
              label="提交方案比较"
              state={submission.state}
              fingerprint={built.request ? JSON.stringify(built.request) : null}
              onSubmit={() => void submission.submit(built.request)}
              onStop={submission.stopWaiting}
            />
          </Card>
        </div>
      </div>
    </>
  );
}
