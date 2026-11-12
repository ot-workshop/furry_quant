/**
 * Research form state and request building. Pure functions only, so the rules can be
 * unit-tested without a DOM. The server re-validates everything; these checks exist
 * to give immediate, specific feedback.
 */
import type {
  FactorDefinition,
  HistoricalIndexOption,
  IndustryLevel,
  ResearchBacktest,
  ResearchFactor,
  ResearchOptions,
  ResearchRequest,
  Snapshot,
  SourceMode,
} from "../api/research";

/** Factor families in display order, so the form groups price and fundamental factors. */
export const FAMILY_ORDER = [
  "momentum",
  "value",
  "size",
  "risk",
  "activity",
  "fundamental",
] as const;

export const DRAFT_KEY = "finance-quant.research-draft.v2";
export const INDUSTRY_LEVELS: IndustryLevel[] = ["L1", "L2", "L3"];
const SUPPORTED_CODE = /^(60\d{4}\.SH|(00|30)\d{4}\.SZ)$/;

export interface FactorRow {
  /** Instance key: the factor name, or ``name_N`` for an additional window. */
  key: string;
  name: string;
  enabled: boolean;
  /** Percent, 0–100. */
  weight: number;
  window: number | null;
  skip: number | null;
  baselineWindow: number | null;
}

export interface FormState {
  name: string;
  sourceMode: SourceMode;
  snapshotId: string;
  codes: string[];
  warehouseId: string;
  factors: FactorRow[];
  neutralization: "off" | "industry";
  industryVersion: string;
  industryLevel: IndustryLevel;
  benchmark: string;
  start: string;
  end: string;
  initialCash: number;
  topk: number;
  nDrop: number;
  /** Percent, 0–100. */
  riskDegree: number;
  rebalanceFrequency: "daily" | "weekly" | "monthly";
  costMode: "legacy" | "itemized";
  /** All rates below are percents. */
  openCost: number;
  closeCost: number;
  commissionRate: number;
  taxMode: "historical" | "fixed";
  stampTaxRate: number;
  transferFeeRate: number;
  impactCost: number;
  minCost: number;
  horizons: number[];
  groups: number;
  minCrossSection: number;
}

export interface BuildResult {
  request: ResearchRequest | null;
  errors: string[];
}

const pct = (value: number | undefined, fallback: number) =>
  round((value ?? fallback) * 100);
const round = (value: number) => Number(value.toFixed(8));
const isDate = (value: string) => /^\d{4}-\d{2}-\d{2}$/.test(value);
export const dateOnly = (value: string | undefined | null) => {
  if (!value) return "";
  const compact = /^(\d{4})(\d{2})(\d{2})$/.exec(value);
  return compact
    ? `${compact[1]}-${compact[2]}-${compact[3]}`
    : value.slice(0, 10);
};

export function industryLevels(options: ResearchOptions): IndustryLevel[] {
  const levels = options.industry_data?.levels;
  if (!levels) return [];
  return INDUSTRY_LEVELS.filter((level) => Boolean(levels[level]));
}

export function defaultIndustryLevel(options: ResearchOptions): IndustryLevel {
  const levels = industryLevels(options);
  return levels.includes("L1") ? "L1" : (levels[0] ?? "L1");
}

export function historicalOption(
  options: ResearchOptions,
  id?: string,
): HistoricalIndexOption | undefined {
  return (
    options.historical_indices?.find((item) => item.id === id) ??
    options.historical_indices?.[0]
  );
}

export function snapshotOf(
  options: ResearchOptions,
  id: string,
): Snapshot | undefined {
  return options.snapshots.find((item) => item.id === id);
}

export function factorRows(
  configs: ResearchFactor[],
  definitions: FactorDefinition[],
): FactorRow[] {
  const rows: FactorRow[] = [];
  for (const definition of definitions) {
    const configured = configs.filter((item) => item.name === definition.name);
    const instances = configured.length ? configured : [null];
    for (const config of instances) {
      rows.push({
        key: config?.id ?? definition.name,
        name: definition.name,
        enabled: Boolean(config),
        weight: config ? round(config.weight * 100) : 0,
        window: config?.window ?? definition.default_window ?? null,
        skip:
          config?.skip ??
          definition.default_skip ??
          (definition.name === "momentum" ? 0 : null),
        baselineWindow:
          config?.baseline_window ?? definition.default_baseline_window ?? null,
      });
    }
  }
  return rows;
}

