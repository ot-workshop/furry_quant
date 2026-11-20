import { query, request } from "./http";

export type EvidenceStatus = "same" | "different" | "unknown";

export interface ComparisonSide {
  id: string;
  name: string | null;
  status: string | null;
  start_date: string | null;
  end_date: string | null;
  created_at: string | null;
  benchmark: {
    code: string | null;
    name: string | null;
    return_type: string | null;
    series_sha256: string | null;
  };
  metrics: {
    total_return: number | null;
    benchmark_return: number | null;
    excess_return: number | null;
    max_drawdown: number | null;
  };
  execution_run_id: string | null;
  performance_version: string | null;
}

export interface ComparisonField {
  label: string;
  baseline: string | null;
  candidate: string | null;
  status: EvidenceStatus;
}

export interface ComparisonDimension {
  key: string;
  label: string;
  status: EvidenceStatus;
  fields: ComparisonField[];
}

export interface ComparisonMetric {
  key: string;
  label: string;
  unit: "percent" | "number";
  baseline: number | null;
  candidate: number | null;
  delta: number | null;
  comparable: boolean;
}

export interface ComparisonRelation {
  status: "same_execution" | "conflict" | "independent" | "unknown";
  execution_pointer: string | null;
  evidence: Array<{
    label: string;
    value?: unknown;
    baseline?: unknown;
    candidate?: unknown;
  }>;
  explanation: string | null;
  derived_run_id?: string;
  source_run_id?: string;
  benchmark_delta_pp?: number | null;
  excess_delta_pp?: number | null;
}

export interface BacktestComparison {
  baseline: ComparisonSide;
  candidate: ComparisonSide;
  direction: "candidate_minus_baseline";
  conclusion: string;
  conclusion_states_cause: boolean;
  comparability: {
    comparable: boolean;
    interval: EvidenceStatus;
    metric_version: EvidenceStatus;
    reasons: string[];
    baseline_interval: [string | null, string | null];
    candidate_interval: [string | null, string | null];
  };
  metrics: ComparisonMetric[];
  dimensions: ComparisonDimension[];
  relation: ComparisonRelation;
  not_implemented: string[];
}

export const comparisons = {
  compare: (
    baselineRunId: string,
    candidateRunId: string,
    signal?: AbortSignal,
  ) =>
    request<BacktestComparison>(
      `/backtest-comparisons${query({
        baseline_run_id: baselineRunId,
        candidate_run_id: candidateRunId,
      })}`,
      { signal },
    ),
};
