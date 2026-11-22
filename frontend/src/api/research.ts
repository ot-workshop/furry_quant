import type { SelectionReference } from "./factorSelection";
import { request } from "./http";

export type ValidationMode = "off" | "verify" | "fallback";
export type SourceMode = "snapshot" | "index_warehouse" | "market_warehouse";
export type MissingPolicy = "zero" | "neutral" | "exclude";
export type TaskKind = "research" | "experiment";
export type TaskStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "interrupted";
export type TaskStage =
  | "queued"
  | "prepare"
  | "factors"
  | "backtest"
  | "evaluation"
  | "experiment"
  | "download"
  | "finished";

export interface SnapshotCode {
  code: string;
  name: string;
}

export interface Snapshot {
  id: string;
  label: string;
  start: string;
  end: string;
  trading_days: number;
  trading_dates?: string[];
  is_subset?: boolean;
  benchmark: string;
  codes: SnapshotCode[];
  validation_mode: ValidationMode;
  source: string;
  financial_coverage?: FinancialCoverage | null;
  st_coverage?: FinancialCoverage;
  listing_coverage?: FinancialCoverage;
  security_by_code?: Record<
    string,
    { st_coverage: boolean; listing_coverage: boolean }
  >;
}

export interface FactorDefinition {
  name: string;
  label: string;
  description: string;
  default_window: number | null;
  default_skip?: number | null;
  default_baseline_window?: number | null;
  family?: string;
  family_label?: string;
  direction?: "higher" | "lower";
  required_fields?: string[];
  formula?: string;
  data_note?: string;
  requires_financials?: boolean;
  missing_policy?: MissingPolicy;
  missing_policies?: MissingPolicy[];
}

export interface FinancialCoverage {
  covered: number;
  total: number;
}

export interface FinancialDataSummary {
  version: string;
  created_at?: string | null;
  codes: number;
  rows: number;
  quarantined: number;
  latest_report_period: string | null;
  earliest_report_period: string | null;
  calendar_start: string;
  calendar_end: string;
  policy: string;
  point_in_time_certified: boolean;
}

export interface BenchmarkOption {
  code: string;
  name: string;
  return_type?: "price" | "gross_total_return";
  coverage?: { start: string; end: string; rows: number; source: string };
}

export interface ResearchFactor {
  name: string;
  weight: number;
  window?: number | null;
  skip?: number;
  baseline_window?: number;
  id?: string;
  missing_policy?: MissingPolicy;
}

export interface ResearchBacktest {
  benchmark?: string | null;
  start: string;
  end: string;
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
}

export interface ResearchEvaluation {
  horizons: number[];
  groups: number;
  min_cross_section: number;
}

export interface SnapshotData {
  mode: "snapshot";
  snapshot_id: string | null;
  codes: string[];
  filters?: UniverseFilters;
}

export interface HistoricalData {
  mode: "index_warehouse";
  warehouse_id: string;
  filters?: UniverseFilters;
}

export interface UniverseFilters {
  boards: Array<"main" | "chinext" | "star">;
  exclude_st: boolean;
  min_listing_days: number;
  min_market_cap: number | null;
  max_market_cap: number | null;
  min_avg_amount: number | null;
  amount_window: number;
  industries: string[];
  industry_level: IndustryLevel;
  industry_version?: string | null;
}

export interface MarketUniverseData {
  mode: "market_warehouse";
  warehouse_id: string;
  filters?: UniverseFilters;
  codes?: string[];
}

export interface HistoricalIndexOption {
  listing_coverage?: FinancialCoverage;
  security_by_code?: Record<
    string,
    { st_coverage: boolean; listing_coverage: boolean }
  >;
  full_market?: boolean;
  st_policy?: string;
  st_coverage?: { covered: number; total: number };
  id: string;
  label: string;
  start: string;
  end: string;
  data_start: string;
  shared_version?: string;
  trading_dates: string[];
  codes_count: number;
  membership_policy: string;
  point_in_time_certified: boolean;
  note: string;
  codes?: string[];
  financial_coverage?: FinancialCoverage | null;
}

export type IndustryLevel = "L1" | "L2" | "L3";

export interface IndustryLevelData {
  label: string;
  rows: number;
  instruments: number;
  sectors: number;
}

