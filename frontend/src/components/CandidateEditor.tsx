import { useId, useMemo } from "react";
import {
  Button,
  Card,
  Flex,
  Input,
  InputNumber,
  Select,
  Space,
  Typography,
} from "antd";
import { DeleteOutlined } from "@ant-design/icons";
import { FactorDataHint } from "./FactorDataHint";
import { FactorMissingPolicy } from "./FactorMissingPolicy";
import type {
  FactorDefinition,
  FinancialCoverage,
  ResearchOptions,
} from "../api/research";
import {
  FAMILY_ORDER,
  FINANCIAL_VERSION_REQUIRED,
  factorAvailable,
  type FactorRow,
  type FormState,
} from "../research/form";
import type { ExperimentCandidateDraft as Candidate } from "../research/experiments";
import { NeutralizationControls } from "../research/sections/NeutralizationControls";

export function CandidateEditor({
  value,
  definitions,
  form,
  options,
  coverage,
  removable,
  onChange,
  onRemove,
  individualIndustry = false,
}: {
  value: Candidate;
  definitions: FactorDefinition[];
  form: FormState;
  options: ResearchOptions;
  coverage: FinancialCoverage | null;
  removable: boolean;
  onChange: (value: Candidate) => void;
  onRemove: () => void;
  individualIndustry?: boolean;
}) {
  const enabled = value.factors.filter((row) => row.enabled);
  const financialVersionHintId = useId();
  const hasFinancialVersion = Boolean(options.financial_data?.version);
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
          disabled:
            !factorAvailable(definition, coverage, hasFinancialVersion) &&
            !value.factors.some(
              (row) => row.key === definition.name && row.enabled,
            ),
        })),
      }));
  }, [definitions, coverage, hasFinancialVersion, value.factors]);
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
      {individualIndustry ? (
        <div style={{ marginBottom: 12 }}>
          <NeutralizationControls
            options={options}
            form={{
              ...form,
              neutralization: value.neutralization?.mode ?? "off",
              industryLevel: value.neutralization?.industry_level ?? "L1",
            }}
            update={(patch) => {
              const mode =
                patch.neutralization ?? value.neutralization?.mode ?? "off";
              const level =
                patch.industryLevel ??
                value.neutralization?.industry_level ??
                "L1";
              onChange({
                ...value,
                neutralization: {
                  mode,
                  ...(mode === "industry" && level !== "L1"
                    ? { industry_level: level }
                    : {}),
                },
              });
            }}
          />
        </div>
      ) : null}
      <Select
        mode="multiple"
        aria-label="选择因子"
        aria-describedby={
          hasFinancialVersion ? undefined : financialVersionHintId
        }
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
      {!hasFinancialVersion ? (
        <Typography.Paragraph
          id={financialVersionHintId}
          style={{ margin: "6px 0 0" }}
        >
          {FINANCIAL_VERSION_REQUIRED}
        </Typography.Paragraph>
      ) : null}
      {enabled.map((row) => {
        const definition = definitions.find((d) => d.name === row.name);
        const available =
          !definition ||
          factorAvailable(definition, coverage, hasFinancialVersion);
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
              disabled={!available}
              suffix="%"
              style={{ width: 110 }}
              onChange={(v) => setRow(row.key, { weight: Number(v ?? 0) })}
            />
            {["value", "gross_margin_ttm"].includes(row.name) ? (
              <FactorMissingPolicy
                label={definition?.label ?? row.name}
                value={row.missingPolicy}
                disabled={!available}
                onChange={(missingPolicy) => setRow(row.key, { missingPolicy })}
              />
            ) : null}
            {definition?.default_window != null ? (
              <InputNumber
                size="small"
                min={2}
                max={252}
                value={row.window}
                disabled={!available}
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
