import { API_BASE, request } from "./http";

export type ExperimentPeriodName = "development" | "validation" | "test";

export interface ExperimentMetadata {
  id: string;
  name: string;
  created_at: string;
  data_version: string;
  dataset_id?: string | null;
  is_synthetic: boolean;
  selected_candidate: string;
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

const id = encodeURIComponent;

export const experimentsApi = {
  list: (signal?: AbortSignal) =>
    request<ExperimentList>("/experiments", { signal }),
  get: (runId: string, signal?: AbortSignal) =>
    request<ExperimentResult>(`/experiments/${id(runId)}`, { signal }),
  downloadUrl: (runId: string) =>
    `${API_BASE}/experiments/${id(runId)}/download`,
  reportUrl: (runId: string) => `${API_BASE}/experiments/${id(runId)}/report`,
};
