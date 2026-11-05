const FACTOR_LABELS: Record<string, string> = {
  momentum: "动量",
  value: "盈利收益率",
  quality: "质量",
  volatility: "低波动",
  book_to_price: "账面市值比",
  size: "小市值",
  reversal: "短期反转",
  medium_momentum: "中期动量",
  downside_volatility: "下行波动率",
  max_drawdown: "滚动最大回撤",
  amount_anomaly: "成交额异动",
  composite: "综合分",
};

const METRIC_LABELS: Record<string, string> = {
  total_return: "总收益",
  annualized_return: "年化收益",
  benchmark_return: "基准收益",
  excess_return: "收益差",
  relative_return: "相对净值收益",
  tracking_error: "年化跟踪误差",
  information_ratio: "信息比率",
  annualized_active_return: "主动收益（年化）",
  max_drawdown: "最大回撤",
  volatility: "年化波动率",
  sharpe_ratio: "夏普比率",
  calmar_ratio: "卡玛比率",
  profit_loss_ratio: "日收益盈亏比",
  turnover: "换手率",
  total_cost: "费用 / 初始资金",
  total_cost_amount: "费用金额（元）",
  avg_turnover: "日均换手率",
  cost: "费用",
  win_rate: "日收益胜率",
  trade_count: "成交笔数",
  total_days: "交易日数",
  years: "折算年数",
};

const PERCENT_METRICS = new Set([
  "total_return",
  "annualized_return",
  "benchmark_return",
  "excess_return",
  "relative_return",
  "tracking_error",
  "annualized_active_return",
  "max_drawdown",
  "volatility",
  "turnover",
  "avg_turnover",
  "total_cost",
  "cost",
  "win_rate",
]);

const COUNT_METRICS = new Set(["trade_count", "total_days"]);
const RATIO_METRICS = new Set([
  "sharpe_ratio",
  "calmar_ratio",
  "profit_loss_ratio",
  "information_ratio",
]);

/** Metrics whose sign is meaningful for up/down colouring. */
export const SIGNED_METRICS = new Set([
  "total_return",
  "annualized_return",
  "benchmark_return",
  "excess_return",
  "relative_return",
  "annualized_active_return",
]);

export const REBALANCE_LABELS: Record<string, string> = {
  daily: "每日",
  weekly: "每周首个交易日",
  monthly: "每月首个交易日",
};

export function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function formatNumber(
  value: number | null | undefined,
  digits = 2,
): string {
  if (!isFiniteNumber(value)) return "—";
  return value.toLocaleString("zh-CN", {
    minimumFractionDigits: 0,
    maximumFractionDigits: digits,
  });
}

export function formatMoney(value: number | null | undefined): string {
  if (!isFiniteNumber(value)) return "—";
  return value.toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function formatPercent(
  value: number | null | undefined,
  digits = 2,
): string {
  if (!isFiniteNumber(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export function formatWeight(value: number | null | undefined): string {
  if (!isFiniteNumber(value)) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

export function factorLabel(
  key: string,
  definitions?: Record<string, { label: string; name: string }>,
): string {
  const definition = definitions?.[key];
  if (definition)
    return definition.label + (definition.name !== key ? ` [${key}]` : "");
  return FACTOR_LABELS[key] ?? key;
}

export function metricLabel(key: string): string {
  return METRIC_LABELS[key] ?? key;
}

export function formatMetricValue(
  key: string,
  value: number | null | undefined,
): string {
  if (!isFiniteNumber(value)) return "—";
  if (COUNT_METRICS.has(key)) return Math.round(value).toLocaleString("zh-CN");
  if (PERCENT_METRICS.has(key)) return formatPercent(value);
  if (RATIO_METRICS.has(key)) return value.toFixed(2);
  if (key === "total_cost_amount") return formatMoney(value);
  return formatNumber(value, 4);
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function formatDuration(
  start: string | null,
  end: string | null,
): string {
  if (!start) return "—";
  const ms = (end ? Date.parse(end) : Date.now()) - Date.parse(start);
  if (!Number.isFinite(ms) || ms < 0) return "—";
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds} 秒`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分 ${seconds % 60} 秒`;
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
}

export function tradeSideLabel(side: string): string {
  return side === "buy" ? "买入" : side === "sell" ? "卖出" : side;
}

export function scoreUnitLabel(unit: string | undefined): string {
  if (!unit) return "评分";
  if (unit === "percentile_0_100") return "0–100 百分位";
  return unit;
}

export function dateText(value: unknown): string {
  if (typeof value !== "string") return "";
  const compact = /^(\d{4})(\d{2})(\d{2})$/.exec(value);
  return compact
    ? `${compact[1]}-${compact[2]}-${compact[3]}`
    : value.slice(0, 10);
}

export function toTextList(value: unknown): string[] {
  if (typeof value === "string") return value.trim() ? [value.trim()] : [];
  if (Array.isArray(value))
    return value.filter(
      (item): item is string =>
        typeof item === "string" && Boolean(item.trim()),
    );
  return [];
}
