import { request } from "./http";

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

/* ------------------------------------------------------- financial data */

export interface FinancialSpec {
  market_version: string;
  refresh_days: number;
}

export interface FinancialPlan {
  id: string;
  spec: FinancialSpec;
  codes: Array<{ code: string; rows: number }>;
  skipped: number;
  estimated_requests: number;
  requests_per_stock: number;
  note: string;
  point_in_time_certified: boolean;
}

export interface FinancialJob {
  id: string;
  status: string;
  total: number;
  completed: number;
  skipped: number;
  downloaded_rows: number;
  current_code?: string;
  message?: string;
  version_id?: string;
  updated_at?: string;
  failures: Array<{ code: string; type?: string; reason: string }>;
}

export interface FinancialVersion {
  id: string;
  created_at?: string;
  codes: number;
  rows: number;
  quarantined: number;
  latest_report_period: string | null;
  current?: boolean;
}

export interface FinancialStatus {
  worker_running: boolean;
  version: FinancialVersion | null;
  versions: FinancialVersion[];
  jobs: FinancialJob[];
  market_versions: Array<{
    id: string;
    scope: MarketScope;
    start: string;
    end: string;
    codes_count: number;
    restricted: boolean;
  }>;
  budget?: NetworkBudget;
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
  financial: (signal?: AbortSignal) =>
    request<FinancialStatus>("/research/financial-download", { signal }),
  previewFinancial: (spec: FinancialSpec) =>
    request<FinancialPlan>("/research/financial-download/preview", {
      method: "POST",
      body: spec,
    }),
  startFinancial: (planId: string) =>
    request<{ id: string; task_id: string; status: string }>(
      "/research/financial-download/start",
      {
        method: "POST",
        body: { plan_id: planId },
      },
    ),
  pauseFinancial: () =>
    request<{ status: string }>("/research/financial-download/pause", {
      method: "POST",
      body: {},
    }),
};
