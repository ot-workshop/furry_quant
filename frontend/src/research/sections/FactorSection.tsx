import { NeutralizationControls } from "./NeutralizationControls";
import { useMemo } from "react";
import type { Dispatch, SetStateAction } from "react";
import {
  Alert,
  Button,
  Card,
  Flex,
  InputNumber,
  Space,
  Switch,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { CopyOutlined, DeleteOutlined } from "@ant-design/icons";
import type { ResearchOptions } from "../../api/research";
import { FactorDataHint } from "../../components/FactorDataHint";
import { FactorMissingPolicy } from "../../components/FactorMissingPolicy";
import {
  addFactorInstance,
  equalWeights,
  FAMILY_ORDER,
  factorAvailable,
  financialCoverage,
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
              <FactorDataHint
                definition={definition}
                form={form}
                options={options}
                row={row}
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
            {["value", "gross_margin_ttm"].includes(row.name) ? (
              <FactorMissingPolicy
                label={definition.label}
                value={row.missingPolicy}
                disabled={!row.enabled}
                onChange={(missingPolicy) => setRow(row.key, { missingPolicy })}
              />
            ) : null}
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
      <NeutralizationControls form={form} options={options} update={update} />
    </Card>
  );
}
