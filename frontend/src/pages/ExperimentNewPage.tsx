import { FactorDataHint } from "../components/FactorDataHint";
import { FactorMissingPolicy } from "../components/FactorMissingPolicy";
import {
  selectionApi,
  parseSelectionReference,
  type SelectionDraft,
} from "../api/factorSelection";
import { selectionOptions } from "../research/selection-draft";
import { buildExperiment } from "../research/experiments";
import { NeutralizationControls } from "../research/sections/NeutralizationControls";
import { useMemo, useRef, useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  ConfigProvider,
  DatePicker,
  Flex,
  Form,
  Input,
  InputNumber,
  List,
  Select,
  Space,
  Typography,
} from "antd";
import {
  DeleteOutlined,
  PlusOutlined,
  RocketOutlined,
} from "@ant-design/icons";
import dayjs, { type Dayjs } from "dayjs";
import { describeError, newIdempotencyKey } from "../api/http";
import {
  researchApi,
  type FactorDefinition,
  type FinancialCoverage,
  type ResearchOptions,
} from "../api/research";
import { navigate, paths } from "../app/routes";
import { EmptyState, ErrorState, Loading, PageHeader } from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import {
  defaultForm,
  formFromRequest,
  FAMILY_ORDER,
  factorAvailable,
  factorRows,
  financialCoverage,
  tradingDates,
  type FactorRow,
  type FormState,
} from "../research/form";
import {
  BacktestSection,
  CostSection,
  DataSection,
  EvaluationSection,
  UniverseFilters,
} from "../research/sections";

interface Candidate {
  id: string;
  name: string;
  factors: FactorRow[];
}

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
  const resource = useApiResource(
    async (signal) => {
      const reference = selection ? parseSelectionReference(selection) : null;
      const [options, seed] = await Promise.all([
        researchApi.options(signal),
        reference
          ? selectionApi.draft(reference, signal)
          : Promise.resolve(null),
      ]);
      return {
        options: seed ? selectionOptions(options, seed) : options,
        seed,
      };
    },
    [selection],
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
  return (
    <ExperimentForm
      key={selection ?? "manual"}
      options={options.data}
      seed={resource.data?.seed ?? null}
    />
  );
}

function ExperimentForm({
  options,
  seed,
}: {
  options: ResearchOptions;
  seed: SelectionDraft | null;
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
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const key = useRef<{ fingerprint: string; key: string } | null>(null);
  const update = (patch: Partial<FormState>) =>
    setForm((current) => ({ ...current, ...patch }));

  const dates = tradingDates(form, options);
  const built = useMemo(
    () => buildExperiment(form, options, candidates, splits, seed?.reference),
    [form, options, candidates, splits, seed],
  );
  const periods = built.periods;

  async function submit() {
    if (!built.request) return;
    const fingerprint = JSON.stringify(built.request);
    if (key.current?.fingerprint !== fingerprint)
      key.current = { fingerprint, key: newIdempotencyKey("experiment") };
    setSubmitting(true);
    setSubmitError(null);
    try {
      const task = await researchApi.submitExperiment(
        built.request,
        key.current.key,
      );
      message.success(`已提交「${task.name}」`);
      navigate(paths.tasks(task.id));
    } catch (error) {
      setSubmitError(describeError(error));
    } finally {
      setSubmitting(false);
    }
  }

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
        <Flex vertical gap={16}>
          {seed ? (
            <Alert
              type="info"
              showIcon
              title={`组合筛选截止日：${seed.reference.parameters.cutoff}`}
              description={seed.warnings.join(" ")}
            />
          ) : null}
          <ConfigProvider componentDisabled={Boolean(seed)}>
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
                  componentDisabled={Boolean(seed && item.id === "milp")}
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
            <ConfigProvider componentDisabled={Boolean(seed)}>
              <NeutralizationControls
                form={form}
                options={options}
                update={update}
              />
            </ConfigProvider>
          </Card>
          <CostSection form={form} update={update} />
          <EvaluationSection form={form} update={update} />
        </Flex>
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
              disabled={!built.request}
              loading={submitting}
              style={{ marginTop: 16 }}
              onClick={() => void submit()}
            >
              提交方案比较
            </Button>
          </Card>
        </div>
      </div>
    </>
  );
}

