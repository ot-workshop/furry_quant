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
  estimated_name_requests?: number;
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
  failures: MarketFailure[];
  repairs?: MarketRepair[];
}

export interface MarketFailure {
  code: string;
  type?: string;
  reason: string;
  category?: string;
  dates?: string[];
  date_count?: number;
  action?: string;
  evidence?: {
    quarantine_file?: string;
    capture?: string;
    issues?: Array<{ rule: string; message: string; dates: string[] }>;
  };
}

export interface MarketRepair {
  code: string;
  action: string;
  reason: string;
  dates?: string[];
  start?: string;
  end?: string;
  detail?: string;
  source?: string;
  known_by?: string;
  evidence_basis?: string;
  validation_level?: string;
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
  full_refresh?: boolean;
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

/* ------------------------------------------------------------ industry data */

export type IndustryLevel = "L1" | "L2" | "L3";

export interface IndustryLevelSummary {
  label: string;
  rows: number;
  instruments: number;
  sectors: number;
}

export interface IndustrySpec {
  levels: IndustryLevel[];
  refresh: boolean;
}

export interface IndustryPlan {
  id: string;
  spec: IndustrySpec;
  sectors: Record<string, number>;
  estimated_requests: number;
  estimated_minutes: number;
  current_version: string | null;
  current_levels: string[];
  note: string;
  point_in_time_certified: boolean;
}

export interface IndustryJob {
  id: string;
  status: string;
  total: number;
  completed: number;
  levels: IndustryLevel[];
  failures: Array<{
    taxonomy: string;
    level: string;
    code: string;
    name?: string;
    reason: string;
  }>;
  empty_sectors?: EmptyIndustrySector[];
  excluded_members?: ExcludedIndustryMember[];
  message?: string;
  version_id?: string;
  updated_at?: string;
}

export interface EmptyIndustrySector {
  taxonomy: string;
  level: IndustryLevel;
  code: string;
  name: string;
  reason: string;
}

export interface ExcludedIndustryMember {
  taxonomy: string;
  level: IndustryLevel;
  sector_code: string;
  code: string;
  in_date: string | null;
  out_date: string | null;
  reason: string;
}

export interface IndustryVersion {
  id: string;
  schema_version: string;
  rows: number;
  instruments: number;
  levels: Partial<Record<IndustryLevel, IndustryLevelSummary>>;
  data_asof?: string | null;
  created_at?: string;
  current?: boolean;
  empty_sectors?: EmptyIndustrySector[];
  excluded_members?: ExcludedIndustryMember[];
}

export interface IndustryStatus {
  worker_running: boolean;
  version: {
    id: string;
    rows: number;
    instruments: number;
    levels: Partial<Record<IndustryLevel, IndustryLevelSummary>>;
    data_asof?: string | null;
    created_at?: string;
    policy?: string;
    empty_sectors?: EmptyIndustrySector[];
    excluded_members?: ExcludedIndustryMember[];
  } | null;
  versions: IndustryVersion[];
  jobs: IndustryJob[];
  budget?: NetworkBudget;
}

export const dataApi = {
  backups: (signal?: AbortSignal) =>
    request<BackupStatus>("/research/backups", { signal }),
  backupOperation: (operation: BackupOperation) =>
    request<MaintenanceTask>("/research/backups", {
      method: "POST",
      body: operation,
    }),
  updates: (signal?: AbortSignal) =>
    request<UpdateStatus>("/research/data-updates", { signal }),
  previewUpdates: (spec: UpdateSpec) =>
    request<UpdatePlan>("/research/data-updates/preview", {
      method: "POST",
      body: spec,
    }),
  startUpdates: (planId: string) =>
    request("/research/data-updates/start", {
      method: "POST",
      body: { plan_id: planId },
    }),
  pauseUpdates: () =>
    request("/research/data-updates/pause", { method: "POST", body: {} }),
  scheduleUpdates: (schedule: UpdateSchedule) =>
    request<UpdateSchedule>("/research/data-updates/schedule", {
      method: "POST",
      body: schedule,
    }),
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
  industry: (signal?: AbortSignal) =>
    request<IndustryStatus>("/research/industry-download", { signal }),
  previewIndustry: (spec: IndustrySpec) =>
    request<IndustryPlan>("/research/industry-download/preview", {
      method: "POST",
      body: spec,
    }),
  startIndustry: (planId: string) =>
    request<{ id: string; task_id: string; status: string }>(
      "/research/industry-download/start",
      {
        method: "POST",
        body: { plan_id: planId },
      },
    ),
  pauseIndustry: () =>
    request<{ status: string }>("/research/industry-download/pause", {
      method: "POST",
      body: {},
    }),
};

export interface UpdateSpec {
  market: MarketSpec | null;
  market_version: string | null;
  financial: { refresh_days: number; full_refresh?: boolean } | null;
  industry: IndustrySpec | null;
  benchmarks: string[];
}
export interface UpdatePlan {
  id: string;
  spec: UpdateSpec;
  stages: string[];
  note: string;
  plans: {
    market?: MarketPlan;
    financial?: FinancialPlan;
    industry?: IndustryPlan;
  };
}
export interface UpdateJob {
  id: string;
  status: string;
  total: number;
  completed: number;
  updated_at: string;
  message?: string;
  steps: Record<
    string,
    {
      status: string;
      message?: string;
      result?: {
        version_id?: string;
        failures?: MarketFailure[];
        repairs?: MarketRepair[];
      };
    }
  >;
}
export interface UpdateSchedule {
  enabled: boolean;
  hour: number;
  spec: UpdateSpec;
}
export interface UpdateStatus {
  jobs: UpdateJob[];
  schedule: UpdateSchedule | null;
  market_versions: MarketVersion[];
}

export type BackupOperation =
  | { action: "create" }
  | { action: "verify"; backup_id: string }
  | { action: "restore"; backup_id: string; database: string };
export interface BackupItem {
  id: string;
  created_at: string;
  files: number;
  bytes: number;
  format: string;
}
export interface MaintenanceTask {
  id: string;
  status: string;
  created_at: string;
  message: string;
  error: string | null;
  request: {
    name: string;
    action: string;
    backup_id: string;
    database?: string;
  };
  result: { message?: string; database?: string; storage_root?: string } | null;
}
export interface BackupStatus {
  backups: BackupItem[];
  jobs: MaintenanceTask[];
  backup_root: string;
  restore_root: string;
  coverage: string[];
}
