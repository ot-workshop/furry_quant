const API_BASE = "/api/v1";

export type ValidationMode = "off" | "verify" | "fallback";
export type FactorName = string;
export type SourceMode = "snapshot" | "download" | "index_warehouse";

export type TaskStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "interrupted";

export type TaskStage =
  | "queued"
  | "download"
  | "prepare"
  | "factors"
  | "backtest"
  | "evaluation"
  | "finished";

export interface SnapshotCode {
  code: string;
  name: string;
}

export interface TradingCalendar { start:string; end:string; trading_dates:string[] }
export type DatePlanInput = {backtest_start:string;backtest_end:string;factors:ResearchFactor[]} &
  ({auto_range:true} | {data_start:string;data_end:string});
export interface DatePlan {
  required_history:number; requested_backtest_start:string; first_trading_day:string;
  proposals:Array<{mode:'auto_range'|'extend_history'|'delay_backtest';data_start:string;data_end:string;backtest_start:string;backtest_end:string;history_days:number;warmup_start?:string;warmup_end?:string}>;
  calendars:TradingCalendar[];
}

export interface Snapshot {
  id: string;
  label: string;
  start: string;
  end: string;
  trading_days: number;
  trading_dates?: string[];
  is_current_download?: boolean;
  benchmark: string;
  codes: SnapshotCode[];
  validation_mode: ValidationMode;
  source: string;
}

export interface FactorDefinition {
  name: FactorName;
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
}

export interface BenchmarkOption {
  code: string;
  name: string;
  return_type?: 'price'|'gross_total_return';
  coverage?: {start:string;end:string;rows:number;source:string};
}

export interface ResearchFactor {
  name: FactorName;
  weight: number;
  window?: number | null;
  skip?: number;
  baseline_window?: number;
  id?: string;
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
  rebalance_frequency?: 'daily'|'weekly'|'monthly';
  cost_mode?: 'legacy'|'itemized';
  commission_rate?: number;
  tax_mode?: 'historical'|'fixed';
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
  snapshot_id: string;
  codes: string[];
}

export interface DownloadData {
  mode: "download";
  codes: string[];
  start: string;
  end: string;
  benchmark: string;
  validation_mode: ValidationMode;
}

export interface HistoricalData {mode:'index_warehouse';warehouse_id:string}
export interface HistoricalIndexOption {id:string;label:string;start:string;end:string;data_start:string;
  shared_version?:string;
  trading_dates:string[];codes_count:number;membership_policy:string;point_in_time_certified:boolean;note:string}

export interface ResearchRequest {
  neutralization?:{mode:"off"|"industry";industry_version?:string};
  name: string;
  data: SnapshotData | DownloadData | HistoricalData;
  factors: ResearchFactor[];
  backtest: ResearchBacktest;
  evaluation: ResearchEvaluation;
}

export interface ResearchResult {
  factor_as_of?:string;
  data_version: string;
  factor_run_id: string;
  backtest_run_id: string;
  evaluation_run_id: string;
}

export interface ResearchTask {
  id: string;
  name: string;
  status: TaskStatus;
  stage: TaskStage;
  message: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  progress: { completed: number; total: number };
  request: ResearchRequest;
  result: ResearchResult | null;
  error: string | null;
  error_details?: {job_id: string; issues: Array<{code: string; api: string; category: string; dates: string[]; description?: string}>;
    kind?:'runtime';stage?:string;exception_type?:string;trace?:Array<{file:string;function:string;line:number}>} | null;
}

export interface ResearchOptions {
  industry_data?:{id:string;source:string;rows:number;instruments:number;policy:string;point_in_time_certified:boolean;data_asof?:string|null}|null;
  historical_index?:HistoricalIndexOption|null;
  historical_indices?:HistoricalIndexOption[];
  calendars?:TradingCalendar[];
  snapshots: Snapshot[];
  factor_definitions: FactorDefinition[];
  benchmarks: BenchmarkOption[];
  comparison_benchmarks?: BenchmarkOption[];
  defaults: ResearchRequest;
  limits: { max_codes: number; max_queued_tasks: number };
}

export interface ResearchTaskList {
  items: ResearchTask[];
  total: number;
}

