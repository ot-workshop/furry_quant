export interface Overview {
  mode: string;
  notice: string;
  factor_runs: number;
  backtests: number;
  instruments: number;
}

export interface Instrument {
  instrument: string;
  name: string;
  exchange?: string;
}

export interface InstrumentList {
  items: Instrument[];
  total: number;
}

export interface FactorRun {
  available_dates?: string[];
  id: string;
  name: string;
  as_of: string;
  universe: string;
  sample_size: number;
  scheme: string;
  data_version: string;
  is_synthetic: boolean;
  factor_definitions?: Record<string, FactorDescriptor>;
  factor_config?: Array<{name: string; weight: number; window: number | null; id?: string; skip?: number; baseline_window?: number}>;
}

export interface FactorDescriptor {
  id: string;
  name: string;
  label: string;
  family: string;
  direction: 'higher' | 'lower';
  required_fields: string[];
  formula: string;
  params: Record<string, number>;
  lookback: number;
  data_note?: string;
}

export interface FactorDiagnostic {
  date: string;
  instrument: string;
  raw: NumericRecord;
  reasons: Record<string, string>;
}

export interface FactorRunList {
  items: FactorRun[];
  total: number;
}

export type NumericRecord = Record<string, number | null>;

export interface FactorScore {
  industry_code?:string|null;industry_name?:string|null;industry_status?:string;classification?:string;
  processed?:NumericRecord;before_scores?:NumericRecord;before_contributions?:NumericRecord;before_composite?:number;before_rank?:number;
  instrument: string;
  name: string;
  industry: string | null;
  date: string;
  raw: NumericRecord | null;
  scores: NumericRecord | null;
  composite: number | null;
  rank: number | null;
  contributions?: NumericRecord;
  valuation_asof?: string;
  is_suspended?: boolean;
}

export interface ScoreMetadata {
  temporal_audit?:TemporalAuditData;
  weights?: Record<string, number>;
  score_unit?: string;
  direction?: string;
  factor_definitions?: Record<string, FactorDescriptor>;
  coverage?: {universe: number; eligible: number; factors: Record<string, {valid: number; missing: number}>};
  [key: string]: unknown;
}

export interface Paginated<T> {
  items: T[];
  total: number;
  offset: number;
  limit: number;
}

export interface FactorScoresResponse extends Paginated<FactorScore> {
  metadata?: ScoreMetadata;
  diagnostics?: FactorDiagnostic[];
}

export interface FactorHistoryItem {
  date: string;
  run_id?: string;
  [key: string]: string | number | null | undefined;
}

export interface FactorHistoryResponse {
  items: FactorHistoryItem[];
  scheme: string;
  is_synthetic?: boolean;
}

export type BacktestStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "interrupted"
  | string;

export interface BacktestMetrics {
  [key: string]: number | null;
}

export interface BacktestSummary {
  id: string;
  name: string;
  status: BacktestStatus;
  strategy: string;
  start_date: string;
  end_date: string;
  created_at: string;
  is_synthetic: boolean;
  metrics: BacktestMetrics | null;
}

export interface BacktestList {
  items: BacktestSummary[];
  total: number;
}

export interface BacktestDetail {
  temporal_audit?:TemporalAuditData;
  benchmark_name?: string;
  execution_run_id?: string;
  benchmark?: {code:string;name:string;return_type:string;source:string;start:string;end:string;observations:number;methodology:string};
  execution_costs?: {cost_mode:'legacy'|'itemized';commission_rate:number;tax_mode:'historical'|'fixed';stamp_tax_rate:number;transfer_fee_rate:number;impact_cost:number;min_cost:number};
  cost_summary?: CostBreakdown;
  rebalance_policy?: {frequency:'daily'|'weekly'|'monthly';scheduled_count:number;dates:string[];timing:string;exit_policy:string};
  execution_summary?:{events:number;blocked_exits:number;pending_exits:Array<{instrument:string;reason:string}>};
  id: string;
  name: string;
  status: BacktestStatus;
  strategy: string;
  start_date: string;
  end_date: string;
  created_at: string;
  is_synthetic: boolean;
  engine?: string | null;
  engine_version?: string | null;
  data_version?: string | null;
  config?: Record<string, unknown> | null;
  limitations?: string[] | null;
  metrics: BacktestMetrics | null;
  performance?: BacktestPerformance | null;
}

