import { useMemo, useRef, useState } from "react";
import {
  Alert,
  App,
  Button,
  Card,
  Checkbox,
  Flex,
  List,
  Select,
  Space,
  Typography,
} from "antd";
import { CopyOutlined } from "@ant-design/icons";
import type { ResearchOptions, ResearchRequest } from "../api/research";
import {
  ExperimentParameters,
  ExperimentSubmission,
} from "../components/ExperimentSubmission";
import { useExperimentSubmission } from "../hooks/useExperimentSubmission";
import { navigate, paths } from "../app/routes";
import { CandidateEditor } from "../components/CandidateEditor";
import { PageHeader } from "../components/ui";
import {
  defaultForm,
  formFromRequest,
  factorRows,
  financialCoverage,
  type FormState,
} from "../research/form";
import {
  buildReturnComparison,
  type ExperimentCandidateDraft,
} from "../research/experiments";
import {
  BacktestSection,
  CostSection,
  DataSection,
  EvaluationSection,
  UniverseFilters,
} from "../research/sections";

export default function ReturnComparisonNew({
  options,
  research = null,
  active = true,
}: {
  options: ResearchOptions;
  research?: ResearchRequest | null;
  active?: boolean;
}) {
  const { message } = App.useApp();
  const restored = useMemo(() => {
    if (!research) return null;
    const restored = formFromRequest(research, options);
    if (
      research.financials &&
      research.financials.version !== options.financial_data?.version
    )
      restored.warnings.push(
        "原任务的财务版本与当前版本不同，本次各方案将使用当前可用财务数据重新计算；原结果保留。",
      );
    return restored;
  }, [research, options]);
  const [form, setForm] = useState<FormState>(() => ({
    ...(restored?.form ?? defaultForm(options)),
    name: "收益来源对照",
  }));
  const [candidates, setCandidates] = useState<ExperimentCandidateDraft[]>(
    () => {
      const initial = restored?.form ?? defaultForm(options);
      const rows = initial.factors;
      return [
        {
          id: "base",
          name: "基准方案",
          factors: rows,
          neutralization: {
            mode: initial.neutralization,
            ...(initial.neutralization === "industry"
              ? { industry_level: initial.industryLevel }
              : {}),
          },
        },
        {
          id: "control_1",
          name: options.industry_data ? "行业处理对照" : "仅动量对照",
          factors: options.industry_data
            ? rows.map((row) => ({ ...row }))
            : factorRows(
                [{ name: "momentum", weight: 1, window: 20 }],
                options.factor_definitions,
              ),
          neutralization: {
            mode:
              options.industry_data && initial.neutralization !== "industry"
                ? "industry"
                : "off",
          },
        },
      ];
    },
  );
  const [baseline, setBaseline] = useState("base");
  const [common, setCommon] = useState(true);
  const serial = useRef(2);
  const submission = useExperimentSubmission(
    "return-comparison",
    (task) => {
      message.success(`已提交「${task.name || form.name}」`);
      navigate(paths.tasks(task.id));
    },
    active,
  );
  const update = (patch: Partial<FormState>) =>
    setForm((value) => ({ ...value, ...patch }));
  const built = useMemo(
    () => buildReturnComparison(form, options, candidates, baseline, common),
    [form, options, candidates, baseline, common],
  );
  const coverage = financialCoverage(form, options);
  return (
    <>
      <PageHeader
        title="收益来源对照"
        subtitle="比较因子、行业处理和缺失规则的影响，并区分股票样本变化"
        back={{ to: paths.experiments(), label: "全部方案比较" }}
      />
      <div className="with-aside">
        <ExperimentParameters waiting={submission.state.kind === "waiting"}>
          {restored?.warnings.length ? (
            <Alert
              type="warning"
              showIcon
              title="原配置需要调整"
              description={restored.warnings.join(" ")}
            />
          ) : null}
          <DataSection form={form} options={options} update={update} />
          <UniverseFilters form={form} options={options} update={update} />
          <BacktestSection
            form={form}
            options={options}
            update={update}
            earliest={built.earliest}
          />
          <Card title="对照设置">
            <Flex vertical gap={16}>
              <Space>
                <Typography.Text>基准方案</Typography.Text>
                <Select
                  aria-label="基准方案"
                  style={{ width: 220 }}
                  value={baseline}
                  onChange={setBaseline}
                  options={candidates.map((c) => ({
                    value: c.id,
                    label: c.name,
                  }))}
                />
              </Space>
              <Checkbox
                checked={common}
                onChange={(e) => setCommon(e.target.checked)}
              >
                固定相同股票样本
              </Checkbox>
              <Typography.Text type="secondary">
                同时运行各方案可用样本和每日共同可评分样本，限制样本后重新排名。共同样本仅使用信号日已知信息；不会要求股票在整个区间都有效。
              </Typography.Text>
            </Flex>
          </Card>
          <Card
            title="对照方案"
            extra={
              <Button
                icon={<CopyOutlined />}
                disabled={candidates.length >= 8}
                onClick={() => {
                  const source = candidates.find((c) => c.id === baseline)!;
                  const index = serial.current++;
                  setCandidates((current) => [
                    ...current,
                    {
                      ...source,
                      id: `control_${index}`,
                      name: `${source.name}对照 ${index}`,
                      factors: source.factors.map((f) => ({ ...f })),
                      neutralization: source.neutralization
                        ? { ...source.neutralization }
                        : undefined,
                    },
                  ]);
                }}
              >
                复制基准方案
              </Button>
            }
          >
            <Typography.Paragraph type="secondary">
              复制方案后增减因子、调整权重、切换行业中性化或缺失处理。因子权重需合计100%；同时修改多个设置时，结果包含这些变化的共同影响。
            </Typography.Paragraph>
            <Flex vertical gap={16}>
              {candidates.map((candidate) => (
                <CandidateEditor
                  key={candidate.id}
                  value={candidate}
                  definitions={options.factor_definitions}
                  form={form}
                  options={options}
                  coverage={coverage}
                  individualIndustry
                  removable={candidates.length > 2 && candidate.id !== baseline}
                  onChange={(next) =>
                    setCandidates((current) =>
                      current.map((c) => (c.id === next.id ? next : c)),
                    )
                  }
                  onRemove={() =>
                    setCandidates((current) =>
                      current.filter((c) => c.id !== candidate.id),
                    )
                  }
                />
              ))}
            </Flex>
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
                  <List.Item>
                    <Typography.Text>{text}</Typography.Text>
                  </List.Item>
                )}
              />
            ) : (
              <Typography.Text type="success">
                配置检查通过，所有方案使用相同交易参数和数据版本。
              </Typography.Text>
            )}
            <ExperimentSubmission
              label="运行收益对照"
              state={submission.state}
              fingerprint={built.request ? JSON.stringify(built.request) : null}
              onSubmit={() => void submission.submit(built.request)}
              onStop={submission.stopWaiting}
            />
            <Typography.Paragraph
              type="secondary"
              style={{ marginTop: 12, marginBottom: 0 }}
            >
              结果为同区间历史对照，不进行样本外选优。已有研究和回测结果保留。
            </Typography.Paragraph>
          </Card>
        </div>
      </div>
    </>
  );
}
