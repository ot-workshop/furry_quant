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
};
