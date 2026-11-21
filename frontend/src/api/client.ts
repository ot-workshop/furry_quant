import type {
  BacktestDetail,
  BacktestList,
  DashboardSummary,
  DataStatus,
  EquityResponse,
  EvaluationAnalysis,
  EvaluationEvidence,
  EvaluationList,
  EvaluationResult,
  ExecutionEventPage,
  FactorHistoryResponse,
  FactorRunList,
  FactorScoresResponse,
  IndustryReport,
  InstrumentList,
  Overview,
  PositionResponse,
  TradeResponse,
} from "../types";
import { API_BASE, query, request } from "./http";

export { ApiError, describeError, isAbortError } from "./http";

const id = encodeURIComponent;

export interface ScoreQuery {
  date?: string;
  instrument?: string;
  sort_by?: string;
  descending?: boolean;
  offset?: number;
  limit?: number;
}

export interface PageQuery {
  offset?: number;
  limit?: number;
}

export const api = {
  overview: (signal?: AbortSignal) =>
    request<Overview>("/overview", { signal }),
  dataStatus: (signal?: AbortSignal) =>
    request<DataStatus>("/data-status", { signal }),
  dashboardSummary: (signal?: AbortSignal) =>
    request<DashboardSummary>("/dashboard/summary", { signal }),

  instruments: (runId: string, q = "", signal?: AbortSignal) =>
    request<InstrumentList>(`/instruments${query({ q, run_id: runId })}`, {
      signal,
    }),
  factorRuns: (signal?: AbortSignal) =>
    request<FactorRunList>("/factor-runs", { signal }),
  factorScores: (
    runId: string,
    params: ScoreQuery = {},
    signal?: AbortSignal,
  ) =>
    request<FactorScoresResponse>(
      `/factor-runs/${id(runId)}/scores${query({ ...params })}`,
      { signal },
    ),
  factorHistory: (
    instrument: string,
    scheme: string,
    runId: string,
    signal?: AbortSignal,
  ) =>
    request<FactorHistoryResponse>(
      `/instruments/${id(instrument)}/factor-history${query({ scheme, run_id: runId })}`,
      { signal },
    ),
  industries: (
    kind: "factor-runs" | "backtests",
    runId: string,
    date?: string | null,
    signal?: AbortSignal,
  ) =>
    request<IndustryReport>(
      `/${kind}/${id(runId)}/industries${query({ date })}`,
      {
        signal,
      },
    ),
  factorTableUrl: (runId: string, name: "history" | "diagnostics") =>
    `${API_BASE}/factor-runs/${id(runId)}/tables/${name}`,

  backtests: (signal?: AbortSignal) =>
    request<BacktestList>("/backtests", { signal }),
  backtest: (runId: string, signal?: AbortSignal) =>
    request<BacktestDetail>(`/backtests/${id(runId)}`, { signal }),
  equity: (runId: string, signal?: AbortSignal) =>
    request<EquityResponse>(`/backtests/${id(runId)}/equity`, { signal }),
  positions: (
    runId: string,
    params: PageQuery & { date?: string },
    signal?: AbortSignal,
  ) =>
    request<PositionResponse>(
      `/backtests/${id(runId)}/positions${query({ ...params })}`,
      {
        signal,
      },
    ),
  trades: (runId: string, params: PageQuery, signal?: AbortSignal) =>
    request<TradeResponse>(
      `/backtests/${id(runId)}/trades${query({ ...params })}`,
      { signal },
    ),
  executionEvents: (runId: string, params: PageQuery, signal?: AbortSignal) =>
    request<ExecutionEventPage>(
      `/backtests/${id(runId)}/execution-events${query({ ...params })}`,
      { signal },
    ),
  backtestDownloadUrl: (runId: string) =>
    `${API_BASE}/backtests/${id(runId)}/download`,

  evaluations: (signal?: AbortSignal) =>
    request<EvaluationList>("/evaluations", { signal }),
  evaluation: (runId: string, signal?: AbortSignal) =>
    request<EvaluationResult>(`/evaluations/${id(runId)}`, { signal }),
  evaluationAnalysis: (
    runId: string,
    params: {
      horizon: number;
      factor?: string;
      window?: number;
      min_periods?: number;
      start?: string;
      end?: string;
      first?: string;
      second?: string;
    },
    signal?: AbortSignal,
  ) =>
    request<EvaluationAnalysis>(
      `/evaluations/${id(runId)}/analysis${query({ ...params })}`,
      { signal },
    ),
  evaluationEvidence: (
    runId: string,
    params: {
      horizon: number;
      factor?: string;
      start?: string;
      end?: string;
      cutoff?: string;
      lag?: number;
    },
    signal?: AbortSignal,
  ) =>
    request<EvaluationEvidence>(
      `/evaluations/${id(runId)}/evidence${query({ ...params })}`,
      { signal },
    ),
  evaluationDownloadUrl: (runId: string) =>
    `${API_BASE}/evaluations/${id(runId)}/download`,
  evaluationReportUrl: (runId: string) =>
    `${API_BASE}/evaluations/${id(runId)}/report`,
  evaluationLabelsUrl: (runId: string, horizon: string) =>
    `${API_BASE}/evaluations/${id(runId)}/labels/${id(horizon)}/download`,
};
