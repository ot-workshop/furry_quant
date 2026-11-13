import { useMemo } from "react";
import type { Dispatch, SetStateAction } from "react";
import {
  Alert,
  Button,
  Card,
  Flex,
  InputNumber,
  Select,
  Space,
  Switch,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { CopyOutlined, DeleteOutlined } from "@ant-design/icons";
import type { IndustryLevel, ResearchOptions } from "../../api/research";
import { Hint } from "../../components/ui";
import {
  addFactorInstance,
  equalWeights,
  FAMILY_ORDER,
  defaultIndustryLevel,
  factorAvailable,
  financialCoverage,
  industryLevels,
  removeFactorInstance,
  weightTotal,
  type FormState,
} from "../form";
import type { Update } from "./types";
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
  const coverage = financialCoverage(form, options);
  const groups = useMemo(() => {
    const byFamily = new Map<string, typeof options.factor_definitions>();
    for (const definition of options.factor_definitions) {
      const family = definition.family ?? "value";
      byFamily.set(family, [...(byFamily.get(family) ?? []), definition]);
    }
    const ordered = [...byFamily.entries()].sort(
      ([left], [right]) =>
        FAMILY_ORDER.indexOf(left as (typeof FAMILY_ORDER)[number]) -
        FAMILY_ORDER.indexOf(right as (typeof FAMILY_ORDER)[number]),
    );
    return ordered.map(([family, items]) => ({
      family,
      label: items[0]?.family_label ?? family,
      names: new Set(items.map((item) => item.name)),
    }));
  }, [options.factor_definitions]);
  const setRow = (key: string, patch: Partial<FormState["factors"][number]>) =>
    update({
      factors: form.factors.map((row) =>
        row.key === key ? { ...row, ...patch } : row,
      ),
    });

  function row(row: FormState["factors"][number]) {
    const definition = definitions[row.name];
    if (!definition) return null;
    const extra = row.key !== row.name;
    const available = factorAvailable(definition, coverage);
    return (
      <div className="factor-row" key={row.key}>
        <Flex align="center" gap={12} wrap>
          <Switch
            size="small"
            checked={row.enabled}
            disabled={!available}
            onChange={(enabled) => setRow(row.key, { enabled })}
          />
          <Flex vertical style={{ flex: "0 0 230px" }}>
            <Space size={6}>
              <Typography.Text
                strong={row.enabled}
                type={
                  available
                    ? row.enabled
                      ? undefined
                      : "secondary"
                    : "secondary"
                }
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
                    {!available ? (
                      <div>
                        当前数据集没有该股票池的财务数据，先在“数据管理 →
                        财务数据”下载后才能使用。
                      </div>
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
            disabled={!row.enabled || !available}
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
              disabled={!row.enabled || !available}
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
                disabled={!row.enabled || !available}
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
                disabled={!row.enabled || !available}
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
                  disabled={!available}
                  onClick={() =>
                    setForm((current) => addFactorInstance(current, row.key))
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
                    setForm((current) => removeFactorInstance(current, row.key))
                  }
                />
              </Tooltip>
            ) : null}
          </Space>
        </Flex>
      </div>
    );
  }

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
      {coverage ? (
        <Alert
          type={coverage.covered >= 2 ? "success" : "warning"}
          showIcon
          style={{ marginBottom: 12 }}
          title={`财务数据覆盖当前股票池 ${coverage.covered} / ${coverage.total} 只`}
          description={
            options.financial_data
              ? `版本 ${options.financial_data.version.slice(0, 8)} · 最新报告期 ${options.financial_data.latest_report_period ?? "—"}；按公告日后下一交易日起可用，历史修订未认证。`
              : undefined
          }
        />
      ) : null}
      {groups.map((group) => {
        const rows = form.factors.filter((item) => group.names.has(item.name));
        if (!rows.length) return null;
        return (
          <div key={group.family} className="factor-group">
            <Typography.Text
              strong
              style={{ display: "block", marginBottom: 8 }}
            >
              {group.label}
            </Typography.Text>
            {rows.map(row)}
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
              industryLevel: checked
                ? industryLevels(options).includes(form.industryLevel)
                  ? form.industryLevel
                  : defaultIndustryLevel(options)
                : form.industryLevel,
            })
          }
        />
        <Typography.Text>行业中性化</Typography.Text>
        <Select<IndustryLevel>
          size="small"
          value={form.industryLevel}
          disabled={form.neutralization !== "industry"}
          onChange={(industryLevel) => update({ industryLevel })}
          options={industryLevels(options).map((level) => ({
            value: level,
            label: `${options.industry_data?.levels?.[level]?.label ?? level}行业`,
          }))}
          style={{ width: 110 }}
        />
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          {options.industry_data
            ? `按当日历史申万${form.industryLevel === "L1" ? "一级" : form.industryLevel === "L2" ? "二级" : "三级"}行业去除行业均值（行业数据覆盖 ${options.industry_data.instruments} 只，核验至 ${options.industry_data.data_asof ?? "未记录"}；历史公开时点未认证）`
            : "历史行业数据尚未准备，暂不可用"}
        </Typography.Text>
      </Flex>
    </Card>
  );
}