/** Whether the selected dataset holds financial statements for at least two stocks. */
export function financialCoverage(
  form: FormState,
  options: ResearchOptions,
): { covered: number; total: number } | null {
  if (!options.financial_data) return null;
  if (form.sourceMode === "index_warehouse") {
    return (
      historicalOption(options, form.warehouseId)?.financial_coverage ?? null
    );
  }
  return snapshotOf(options, form.snapshotId)?.financial_coverage ?? null;
}

/** A fundamental factor is selectable only when the dataset carries statements. */
export function factorAvailable(
  definition: FactorDefinition,
  coverage: { covered: number; total: number } | null,
): boolean {
  return (
    !definition.requires_financials ||
    Boolean(coverage && coverage.covered >= 2)
  );
}

/** Drop enabled factors that the chosen dataset cannot serve. */
export function pruneFactors(
  form: FormState,
  options: ResearchOptions,
): FormState {
  const coverage = financialCoverage(form, options);
  const unavailable = new Set(
    options.factor_definitions
      .filter((definition) => !factorAvailable(definition, coverage))
      .map((definition) => definition.name),
  );
  if (!unavailable.size) return form;
  return {
    ...form,
    factors: form.factors.map((row) =>
      unavailable.has(row.name) ? { ...row, enabled: false, weight: 0 } : row,
    ),
  };
}

function backtestFields(backtest: Partial<ResearchBacktest>) {
  return {
    initialCash: backtest.initial_cash ?? 100000,
    topk: backtest.topk ?? 3,
    nDrop: backtest.n_drop ?? 1,
    riskDegree: pct(backtest.risk_degree, 0.9),
    rebalanceFrequency: backtest.rebalance_frequency ?? "daily",
    costMode: backtest.cost_mode ?? "legacy",
    openCost: pct(backtest.open_cost, 0.001),
    closeCost: pct(backtest.close_cost, 0.001),
    commissionRate: pct(backtest.commission_rate, 0.0003),
    taxMode: backtest.tax_mode ?? "historical",
    stampTaxRate: pct(backtest.stamp_tax_rate, 0.0005),
    transferFeeRate: pct(backtest.transfer_fee_rate, 0.00001),
    impactCost: pct(backtest.impact_cost, 0.0005),
    minCost: backtest.min_cost ?? 5,
    benchmark: backtest.benchmark ?? "",
  } as const;
}

export function defaultForm(options: ResearchOptions): FormState {
  const defaults = options.defaults;
  const snapshot =
    defaults.data.mode === "snapshot" && defaults.data.snapshot_id
      ? snapshotOf(options, defaults.data.snapshot_id)
      : options.snapshots[0];
  const historical = historicalOption(options);
  return {
    name: defaults.name ?? "我的研究",
    sourceMode: snapshot || !historical ? "snapshot" : "index_warehouse",
    snapshotId: snapshot?.id ?? "",
    codes: snapshot?.codes.map((item) => item.code) ?? [],
    warehouseId: historical?.id ?? "",
    factors: factorRows(defaults.factors, options.factor_definitions),
    neutralization: defaults.neutralization?.mode ?? "off",
    industryVersion:
      defaults.neutralization?.industry_version ??
      options.industry_data?.id ??
      "",
    industryLevel:
      defaults.neutralization?.industry_level ?? defaultIndustryLevel(options),
    start: dateOnly(defaults.backtest.start),
    end: dateOnly(defaults.backtest.end),
    ...backtestFields(defaults.backtest),
    horizons: defaults.evaluation.horizons,
    groups: defaults.evaluation.groups,
    minCrossSection: defaults.evaluation.min_cross_section,
  };
}

