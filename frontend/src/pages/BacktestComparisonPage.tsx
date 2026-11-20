import { useState } from "react";
import { Alert, Button, Flex, Select, Switch, Tag, Typography } from "antd";
import { SwapOutlined } from "@ant-design/icons";
import {
  comparisons,
  type BacktestComparison,
  type EvidenceStatus,
} from "../api/comparisons";
import { api } from "../api/client";
import { EmptyState, PageHeader, Resource, Section } from "../components/ui";
import { useApiResource } from "../hooks/useApiResource";
import type { BacktestSummary } from "../types";
import { formatDateTime, formatPercent } from "../utils/format";

const STATUS_TEXT: Record<EvidenceStatus, string> = {
  same: "相同",
  different: "不同",
  unknown: "未知",
};

const STATUS_COLOR: Record<EvidenceStatus, string> = {
  same: "default",
  different: "warning",
  unknown: "default",
};

/** Text plus colour: the label carries the meaning, colour only supports it. */
function EvidenceTag({ status }: { status: EvidenceStatus }) {
  return (
    <Tag
      color={STATUS_COLOR[status]}
      variant={status === "unknown" ? "outlined" : "filled"}
    >
      {STATUS_TEXT[status]}
    </Tag>
  );
}

function shortId(value: string | null | undefined): string {
  if (!value) return "—";
  return value.length > 18 ? `${value.slice(0, 10)}…${value.slice(-6)}` : value;
}

function valueText(value: number | null, unit: "percent" | "number"): string {
  if (value === null || value === undefined) return "—";
  return unit === "percent" ? formatPercent(value, 3) : String(value);
}

