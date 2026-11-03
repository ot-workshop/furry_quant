import type {
  DownloadData,
  HistoricalData,
  FactorDefinition,
  FactorName,
  ResearchOptions,
  ResearchRequest,
  SnapshotData,
  Snapshot,
  SourceMode,
  TaskStage,
  TaskStatus,
  ValidationMode,
} from "../api/research";

const FACTOR_NAMES: FactorName[] = ["momentum", "value", "volatility"];
export function historicalOption(options:ResearchOptions,id?:string){
  return options.historical_indices?.find(item=>item.id===id) ?? options.historical_index ?? options.historical_indices?.[0];
}
const FACTOR_LABELS: Record<FactorName, string> = {
  momentum: "动量",
  value: "盈利收益率",
  volatility: "低波动",
};
const CODE_PATTERN = /^\d{6}\.(SH|SZ|BJ)$/;
const DRAFT_KEY = "finance-quant.research-draft.v1";

const STAGE_ORDER: TaskStage[] = [
  "queued",
  "download",
  "prepare",
  "factors",
  "backtest",
  "evaluation",
  "finished",
];
const STAGE_LABELS: Record<TaskStage, string> = {
  queued: "排队",
  download: "下载数据",
  prepare: "准备数据",
  factors: "计算因子",
  backtest: "回测",
  evaluation: "因子评估",
  finished: "完成",
};
const STATUS_LABELS: Record<TaskStatus, string> = {
  queued: "排队中",
  running: "运行中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};
const STATUS_CLASSES: Record<TaskStatus, string> = {
  queued: "status-queued",
  running: "status-running",
  succeeded: "status-succeeded",
  failed: "status-failed",
  cancelled: "status-unknown",
  interrupted: "status-interrupted",
};
const VALIDATION_LABELS: Record<ValidationMode, string> = {
  off: "关闭校验",
  verify: "严格校验",
  fallback: "异常时整段回退",
};
const VALIDATION_OPTIONS: Array<{ value: ValidationMode; label: string }> = [
  { value: "off", label: "关闭校验" },
  { value: "verify", label: "严格校验（缺失即失败）" },
  { value: "fallback", label: "异常时整段回退（默认）" },
];

interface FactorFormState {
  enabled: boolean;
  weight: string;
  window: string;
  name?: string;
  skip?: string;
  baselineWindow?: string;
  definition?: FactorDefinition;
}

interface FormState {
  name: string;
  sourceMode: SourceMode;
  warehouseId:string;
  autoDates: boolean;
  snapshotId: string;
  selectedCodes: string[];
  downloadCodes: string;
  dataStart: string;
  dataEnd: string;
  benchmark: string;
  comparisonBenchmark: string;
  neutralizationMode:"off"|"industry";
  industryVersion:string;
  validationMode: ValidationMode;
  factors: Record<FactorName, FactorFormState>;
  btStart: string;
  btEnd: string;
  initialCash: string;
  topk: string;
  nDrop: string;
  riskDegree: string;
  openCost: string;
  closeCost: string;
  minCost: string;
  rebalanceFrequency:'daily'|'weekly'|'monthly';
  costMode:'legacy'|'itemized';
  commissionRate:string;
  taxMode:'historical'|'fixed';
  stampTaxRate:string;
  transferFeeRate:string;
  impactCost:string;
  horizons: string;
  groups: string;
  minCrossSection: string;
  advancedOpen: boolean;
}

interface BuildResult {
  request: ResearchRequest | null;
  errors: string[];
  codeCount: number;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function unique(values: string[]): string[] {
  return Array.from(new Set(values));
}

function numberText(value: unknown, digits = 6): string {
  return typeof value === "number" && Number.isFinite(value)
    ? String(Number(value.toFixed(digits)))
    : "";
}

function percentText(value: unknown, digits = 6): string {
  return typeof value === "number" && Number.isFinite(value)
    ? String(Number((value * 100).toFixed(digits)))
    : "";
}

function dateText(value: unknown): string {
  return typeof value === "string" ? value.slice(0, 10) : "";
}

function toNumber(text: string): number {
  const trimmed = text.trim();
  return trimmed ? Number(trimmed) : Number.NaN;
}

function validDate(value: string): boolean {
  return /^\d{4}-\d{2}-\d{2}$/.test(value);
}

function parseCodes(text: string): string[] {
  return text
    .split(/[\s,，、;；]+/)
    .map((item) => item.trim().toUpperCase().replace(/^(SH|SZ|BJ)(\d{6})$/, "$2.$1"))
    .filter(Boolean);
}

function findDuplicates(values: string[]): string[] {
  const seen = new Set<string>();
  const duplicates = new Set<string>();
  for (const value of values) {
    if (seen.has(value)) duplicates.add(value);
    seen.add(value);
  }
  return Array.from(duplicates);
}

function parseHorizons(text: string): { values: number[]; error: string | null } {
  const tokens = text
    .split(/[\s,，、;；]+/)
    .map((item) => item.trim())
    .filter(Boolean);
  if (!tokens.length) {
    return { values: [], error: "请至少填写一个诊断持有期。" };
  }
  const values: number[] = [];
  for (const token of tokens) {
    const value = Number(token);
    if (!Number.isInteger(value) || value < 1 || value > 252) {
      return { values: [], error: `诊断持有期“${token}”需为 1–252 的整数。` };
    }
    values.push(value);
  }
  return {
    values: Array.from(new Set(values)).sort((a, b) => a - b),
    error: null,
  };
}

function clampTopk(
  topk: string,
  count: number,
): { topk: string; clamped: boolean } {
  if (count <= 0) return { topk, clamped: false };
  const parsed = toNumber(topk);
  if (!Number.isFinite(parsed) || parsed <= count) {
    return { topk, clamped: false };
  }
  return { topk: String(Math.max(1, Math.floor(count))), clamped: true };
}

function earliestBacktestDate(snapshot: Snapshot, factors: Record<FactorName, FactorFormState>): string | null {
  const lookback = Math.max(0, ...Object.entries(factors).filter(([, row]) => row.enabled)
    .map(([key, row]) => {
      const name = row.name ?? key;
      if (row.definition?.default_window === null || ['value','book_to_price','size'].includes(name)) return 0;
      const window = toNumber(row.window);
      return name === 'amount_anomaly' ? window + toNumber(row.baselineWindow ?? String(row.definition?.default_baseline_window ?? 60)) - 1 : window;
    }).filter(Number.isFinite));
  // A lookback score first exists at index lookback; execution is next session.
  return snapshot.trading_dates?.[lookback + 1] ?? null;
}

function warmupInfo(form: FormState, options: ResearchOptions) {
  const enabled = Object.entries(form.factors).filter(([, row]) => row.enabled);
  if (!enabled.length || !validDate(form.btStart)) return null;
  const spans = enabled.map(([key,row]) => {
    const name = row.name ?? key;
    if (row.definition?.default_window === null || ['value','book_to_price','size'].includes(name)) return 0;
    const window = toNumber(row.window);
    const baseline = name === 'amount_anomaly' ? toNumber(row.baselineWindow ?? '60') : 1;
    return Number.isInteger(window) && window >= 2 && window <= 252 && Number.isInteger(baseline) && baseline >= 1 && baseline <= 252 ? window + baseline - 1 : NaN;
  });
  if (spans.some(n => !Number.isFinite(n))) return null;
  const required = Math.max(...spans) + 1;
  const selected = options.snapshots.find(s => s.id === form.snapshotId);
  const historical=form.sourceMode==='index_warehouse' ? historicalOption(options,form.warehouseId) : null;
  const start = historical?.data_start ?? (form.sourceMode === 'snapshot' ? dateText(selected?.start) : form.dataStart);
  const end = historical?.end ?? (form.sourceMode === 'snapshot' ? dateText(selected?.end) : form.dataEnd);
  if (!validDate(start) || !validDate(end) || start > form.btStart) return null;
  const calendar = historical ?? (form.sourceMode === 'snapshot' ? selected : [...options.snapshots,...(options.calendars ?? [])]
    .filter(s => s.trading_dates?.length && dateText(s.start) <= start && dateText(s.end) >= form.btStart)
    .sort((a,b) => b.end.localeCompare(a.end))[0]);
  const dates = calendar?.trading_dates?.filter(d => d >= start && d <= end);
  const known = Boolean(dates?.length);
  const available = known ? dates!.filter(d => d < form.btStart).length : null;
  const earliest = known ? (historical ? dates!.filter(d=>d>=historical.start && d>=dates![required])[0] ?? null : dates![required] ?? null) : null;
  const upper = Math.floor((Date.parse(form.btStart) - Date.parse(start)) / 86400000);
  let problem: string | null = null;
  if ((available ?? upper) < required) problem = `历史预热不足：至少需要${required}个回测前历史交易日，${available == null ? `当前数据起点至回测起点最多只有${upper}个自然日` : `实际只有${available}个` }。${earliest ? `最早可从${earliest}开始回测。` : ''}请提前数据开始日期或延后回测起点。`;
  return {required,available,earliest,known,problem};
}

function matchingSnapshot(form: FormState, options: ResearchOptions): Snapshot | null {
  if (form.sourceMode !== 'download') return null;
  const codes = unique(parseCodes(form.downloadCodes));
  return options.snapshots.filter(s => codes.length >= 2 && codes.every(c => s.codes.some(item => item.code === c)) &&
    dateText(s.start) === form.dataStart && dateText(s.end) === form.dataEnd &&
    s.benchmark === form.benchmark && s.validation_mode === form.validationMode)
    .sort((a,b) => Number(b.codes.length === codes.length) - Number(a.codes.length === codes.length) || Number(b.is_current_download === true) - Number(a.is_current_download === true))[0] ?? null;
}

function makeDefaultFactors(
  configs: ResearchRequest["factors"],
  definitions: FactorDefinition[],
): Record<FactorName, FactorFormState> {
  const result = {} as Record<FactorName, FactorFormState>;
  const keys = unique([...definitions.map(d => d.name), ...configs.map(c => c.id ?? c.name)]);
  for (const key of keys) {
    const config = configs.find(item => (item.id ?? item.name) === key) ?? null;
    const name = config?.name ?? key;
    const definition = definitions.find(item => item.name === name);
    result[key] = {
      enabled: Boolean(config),
      name, definition,
      weight: config ? percentText(config.weight) : "0",
      window:
        config?.window != null
          ? String(config.window)
          : definition?.default_window != null
            ? String(definition.default_window)
            : "",
      skip: String(config?.skip ?? definition?.default_skip ?? (name === 'momentum' ? 0 : '')),
      baselineWindow: String(config?.baseline_window ?? definition?.default_baseline_window ?? ''),
    };
  }
  return result;
}

function makeEmptyFactors(): Record<FactorName, FactorFormState> {
  const result = {} as Record<FactorName, FactorFormState>;
  for (const name of FACTOR_NAMES) {
    result[name] = { enabled: false, weight: "0", window: "" };
  }
  return result;
}

function buildDefaultForm(options: ResearchOptions): FormState {
  const defaults = options.defaults;
  const data = defaults.data;
  const download = data.mode === "download" ? data : null;
  const snapshot =
    data.mode === "snapshot"
      ? options.snapshots.find((item) => item.id === data.snapshot_id) ??
        options.snapshots[0] ??
        null
      : options.snapshots[0] ?? null;

  const selectedCodes =
    data.mode === "snapshot" && snapshot
      ? unique(
          data.codes.filter((code) =>
            snapshot.codes.some((item) => item.code === code),
          ),
        )
      : snapshot?.codes.map((item) => item.code) ?? [];

  return {
    name: defaults.name ?? "",
    sourceMode: data.mode,
    warehouseId:data.mode==='index_warehouse' ? data.warehouse_id : historicalOption(options)?.id ?? '',
    autoDates: true,
    snapshotId: snapshot?.id ?? "",
    selectedCodes,
    downloadCodes: download ? unique(download.codes).join(", ") : "",
    dataStart: download
      ? dateText(download.start)
      : dateText(snapshot?.start),
    dataEnd: download ? dateText(download.end) : dateText(snapshot?.end),
    benchmark:
      download?.benchmark ??
      snapshot?.benchmark ??
      options.benchmarks[0]?.code ??
      "",
    validationMode: download?.validation_mode ?? "fallback",
    comparisonBenchmark: defaults.backtest.benchmark ?? '',
    neutralizationMode:defaults.neutralization?.mode ?? 'off',
    industryVersion:defaults.neutralization?.industry_version ?? options.industry_data?.id ?? '',
    factors: makeDefaultFactors(defaults.factors, options.factor_definitions),
    btStart: dateText(defaults.backtest.start),
    btEnd: dateText(defaults.backtest.end),
    initialCash: numberText(defaults.backtest.initial_cash),
    topk: numberText(defaults.backtest.topk, 0),
    nDrop: numberText(defaults.backtest.n_drop, 0),
    riskDegree: percentText(defaults.backtest.risk_degree),
    openCost: percentText(defaults.backtest.open_cost),
    closeCost: percentText(defaults.backtest.close_cost),
    minCost: numberText(defaults.backtest.min_cost),
    rebalanceFrequency:defaults.backtest.rebalance_frequency ?? 'daily',
    costMode:defaults.backtest.cost_mode ?? 'legacy',
    commissionRate:percentText(defaults.backtest.commission_rate ?? .0003),
    taxMode:defaults.backtest.tax_mode ?? 'historical',
    stampTaxRate:percentText(defaults.backtest.stamp_tax_rate ?? .0005),
    transferFeeRate:percentText(defaults.backtest.transfer_fee_rate ?? .00001),
    impactCost:percentText(defaults.backtest.impact_cost ?? 0),
    horizons: defaults.evaluation.horizons.join(", "),
    groups: numberText(defaults.evaluation.groups, 0),
    minCrossSection: numberText(defaults.evaluation.min_cross_section, 0),
    advancedOpen: false,
  };
}

function textField(value: unknown, fallback: string): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return fallback;
}

function stringArray(value: unknown, fallback: string[]): string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string")
    ? value
    : fallback;
}

