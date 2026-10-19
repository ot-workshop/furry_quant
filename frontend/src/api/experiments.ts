const API_BASE = "/api/v1";

export interface ExperimentMetadata {
  id: string;
  name: string;
  created_at: string;
  data_version: string;
  is_synthetic: false;
  selected_candidate: string;
  validation_kind: string;
}

export interface ExperimentPeriod {
  start: string;
  end: string;
}

export interface ExperimentCandidateFactor {
  name: string;
  weight: number;
  window: number | null;
}

export interface ExperimentCandidate {
  id: string;
  name: string;
  factors: ExperimentCandidateFactor[];
}

export interface ExperimentPlan {
  name: string;
  periods: {
    development: ExperimentPeriod;
    validation: ExperimentPeriod;
    test: ExperimentPeriod;
  };
  candidates: ExperimentCandidate[];
  selection_metric: string;
}

export interface ExperimentBacktestParameters {
  initial_cash: number;
  topk: number;
  n_drop: number;
  risk_degree: number;
  open_cost: number;
  close_cost: number;
  min_cost: number;
}

export interface ExperimentEvaluationParameters {
  horizons: number[];
  groups: number;
  min_cross_section: number;
}

export interface ExperimentSharedParameters {
  backtest: ExperimentBacktestParameters;
  codes: string[];
  benchmark: string;
  evaluation: ExperimentEvaluationParameters;
}

export interface ExperimentSelectionRanking {
  candidate_id: string;
  score: number;
}

export interface ExperimentSelection {
  candidate_id: string;
  metric: string;
  score: number;
  locked_at: string;
  tie_break: string;
  ranking: ExperimentSelectionRanking[];
}

export interface ExperimentPeriodMetrics {
  total_return: number;
  benchmark_return: number;
  excess_return: number;
  max_drawdown: number;
  total_cost_amount: number;
  avg_turnover: number;
}

export type ExperimentPeriodName = "development" | "validation" | "test";

export interface ExperimentRow {
  candidate_id: string;
  period: ExperimentPeriodName;
  backtest_run_id: string;
  factor_run_id: string;
  evaluation_run_id: string;
  trading_days: number;
  metrics: ExperimentPeriodMetrics;
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

export class ExperimentsApiError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ExperimentsApiError";
  }
}

function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    (error as { name?: unknown }).name === "AbortError"
  );
}

async function request<T>(path: string, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      signal,
      headers: { Accept: "application/json" },
    });
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw new ExperimentsApiError("无法连接后端服务，请确认本地 API 已启动。");
  }

  if (!response.ok) {
    throw new ExperimentsApiError(`请求失败（HTTP ${response.status}）。`);
  }

  try {
    return (await response.json()) as T;
  } catch {
    throw new ExperimentsApiError("后端返回了无法解析的数据。");
  }
}

function encoded(id: string): string {
  return encodeURIComponent(id);
}

export const experimentsApi = {
  list(signal?: AbortSignal): Promise<ExperimentList> {
    return request<ExperimentList>("/experiments", signal);
  },

  get(id: string, signal?: AbortSignal): Promise<ExperimentResult> {
    return request<ExperimentResult>(
      `/experiments/${encoded(id)}`,
      signal,
    );
  },

  downloadUrl(id: string): string {
    return `${API_BASE}/experiments/${encoded(id)}/download`;
  },

  reportUrl(id: string): string {
    return `${API_BASE}/experiments/${encoded(id)}/report`;
  },
};

export function backtestDownloadUrl(id: string): string {
  return `${API_BASE}/backtests/${encoded(id)}/download`;
}

export function evaluationReportUrl(id: string): string {
  return `${API_BASE}/evaluations/${encoded(id)}/report`;
}