function deltaText(value: number | null): string {
  if (value === null || value === undefined) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(3)} pp`;
}

function SideOption({ item }: { item: BacktestSummary }) {
  return (
    <Flex vertical>
      <Typography.Text>{item.name}</Typography.Text>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        {item.start_date} ~ {item.end_date} · {shortId(item.id)}
      </Typography.Text>
    </Flex>
  );
}

function ChoosePanel({
  items,
  baseline,
  candidate,
  onChange,
  onCompare,
  onSwap,
  loading,
}: {
  items: BacktestSummary[];
  baseline: string | null;
  candidate: string | null;
  onChange: (next: { baseline?: string; candidate?: string }) => void;
  onCompare: () => void;
  onSwap: () => void;
  loading: boolean;
}) {
  const options = items.map((item) => ({
    value: item.id,
    label: <SideOption item={item} />,
    name: item.name,
    start: item.start_date ?? "",
    end: item.end_date ?? "",
  }));
  const filter = (
    input: string,
    option?: { name?: string; value?: string; start?: string; end?: string },
  ) =>
    `${option?.name ?? ""} ${option?.value ?? ""} ${option?.start ?? ""} ${
      option?.end ?? ""
    }`
      .toLowerCase()
      .includes(input.toLowerCase());
  return (
    <Flex vertical gap={12}>
      <Flex gap={12} wrap align="flex-end">
        <Flex vertical gap={4} style={{ minWidth: 0, flex: "1 1 240px" }}>
          <Typography.Text type="secondary">A · 对照（变化前）</Typography.Text>
          <Select
            showSearch
            allowClear
            placeholder="选择一条已完成回测"
            value={baseline}
            options={options}
            filterOption={filter}
            onChange={(value) => onChange({ baseline: value ?? undefined })}
            aria-label="A 对照回测"
          />
        </Flex>
        <Flex vertical gap={4} style={{ minWidth: 0, flex: "1 1 240px" }}>
          <Typography.Text type="secondary">B · 候选（变化后）</Typography.Text>
          <Select
            showSearch
            allowClear
            placeholder="选择一条已完成回测"
            value={candidate}
            options={options}
            filterOption={filter}
            onChange={(value) => onChange({ candidate: value ?? undefined })}
            aria-label="B 候选回测"
          />
        </Flex>
        <Button
          icon={<SwapOutlined />}
          onClick={onSwap}
          disabled={!baseline || !candidate}
        >
          交换 A/B
        </Button>
        <Button
          type="primary"
          onClick={onCompare}
          loading={loading}
          disabled={!baseline || !candidate || baseline === candidate}
        >
          开始对照
        </Button>
      </Flex>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        差值为 B − A，收益类用百分点；只读取两条已存结果，不重跑、不修改原结果。
      </Typography.Text>
      {baseline && candidate && baseline === candidate ? (
        <Typography.Text type="warning" style={{ fontSize: 12 }}>
          请选择两条不同的回测。
        </Typography.Text>
      ) : null}
    </Flex>
  );
}

function MetricBlock({
  metric,
}: {
  metric: BacktestComparison["metrics"][number];
}) {
  const notComparable = !metric.comparable;
  return (
    <div className="cmp-block">
      <Flex justify="space-between" align="center" gap={8}>
        <Typography.Text strong>{metric.label}</Typography.Text>
        {notComparable ? <Tag color="default">差值不可比</Tag> : null}
      </Flex>
      <div className="cmp-row">
        <span className="cmp-key">A · 对照</span>
        <span className="cmp-value">
          {valueText(metric.baseline, metric.unit)}
        </span>
      </div>
      <div className="cmp-row">
        <span className="cmp-key">B · 候选</span>
        <span className="cmp-value">
          {valueText(metric.candidate, metric.unit)}
        </span>
      </div>
      <div className="cmp-row">
        <span className="cmp-key">B − A</span>
        <span className="cmp-value">
          {notComparable ? "不可比" : deltaText(metric.delta)}
        </span>
      </div>
    </div>
  );
}

function FieldRow({
  field,
}: {
  field: BacktestComparison["dimensions"][number]["fields"][number];
}) {
  return (
    <div className="cmp-row cmp-row-field">
      <span className="cmp-key">{field.label}</span>
      <span className="cmp-value">
        <span className="cmp-side">A</span> {field.baseline ?? "—"}
      </span>
      <span className="cmp-value">
        <span className="cmp-side">B</span> {field.candidate ?? "—"}
      </span>
      <span className="cmp-status">
        <EvidenceTag status={field.status} />
      </span>
    </div>
  );
}

export default function BacktestComparisonPage() {
  const list = useApiResource((signal) => api.backtests(signal), []);
  const [baseline, setBaseline] = useState<string | null>(null);
  const [candidate, setCandidate] = useState<string | null>(null);
  const [request, setRequest] = useState<{
    baseline: string;
    candidate: string;
  } | null>(null);
  const [onlyChanged, setOnlyChanged] = useState(true);
  const comparison = useApiResource(
    request
      ? (signal) =>
          comparisons.compare(request.baseline, request.candidate, signal)
      : null,
    [request?.baseline, request?.candidate],
  );

  const items = (list.data?.items ?? []).filter(
    (item) => item.status === "succeeded",
  );
  const data = comparison.data;
  const dimensions = data
    ? [...data.dimensions].sort((left, right) => {
        const order: Record<EvidenceStatus, number> = {
          different: 0,
          unknown: 1,
          same: 2,
        };
        return order[left.status] - order[right.status];
      })
    : [];
  const visible = dimensions
    .map((dimension) =>
      onlyChanged
        ? {
            ...dimension,
            fields: dimension.fields.filter((field) => field.status !== "same"),
          }
        : dimension,
    )
    .filter((dimension) => !onlyChanged || dimension.fields.length > 0);

  return (
    <>
      <PageHeader
        title="回测差异对照"
        subtitle="选择两条已完成回测，逐项核对数据、代码、因子与基准证据"
      />
      <Section title="选择对照">
        <Resource
          state={list}
          empty={(data) =>
            data.items.length ? null : <EmptyState title="暂无可对照的回测" />
          }
        >
          {() => (
            <ChoosePanel
              items={items}
              baseline={baseline}
              candidate={candidate}
              onChange={(next) => {
                if ("baseline" in next) setBaseline(next.baseline ?? null);
                if ("candidate" in next) setCandidate(next.candidate ?? null);
                // A displayed result only ever describes the pair that was asked
                // for; any change to the pickers drops it (and any old error).
                setRequest(null);
              }}
              onCompare={() =>
                baseline && candidate && setRequest({ baseline, candidate })
              }
              onSwap={() => {
                setBaseline(candidate);
                setCandidate(baseline);
                // Keep what is on screen identical to what was requested: swap
                // re-asks the other way round instead of re-labelling old numbers.
                setRequest(
                  candidate && baseline && candidate !== baseline
                    ? { baseline: candidate, candidate: baseline }
                    : null,
                );
              }}
              loading={comparison.loading}
            />
          )}
        </Resource>
      </Section>

      {request ? (
        <Resource state={comparison}>
          {() =>
            data ? (
              <Flex vertical gap={16}>
                <Section title="证据结论">
                  <Flex vertical gap={8}>
                    <Alert
                      type={
                        data.relation.status === "conflict" ? "warning" : "info"
                      }
                      showIcon
                      title={data.conclusion}
                      description={data.relation.explanation ?? undefined}
                    />
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      当前结果：A {data.baseline.name}（{data.baseline.id}） ↔
                      B {data.candidate.name}（{data.candidate.id}）
                    </Typography.Text>
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      比较区间{" "}
                      {data.comparability.baseline_interval.join(" ~ ")} ↔{" "}
                      {data.comparability.candidate_interval.join(" ~ ")} · 基准{" "}
                      {data.baseline.benchmark.name ??
                        data.baseline.benchmark.code ??
                        "未知"}{" "}
                      {data.baseline.benchmark.return_type
                        ? `(${data.baseline.benchmark.return_type})`
                        : ""}
                    </Typography.Text>
                    {data.comparability.reasons.length ? (
                      <Typography.Text type="warning" style={{ fontSize: 12 }}>
                        {data.comparability.reasons.join("；")}
                      </Typography.Text>
                    ) : null}
                  </Flex>
                </Section>

                <Section title="核心结果">
                  <div className="cmp-grid">
                    {data.metrics.map((metric) => (
                      <MetricBlock key={metric.key} metric={metric} />
                    ))}
                  </div>
                </Section>

                <Section
                  id="comparison-evidence"
                  title="版本与参数证据"
                  extra={
                    <Flex align="center" gap={8}>
                      <Typography.Text
                        type="secondary"
                        style={{ fontSize: 12 }}
                      >
                        只看不同与未知
                      </Typography.Text>
                      <Switch
                        checked={onlyChanged}
                        onChange={setOnlyChanged}
                        size="small"
                        aria-label="只看不同与未知条目"
                      />
                    </Flex>
                  }
                >
                  <Flex vertical gap={12}>
                    {visible.length ? (
                      visible.map((dimension) => (
                        <div key={dimension.key} className="cmp-block">
                          <Flex justify="space-between" align="center" gap={8}>
                            <Typography.Text strong>
                              {dimension.label}
                            </Typography.Text>
                            <EvidenceTag status={dimension.status} />
                          </Flex>
                          {dimension.fields.map((field) => (
                            <FieldRow
                              key={`${dimension.key}-${field.label}`}
                              field={field}
                            />
                          ))}
                        </div>
                      ))
                    ) : (
                      <Typography.Text type="secondary">
                        已存证据里没有不同或未知的条目。
                      </Typography.Text>
                    )}
                    <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                      A：{data.baseline.name}（{data.baseline.id}） · 创建于{" "}
                      {formatDateTime(data.baseline.created_at)}； B：
                      {data.candidate.name}（{data.candidate.id}） · 创建于{" "}
                      {formatDateTime(data.candidate.created_at)}
                    </Typography.Text>
                  </Flex>
                </Section>

                <Section title="首版未实现">
                  <ul className="notes-list">
                    {data.not_implemented.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </Section>
              </Flex>
            ) : (
              <EmptyState
                title="没有对照结果"
                description={comparison.error ?? "请选择两条回测后开始对照"}
              />
            )
          }
        </Resource>
      ) : (
        <Section title="对照结果">
          <EmptyState
            title="尚未开始对照"
            description="选择 A 与 B 两条已完成回测后点击“开始对照”"
          />
        </Section>
      )}
    </>
  );
}
