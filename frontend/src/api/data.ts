import { API_BASE, request } from "./http";

export type MarketScope = "csi300" | "all_market" | "custom";

export interface MarketSpec {
  scope: MarketScope;
  start: string;
  end: string;
  warmup: number;
  codes: string[];
  refresh_sessions?: number;
  deep_refresh?: boolean;
}

export interface MarketPlan {
  id: string;
  spec: MarketSpec;
  start: string;
  end: string;
  data_start: string;
  codes_count: number;
  reused_rows: number;
  missing_rows: number;
  revision_rows?: number;
  estimated_quote_requests: number;
  excluded_unsupported?: number;
  restricted: boolean;
  note: string;
  tasks: Array<{
    code: string;
    name?: string;
    expected_rows: number;
    reused_rows: number;
    gaps: string[][];
  }>;
}

export interface MarketJob {
  id: string;
  spec: MarketSpec;
  status: string;
  completed: number;
  total: number;
  reused_rows: number;
  downloaded_rows: number;
  current_code?: string;
  message?: string;
  version_id?: string;
  updated_at?: string;
  failures: Array<{ code: string; type?: string; reason: string }>;
}

export interface MarketVersion {
  id: string;
  scope: MarketScope;
  start: string;
  end: string;
  data_start: string;
  codes_count: number;
  restricted: boolean;
  snapshot_id: string | null;
  created_at: string;
  validation_counts?: Record<string, number>;
}

export interface NetworkBudget {
  day: string;
  requests: Array<{ source: string; api: string; count: number }>;
  holds: Array<{ source: string; reason: string }>;
}

export interface MarketStatus {
  worker_running: boolean;
  registered_codes: number;
  jobs: MarketJob[];
  versions: MarketVersion[];
  budget?: NetworkBudget;
}

export interface IndexDownloadStatus {
  validation_counts?: {
    single_source_passed: number;
    dual_source_verified: number;
    secondary_fallback_passed: number;
  };
  status: string;
  worker_running: boolean;
  pause_requested: boolean;
  total?: number;
  verified: number;
  failed: number;
  raw_captures?: number;
  current_code?: string;
  message?: string;
  updated_at?: string;
  plan?: {
    start: string;
    end: string;
    data_start: string;
    warmup_sessions: number;
    snapshots: number;
    membership_policy: string;
  };
  budget: NetworkBudget;
  failures: Array<{
    code: string;
    type: string;
    reason: string;
    dates?: string[];
  }>;
  limits: {
    baostock_daily_requests: number;
    baostock_per_minute: number;
    tushare_per_minute: number;
    tushare_daily_per_api: number;
  };
}

export interface IndexHandlingAction {
  rule: string;
  title: string;
  reason: string;
  handling: string;
  dates: string[];
  kind: "handled" | "warning" | "info" | "unresolved";
  evidence: Array<{
    source: string;
    api?: string;
    file?: string;
    sha256?: string | Record<string, string>;
    url?: string;
    known_by?: string;
  }>;
}

export interface IndexHandlingReport {
  schema_version: string;
  source_id: string;
  status: string;
  source_updated_at: string | null;
  generated_at: string;
  scope: string;
  job_handling?: string;
  summary: {
    total: number;
    passed: number;
    needs_review: number;
    pending: number;
    auto_handled: number;
    action_counts: Record<string, number>;
    validation_counts: Record<string, number>;
  };
  rules: string[];
  stocks: Array<{
    code: string;
    name: string;
    validation_level: string | null;
    result: string;
    outcome: "auto_handled" | "passed" | "needs_review";
    actions: IndexHandlingAction[];
  }>;
}

export const dataApi = {
  market: (signal?: AbortSignal) =>
    request<MarketStatus>("/research/market-download", { signal }),
  previewMarket: (spec: MarketSpec) =>
    request<MarketPlan>("/research/market-download/preview", {
      method: "POST",
      body: spec,
    }),
  startMarket: (planId: string) =>
    request<{ id: string; task_id: string; status: string }>(
      "/research/market-download/start",
      {
        method: "POST",
        body: { plan_id: planId },
      },
    ),
  pauseMarket: () =>
    request<{ status: string }>("/research/market-download/pause", {
      method: "POST",
      body: {},
    }),

  index: (signal?: AbortSignal) =>
    request<IndexDownloadStatus>("/research/index-download", { signal }),
  startIndex: () =>
    request<IndexDownloadStatus>("/research/index-download/start", {
      method: "POST",
      body: {},
    }),
  pauseIndex: () =>
    request<IndexDownloadStatus>("/research/index-download/pause", {
      method: "POST",
      body: {},
    }),
  indexReport: (signal?: AbortSignal) =>
    request<IndexHandlingReport>("/research/index-download/report", { signal }),
  indexReportUrl: `${API_BASE}/research/index-download/report/download`,
};