function mergeDraft(
  base: FormState,
  raw: unknown,
  options: ResearchOptions,
): FormState {
  if (!isRecord(raw)) return base;

  const sourceMode: SourceMode =
    raw.sourceMode === "snapshot" || raw.sourceMode === "download" || raw.sourceMode==='index_warehouse'
      ? raw.sourceMode
      : base.sourceMode;
  const requestedSnapshotId =
    typeof raw.snapshotId === "string" ? raw.snapshotId : base.snapshotId;
  const snapshot =
    options.snapshots.find((item) => item.id === requestedSnapshotId) ?? null;
  const snapshotId = snapshot?.id ?? "";
  const rawCodes = stringArray(raw.selectedCodes, base.selectedCodes);
  const selectedCodes = snapshot
    ? unique(
        rawCodes.filter((code) =>
          snapshot.codes.some((item) => item.code === code),
        ),
      )
    : [];

  const rawFactors = isRecord(raw.factors) ? raw.factors : {};
  const factors = { ...base.factors };
  for (const [key, item] of Object.entries(rawFactors)) {
    if (!isRecord(item)) continue;
    const name = typeof item.name === 'string' ? item.name : key;
    const definition = options.factor_definitions.find(d => d.name === name);
    if (!definition || !/^[a-z][a-z0-9_]{0,63}$/.test(key) || ['composite','date','instrument','rank'].includes(key)) continue;
    const prior = factors[key] ?? {enabled:false,weight:'0',window:String(definition.default_window ?? '')};
    factors[key] = {
      name, definition,
      enabled:
        typeof item.enabled === "boolean" ? item.enabled : prior.enabled,
      weight: textField(item.weight, prior.weight),
      window: textField(item.window, prior.window),
      skip: textField(item.skip, prior.skip ?? String(definition.default_skip ?? '')),
      baselineWindow: textField(item.baselineWindow, prior.baselineWindow ?? String(definition.default_baseline_window ?? '')),
    };
  }

  const validationMode: ValidationMode =
    raw.validationMode === "off" ||
    raw.validationMode === "verify" ||
    raw.validationMode === "fallback"
      ? raw.validationMode
      : base.validationMode;

  return {
    ...base,
    name: textField(raw.name, base.name),
    sourceMode,
    warehouseId:textField(raw.warehouseId,base.warehouseId),
    autoDates: typeof raw.autoDates === 'boolean' ? raw.autoDates : base.autoDates,
    snapshotId,
    selectedCodes,
    downloadCodes: textField(raw.downloadCodes, base.downloadCodes),
    dataStart: textField(raw.dataStart, base.dataStart),
    dataEnd: textField(raw.dataEnd, base.dataEnd),
    benchmark: textField(raw.benchmark, base.benchmark),
    comparisonBenchmark: textField(raw.comparisonBenchmark, base.comparisonBenchmark),
    neutralizationMode:raw.neutralizationMode==='industry'?'industry':raw.neutralizationMode==='off'?'off':base.neutralizationMode,
    industryVersion:textField(raw.industryVersion,base.industryVersion),
    validationMode,
    factors,
    btStart: textField(raw.btStart, base.btStart),
    btEnd: textField(raw.btEnd, base.btEnd),
    initialCash: textField(raw.initialCash, base.initialCash),
    topk: textField(raw.topk, base.topk),
    nDrop: textField(raw.nDrop, base.nDrop),
    riskDegree: textField(raw.riskDegree, base.riskDegree),
    openCost: textField(raw.openCost, base.openCost),
    closeCost: textField(raw.closeCost, base.closeCost),
    minCost: textField(raw.minCost, base.minCost),
    rebalanceFrequency:raw.rebalanceFrequency==='daily'||raw.rebalanceFrequency==='weekly'||raw.rebalanceFrequency==='monthly' ? raw.rebalanceFrequency : base.rebalanceFrequency,
    costMode:raw.costMode==='legacy'||raw.costMode==='itemized' ? raw.costMode : base.costMode,
    commissionRate:textField(raw.commissionRate,base.commissionRate),
    taxMode:raw.taxMode==='historical'||raw.taxMode==='fixed' ? raw.taxMode : base.taxMode,
    stampTaxRate:textField(raw.stampTaxRate,base.stampTaxRate),
    transferFeeRate:textField(raw.transferFeeRate,base.transferFeeRate),
    impactCost:textField(raw.impactCost,base.impactCost),
    horizons: textField(raw.horizons, base.horizons),
    groups: textField(raw.groups, base.groups),
    minCrossSection: textField(raw.minCrossSection, base.minCrossSection),
    advancedOpen:
      typeof raw.advancedOpen === "boolean"
        ? raw.advancedOpen
        : base.advancedOpen,
  };
}