export interface TemporalAuditData {
  version:string;point_in_time_certified:false;valuation_fields_used:string[];note:string;
  items:Array<{key:string;label:string;status:string;detail:string}>;
  capacity_diagnostics?:{valid_stock_days:number;missing_lookback_stock_days:number;max_prior_turnover_ratio:number|null;above_ten_percent:number;note:string};
}

export interface AnnualReturn {
  year: number;
  start_date: string;
  end_date: string;
  trading_days: number;
  strategy_return: number | null;
  benchmark_return: number | null;
}

export interface BacktestPerformance {
  version: string;
  available: boolean;
  metrics: BacktestMetrics;
  benchmark_metrics: BacktestMetrics;
  drawdown: { peak_date: string | null; trough_date: string | null };
  benchmark_drawdown: { peak_date: string | null; trough_date: string | null };
  annual_returns: AnnualReturn[];
  methodology: { trading_days_per_year: number; annual_risk_free_rate: number; std_ddof: number };
}

export interface EquityPoint {
  date: string;
  equity: number | null;
  benchmark: number | null;
  drawdown: number | null;
  cost: number | null;
}

export interface EquityResponse {
  items: EquityPoint[];
}

export interface Position {
  date: string;
  instrument: string;
  name: string;
  amount: number | null;
  price: number | null;
  weight: number | null;
  cash: number | null;
}

export type PositionResponse = Paginated<Position>;

export interface Trade {
  cost_breakdown?:CostBreakdown;
  date: string;
  instrument: string;
  name?: string | null;
  side: string;
  amount: number | null;
  price: number | null;
  value: number | null;
  cost: number | null;
}

export type TradeResponse = Paginated<Trade>;
export interface CostBreakdown {commission:number;stamp_tax:number;transfer_fee:number;impact:number;legacy_fee:number;total:number;}

/* --------------------------------------------------------------- data status */

export type DataPartitionCounts = {
  succeeded: number;
  failed: number;
  [status: string]: number;
};

export type CoverageStatus = "complete" | "incomplete" | string;

export interface StockCoverage {
  code: string;
  expected: number;
  actual: number;
  missing_dates: string[];
  extra_dates: string[];
  duplicate_dates: string[];
  invalid_dates: string[];
  complete: boolean;
  missing_valuation_dates?: string[];
}

export interface Coverage {
  cross_validation?: CrossValidationReport;
  adjustment_anomalies?: Array<{instrument: string; date: string; previous_date: string; adjusted_return: number; reported_change: number | null}>;
  status: CoverageStatus;
  expected_sessions: number;
  stock_count: number;
  stocks: StockCoverage[];
  benchmark: StockCoverage;
  note: string;
}

export interface CrossValidationStock {
  code: string;
  status: string;
  primary_issues: string[];
  reason?: string;
  price_source: string;
  adjustment_source: string;
  raw_price_differences: string[];
  return_differences: Array<{ date: string; primary_return: number | null; secondary_return: number; absolute_difference: number | null }>;
  corporate_action_differences: Array<{ date: string; primary_return: number | null; secondary_return: number; absolute_difference: number | null }>;
}

export interface CrossValidationReport {
  policy: string;
  mode: string;
  status: string;
  price_absolute_tolerance_cny: number;
  return_absolute_tolerance: number;
  fallback_codes: string[];
  conflict_codes: string[];
  stocks: CrossValidationStock[];
  notes: string[];
}

export interface DataSnapshot {
  id: string;
  created_at: string;
  start: string;
  end: string;
  codes: string[];
  benchmark: string;
  trading_days: number;
  rows: number;
  coverage: Coverage;
}

export type DataJobStatus =
  | "running"
  | "succeeded"
  | "failed"
  | "partial"
  | "interrupted"
  | string;

export interface DataJobConfig {
  start: string;
  end: string;
  codes: string[];
  benchmark: string;
  refresh: boolean;
}

