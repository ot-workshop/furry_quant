import type { FactorSelection } from "./factorSelection";
import type {
  ResearchRequest,
  Neutralization,
  ExperimentCandidateInput,
} from "./research";
import { API_BASE, request } from "./http";

export type ExperimentPeriodName = "development" | "validation" | "test";

export interface ExperimentMetadata {
  factor_selection?: FactorSelection;
  id: string;
  name: string;
  created_at: string;
  data_version: string;
  dataset_id?: string | null;
  is_synthetic: boolean;
  selected_candidate: string | null;
  validation_kind: string;
}

export interface ExperimentPeriod {
  start: string;
  end: string;
}

export interface ExperimentCandidate {
  id: string;
  name: string;
  factors: Array<{
    name: string;
    weight: number;
    window: number | null;
    id?: string;
  }>;
}

export interface ExperimentPlan {
  name: string;
  periods: Record<ExperimentPeriodName, ExperimentPeriod>;
  candidates: ExperimentCandidate[];
  selection_metric: string;
}

export interface ExperimentSharedParameters {
  universe?: ResearchRequest["data"];
  universe_label?: string;
  membership_policy?: string;
  neutralization?: Neutralization;
  backtest: {
    initial_cash: number;
    topk: number;
    n_drop: number;
    risk_degree: number;
    open_cost: number;
    close_cost: number;
    min_cost: number;
    rebalance_frequency?: "daily" | "weekly" | "monthly";
    cost_mode?: "legacy" | "itemized";
    commission_rate?: number;
    tax_mode?: "historical" | "fixed";
    stamp_tax_rate?: number;
    transfer_fee_rate?: number;
    impact_cost?: number;
  };
  codes: string[];
  benchmark: string;
  evaluation: { horizons: number[]; groups: number; min_cross_section: number };
}

export interface ExperimentSelection {
  candidate_id: string;
  metric: string;
  score: number;
  locked_at: string;
  tie_break: string;
  ranking: Array<{ candidate_id: string; score: number }>;
}

export interface ExperimentRow {
  coverage?: {
    signal_days: number;
    scored_days: number;
    min_pool: number;
    max_pool: number;
    min_scored: number;
    max_scored: number;
  };
  candidate_id: string;
  period: ExperimentPeriodName;
  backtest_run_id: string;
  factor_run_id: string;
  evaluation_run_id: string;
  trading_days: number;
  metrics: {
    total_return: number;
    benchmark_return: number;
    excess_return: number;
    max_drawdown: number;
    total_cost_amount: number;
    avg_turnover: number;
  };
}

export interface ExperimentResult {
  metadata: ExperimentMetadata;
  plan: ExperimentPlan;
  shared_parameters: ExperimentSharedParameters;
  selection: ExperimentSelection;
  rows: ExperimentRow[];
  limitations: string[];
}

export interface ExperimentList {
  items: ExperimentMetadata[];
  total: number;
}

export interface ReturnComparisonRow
  extends Omit<ExperimentRow, "period" | "coverage"> {
  period: "analysis";
  sample: "independent" | "common";
  metrics: ExperimentRow["metrics"] & {
    volatility?: number;
    sharpe_ratio?: number;
    trade_count?: number;
  };
  coverage: {
    signal_days: number;
    scored_days: number;
    min_scored: number;
    max_scored: number;
  };
}
export interface ReturnComparisonResult {
  metadata: ExperimentMetadata;
  plan: {
    name: string;
    comparison: { baseline_id: string; sample_mode: "common" | "independent" };
    periods: { analysis: ExperimentPeriod };
    candidates: ExperimentCandidateInput[];
  };
  shared_parameters: ExperimentSharedParameters;
  rows: ReturnComparisonRow[];
  selection: null;
  comparisons: Array<{
    candidate_id: string;
    independent_difference: number;
    common_difference: number | null;
    sample_difference: number | null;
  }>;
  sample_days: Array<{
    date: string;
    pool: number;
    common: number;
    candidates: Record<string, number>;
  }>;
  limitations: string[];
}

const id = encodeURIComponent;

export const experimentsApi = {
  list: (signal?: AbortSignal) =>
    request<ExperimentList>("/experiments", { signal }),
  get: (runId: string, signal?: AbortSignal) =>
    request<ExperimentResult | ReturnComparisonResult>(
      `/experiments/${id(runId)}`,
      { signal },
    ),
  downloadUrl: (runId: string) =>
    `${API_BASE}/experiments/${id(runId)}/download`,
  reportUrl: (runId: string) => `${API_BASE}/experiments/${id(runId)}/report`,
};