export interface Neutralization {
  mode: "off" | "industry";
  industry_version?: string;
  industry_level?: IndustryLevel;
}

export interface ResearchRequest {
  financials?: {
    version: string;
    availability_policy?: string;
  };
  neutralization?: Neutralization;
  name: string;
  data: SnapshotData | HistoricalData | MarketUniverseData;
  factors: ResearchFactor[];
  backtest: ResearchBacktest;
  evaluation: ResearchEvaluation;
}

export interface ExperimentCandidateInput {
  id: string;
  name: string;
  factors: ResearchFactor[];
  neutralization?: Neutralization;
}

export interface ExperimentRequest {
  factor_selection?: SelectionReference;
  name: string;
  data: ResearchRequest["data"];
  backtest: Omit<ResearchBacktest, "start" | "end">;
  plan:
    | {
        comparison?: undefined;
        periods: Record<
          "development" | "validation" | "test",
          { start: string; end: string }
        >;
        candidates: ExperimentCandidateInput[];
      }
    | {
        comparison: {
          baseline_id: string;
          sample_mode: "common" | "independent";
        };
        periods: { analysis: { start: string; end: string } };
        candidates: ExperimentCandidateInput[];
      };
  evaluation: ResearchEvaluation;
  neutralization?: Neutralization;
}

export interface ResearchResult {
  factor_as_of?: string;
  dataset_id?: string;
  data_version?: string;
  factor_run_id?: string;
  backtest_run_id?: string;
  evaluation_run_id?: string;
  experiment_run_id?: string;
  selected_candidate?: string | null;
  reused?: boolean;
}

export interface TaskErrorDetails {
  kind?: "runtime";
  stage?: string | null;
  exit_code?: number | null;
  oom_killed?: boolean | null;
  exception_type?: string;
  summary?: string;
  trace?: Array<{ file: string; function: string; line: number }>;
}

export interface ResearchTask {
  id: string;
  kind?: TaskKind;
  name: string;
  status: TaskStatus;
  stage: TaskStage;
  message: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  progress: { completed: number; total: number };
  request: ResearchRequest | ExperimentRequest;
  result: ResearchResult | null;
  error: string | null;
  error_details?: TaskErrorDetails | null;
}

export interface ResearchOptions {
  industry_data?: {
    id: string;
    source: string;
    rows: number;
    instruments: number;
    policy: string;
    point_in_time_certified: boolean;
    data_asof?: string | null;
    levels?: Partial<Record<IndustryLevel, IndustryLevelData>>;
    sectors?: Array<{
      level: IndustryLevel;
      industry_code: string;
      industry_name: string;
    }>;
  } | null;
  historical_indices?: HistoricalIndexOption[];
  market_universes?: HistoricalIndexOption[];
  snapshots: Snapshot[];
  financial_data?: FinancialDataSummary | null;
  factor_definitions: FactorDefinition[];
  benchmarks: BenchmarkOption[];
  comparison_benchmarks?: BenchmarkOption[];
  defaults: ResearchRequest;
  limits: { max_codes: number; max_queued_tasks: number };
}

export interface TaskList {
  items: ResearchTask[];
  total: number;
}

const id = encodeURIComponent;

export const researchApi = {
  options: (signal?: AbortSignal) =>
    request<ResearchOptions>("/research/options", { signal }),
  tasks: (signal?: AbortSignal) =>
    request<TaskList>("/research/tasks", { signal }),
  task: (taskId: string, signal?: AbortSignal) =>
    request<ResearchTask>(`/research/tasks/${id(taskId)}`, { signal }),
  submit: (body: ResearchRequest, key: string) =>
    request<ResearchTask>("/research/tasks", {
      method: "POST",
      body,
      headers: { "Idempotency-Key": key },
    }),
  submitExperiment: (
    body: ExperimentRequest,
    key: string,
    signal?: AbortSignal,
  ) =>
    request<ResearchTask>("/research/experiments", {
      method: "POST",
      body,
      signal,
      headers: { "Idempotency-Key": key },
    }),
  cancel: (taskId: string) =>
    request<ResearchTask>(`/research/tasks/${id(taskId)}/cancel`, {
      method: "POST",
      body: {},
    }),
};

export function isExperimentTask(
  task: ResearchTask,
): task is ResearchTask & { request: ExperimentRequest } {
  return task.kind === "experiment";
}