export interface DataJobFailure {
  api: string;
  params: Record<string, string>;
  error: string;
  missing_dates?: string[];
  extra_dates?: string[];
}

export interface DataJobFailureReport {
  status: string;
  reason: string;
  failures?: DataJobFailure[];
}

export type DataJobReport = Coverage | DataJobFailureReport;

export interface DataJob {
  id: string;
  status: DataJobStatus;
  started_at: string;
  finished_at: string | null;
  total: number;
  completed: number;
  cached: number;
  failed: number;
  snapshot_id: string | null;
  config: DataJobConfig;
  report: DataJobReport | null;
}

export interface DataStatus {
  active_snapshot_id?: string | null;
  partitions: DataPartitionCounts;
  snapshots: DataSnapshot[];
  jobs: DataJob[];
}

/* -------------------------------------------------------------- evaluations */

export interface EvaluationFactorConfig {
  name: string;
  weight: number;
  window: number | null;
  id?: string;
  skip?: number;
  baseline_window?: number;
}

export interface EvaluationConfig {
  horizons: number[];
  groups: number;
  min_cross_section: number;
}

export interface EvaluationMetadata {
  temporal_audit?:TemporalAuditData;
  id: string;
  name: string;
  is_synthetic: boolean;
  created_at: string;
  data_version: string;
  factor_run_id: string;
  factor_config: EvaluationFactorConfig[];
  evaluation: EvaluationConfig;
  start: string;
  end: string;
  sample_size: number;
  provenance: Record<string, unknown>;
  factor_definitions?: Record<string, FactorDescriptor>;
}

export interface EvaluationList {
  items: EvaluationMetadata[];
  total: number;
}

export interface ICStats {
  mean: number | null;
  std: number | null;
  ir: number | null;
  positive_fraction: number | null;
  valid_dates: number;
}

export interface FactorGroupReturn {
  group: number;
  mean_return: number | null;
  dates: number;
  observations: number;
}

export interface FactorDailyDiagnostic {
  date: string;
  entry_date: string;
  exit_date: string | null;
  observations: number;
  ic: number | null;
  rank_ic: number | null;
  ic_exclusion: string | null;
  group_returns: Record<string, number | null>;
  group_counts: Record<string, number>;
  high_minus_low: number | null;
}

export interface FactorDiagnostic {
  summary: {
    ic: ICStats;
    rank_ic: ICStats;
    mean_high_minus_low: number | null;
    spread_dates: number;
    signal_dates: number;
    label_observations: number;
  };
  groups: FactorGroupReturn[];
  daily: FactorDailyDiagnostic[];
}

export interface SignalCoverageRow {
  signal_date: string;
  entry_date: string;
  scored_instruments: number;
  universe: number;
}

export interface LabelCoverageRow {
  date: string;
  scored: number;
  valid_labels: number;
  exclusions: Record<string, number>;
}

export interface LabelObservation {
  date: string;
  instrument: string;
  entry_date: string;
  exit_date: string;
  forward_return: number;
}

export interface LabelDiagnostic {
  coverage: LabelCoverageRow[];
  observations: LabelObservation[];
  valid_observations: number;
  excluded_observations: number;
}

export interface CorrelationDailyPoint {
  date: string;
  rank_correlation: number | null;
}

export interface CorrelationPair {
  first: string;
  second: string;
  mean_rank_correlation: number | null;
  valid_dates: number;
  daily: CorrelationDailyPoint[];
}

export interface EvaluationCorrelations {
  method: string;
  pairs: CorrelationPair[];
}

export interface EvaluationMethodology {
  ic_engine: string;
  label: string;
  signal_range: string;
  groups: string;
  statistics: string;
  directions: Record<string, string>;
  limitations: string[];
}

export interface EvaluationResult {
  tables?:Record<string,{file:string;rows:number;sha256:string}>;
  metadata: EvaluationMetadata;
  methodology: EvaluationMethodology;
  signal_coverage: SignalCoverageRow[];
  labels: Record<string, LabelDiagnostic>;
  factors: Record<string, { horizons: Record<string, FactorDiagnostic> }>;
  correlations: EvaluationCorrelations;
}