/** Restore a submitted request into the form, reporting anything that no longer applies. */
export function formFromRequest(
  request: ResearchRequest,
  options: ResearchOptions,
): { form: FormState; warnings: string[] } {
  const base = defaultForm(options);
  const warnings: string[] = [];
  let form: FormState = {
    ...base,
    name: request.name,
    factors: factorRows(request.factors, options.factor_definitions),
    neutralization: request.neutralization?.mode ?? "off",
    industryVersion:
      request.neutralization?.industry_version ?? base.industryVersion,
    industryLevel: request.neutralization?.industry_level ?? base.industryLevel,
    start: dateOnly(request.backtest.start),
    end: dateOnly(request.backtest.end),
    ...backtestFields(request.backtest),
    horizons: request.evaluation.horizons,
    groups: request.evaluation.groups,
    minCrossSection: request.evaluation.min_cross_section,
  };
  if (request.data.mode === "index_warehouse") {
    const option = historicalOption(options, request.data.warehouse_id);
    if (!option) warnings.push("原历史成分仓库已不可用，请重新选择数据集。");
    form = {
      ...form,
      sourceMode: "index_warehouse",
      warehouseId: option?.id ?? "",
    };
  } else {
    const snapshot = request.data.snapshot_id
      ? snapshotOf(options, request.data.snapshot_id)
      : undefined;
    if (!snapshot) {
      warnings.push("原数据快照已不可用，已切换到默认数据集。");
    } else {
      const available = new Set(snapshot.codes.map((item) => item.code));
      const codes = request.data.codes.filter((code) => available.has(code));
      if (codes.length !== request.data.codes.length)
        warnings.push("部分股票已不在该快照中，已从股票池移除。");
      form = {
        ...form,
        sourceMode: "snapshot",
        snapshotId: snapshot.id,
        codes,
      };
    }
  }
  return { form, warnings };
}

export function poolSize(form: FormState, options: ResearchOptions): number {
  if (form.sourceMode === "index_warehouse")
    return historicalOption(options, form.warehouseId)?.codes_count ?? 300;
  return form.codes.length;
}

export function tradingDates(
  form: FormState,
  options: ResearchOptions,
): string[] {
  if (form.sourceMode === "index_warehouse")
    return historicalOption(options, form.warehouseId)?.trading_dates ?? [];
  return snapshotOf(options, form.snapshotId)?.trading_dates ?? [];
}

export function dataRange(
  form: FormState,
  options: ResearchOptions,
): { start: string; end: string; dataStart: string } | null {
  if (form.sourceMode === "index_warehouse") {
    const option = historicalOption(options, form.warehouseId);
    return option
      ? { start: option.start, end: option.end, dataStart: option.data_start }
      : null;
  }
  const snapshot = snapshotOf(options, form.snapshotId);
  return snapshot
    ? {
        start: dateOnly(snapshot.start),
        end: dateOnly(snapshot.end),
        dataStart: dateOnly(snapshot.start),
      }
    : null;
}

/** Trading days of history each enabled factor needs before the first signal. */
export function requiredHistory(
  form: FormState,
  definitions: FactorDefinition[],
): number | null {
  const spans: number[] = [];
  for (const row of form.factors.filter((item) => item.enabled)) {
    const definition = definitions.find((item) => item.name === row.name);
    if (!definition || definition.default_window == null) {
      spans.push(0);
      continue;
    }
    if (!Number.isInteger(row.window) || (row.window as number) < 2)
      return null;
    const baseline =
      definition.default_baseline_window != null
        ? (row.baselineWindow ?? definition.default_baseline_window)
        : 1;
    spans.push((row.window as number) + baseline - 1);
  }
  return spans.length ? Math.max(...spans) + 1 : null;
}

/** Earliest backtest start date that leaves enough history for the enabled factors. */
export function earliestStart(
  form: FormState,
  options: ResearchOptions,
): string | null {
  const required = requiredHistory(form, options.factor_definitions);
  const range = dataRange(form, options);
  if (required === null || !range) return null;
  const dates = tradingDates(form, options).filter(
    (day) => day >= range.dataStart,
  );
  const candidate = dates[required];
  if (!candidate) return null;
  return candidate < range.start
    ? (dates.find((day) => day >= range.start) ?? null)
    : candidate;
}

export function weightTotal(form: FormState): number {
  return round(
    form.factors
      .filter((row) => row.enabled)
      .reduce((sum, row) => sum + (row.weight || 0), 0),
  );
}

export function equalWeights(form: FormState): FormState {
  const enabled = form.factors.filter((row) => row.enabled);
  if (!enabled.length) return form;
  const base = Math.floor(10000 / enabled.length);
  let remainder = 10000 - base * enabled.length;
  return {
    ...form,
    factors: form.factors.map((row) => {
      if (!row.enabled) return row;
      const basis = base + (remainder-- > 0 ? 1 : 0);
      return { ...row, weight: basis / 100 };
    }),
  };
}