function CandidateEditor({
  value,
  definitions,
  form,
  options,
  coverage,
  removable,
  onChange,
  onRemove,
}: {
  value: Candidate;
  definitions: FactorDefinition[];
  form: FormState;
  options: ResearchOptions;
  coverage: FinancialCoverage | null;
  removable: boolean;
  onChange: (value: Candidate) => void;
  onRemove: () => void;
}) {
  const enabled = value.factors.filter((row) => row.enabled);
  const setRow = (rowKey: string, patch: Partial<FactorRow>) =>
    onChange({
      ...value,
      factors: value.factors.map((row) =>
        row.key === rowKey ? { ...row, ...patch } : row,
      ),
    });
  const groups = useMemo(() => {
    const byFamily = new Map<string, FactorDefinition[]>();
    for (const definition of definitions) {
      const family = definition.family ?? "value";
      byFamily.set(family, [...(byFamily.get(family) ?? []), definition]);
    }
    return [...byFamily.entries()]
      .sort(
        ([left], [right]) =>
          FAMILY_ORDER.indexOf(left as (typeof FAMILY_ORDER)[number]) -
          FAMILY_ORDER.indexOf(right as (typeof FAMILY_ORDER)[number]),
      )
      .map(([family, items]) => ({
        family,
        label: items[0]?.family_label ?? family,
        options: items.map((definition) => ({
          value: definition.name,
          label: definition.label,
          disabled: !factorAvailable(definition, coverage),
        })),
      }));
  }, [definitions, coverage]);
  return (
    <Card
      size="small"
      type="inner"
      title={
        <Input
          value={value.name}
          variant="borderless"
          maxLength={80}
          onChange={(e) => onChange({ ...value, name: e.target.value })}
          style={{ fontWeight: 600, paddingInline: 0 }}
        />
      }
      extra={
        removable ? (
          <Button
            type="text"
            danger
            icon={<DeleteOutlined />}
            onClick={onRemove}
            aria-label="删除方案"
          />
        ) : null
      }
    >
      <Select
        mode="multiple"
        value={enabled.map((row) => row.key)}
        onChange={(keys: string[]) =>
          onChange({
            ...value,
            factors: value.factors.map((row) => ({
              ...row,
              enabled: keys.includes(row.key),
            })),
          })
        }
        options={groups}
        optionFilterProp="label"
        optionRender={(option) => {
          const definition = definitions.find(
            (item) => item.name === option.value,
          );
          return (
            <Space>
              {option.label}
              {definition ? (
                <FactorDataHint
                  definition={definition}
                  form={form}
                  options={options}
                  row={value.factors.find(
                    (row) => row.name === definition.name,
                  )}
                />
              ) : null}
            </Space>
          );
        }}
        style={{ width: "100%" }}
        placeholder="选择因子"
      />
      {enabled.map((row) => {
        const definition = definitions.find((d) => d.name === row.name);
        return (
          <Flex
            key={row.key}
            gap={12}
            align="center"
            style={{ marginTop: 8 }}
            wrap
          >
            <Space style={{ width: 145 }}>
              <Typography.Text>{definition?.label ?? row.name}</Typography.Text>
              {definition ? (
                <FactorDataHint
                  definition={definition}
                  form={form}
                  options={options}
                  row={row}
                />
              ) : null}
            </Space>
            <InputNumber
              size="small"
              min={0}
              max={100}
              value={row.weight}
              suffix="%"
              style={{ width: 110 }}
              onChange={(v) => setRow(row.key, { weight: Number(v ?? 0) })}
            />
            {["value", "gross_margin_ttm"].includes(row.name) ? (
              <FactorMissingPolicy
                label={definition?.label ?? row.name}
                value={row.missingPolicy}
                onChange={(missingPolicy) => setRow(row.key, { missingPolicy })}
              />
            ) : null}
            {definition?.default_window != null ? (
              <InputNumber
                size="small"
                min={2}
                max={252}
                value={row.window}
                prefix="窗口"
                suffix="日"
                style={{ width: 130 }}
                onChange={(v) =>
                  setRow(row.key, { window: v === null ? null : Number(v) })
                }
              />
            ) : null}
          </Flex>
        );
      })}
    </Card>
  );
}
