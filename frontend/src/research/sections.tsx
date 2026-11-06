import { useMemo } from "react";
import type { Dispatch, SetStateAction } from "react";
import {
  Alert,
  Button,
  Card,
  Collapse,
  DatePicker,
  Flex,
  Form,
  Input,
  InputNumber,
  Segmented,
  Select,
  Space,
  Switch,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { CopyOutlined, DeleteOutlined } from "@ant-design/icons";
import dayjs, { type Dayjs } from "dayjs";
import type { ResearchOptions } from "../api/research";
import { Hint } from "../components/ui";
import {
  addFactorInstance,
  dataRange,
  equalWeights,
  historicalOption,
  removeFactorInstance,
  snapshotOf,
  tradingDates,
  weightTotal,
  type FormState,
} from "./form";

type Update = (patch: Partial<FormState>) => void;

const SOURCE_LABELS: Record<string, string> = {
  official_tushare_shared: "官方共享行情",
  tushare_baostock: "联合数据",
};

export function DataSection({
  form,
  options,
  update,
}: {
  form: FormState;
  options: ResearchOptions;
  update: Update;
}) {
  const snapshot = snapshotOf(options, form.snapshotId);
  const historical = historicalOption(options, form.warehouseId);
  const historicalChoices = options.historical_indices ?? [];
  const exports = options.snapshots.filter((item) => !item.is_subset);
  const subsets = options.snapshots.filter((item) => item.is_subset);

  function chooseSnapshot(id: string) {
    const next = snapshotOf(options, id);
    if (!next) return;
    update({
      snapshotId: id,
      codes: next.codes.map((item) => item.code),
      topk: Math.min(form.topk, next.codes.length),
    });
  }

  return (
    <Card title="1. 数据与股票池">
      <Form layout="vertical">
        <Form.Item label="研究名称" required>
          <Input
            value={form.name}
            maxLength={80}
            showCount
            onChange={(e) => update({ name: e.target.value })}
          />
        </Form.Item>
        <Form.Item label="数据类型">
          <Segmented
            value={form.sourceMode}
            onChange={(value) =>
              update({ sourceMode: value as FormState["sourceMode"] })
            }
            options={[
              {
                value: "snapshot",
                label: "固定股票池",
                disabled: !options.snapshots.length,
              },
              {
                value: "index_warehouse",
                label: "沪深300历史成分（动态）",
                disabled: !historicalChoices.length,
              },
            ]}
          />
        </Form.Item>
        {form.sourceMode === "snapshot" ? (
          <>
            <Form.Item
              label="数据集"
              extra="数据集来自数据管理中的行情下载；需要其他股票时，请先下载包含它们的行情。"
            >
              <Select
                value={form.snapshotId || undefined}
                placeholder="选择数据集"
                onChange={chooseSnapshot}
                options={[
                  {
                    label: "行情下载",
                    options: exports.map((item) => ({
                      value: item.id,
                      label: `${SOURCE_LABELS[item.source] ?? "行情"} · ${item.codes.length} 只 · ${item.start} ~ ${item.end}`,
                    })),
                  },
                  ...(subsets.length
                    ? [
                        {
                          label: "以往研究的股票子集",
                          options: subsets.map((item) => ({
                            value: item.id,
                            label: `${item.codes.length} 只 · ${item.start} ~ ${item.end}`,
                          })),
                        },
                      ]
                    : []),
                ]}
              />
            </Form.Item>
            {snapshot ? (
              <Form.Item
                label={
                  <Space>
                    股票池
                    <Typography.Text type="secondary">
                      已选 {form.codes.length} / {snapshot.codes.length}
                    </Typography.Text>
                  </Space>
                }
                extra="至少 2 只；可从数据集中挑选任意子集。"
              >
                <Select
                  mode="multiple"
                  value={form.codes}
                  onChange={(codes: string[]) => update({ codes })}
                  options={snapshot.codes.map((item) => ({
                    value: item.code,
                    label: `${item.name} ${item.code}`,
                  }))}
                  optionFilterProp="label"
                  maxTagCount="responsive"
                  allowClear
                  placeholder="搜索股票名称或代码"
                />
                <Space style={{ marginTop: 8 }}>
                  <Button
                    size="small"
                    onClick={() =>
                      update({ codes: snapshot.codes.map((item) => item.code) })
                    }
                  >
                    全选
                  </Button>
                  <Button size="small" onClick={() => update({ codes: [] })}>
                    清空
                  </Button>
                </Space>
              </Form.Item>
            ) : null}
          </>
        ) : (
          <>
            <Form.Item label="历史成分版本">
              <Select
                value={form.warehouseId || undefined}
                onChange={(warehouseId: string) => update({ warehouseId })}
                options={historicalChoices.map((item) => ({
                  value: item.id,
                  label: `${item.start} ~ ${item.end}${item.shared_version ? ` · 共享版本 ${item.shared_version.slice(0, 8)}` : ""}`,
                }))}
              />
            </Form.Item>
            {historical ? (
              <Alert
                type="info"
                showIcon
                title={`每日按当时的沪深300成分排名（约 ${historical.codes_count} 只股票参与过），含科创板`}
                description={`研究区间 ${historical.start} ~ ${historical.end}；预热数据自 ${historical.data_start} 起。调出成分的持仓会持续尝试卖出，停牌或跌停时保留并记录原因。${historical.note ?? ""}`}
              />
            ) : null}
          </>
        )}
      </Form>
    </Card>
  );
}

export function FactorSection({
  form,
  options,
  update,
  setForm,
}: {
  form: FormState;
  options: ResearchOptions;
  update: Update;
  setForm: Dispatch<SetStateAction<FormState>>;
}) {
  const total = weightTotal(form);
  const definitions = useMemo(
    () =>
      Object.fromEntries(options.factor_definitions.map((d) => [d.name, d])),
    [options.factor_definitions],
  );
  const setRow = (key: string, patch: Partial<FormState["factors"][number]>) =>
    update({
      factors: form.factors.map((row) =>
        row.key === key ? { ...row, ...patch } : row,
      ),
    });

  return (
    <Card
      title="2. 因子与权重"
      extra={
        <Space>
          <Typography.Text
            type={Math.abs(total - 100) < 1e-6 ? "success" : "danger"}
          >
            合计 {total}%
          </Typography.Text>
          <Button
            size="small"
            onClick={() => setForm((current) => equalWeights(current))}
          >
            等权分配
          </Button>
        </Space>
      }
    >
      {form.factors.map((row) => {
        const definition = definitions[row.name];
        if (!definition) return null;
        const extra = row.key !== row.name;
        return (
          <div className="factor-row" key={row.key}>
            <Flex align="center" gap={12} wrap>
              <Switch
                size="small"
                checked={row.enabled}
                onChange={(enabled) => setRow(row.key, { enabled })}
              />
              <Flex vertical style={{ flex: "0 0 230px" }}>
                <Space size={6}>
                  <Typography.Text
                    strong={row.enabled}
                    type={row.enabled ? undefined : "secondary"}
                  >
                    {definition.label}
                    {extra ? ` · ${row.key}` : ""}
                  </Typography.Text>
                  <Hint
                    text={
                      <div>
                        <div>{definition.description}</div>
                        {definition.formula ? (
                          <div>公式：{definition.formula}</div>
                        ) : null}
                        <div>
                          {definition.direction === "lower"
                            ? "原始值越低越好"
                            : "原始值越高越好"}
                        </div>
                        {definition.data_note ? (
                          <div>{definition.data_note}</div>
                        ) : null}
                      </div>
                    }
                  />
                  {definition.family_label ? (
                    <Tag>{definition.family_label}</Tag>
                  ) : null}
                </Space>
              </Flex>
              <InputNumber
                size="small"
                min={0}
                max={100}
                step={5}
                value={row.weight}
                disabled={!row.enabled}
                onChange={(value) =>
                  setRow(row.key, { weight: Number(value ?? 0) })
                }
                suffix="%"
                style={{ width: 110 }}
                aria-label={`${definition.label}权重`}
              />
              {definition.default_window != null ? (
                <InputNumber
                  size="small"
                  min={2}
                  max={252}
                  value={row.window}
                  disabled={!row.enabled}
                  onChange={(value) =>
                    setRow(row.key, {
                      window: value === null ? null : Number(value),
                    })
                  }
                  prefix="窗口"
                  suffix="日"
                  style={{ width: 130 }}
                />
              ) : (
                <Typography.Text
                  type="secondary"
                  style={{ width: 130, fontSize: 12 }}
                >
                  无窗口参数
                </Typography.Text>
              )}
              {definition.default_skip != null ? (
                <Tooltip title="计算时剔除最近若干交易日，常用于避开短期反转">
                  <InputNumber
                    size="small"
                    min={0}
                    max={251}
                    value={row.skip}
                    disabled={!row.enabled}
                    onChange={(value) =>
                      setRow(row.key, {
                        skip: value === null ? null : Number(value),
                      })
                    }
                    prefix="剔除"
                    suffix="日"
                    style={{ width: 120 }}
                  />
                </Tooltip>
              ) : null}
              {definition.default_baseline_window != null ? (
                <Tooltip title="与此前不重叠的基准窗口比较">
                  <InputNumber
                    size="small"
                    min={2}
                    max={252}
                    value={row.baselineWindow}
                    disabled={!row.enabled}
                    onChange={(value) =>
                      setRow(row.key, {
                        baselineWindow: value === null ? null : Number(value),
                      })
                    }
                    prefix="基准"
                    suffix="日"
                    style={{ width: 120 }}
                  />
                </Tooltip>
              ) : null}
              <Space size={0}>
                {definition.default_window != null ? (
                  <Tooltip title="再添加一个不同窗口的同类因子">
                    <Button
                      size="small"
                      type="text"
                      icon={<CopyOutlined />}
                      onClick={() =>
                        setForm((current) =>
                          addFactorInstance(current, row.key),
                        )
                      }
                    />
                  </Tooltip>
                ) : null}
                {extra ? (
                  <Tooltip title="移除这个实例">
                    <Button
                      size="small"
                      type="text"
                      danger
                      icon={<DeleteOutlined />}
                      onClick={() =>
                        setForm((current) =>
                          removeFactorInstance(current, row.key),
                        )
                      }
                    />
                  </Tooltip>
                ) : null}
              </Space>
            </Flex>
          </div>
        );
      })}
      <Flex align="center" gap={12} style={{ marginTop: 16 }}>
        <Switch
          checked={form.neutralization === "industry"}
          disabled={!options.industry_data}
          onChange={(checked) =>
            update({
              neutralization: checked ? "industry" : "off",
              industryVersion: options.industry_data?.id ?? "",
            })
          }
        />
        <Typography.Text>行业中性化</Typography.Text>
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {options.industry_data
            ? `按当日历史申万一级行业去除行业均值（行业数据覆盖 ${options.industry_data.instruments} 只，核验至 ${options.industry_data.data_asof ?? "未记录"}）`
            : "历史行业数据尚未准备，暂不可用"}
        </Typography.Text>
      </Flex>
    </Card>
  );
}

export function BacktestSection({
  form,
  options,
  update,
  earliest,
}: {
  form: FormState;
  options: ResearchOptions;
  update: Update;
  earliest: string | null;
}) {
  const dates = useMemo(
    () => new Set(tradingDates(form, options)),
    [form, options],
  );
  const range = dataRange(form, options);
  const disabledDate = (day: Dayjs) => {
    const text = day.format("YYYY-MM-DD");
    if (range && (text < range.start || text > range.end)) return true;
    return dates.size > 0 && !dates.has(text);
  };
  const benchmarks = options.comparison_benchmarks ?? options.benchmarks;
  return (
    <Card title="3. 回测设置">
      <Form layout="vertical">
        <Form.Item
          label="回测区间"
          extra={
            earliest ? (
              <Space size={4}>
                <span>
                  按当前因子窗口，最早可从 {earliest}{" "}
                  开始（之前的数据用于预热）。
                </span>
                {form.start !== earliest ? (
                  <Button
                    size="small"
                    type="link"
                    onClick={() => update({ start: earliest })}
                  >
                    使用该日期
                  </Button>
                ) : null}
              </Space>
            ) : (
              "只能选择数据集内的交易日。"
            )
          }
        >
          <DatePicker.RangePicker
            value={[
              form.start ? dayjs(form.start) : null,
              form.end ? dayjs(form.end) : null,
            ]}
            disabledDate={disabledDate}
            allowClear={false}
            onChange={(values) =>
              update({
                start: values?.[0]?.format("YYYY-MM-DD") ?? "",
                end: values?.[1]?.format("YYYY-MM-DD") ?? "",
              })
            }
          />
        </Form.Item>
        <div className="grid-3">
          <Form.Item label="目标持股数量">
            <InputNumber
              min={1}
              max={300}
              value={form.topk}
              onChange={(v) => update({ topk: Number(v ?? 1) })}
              style={{ width: "100%" }}
              suffix="只"
            />
          </Form.Item>
          <Form.Item label="初始资金">
            <InputNumber
              min={1}
              step={10000}
              value={form.initialCash}
              onChange={(v) => update({ initialCash: Number(v ?? 0) })}
              style={{ width: "100%" }}
              suffix="元"
            />
          </Form.Item>
          <Form.Item
            label="评价基准"
            tooltip="只影响基准与相对绩效，不影响选股与成交"
          >
            <Select
              value={form.benchmark}
              onChange={(benchmark: string) => update({ benchmark })}
              options={[
                { value: "", label: "沿用数据集基准" },
                ...benchmarks.map((item) => ({
                  value: item.code,
                  label: `${item.name}（${item.code}）`,
                })),
              ]}
            />
          </Form.Item>
        </div>
        <div className="grid-3">
          <Form.Item
            label="调仓频率"
            tooltip="调仓日使用前一交易日收盘后的评分，次日开盘成交"
          >
            <Segmented
              block
              value={form.rebalanceFrequency}
              onChange={(value) =>
                update({
                  rebalanceFrequency: value as FormState["rebalanceFrequency"],
                })
              }
              options={[
                { value: "daily", label: "每日" },
                { value: "weekly", label: "每周" },
                { value: "monthly", label: "每月" },
              ]}
            />
          </Form.Item>
          <Form.Item
            label="每次最多替换"
            tooltip="每个调仓日最多卖出并替换的持仓数量"
          >
            <InputNumber
              min={0}
              max={form.topk}
              value={form.nDrop}
              onChange={(v) => update({ nDrop: Number(v ?? 0) })}
              style={{ width: "100%" }}
              suffix="只"
            />
          </Form.Item>
          <Form.Item
            label="股票仓位"
            tooltip="可用资金中投入股票的比例，其余保留现金"
          >
            <InputNumber
              min={1}
              max={100}
              value={form.riskDegree}
              onChange={(v) => update({ riskDegree: Number(v ?? 0) })}
              style={{ width: "100%" }}
              suffix="%"
            />
          </Form.Item>
        </div>
      </Form>
    </Card>
  );
}

export function CostSection({
  form,
  update,
}: {
  form: FormState;
  update: Update;
}) {
  const legacy = form.costMode === "legacy";
  const summary = legacy
    ? `综合费率 买入 ${form.openCost}% / 卖出 ${form.closeCost}%，冲击成本 ${form.impactCost}%`
    : `佣金 ${form.commissionRate}%，${form.taxMode === "historical" ? "历史税费" : "自定义税费"}，冲击成本 ${form.impactCost}%`;
  return (
    <Collapse
      items={[
        {
          key: "cost",
          label: (
            <Space>
              <Typography.Text strong>4. 交易成本</Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                {summary}
              </Typography.Text>
            </Space>
          ),
          children: (
            <Form layout="vertical">
              <Form.Item label="费用模式">
                <Segmented
                  value={form.costMode}
                  onChange={(value) =>
                    update({ costMode: value as FormState["costMode"] })
                  }
                  options={[
                    { value: "legacy", label: "综合费率" },
                    {
                      value: "itemized",
                      label: "分项（佣金 + 印花税 + 过户费）",
                    },
                  ]}
                />
              </Form.Item>
              <div className="grid-3">
                {legacy ? (
                  <>
                    <Form.Item label="买入综合费率">
                      <Pct
                        value={form.openCost}
                        onChange={(openCost) => update({ openCost })}
                      />
                    </Form.Item>
                    <Form.Item label="卖出综合费率">
                      <Pct
                        value={form.closeCost}
                        onChange={(closeCost) => update({ closeCost })}
                      />
                    </Form.Item>
                  </>
                ) : (
                  <>
                    <Form.Item
                      label="券商佣金（双边）"
                      tooltip="含经手费等规费，不含印花税与过户费"
                    >
                      <Pct
                        value={form.commissionRate}
                        onChange={(commissionRate) =>
                          update({ commissionRate })
                        }
                      />
                    </Form.Item>
                    <Form.Item label="印花税与过户费">
                      <Segmented
                        block
                        value={form.taxMode}
                        onChange={(value) =>
                          update({ taxMode: value as FormState["taxMode"] })
                        }
                        options={[
                          { value: "historical", label: "按历史费率" },
                          { value: "fixed", label: "自定义" },
                        ]}
                      />
                    </Form.Item>
                    {form.taxMode === "fixed" ? (
                      <>
                        <Form.Item label="卖出印花税">
                          <Pct
                            value={form.stampTaxRate}
                            onChange={(stampTaxRate) =>
                              update({ stampTaxRate })
                            }
                          />
                        </Form.Item>
                        <Form.Item label="过户费（双边）">
                          <Pct
                            value={form.transferFeeRate}
                            onChange={(transferFeeRate) =>
                              update({ transferFeeRate })
                            }
                          />
                        </Form.Item>
                      </>
                    ) : null}
                  </>
                )}
                <Form.Item
                  label="冲击成本（双边）"
                  tooltip="按成交额比例扣除的滑点假设，默认 0.05%"
                >
                  <Pct
                    value={form.impactCost}
                    onChange={(impactCost) => update({ impactCost })}
                  />
                </Form.Item>
                <Form.Item label={legacy ? "单笔最低费用" : "单笔最低佣金"}>
                  <InputNumber
                    min={0}
                    value={form.minCost}
                    onChange={(v) => update({ minCost: Number(v ?? 0) })}
                    style={{ width: "100%" }}
                    suffix="元"
                  />
                </Form.Item>
              </div>
            </Form>
          ),
        },
      ]}
    />
  );
}

export function EvaluationSection({
  form,
  update,
}: {
  form: FormState;
  update: Update;
}) {
  return (
    <Collapse
      items={[
        {
          key: "evaluation",
          label: (
            <Space>
              <Typography.Text strong>5. 因子评估</Typography.Text>
              <Typography.Text type="secondary" style={{ fontSize: 13 }}>
                持有期 {form.horizons.join("、")} 日 · {form.groups} 组
              </Typography.Text>
            </Space>
          ),
          children: (
            <Form layout="vertical">
              <div className="grid-3">
                <Form.Item
                  label="评估持有期（交易日）"
                  tooltip="只用于计算 IC 与分组收益，不影响回测"
                >
                  <Select
                    mode="tags"
                    value={form.horizons.map(String)}
                    onChange={(values: string[]) =>
                      update({
                        horizons: values
                          .map(Number)
                          .filter((n) => Number.isInteger(n) && n > 0),
                      })
                    }
                    options={[1, 5, 10, 20].map((n) => ({
                      value: String(n),
                      label: `${n} 日`,
                    }))}
                    tokenSeparators={[",", " ", "，"]}
                  />
                </Form.Item>
                <Form.Item label="分组数">
                  <InputNumber
                    min={2}
                    max={10}
                    value={form.groups}
                    onChange={(v) => update({ groups: Number(v ?? 2) })}
                    style={{ width: "100%" }}
                  />
                </Form.Item>
                <Form.Item label="计算 IC 的最少股票数">
                  <InputNumber
                    min={3}
                    value={form.minCrossSection}
                    onChange={(v) =>
                      update({ minCrossSection: Number(v ?? 3) })
                    }
                    style={{ width: "100%" }}
                  />
                </Form.Item>
              </div>
            </Form>
          ),
        },
      ]}
    />
  );
}

function Pct({
  value,
  onChange,
}: {
  value: number;
  onChange: (value: number) => void;
}) {
  return (
    <InputNumber
      min={0}
      max={99}
      step={0.01}
      value={value}
      onChange={(v) => onChange(Number(v ?? 0))}
      style={{ width: "100%" }}
      suffix="%"
    />
  );
}