export function addFactorInstance(form: FormState, key: string): FormState {
  const source = form.factors.find((row) => row.key === key);
  if (!source) return form;
  let n = 2;
  while (form.factors.some((row) => row.key === `${source.name}_${n}`)) n += 1;
  const index = form.factors.findIndex((row) => row.key === key);
  const copy: FactorRow = {
    ...source,
    key: `${source.name}_${n}`,
    enabled: true,
    weight: 0,
  };
  return {
    ...form,
    factors: [
      ...form.factors.slice(0, index + 1),
      copy,
      ...form.factors.slice(index + 1),
    ],
  };
}

export function removeFactorInstance(form: FormState, key: string): FormState {
  return {
    ...form,
    factors: form.factors.filter(
      (row) => row.key !== key || row.key === row.name,
    ),
  };
}

export function buildFactors(
  form: FormState,
  definitions: FactorDefinition[],
  errors: string[],
): ResearchFactor[] {
  const enabled = form.factors.filter((row) => row.enabled);
  if (!enabled.length) errors.push("请至少启用一个因子。");
  if (enabled.length > 20) errors.push("最多启用 20 个因子实例。");
  const result: ResearchFactor[] = [];
  for (const row of enabled) {
    const definition = definitions.find((item) => item.name === row.name);
    const label = definition?.label ?? row.name;
    if (!definition) {
      errors.push(`不支持的因子：${row.name}`);
      continue;
    }
    if (!(row.weight > 0 && row.weight <= 100))
      errors.push(`${label}的权重需在 0–100% 之间且大于 0。`);
    const factor: ResearchFactor = {
      name: row.name,
      weight: round(row.weight / 100),
      window: null,
    };
    if (definition.default_window != null) {
      if (
        !Number.isInteger(row.window) ||
        (row.window as number) < 2 ||
        (row.window as number) > 252
      )
        errors.push(`${label}的窗口需为 2–252 的整数。`);
      else factor.window = row.window;
    }
    if (definition.default_skip != null || row.name === "momentum") {
      const skip = row.skip ?? 0;
      if (
        !Number.isInteger(skip) ||
        skip < 0 ||
        (factor.window != null && skip >= factor.window)
      )
        errors.push(`${label}的剔除天数需为非负整数且小于窗口。`);
      else if (row.name !== "momentum" || skip !== 0) factor.skip = skip;
    }
    if (definition.default_baseline_window != null) {
      const baseline = row.baselineWindow ?? definition.default_baseline_window;
      if (!Number.isInteger(baseline) || baseline < 2 || baseline > 252)
        errors.push(`${label}的基准窗口需为 2–252 的整数。`);
      else factor.baseline_window = baseline;
    }
    if (row.key !== row.name) factor.id = row.key;
    result.push(factor);
  }
  const total = weightTotal(form);
  if (enabled.length && Math.abs(total - 100) > 1e-6)
    errors.push(`已启用因子的权重合计需为 100%，当前为 ${total}%。`);
  return result;
}

export function buildBacktest(
  form: FormState,
  errors: string[],
): Omit<ResearchBacktest, "start" | "end"> {
  const rate = (value: number, label: string, active: boolean) => {
    if (active && !(value >= 0 && value < 100))
      errors.push(`${label}需在 0%–100% 之间。`);
    return round(value / 100);
  };
  if (!(form.initialCash > 0) || form.initialCash > 1e12)
    errors.push("初始资金需为正数且不超过一万亿元。");
  if (!Number.isInteger(form.topk) || form.topk < 1)
    errors.push("目标持股数量需为正整数。");
  if (!Number.isInteger(form.nDrop) || form.nDrop < 0 || form.nDrop > form.topk)
    errors.push("每次最多替换数量需为非负整数，且不超过目标持股数量。");
  if (!(form.riskDegree > 0 && form.riskDegree <= 100))
    errors.push("股票仓位需在 0–100% 之间。");
  if (!(form.minCost >= 0)) errors.push("最低费用需为非负数。");
  const legacy = form.costMode === "legacy";
  const fixedTax = form.costMode === "itemized" && form.taxMode === "fixed";
  return {
    initial_cash: form.initialCash,
    topk: form.topk,
    n_drop: form.nDrop,
    risk_degree: round(form.riskDegree / 100),
    open_cost: rate(form.openCost, "买入综合费率", legacy),
    close_cost: rate(form.closeCost, "卖出综合费率", legacy),
    min_cost: form.minCost,
    rebalance_frequency: form.rebalanceFrequency,
    cost_mode: form.costMode,
    commission_rate: rate(form.commissionRate, "券商佣金", !legacy),
    tax_mode: form.taxMode,
    stamp_tax_rate: rate(form.stampTaxRate, "卖出印花税", fixedTax),
    transfer_fee_rate: rate(form.transferFeeRate, "过户费", fixedTax),
    impact_cost: rate(form.impactCost, "冲击成本", true),
    ...(form.benchmark ? { benchmark: form.benchmark } : {}),
  };
}

