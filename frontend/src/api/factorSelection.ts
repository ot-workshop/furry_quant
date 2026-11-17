import { query, request } from "./http";
import type {
  ExperimentCandidateInput,
  FinancialCoverage,
  ResearchOptions,
  ResearchRequest,
} from "./research";
import type { ICStats } from "../types";

export interface SelectionParameters {
  horizon: number;
  cutoff: string;
  factor_count: number;
  min_ic: number;
  min_ir: number;
  max_correlation: number;
  min_dates: number;
  start?: string | null;
}

export interface SelectionReference {
  evaluation_id: string;
  parameters: SelectionParameters;
  selection_id: string;
}

export interface SelectionCandidate {
  key: string;
  rank_ic: ICStats;
  last_label_exit: string | null;
  unmatured_dates: number;
  status:
    | "quality_excluded"
    | "eligible"
    | "dominated"
    | "selected"
    | "not_selected";
  reasons: string[];
}

export interface FactorSelection {
  selection_id: string;
  evaluation_id: string;
  source_sha256: string;
  reference: SelectionReference;
  parameters: SelectionParameters;
  status: "optimal" | "infeasible";
  selected_keys: string[];
  candidates: SelectionCandidate[];
  stages: {
    input: number;
    quality_passed: number;
    dominance_removed: number;
    milp_candidates: number;
  };
  max_feasible_count: number | null;
  objective_sum_ic: number | null;
  max_selected_correlation: number | null;
  mean_selected_correlation: number | null;
  message: string;
  methodology: string[];
}

export interface SelectionDraft {
  research: ResearchRequest;
  candidates: ExperimentCandidateInput[];
  periods: Record<
    "development" | "validation" | "test",
    { start: string; end: string }
  >;
  reference: SelectionReference;
  selection: FactorSelection;
  warnings: string[];
  financial_data: ResearchOptions["financial_data"];
  financial_coverage: FinancialCoverage | null;
  industry_data: ResearchOptions["industry_data"];
}

export function parseSelectionReference(value: string): SelectionReference {
  const reference = JSON.parse(value) as SelectionReference;
  if (
    !reference ||
    typeof reference.evaluation_id !== "string" ||
    typeof reference.selection_id !== "string" ||
    !reference.parameters ||
    typeof reference.parameters.cutoff !== "string"
  )
    throw new Error("组合筛选来源链接无效，请回到因子相关性页面重新筛选。");
  return reference;
}

export const selectionApi = {
  run: (
    evaluationId: string,
    parameters: SelectionParameters,
    signal?: AbortSignal,
  ) =>
    request<FactorSelection>(
      `/evaluations/${encodeURIComponent(evaluationId)}/selection${query({ ...parameters })}`,
      { signal },
    ),
  draft: (reference: SelectionReference, signal?: AbortSignal) =>
    request<SelectionDraft>(
      `/evaluations/${encodeURIComponent(reference.evaluation_id)}/selection/draft${query({ ...reference.parameters, selection_id: reference.selection_id })}`,
      { signal },
    ),
};