export interface IndexDownloadStatus {
  validation_counts?:{single_source_passed:number;dual_source_verified:number;secondary_fallback_passed:number};
  status:string;worker_running:boolean;pause_requested:boolean;total?:number;verified:number;failed:number;
  raw_captures?:number;current_code?:string;message?:string;updated_at?:string;
  plan?:{start:string;end:string;data_start:string;warmup_sessions:number;snapshots:number;membership_policy:string};
  budget:{day:string;requests:Array<{source:string;api:string;count:number}>;holds:Array<{source:string;reason:string}>};
  failures:Array<{code:string;type:string;reason:string;dates?:string[]}>;
  limits:{baostock_daily_requests:number;baostock_per_minute:number;tushare_per_minute:number;tushare_daily_per_api:number};
}

export interface IndexHandlingAction {
  rule:string;title:string;reason:string;handling:string;dates:string[];
  kind:'handled'|'warning'|'info'|'unresolved';
  evidence:Array<{source:string;api?:string;file?:string;sha256?:string|Record<string,string>;url?:string;known_by?:string;params?:Record<string,string>}>;
}
export interface IndexHandlingReport {
  schema_version:string;source_id:string;status:string;source_updated_at:string|null;generated_at:string;scope:string;job_handling?:string;
  summary:{total:number;passed:number;needs_review:number;pending:number;auto_handled:number;action_counts:Record<string,number>;validation_counts:Record<string,number>};
  rules:string[];
  stocks:Array<{code:string;name:string;validation_level:string|null;result:string;outcome:'auto_handled'|'passed'|'needs_review';actions:IndexHandlingAction[];receipt_file?:string}>;
}

export class ResearchApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ResearchApiError";
    this.status = status;
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

interface RequestOptions {
  method?: string;
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
}

async function request<T>(
  path: string,
  { method = "GET", body, headers, signal }: RequestOptions = {},
): Promise<T> {
  const requestHeaders: Record<string, string> = {
    Accept: "application/json",
    ...headers,
  };
  if (body !== undefined) {
    requestHeaders["Content-Type"] = "application/json";
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      signal,
      headers: requestHeaders,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (error) {
    if (isAbortError(error)) throw error;
    throw new ResearchApiError(
      "无法连接后端服务，请确认本地 API 已启动。",
      0,
    );
  }

  if (!response.ok) {
    let detail = `请求失败（HTTP ${response.status}）`;
    try {
      const payload = (await response.json()) as { detail?: unknown };
      if (typeof payload.detail === "string" && payload.detail.trim()) {
        detail = payload.detail;
      }
    } catch {
      // 非 JSON 或错误详情不是安全字符串时，保留通用提示。
    }
    throw new ResearchApiError(detail, response.status);
  }

  try {
    return (await response.json()) as T;
  } catch {
    throw new ResearchApiError("后端返回了无法解析的数据。", response.status);
  }
}

function encoded(id: string): string {
  return encodeURIComponent(id);
}

export const researchApi = {
  indexHandlingReport(signal?:AbortSignal):Promise<IndexHandlingReport> {
    return request<IndexHandlingReport>('/research/index-download/report',{signal});
  },
  indexDownloadStatus(signal?:AbortSignal):Promise<IndexDownloadStatus> {
    return request<IndexDownloadStatus>('/research/index-download',{signal});
  },
  indexDownloadAction(action:'start'|'pause'):Promise<IndexDownloadStatus> {
    return request<IndexDownloadStatus>(`/research/index-download/${action}`,{method:'POST',body:{}});
  },
  planDates(body:DatePlanInput):Promise<DatePlan> {
    return request<DatePlan>('/research/date-plan',{method:'POST',body});
  },
  getOptions(signal?: AbortSignal): Promise<ResearchOptions> {
    return request<ResearchOptions>("/research/options", { signal });
  },

  listTasks(signal?: AbortSignal): Promise<ResearchTaskList> {
    return request<ResearchTaskList>("/research/tasks", { signal });
  },

  getTask(id: string, signal?: AbortSignal): Promise<ResearchTask> {
    return request<ResearchTask>(`/research/tasks/${encoded(id)}`, { signal });
  },

  createTask(
    body: ResearchRequest,
    idempotencyKey: string,
  ): Promise<ResearchTask> {
    return request<ResearchTask>("/research/tasks", {
      method: "POST",
      body,
      headers: { "Idempotency-Key": idempotencyKey },
    });
  },

  cancelTask(id: string): Promise<ResearchTask> {
    return request<ResearchTask>(`/research/tasks/${encoded(id)}/cancel`, {
      method: "POST",
      body: {},
    });
  },
};

export function describeResearchError(error: unknown): string {
  if (error instanceof ResearchApiError) {
    return error.status > 0
      ? `${error.message}（HTTP ${error.status}）`
      : error.message;
  }
  if (error instanceof Error) return error.message;
  return "发生未知错误。";
}