export function buildEvaluation(form: FormState, errors: string[]) {
  if (!form.horizons.length) errors.push("请至少填写一个评估持有期。");
  if (form.horizons.some((h) => !Number.isInteger(h) || h < 1 || h > 252))
    errors.push("评估持有期需为 1–252 的整数。");
  if (!Number.isInteger(form.groups) || form.groups < 2 || form.groups > 10)
    errors.push("分组数需为 2–10 的整数。");
  if (
    !Number.isInteger(form.minCrossSection) ||
    form.minCrossSection < Math.max(3, form.groups)
  )
    errors.push("计算 IC 的最少股票数需不小于 3 且不少于分组数。");
  return {
    horizons: [...new Set(form.horizons)].sort((a, b) => a - b),
    groups: form.groups,
    min_cross_section: form.minCrossSection,
  };
}

export function buildData(
  form: FormState,
  options: ResearchOptions,
  errors: string[],
): ResearchRequest["data"] | null {
  if (form.sourceMode === "index_warehouse") {
    const option = historicalOption(options, form.warehouseId);
    if (!option || option.id !== form.warehouseId) {
      errors.push("请选择可用的沪深300历史成分仓库。");
      return null;
    }
    if (form.topk > 300) errors.push("历史动态池目标持股数不能超过 300 只。");
    return { mode: "index_warehouse", warehouse_id: option.id };
  }
  const snapshot = snapshotOf(options, form.snapshotId);
  if (!snapshot) {
    errors.push("请选择数据集。没有可用数据集时，请先到“数据管理”下载行情。");
    return null;
  }
  const available = new Set(snapshot.codes.map((item) => item.code));
  const codes = [...new Set(form.codes)].filter((code) => available.has(code));
  if (codes.length < 2) errors.push("股票池至少需要 2 只股票。");
  if (codes.length > options.limits.max_codes)
    errors.push(`股票池最多 ${options.limits.max_codes} 只。`);
  const unsupported = codes.filter((code) => !SUPPORTED_CODE.test(code));
  if (unsupported.length)
    errors.push(
      `当前仅支持沪深主板与创业板 A 股：${unsupported.slice(0, 5).join("、")}${unsupported.length > 5 ? " 等" : ""}。`,
    );
  if (form.topk > codes.length && codes.length)
    errors.push(`目标持股数量不能超过股票池数量（${codes.length}）。`);
  return { mode: "snapshot", snapshot_id: snapshot.id, codes };
}

export function checkDates(
  form: FormState,
  options: ResearchOptions,
  start: string,
  end: string,
  errors: string[],
  maxHorizon: number,
) {
  if (!isDate(start) || !isDate(end)) {
    errors.push("请填写回测开始与结束日期。");
    return;
  }
  if (start >= end) errors.push("回测开始日期需早于结束日期。");
  const range = dataRange(form, options);
  if (range && (start < range.start || end > range.end))
    errors.push(
      `回测区间需位于数据集范围 ${range.start} 至 ${range.end} 之内。`,
    );
  const dates = tradingDates(form, options);
  if (dates.length) {
    const left = dates.indexOf(start);
    const right = dates.indexOf(end);
    if (left < 0 || right < 0) errors.push("回测起止日期必须是交易日。");
    else if (right - left + 1 <= maxHorizon)
      errors.push("回测交易日数需超过最长评估持有期。");
  }
  const earliest = earliestStart(form, options);
  if (earliest && start < earliest)
    errors.push(
      `历史预热不足：按当前因子窗口，最早可从 ${earliest} 开始回测。`,
    );
}