function readDraft(): unknown {
  try {
    if (typeof window === "undefined") return null;
    const raw = window.localStorage.getItem(DRAFT_KEY);
    return raw ? (JSON.parse(raw) as unknown) : null;
  } catch {
    return null;
  }
}

function buildRequest(form: FormState, options: ResearchOptions, settings: {skipWarmup?:boolean} = {}): BuildResult {
  const errors: string[] = [];
  const name = form.name.trim();
  if (!name) errors.push("请填写实验名称。");
  if (name.length > 80) errors.push("实验名称不能超过 80 个字符。");

  let codes: string[] = [];
  let data: SnapshotData | DownloadData | HistoricalData | null = null;
  let dataStart = "";
  let dataEnd = "";

  if (form.sourceMode==='index_warehouse') {
    const historical=historicalOption(options,form.warehouseId);
    if(!historical || historical.id!==form.warehouseId)errors.push('历史成分仓库未完成或版本已变化，请切换来源重新确认。');
    else {
      data={mode:'index_warehouse',warehouse_id:historical.id};dataStart=historical.data_start;dataEnd=historical.end;
      if(form.btStart<historical.start || form.btEnd>historical.end)errors.push('回测区间超出历史仓库研究范围。');
    }
  } else if (form.sourceMode === "snapshot") {
    const snapshot =
      options.snapshots.find((item) => item.id === form.snapshotId) ?? null;
    if (!snapshot) {
      errors.push("请选择已有的数据快照。");
    } else {
      const requested = unique(form.selectedCodes);
      codes = requested.filter((code) =>
        snapshot.codes.some((item) => item.code === code),
      );
      if (codes.length !== requested.length) {
        errors.push("部分股票不在所选快照中，请重新选择。");
      }
      if (codes.length < 2) errors.push("请至少勾选 2 只股票。");
      dataStart = dateText(snapshot.start);
      dataEnd = dateText(snapshot.end);
      if (codes.length >= 2) {
        data = { mode: "snapshot", snapshot_id: snapshot.id, codes };
      }
    }
  } else {
    codes = parseCodes(form.downloadCodes);
    const duplicates = findDuplicates(codes);
    if (duplicates.length) {
      errors.push(`股票代码重复：${duplicates.join("、")}。`);
    }
    const invalid = codes.filter((code) => !CODE_PATTERN.test(code));
    if (invalid.length) {
      errors.push(
        `股票代码格式无效：${invalid.slice(0, 5).join("、")}${
          invalid.length > 5 ? " 等" : ""
        }。`,
      );
    }
    if (codes.length < 2) errors.push("请至少填写 2 个股票代码。");
    dataStart = form.dataStart;
    dataEnd = form.dataEnd;
    if (!validDate(form.dataStart) || !validDate(form.dataEnd)) {
      errors.push("请填写有效的数据开始与结束日期。");
    } else if (form.dataStart >= form.dataEnd) {
      errors.push("数据开始日期必须早于结束日期。");
    }
    if (!form.benchmark) errors.push("请选择基准。");
    if (!["off", "verify", "fallback"].includes(form.validationMode)) {
      errors.push("请选择有效的数据校验模式。");
    }
    if (
      validDate(form.dataStart) &&
      validDate(form.dataEnd) &&
      form.dataStart < form.dataEnd &&
      form.benchmark &&
      codes.length >= 2 &&
      duplicates.length === 0 &&
      invalid.length === 0
    ) {
      data = {
        mode: "download",
        codes,
        start: form.dataStart,
        end: form.dataEnd,
        benchmark: form.benchmark,
        validation_mode: form.validationMode,
      };
    }
  }

  const unsupported = codes.filter(code => CODE_PATTERN.test(code) && !/^(60\d{4}\.SH|(00|30)\d{4}\.SZ)$/.test(code));
  if (unsupported.length) {
    errors.push(`当前回测支持沪深主板及创业板A股；科创板、北交所、B股、ETF和指数尚未适配数据或交易规则：${unsupported.join("、")}。`);
  }
  if (form.sourceMode!=='index_warehouse' && options.limits.max_codes > 0 && codes.length > options.limits.max_codes) {
    errors.push(`股票数量不能超过 ${options.limits.max_codes} 只。`);
  }

  if (!validDate(form.btStart) || !validDate(form.btEnd)) {
    errors.push("请填写有效的回测开始与结束日期。");
  } else if (form.btStart >= form.btEnd) {
    errors.push("回测开始日期必须早于结束日期。");
  } else {
    if (dataStart && form.btStart < dataStart) {
      errors.push("回测开始日期不能早于数据起始日期。");
    }
    if (dataEnd && form.btEnd > dataEnd) {
      errors.push("回测结束日期不能晚于数据结束日期。");
    }
  }

  const enabledFactors = Object.keys(form.factors).filter(
    (item) => form.factors[item].enabled,
  );
  if (!enabledFactors.length) errors.push("请至少启用一个因子。");

  const factorPayload: ResearchRequest["factors"] = [];
  let weightSum = 0;
  for (const factorName of enabledFactors) {
    const factorForm = form.factors[factorName];
    const formulaName = factorForm.name ?? factorName;
    const definition = options.factor_definitions.find(d => d.name === formulaName);
    const label = definition?.label ?? FACTOR_LABELS[formulaName] ?? formulaName;
    if (!definition) errors.push(`不支持的因子：${formulaName}`);
    const weight = toNumber(factorForm.weight);
    if (!Number.isFinite(weight) || weight <= 0 || weight > 100) {
      errors.push(`${label}的权重无效。`);
    } else {
      weightSum += weight;
    }

    let window: number | null = null;
    if (definition?.default_window != null) {
      const parsedWindow = toNumber(factorForm.window);
      if (
        !Number.isInteger(parsedWindow) ||
        parsedWindow < 2 ||
        parsedWindow > 252
      ) {
        errors.push(
          `${label}的窗口需为 2–252 的整数。`,
        );
      } else {
        window = parsedWindow;
      }
    }

    const params: {skip?:number;baseline_window?:number;id?:string} = {};
    if (definition?.default_skip != null || formulaName === 'momentum') {
      const skip = toNumber(factorForm.skip ?? String(definition?.default_skip ?? 0));
      if (!Number.isInteger(skip) || skip < 0 || window == null || skip >= window) errors.push(`${label}剔除天数须为非负整数且小于窗口。`);
      else if (formulaName !== 'momentum' || skip !== 0) params.skip = skip;
    }
    if (definition?.default_baseline_window != null) {
      const baseline = toNumber(factorForm.baselineWindow ?? String(definition.default_baseline_window));
      if (!Number.isInteger(baseline) || baseline < 2 || baseline > 252) errors.push(`${label}基准窗口须为2–252日。`);
      else params.baseline_window = baseline;
    }
    if (factorName !== formulaName) params.id = factorName;
    if (enabledFactors.length > 20) errors.push('最多启用20个因子实例。');
    if (Number.isFinite(weight) && weight > 0 && weight <= 100) {
      factorPayload.push({
        name: formulaName,
        weight: weight / 100,
        window,
        ...params,
      });
    }
  }
  if (
    enabledFactors.length &&
    Math.abs(weightSum - 100) > 1e-7
  ) {
    errors.push(
      `已启用因子的权重合计需为 100%，当前为 ${
        Number.isFinite(weightSum) ? Number(weightSum.toFixed(2)) : "—"
      }%。`,
    );
  }

  const initialCash = toNumber(form.initialCash);
  if (!Number.isFinite(initialCash) || initialCash <= 0) {
    errors.push("初始资金需为正数。");
  }
  const topk = toNumber(form.topk);
  if (!Number.isInteger(topk) || topk < 1) {
    errors.push("持仓数 topk 需为大于 0 的整数。");
  } else if (codes.length > 0 && topk > codes.length) {
    errors.push(
      `持仓数 topk 不能超过当前股票池数量（${codes.length}）。`,
    );
  }
  if(form.sourceMode==='index_warehouse' && topk>300)errors.push('历史动态池目标持股数不能超过300只。');
  const nDrop = toNumber(form.nDrop);
  if (!Number.isInteger(nDrop) || nDrop < 0 || nDrop > topk) {
    errors.push("每次剔除数量须为非负整数，且不能超过目标持股数量。");
  }
  const riskDegree = toNumber(form.riskDegree);
  if (
    !Number.isFinite(riskDegree) ||
    riskDegree <= 0 ||
    riskDegree > 100
  ) {
    errors.push("股票配置比例需在 0–100% 之间。");
  }
  const openCost = toNumber(form.openCost);
  if (form.costMode==='legacy' && (!Number.isFinite(openCost) || openCost < 0 || openCost >= 100)) {
    errors.push("买入费率须在 0% 至 100% 之间（不含100%）。");
  }
  const closeCost = toNumber(form.closeCost);
  if (form.costMode==='legacy' && (!Number.isFinite(closeCost) || closeCost < 0 || closeCost >= 100)) {
    errors.push("卖出费率须在 0% 至 100% 之间（不含100%）。");
  }
  const minCost = toNumber(form.minCost);
  if (!Number.isFinite(minCost) || minCost < 0) {
    errors.push("最低费用需为非负数。");
  }
  if(!['daily','weekly','monthly'].includes(form.rebalanceFrequency))errors.push('请选择有效的调仓频率。');
  if(!['legacy','itemized'].includes(form.costMode))errors.push('请选择有效的费用模式。');
  if(!['historical','fixed'].includes(form.taxMode))errors.push('请选择有效的税费模式。');
  const rate=(value:string,label:string,active:boolean,fallback:number)=>{
    const parsed=toNumber(value),valid=Number.isFinite(parsed)&&parsed>=0&&parsed<100;
    if(active&&!valid)errors.push(`${label}须在0%至100%之间（不含100%）。`);
    return valid ? parsed/100 : fallback;
  };
  const commissionRate=rate(form.commissionRate,'券商佣金',form.costMode==='itemized',.0003);
  const stampTaxRate=rate(form.stampTaxRate,'卖出印花税',form.costMode==='itemized'&&form.taxMode==='fixed',.0005);
  const transferFeeRate=rate(form.transferFeeRate,'双边过户费',form.costMode==='itemized'&&form.taxMode==='fixed',.00001);
  const impactCost=rate(form.impactCost,'双边冲击成本',true,0);
  if(form.costMode==='itemized' && form.taxMode==='historical' && form.btStart<'2015-08-01')errors.push('历史税费表仅支持2015-08-01起，更早区间请使用自定义税费。');
  const maximumRate=form.costMode==='legacy' ? Math.max(openCost,closeCost)/100 : commissionRate+(form.taxMode==='historical' ? .00102 : stampTaxRate+transferFeeRate);
  if(maximumRate+impactCost>=1)errors.push('佣金、税费与冲击成本的合计比例须小于100%。');
  const groups = toNumber(form.groups);
  if(form.comparisonBenchmark && !(options.comparison_benchmarks ?? options.benchmarks).some(item=>item.code===form.comparisonBenchmark))errors.push('请选择有效的评价基准。');
  if (!Number.isInteger(groups) || groups < 2 || groups > 10) {
    errors.push("诊断分组数须为 2–10 的整数。");
  }
  const minCrossSection = toNumber(form.minCrossSection);
  if (!Number.isInteger(minCrossSection) || minCrossSection < 3 || minCrossSection < groups) {
    errors.push("最小有效股票数须为不小于3的整数，且不能少于分组数。");
  }
  const horizons = parseHorizons(form.horizons);
  if (horizons.error) errors.push(horizons.error);
  if (form.sourceMode === "snapshot" || form.sourceMode==='index_warehouse') {
    const snapshot = form.sourceMode==='index_warehouse' ? historicalOption(options,form.warehouseId) : options.snapshots.find(item => item.id === form.snapshotId);
    const dates = snapshot?.trading_dates;
    if (snapshot && dates?.length && validDate(form.btStart) && validDate(form.btEnd)) {
      const left = dates.indexOf(form.btStart), right = dates.indexOf(form.btEnd);
      if (left < 0 || right < 0) errors.push("回测起止日期必须是所选快照中的交易日。");
      else {
        if (horizons.values.length && right - left + 1 <= Math.max(...horizons.values)) errors.push("回测交易日数须超过最长评估持有期。");
      }
    }
  }

  const readiness = warmupInfo(form, options);
  if (!settings.skipWarmup && readiness?.problem) errors.push(readiness.problem);

  if (errors.length || !data) {
    return {
      request: null,
      errors: errors.length ? errors : ["配置不完整，请检查表单。"],
      codeCount: codes.length,
    };
  }

  return {
    request: {
      name,
      data,
      factors: factorPayload,
      ...(form.neutralizationMode==='industry'||form.industryVersion ? {neutralization:{mode:form.neutralizationMode,...(form.industryVersion?{industry_version:form.industryVersion}:{})}} : {}),
      backtest: {
        start: form.btStart,
        end: form.btEnd,
        initial_cash: initialCash,
        topk,
        n_drop: nDrop,
        risk_degree: riskDegree / 100,
        open_cost: Number.isFinite(openCost)&&openCost>=0&&openCost<100 ? openCost / 100 : .001,
        close_cost: Number.isFinite(closeCost)&&closeCost>=0&&closeCost<100 ? closeCost / 100 : .001,
        min_cost: minCost,
        rebalance_frequency:form.rebalanceFrequency,cost_mode:form.costMode,
        commission_rate:commissionRate,tax_mode:form.taxMode,stamp_tax_rate:stampTaxRate,
        transfer_fee_rate:transferFeeRate,impact_cost:impactCost,
        ...(form.comparisonBenchmark ? {benchmark:form.comparisonBenchmark} : {}),
      },
      evaluation: {
        horizons: horizons.values,
        groups,
        min_cross_section: minCrossSection,
      },
    },
    errors: [],
    codeCount: form.sourceMode==='index_warehouse' ? 300 : codes.length,
  };
}

