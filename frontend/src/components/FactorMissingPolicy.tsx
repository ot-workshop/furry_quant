import { Select } from "antd";
import type { MissingPolicy } from "../api/research";

export const MISSING_POLICY_LABELS: Record<MissingPolicy, string> = {
  zero: "缺失记0分",
  neutral: "缺失记50分",
  exclude: "缺失时剔除",
};

export function FactorMissingPolicy({
  value = "zero",
  label = "盈利收益率",
  disabled,
  onChange,
}: {
  value?: MissingPolicy;
  label?: string;
  disabled?: boolean;
  onChange: (value: MissingPolicy) => void;
}) {
  return (
    <Select<MissingPolicy>
      size="small"
      value={value}
      disabled={disabled}
      onChange={onChange}
      aria-label={`${label}缺失处理`}
      style={{ width: 150 }}
      options={Object.entries(MISSING_POLICY_LABELS).map(([value, label]) => ({
        value: value as MissingPolicy,
        label,
      }))}
    />
  );
}
