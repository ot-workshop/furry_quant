import type {
  FactorDefinition,
  ResearchOptions,
  ResearchRequest,
} from "../api/research";
import { request } from "../api/http";
import { buildData, type FactorRow, type FormState } from "./form";

export interface FactorCoverageRequest {
  data: ResearchRequest["data"];
  start: string;
  factor: Omit<ResearchRequest["factors"][number], "weight">;
  financial_version?: string;
  industry_version?: string;
}
export interface FactorCoverage {
  as_of: string;
  execution_date: string;
  data_version: string;
  financial_version: string | null;
  total: number;
  available: number;
  unavailable: number;
  not_applicable?: number;
  not_applicable_score?: number;
  industry_version?: string | null;
  missing_policy?: "zero" | "neutral" | "exclude";
  breakdown?: { positive: number; zero: number; negative: number };
  reasons: Array<{ code: string; label: string; count: number }>;
  examples: Array<{ code: string; name: string; reason: string }>;
  notes: string[];
}
export function coverageRequest(
  form: FormState,
  options: ResearchOptions,
  row: FactorRow,
): FactorCoverageRequest {
  const errors: string[] = [];
  const data = buildData({ ...form, topk: 1 }, options, errors);
  if (!data || errors.length)
    throw new Error(errors.join(" ") || "请先选择股票池。");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(form.start))
    throw new Error("请先设置回测开始日期。");
  const factor = {
    name: row.name,
    ...(row.key !== row.name ? { id: row.key } : {}),
    ...(row.window !== null ? { window: row.window } : {}),
    ...(row.skip !== null ? { skip: row.skip } : {}),
    ...(row.baselineWindow !== null
      ? { baseline_window: row.baselineWindow }
      : {}),
    ...(["value", "gross_margin_ttm"].includes(row.name)
      ? { missing_policy: row.missingPolicy ?? ("zero" as const) }
      : {}),
  };
  return {
    data,
    start: form.start,
    factor,
    ...(row.name === "gross_margin_ttm" &&
    (form.industryVersion || options.industry_data?.id)
      ? { industry_version: form.industryVersion || options.industry_data?.id }
      : {}),
    ...(options.financial_data
      ? { financial_version: options.financial_data.version }
      : {}),
  };
}
export function defaultCoverageRow(definition: FactorDefinition): FactorRow {
  return {
    key: definition.name,
    name: definition.name,
    enabled: false,
    weight: 0,
    window: definition.default_window,
    skip: definition.default_skip ?? null,
    baselineWindow: definition.default_baseline_window ?? null,
    ...(["value", "gross_margin_ttm"].includes(definition.name)
      ? { missingPolicy: "zero" as const }
      : {}),
  };
}
export const fetchFactorCoverage = (
  body: FactorCoverageRequest,
  signal?: AbortSignal,
) =>
  request<FactorCoverage>("/research/factor-coverage", {
    method: "POST",
    body,
    signal,
  });