function formFromRequest(
  request: ResearchRequest,
  options: ResearchOptions,
): { form: FormState; warning: string | null } {
  const base = buildDefaultForm(options);
  let warning: string | null = null;
  let snapshotId = base.snapshotId;
  let selectedCodes: string[] = [];
  let dataStart = base.dataStart;
  let dataEnd = base.dataEnd;
  let benchmark = base.benchmark;
  let validationMode: ValidationMode = base.validationMode;

  const requestData = request.data;
  if (requestData.mode === "snapshot") {
    const snapshot =
      options.snapshots.find(
        (item) => item.id === requestData.snapshot_id,
      ) ?? null;
    if (snapshot) {
      snapshotId = snapshot.id;
      selectedCodes = unique(
        requestData.codes.filter((code) =>
          snapshot.codes.some((item) => item.code === code),
        ),
      );
      if (selectedCodes.length !== unique(requestData.codes).length) {
        warning = "原任务中的部分股票不在当前快照中，已从股票池移除。";
      }
      dataStart = dateText(snapshot.start);
      dataEnd = dateText(snapshot.end);
      benchmark = snapshot.benchmark || benchmark;
    } else {
      warning = "原快照不在当前可用列表中，请重新选择数据快照。";
    }
  } else if(requestData.mode==='download') {
    selectedCodes = options.snapshots[0]?.codes.map((item) => item.code) ?? [];
    dataStart = dateText(requestData.start);
    dataEnd = dateText(requestData.end);
    benchmark = requestData.benchmark;
    validationMode = requestData.validation_mode;
  }

  const download = requestData.mode === "download" ? requestData : null;
  const codeCount = requestData.mode==='index_warehouse' ? 300 : download
    ? unique(download.codes).length
    : selectedCodes.length;
  let topk = request.backtest.topk;
  if (codeCount > 0 && topk > codeCount) {
    topk = codeCount;
    warning = `${warning ? `${warning} ` : ""}持仓数 topk 已调整为 ${codeCount}，不超过股票池数量。`;
  }

  const factors = makeDefaultFactors(request.factors, options.factor_definitions);

  return {
    form: {
      ...base,
      name: request.name,
      sourceMode: request.data.mode,
      warehouseId:requestData.mode==='index_warehouse' ? requestData.warehouse_id : base.warehouseId,
      autoDates: false,
      snapshotId,
      selectedCodes,
      downloadCodes: download
        ? unique(download.codes).join(", ")
        : base.downloadCodes,
      dataStart,
      dataEnd,
      benchmark,
      comparisonBenchmark: request.backtest.benchmark ?? '',
      neutralizationMode:request.neutralization?.mode ?? 'off',
      industryVersion:request.neutralization?.industry_version ?? options.industry_data?.id ?? '',
      validationMode,
      factors,
      btStart: dateText(request.backtest.start),
      btEnd: dateText(request.backtest.end),
      initialCash: numberText(request.backtest.initial_cash),
      topk: String(topk),
      nDrop: numberText(request.backtest.n_drop, 0),
      riskDegree: percentText(request.backtest.risk_degree),
      openCost: percentText(request.backtest.open_cost),
      closeCost: percentText(request.backtest.close_cost),
      minCost: numberText(request.backtest.min_cost),
      rebalanceFrequency:request.backtest.rebalance_frequency ?? 'daily',
      costMode:request.backtest.cost_mode ?? 'legacy',
      commissionRate:percentText(request.backtest.commission_rate ?? .0003),
      taxMode:request.backtest.tax_mode ?? 'historical',
      stampTaxRate:percentText(request.backtest.stamp_tax_rate ?? .0005),
      transferFeeRate:percentText(request.backtest.transfer_fee_rate ?? .00001),
      impactCost:percentText(request.backtest.impact_cost ?? 0),
      horizons: request.evaluation.horizons.join(", "),
      groups: numberText(request.evaluation.groups, 0),
      minCrossSection: numberText(
        request.evaluation.min_cross_section,
        0,
      ),
      advancedOpen: true,
    },
    warning,
  };
}

function makeIdempotencyKey(): string {
  try {
    const cryptoObject = globalThis.crypto as
      | (Crypto & { randomUUID?: () => string })
      | undefined;
    if (cryptoObject && typeof cryptoObject.randomUUID === "function") {
      return cryptoObject.randomUUID();
    }
  } catch {
    // 回退到时间戳随机串。
  }
  return `research-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export {
  FACTOR_NAMES,
  FACTOR_LABELS,
  CODE_PATTERN,
  DRAFT_KEY,
  STAGE_ORDER,
  STAGE_LABELS,
  STATUS_LABELS,
  STATUS_CLASSES,
  VALIDATION_LABELS,
  VALIDATION_OPTIONS,
  isRecord,
  unique,
  numberText,
  percentText,
  dateText,
  toNumber,
  validDate,
  parseCodes,
  findDuplicates,
  parseHorizons,
  clampTopk,
  earliestBacktestDate,
  warmupInfo,
  matchingSnapshot,
  makeDefaultFactors,
  makeEmptyFactors,
  buildDefaultForm,
  textField,
  stringArray,
  mergeDraft,
  readDraft,
  buildRequest,
  formFromRequest,
  makeIdempotencyKey,
};
export type { FactorFormState, FormState, BuildResult };
