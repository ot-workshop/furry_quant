import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  DatePicker,
  Descriptions,
  Flex,
  Form,
  InputNumber,
  Steps,
  Table,
  Tag,
  Typography,
} from "antd";
import { FilterOutlined, ExperimentOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import {
  selectionApi,
  type FactorSelection,
  type SelectionCandidate,
  type SelectionParameters,
} from "../api/factorSelection";
import { describeError, isAbortError } from "../api/http";
import { navigate, paths } from "../app/routes";
import { Notes, Section } from "../components/ui";
import type { EvaluationResult } from "../types";
import { factorLabel, formatNumber } from "../utils/format";
import type { AnalysisRange } from "./AnalysisDates";

const STATUS = {
  quality_excluded: "质量未通过",
  eligible: "候选",
  dominated: "冗余剔除",
  selected: "入选",
  not_selected: "未入选",
};

export function FactorSelectionPanel({
  result,
  horizon,
  range,
}: {
  result: EvaluationResult;
  horizon: string;
  range: AnalysisRange;
}) {
  const count = Object.keys(result.factors).filter(
    (key) => key !== "composite",
  ).length;
  const [parameters, setParameters] = useState<SelectionParameters>({
    horizon: Number(horizon),
    cutoff: range?.[1] ?? result.metadata.end,
    start: range?.[0] ?? null,
    factor_count: Math.min(3, count),
    min_ic: 0,
    min_ir: 0,
    max_correlation: 0.6,
    min_dates: 20,
  });
  const [saved, setSaved] = useState<{
    signature: string;
    result: FactorSelection;
  } | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const active = useRef<AbortController | null>(null);
  useEffect(() => () => active.current?.abort(), []);
  const signature = JSON.stringify(parameters);
  const selected = saved?.signature === signature ? saved.result : null;
  const update = (patch: Partial<SelectionParameters>) => {
    setParameters((value) => ({ ...value, ...patch }));
    setError(null);
  };

  async function run() {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    setPending(true);
    setError(null);
    try {
      const value = await selectionApi.run(
        result.metadata.id,
        parameters,
        controller.signal,
      );
      if (!controller.signal.aborted) setSaved({ signature, result: value });
    } catch (failure) {
      if (!isAbortError(failure)) setError(describeError(failure));
    } finally {
      if (!controller.signal.aborted) setPending(false);
    }
  }

  return (
    <Section
      title="组合筛选"
      description="按 IC、IR 和相关性筛选因子组合，可继续进行分段比较。"
    >
      <Form layout="vertical">
        <div className="grid-3">
          <Form.Item label="因子数量">
            <InputNumber
              aria-label="筛选因子数量"
              min={1}
              max={Math.max(1, count)}
              precision={0}
              value={parameters.factor_count}
              onChange={(value) =>
                value !== null && update({ factor_count: Number(value) })
              }
            />
          </Form.Item>
          <Form.Item label="最低平均 Rank IC">
            <InputNumber
              aria-label="最低平均 Rank IC"
              min={0}
              max={1}
              step={0.005}
              value={parameters.min_ic}
              onChange={(value) =>
                value !== null && update({ min_ic: Number(value) })
              }
            />
          </Form.Item>
          <Form.Item label="最低 Rank IC IR（未年化）">
            <InputNumber
              aria-label="最低 Rank IC IR"
              min={0}
              step={0.1}
              value={parameters.min_ir}
              onChange={(value) =>
                value !== null && update({ min_ir: Number(value) })
              }
            />
          </Form.Item>
          <Form.Item label="平均绝对相关上限">
            <InputNumber
              aria-label="组合相关性上限"
              min={0}
              max={1}
              step={0.05}
              value={parameters.max_correlation}
              onChange={(value) =>
                value !== null && update({ max_correlation: Number(value) })
              }
            />
          </Form.Item>
          <Form.Item label="最少有效日期">
            <InputNumber
              aria-label="筛选最少有效日期"
              min={2}
              max={25200}
              precision={0}
              value={parameters.min_dates}
              onChange={(value) =>
                value !== null && update({ min_dates: Number(value) })
              }
            />
          </Form.Item>
          <Form.Item label="推荐截止日（当日收盘后）">
            <DatePicker
              aria-label="推荐截止日"
              allowClear={false}
              value={dayjs(parameters.cutoff)}
              disabledDate={(day) =>
                day.format("YYYY-MM-DD") > result.metadata.end ||
                Boolean(
                  parameters.start &&
                    day.format("YYYY-MM-DD") < parameters.start,
                )
              }
              onChange={(day) =>
                day && update({ cutoff: day.format("YYYY-MM-DD") })
              }
            />
          </Form.Item>
        </div>
      </Form>
      <Typography.Paragraph type="secondary">
        统计区间：{parameters.start ?? result.metadata.start} 至{" "}
        {parameters.cutoff}；持有期 {horizon} 个交易日。
      </Typography.Paragraph>
      <Flex gap={12} wrap>
        <Button
          type="primary"
          icon={<FilterOutlined />}
          loading={pending}
          disabled={!count}
          onClick={() => void run()}
        >
          筛选组合
        </Button>
        <Button
          icon={<ExperimentOutlined />}
          disabled={selected?.status !== "optimal" || pending}
          onClick={() =>
            selected &&
            navigate(paths.newExperiment(JSON.stringify(selected.reference)))
          }
        >
          创建分段比较
        </Button>
      </Flex>
      {error ? (
        <Alert
          type="error"
          showIcon
          title="筛选未完成"
          description={error}
          style={{ marginTop: 16 }}
        />
      ) : null}
      {saved && !selected ? (
        <Typography.Paragraph type="secondary" style={{ marginTop: 12 }}>
          参数已修改，请重新筛选后创建比较。
        </Typography.Paragraph>
      ) : null}
      {selected ? (
        <Flex vertical gap={16} style={{ marginTop: 20 }}>
          <Alert
            type={selected.status === "optimal" ? "success" : "warning"}
            showIcon
            title={selected.message}
            description={
              selected.status === "optimal"
                ? "组合按等权配置，可创建分段比较查看回测表现。"
                : "请调整因子数量或筛选条件后重试。"
            }
          />
          <Steps
            size="small"
            current={4}
            items={[
              { title: "原始候选", content: `${selected.stages.input} 个` },
              {
                title: "质量预筛",
                content: `通过 ${selected.stages.quality_passed} 个`,
              },
              {
                title: "冗余预筛",
                content: `剔除 ${selected.stages.dominance_removed} 个`,
              },
              {
                title: "MILP",
                content: `候选 ${selected.stages.milp_candidates} 个 / 入选 ${selected.selected_keys.length} 个`,
              },
            ]}
          />
          {selected.status === "optimal" ? (
            <Descriptions
              size="small"
              column={{ xs: 1, md: 3 }}
              items={[
                {
                  key: "chosen",
                  label: "推荐因子",
                  children: selected.selected_keys
                    .map((key) =>
                      factorLabel(key, result.metadata.factor_definitions),
                    )
                    .join("、"),
                },
                {
                  key: "maximum",
                  label: "组合内最大平均绝对相关",
                  children: formatNumber(selected.max_selected_correlation, 4),
                },
                {
                  key: "average",
                  label: "组合内平均绝对相关",
                  children: formatNumber(selected.mean_selected_correlation, 4),
                },
              ]}
            />
          ) : null}
          <Table<SelectionCandidate>
            size="small"
            rowKey="key"
            dataSource={selected.candidates}
            pagination={false}
            scroll={{ x: 950 }}
            columns={[
              {
                title: "因子",
                render: (_, item) =>
                  factorLabel(item.key, result.metadata.factor_definitions),
              },
              {
                title: "平均 Rank IC",
                render: (_, item) => formatNumber(item.rank_ic.mean, 4),
              },
              {
                title: "IR",
                render: (_, item) => formatNumber(item.rank_ic.ir, 3),
              },
              {
                title: "有效日期",
                render: (_, item) => item.rank_ic.valid_dates,
              },
              { title: "未到期标签日期", dataIndex: "unmatured_dates" },
              {
                title: "最晚已实现标签",
                render: (_, item) => item.last_label_exit ?? "—",
              },
              {
                title: "状态",
                render: (_, item) => (
                  <Tag
                    color={item.status === "selected" ? "success" : "default"}
                  >
                    {STATUS[item.status]}
                  </Tag>
                ),
              },
              { title: "原因", render: (_, item) => item.reasons.join("；") },
            ]}
          />
          <Notes title="计算口径" items={selected.methodology} />
        </Flex>
      ) : null}
    </Section>
  );
}
