import { useMemo, useRef, useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
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
  type ExperimentRequest,
  type FactorDefinition,
  type ResearchOptions,
} from "../api/research";
import { navigate, paths } from "../app/routes";
import { ErrorState, Loading, PageHeader } from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import {
  buildBacktest,
  buildData,
  buildEvaluation,
  buildFactors,
  defaultForm,
  factorRows,
  tradingDates,
  type FactorRow,
  type FormState,
} from "../research/form";
import {
  BacktestSection,
  CostSection,
  DataSection,
  EvaluationSection,
} from "../research/sections";
import { factorLabel } from "../utils/format";

interface Candidate {
  id: string;
  name: string;
  factors: FactorRow[];
}

function candidate(
  index: number,
  names: string[],
  definitions: FactorDefinition[],
): Candidate {
  const rows = factorRows([], definitions).map((row) =>
    names.includes(row.name)
      ? {
          ...row,
          enabled: true,
          weight: Math.round(10000 / names.length) / 100,
        }
      : row,
  );
  return {
    id: `plan_${index}`,
    name: `方案 ${String.fromCharCode(65 + index)}`,
    factors: rows,
  };
}

export default function ExperimentNewPage() {
  const options = useApiResource((signal) => researchApi.options(signal), []);
  if (options.data === null)
    return options.error ? (
      <ErrorState message={options.error} onRetry={options.reload} />
    ) : (
      <Loading rows={10} />
    );
  return <ExperimentForm options={options.data} />;
}

function ExperimentForm({ options }: { options: ResearchOptions }) {
  const { message } = App.useApp();
  const definitions = options.factor_definitions;
  const [form, setForm] = useState<FormState>(() => ({
    ...defaultForm(options),
    sourceMode: "snapshot",
    name: "方案比较",
  }));
  const [candidates, setCandidates] = useState<Candidate[]>(() => [
    candidate(0, ["momentum"], definitions),
    candidate(1, ["value"], definitions),
  ]);
  const [splits, setSplits] = useState<{ validation: string; test: string }>({
    validation: "",
    test: "",
  });
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const key = useRef<{ fingerprint: string; key: string } | null>(null);
  const update = (patch: Partial<FormState>) =>
    setForm((current) => ({ ...current, ...patch }));

  const dates = tradingDates(form, options);
  const periods = useMemo(() => {
    const previous = (day: string) => dates.filter((d) => d < day).at(-1) ?? "";
    if (!form.start || !form.end || !splits.validation || !splits.test)
      return null;
    return {
      development: { start: form.start, end: previous(splits.validation) },
      validation: { start: splits.validation, end: previous(splits.test) },
      test: { start: splits.test, end: form.end },
    };
  }, [dates, form.start, form.end, splits]);

  const built = useMemo(() => {
    const errors: string[] = [];
    if (!form.name.trim()) errors.push("请填写名称。");
    const data = buildData(
      { ...form, sourceMode: "snapshot" },
      options,
      errors,
    );
    const backtest = buildBacktest(form, errors);
    const evaluation = buildEvaluation(form, errors);
    if (!periods) errors.push("请设置总区间以及验证段、测试段的开始日期。");
    else if (
      !(
        periods.development.start <= periods.development.end &&
        periods.validation.start <= periods.validation.end &&
        periods.test.start <= periods.test.end
      )
    )
      errors.push("三个分段需按时间先后排列，且每段至少包含一个交易日。");
    if (candidates.length < 2 || candidates.length > 8)
      errors.push("候选方案需为 2–8 个。");
    const planCandidates = candidates.map((item, index) => {
      const factorErrors: string[] = [];
      const factors = buildFactors(
        { ...form, factors: item.factors },
        definitions,
        factorErrors,
      );
      errors.push(
        ...factorErrors.map(
          (text) => `${item.name || `方案 ${index + 1}`}：${text}`,
        ),
      );
      return { id: item.id, name: item.name.trim(), factors };
    });
    if (
      new Set(planCandidates.map((c) => JSON.stringify(c.factors))).size !==
      planCandidates.length
    )
      errors.push("候选方案的因子配置不能完全相同。");
    const request: ExperimentRequest | null =
      errors.length || !data || data.mode !== "snapshot" || !periods
        ? null
        : {
            name: form.name.trim(),
            data,
            backtest,
            plan: { periods, candidates: planCandidates },
            evaluation,
          };
    return { request, errors };
  }, [form, options, periods, candidates, definitions]);

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
          <DataSection
            form={{ ...form, sourceMode: "snapshot" }}
            options={{
              ...options,
              historical_index: null,
              historical_indices: [],
            }}
            update={update}
          />
          <BacktestSection
            form={form}
            options={options}
            update={update}
            earliest={null}
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
                    candidate(current.length, ["momentum"], definitions),
                  ])
                }
              >
                添加方案
              </Button>
            }
          >
            <Flex vertical gap={16}>
              {candidates.map((item, index) => (
                <CandidateEditor
                  key={item.id}
                  value={item}
                  definitions={definitions}
                  removable={candidates.length > 2}
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
              ))}
            </Flex>
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
  removable,
  onChange,
  onRemove,
}: {
  value: Candidate;
  definitions: FactorDefinition[];
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
        options={value.factors.map((row) => ({
          value: row.key,
          label: factorLabel(row.name),
        }))}
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
            <Typography.Text style={{ width: 120 }}>
              {definition?.label ?? row.name}
            </Typography.Text>
            <InputNumber
              size="small"
              min={0}
              max={100}
              value={row.weight}
              suffix="%"
              style={{ width: 110 }}
              onChange={(v) => setRow(row.key, { weight: Number(v ?? 0) })}
            />
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
