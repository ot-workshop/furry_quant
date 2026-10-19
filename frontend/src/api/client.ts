import type {
  BacktestDetail,
  BacktestList,
  DataStatus,
  EquityResponse,
  EvaluationList,
  EvaluationResult,
  FactorHistoryResponse,
  FactorRunList,
  FactorScoresResponse,
  InstrumentList,
  Overview,
  PositionResponse,
  TradeResponse,
} from "../types";

const API_BASE = "/api/v1";

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

type QueryValue = string | number | boolean | null | undefined;

function queryString(params: Record<string, QueryValue>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
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
    throw new ApiError("无法连接后端服务，请确认本地 API 已启动", 0);
  }

  if (!response.ok) {
    let detail = `请求失败（HTTP ${response.status}）`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string" && body.detail.trim()) {
        detail = body.detail;
      }
    } catch {
      // 非 JSON 错误响应，保留通用提示。
    }
    throw new ApiError(detail, response.status);
  }

  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError("后端返回了无法解析的数据", response.status);
  }
}

export function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    (error as { name?: unknown }).name === "AbortError"
  );
}

export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    return error.status > 0 ? `${error.message}（HTTP ${error.status}）` : error.message;
  }
  if (error instanceof Error) return error.message;
  return "发生未知错误";
}

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

export interface PositionQuery extends PageQuery {
  date?: string;
}

export const api = {
  getOverview(signal?: AbortSignal): Promise<Overview> {
    return request<Overview>("/overview", signal);
  },

  getInstruments(q?: string, signal?: AbortSignal): Promise<InstrumentList> {
    return request<InstrumentList>(`/instruments${queryString({ q })}`, signal);
  },

  getFactorRuns(signal?: AbortSignal): Promise<FactorRunList> {
    return request<FactorRunList>("/factor-runs", signal);
  },

  getDataStatus(signal?: AbortSignal): Promise<DataStatus> {
    return request<DataStatus>("/data-status", signal);
  },

  getFactorScores(
    runId: string,
    params: ScoreQuery = {},
    signal?: AbortSignal,
  ): Promise<FactorScoresResponse> {
    return request<FactorScoresResponse>(
      `/factor-runs/${encodeURIComponent(runId)}/scores${queryString({
        instrument: params.instrument,
        date: params.date,
        sort_by: params.sort_by,
        descending: params.descending,
        offset: params.offset,
        limit: params.limit,
      })}`,
      signal,
    );
  },

  getFactorHistory(
    instrument: string,
    scheme: string,
    signal?: AbortSignal,
    runId?: string,
  ): Promise<FactorHistoryResponse> {
    return request<FactorHistoryResponse>(
      `/instruments/${encodeURIComponent(instrument)}/factor-history${queryString({
        scheme,
        run_id: runId,
      })}`,
      signal,
    );
  },

  getEvaluations(signal?: AbortSignal): Promise<EvaluationList> {
    return request<EvaluationList>("/evaluations", signal);
  },

  getEvaluation(id: string, signal?: AbortSignal): Promise<EvaluationResult> {
    return request<EvaluationResult>(
      `/evaluations/${encodeURIComponent(id)}`,
      signal,
    );
  },

  evaluationDownloadUrl(id: string): string {
    return `${API_BASE}/evaluations/${encodeURIComponent(id)}/download`;
  },

  evaluationReportUrl(id: string): string {
    return `${API_BASE}/evaluations/${encodeURIComponent(id)}/report`;
  },

  getBacktests(signal?: AbortSignal): Promise<BacktestList> {
    return request<BacktestList>("/backtests", signal);
  },

  getBacktest(id: string, signal?: AbortSignal): Promise<BacktestDetail> {
    return request<BacktestDetail>(`/backtests/${encodeURIComponent(id)}`, signal);
  },

  getBacktestEquity(id: string, signal?: AbortSignal): Promise<EquityResponse> {
    return request<EquityResponse>(
      `/backtests/${encodeURIComponent(id)}/equity`,
      signal,
    );
  },

  getBacktestPositions(
    id: string,
    params: PositionQuery = {},
    signal?: AbortSignal,
  ): Promise<PositionResponse> {
    return request<PositionResponse>(
      `/backtests/${encodeURIComponent(id)}/positions${queryString({
        date: params.date,
        offset: params.offset,
        limit: params.limit,
      })}`,
      signal,
    );
  },

  getBacktestTrades(
    id: string,
    params: PageQuery = {},
    signal?: AbortSignal,
  ): Promise<TradeResponse> {
    return request<TradeResponse>(
      `/backtests/${encodeURIComponent(id)}/trades${queryString({
        offset: params.offset,
        limit: params.limit,
      })}`,
      signal,
    );
  },

  downloadUrl(id: string): string {
    return `${API_BASE}/backtests/${encodeURIComponent(id)}/download`;
  },
};