export function buildRequest(
  form: FormState,
  options: ResearchOptions,
): BuildResult {
  const errors: string[] = [];
  const name = form.name.trim();
  if (!name || name.length > 80) errors.push("请填写 1–80 个字符的研究名称。");
  const data = buildData(form, options, errors);
  const factors = buildFactors(form, options.factor_definitions, errors);
  const coverage = financialCoverage(form, options);
  const blocked = form.factors
    .filter((row) => row.enabled)
    .map((row) => options.factor_definitions.find((d) => d.name === row.name))
    .filter(
      (definition): definition is FactorDefinition =>
        Boolean(definition?.requires_financials) &&
        !factorAvailable(definition as FactorDefinition, coverage),
    );
  if (blocked.length)
    errors.push(
      `当前数据集没有财务数据，不能使用基本面因子：${blocked
        .map((definition) => definition.label)
        .join("、")}。请先在“数据管理 → 财务数据”下载。`,
    );
  const backtest = buildBacktest(form, errors);
  const evaluation = buildEvaluation(form, errors);
  checkDates(
    form,
    options,
    form.start,
    form.end,
    errors,
    Math.max(0, ...evaluation.horizons),
  );
  if (form.neutralization === "industry" && !options.industry_data)
    errors.push("历史行业数据尚未准备，暂不能开启行业中性化。");
  if (form.neutralization === "industry") {
    const levels = options.industry_data?.levels;
    const label =
      form.industryLevel === "L1"
        ? "一级"
        : form.industryLevel === "L2"
          ? "二级"
          : "三级";
    if (!levels || !levels[form.industryLevel])
      errors.push(`历史行业数据不包含${label}行业，请重新下载或切换级别。`);
  }
  if (errors.length || !data) return { request: null, errors };
  return {
    request: {
      name,
      data,
      factors,
      backtest: { ...backtest, start: form.start, end: form.end },
      evaluation,
      ...(form.neutralization === "industry"
        ? {
            neutralization: {
              mode: "industry",
              industry_version:
                form.industryVersion || options.industry_data?.id,
              ...(form.industryLevel !== "L1"
                ? { industry_level: form.industryLevel }
                : {}),
            },
          }
        : {}),
    },
    errors: [],
  };
}

export function readDraft(): Partial<FormState> | null {
  try {
    const raw = window.localStorage.getItem(DRAFT_KEY);
    const parsed = raw ? (JSON.parse(raw) as unknown) : null;
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Partial<FormState>)
      : null;
  } catch {
    return null;
  }
}

export function saveDraft(form: FormState) {
  try {
    window.localStorage.setItem(DRAFT_KEY, JSON.stringify(form));
  } catch {
    /* drafts are a convenience only */
  }
}

export function clearDraft() {
  try {
    window.localStorage.removeItem(DRAFT_KEY);
  } catch {
    /* ignore */
  }
}

/** Apply a saved draft over fresh defaults, dropping anything no longer valid. */
export function mergeDraft(
  base: FormState,
  draft: Partial<FormState>,
  options: ResearchOptions,
): FormState {
  const merged: FormState = { ...base };
  for (const key of Object.keys(base) as Array<keyof FormState>) {
    const value = draft[key];
    if (value !== undefined && typeof value === typeof base[key])
      (merged as unknown as Record<string, unknown>)[key] = value;
  }
  if (Array.isArray(draft.factors)) {
    const known = new Set(options.factor_definitions.map((d) => d.name));
    merged.factors = draft.factors.filter(
      (row): row is FactorRow =>
        typeof row?.key === "string" && known.has(row.name),
    );
    for (const row of base.factors)
      if (!merged.factors.some((item) => item.name === row.name))
        merged.factors.push(row);
  }
  if (Array.isArray(draft.horizons))
    merged.horizons = draft.horizons.filter(Number.isInteger);
  const snapshot = snapshotOf(options, merged.snapshotId);
  if (!snapshot) {
    merged.snapshotId = base.snapshotId;
    merged.codes = base.codes;
  } else {
    const available = new Set(snapshot.codes.map((item) => item.code));
    merged.codes = (
      Array.isArray(draft.codes) ? draft.codes : base.codes
    ).filter((code) => available.has(code));
  }
  if (!industryLevels(options).includes(merged.industryLevel))
    merged.industryLevel = base.industryLevel;
  if (!historicalOption(options, merged.warehouseId))
    merged.warehouseId = base.warehouseId;
  if (merged.sourceMode === "index_warehouse" && !merged.warehouseId)
    merged.sourceMode = "snapshot";
  return merged;
}
