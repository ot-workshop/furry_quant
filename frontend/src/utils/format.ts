const FACTOR_LABELS: Record<string, string> = {
  momentum: "动量",
  value: "价值",
  quality: "质量",
  volatility: "低波动",
  book_to_price: "账面价值收益率",
  size: "小市值",
  reversal: "短期反转",
  medium_momentum: "中期动量",
  downside_volatility: "下行波动率",
  max_drawdown: "滚动最大回撤",
  amount_anomaly: "成交额异动",
  composite: "综合",
};

const OVERVIEW_MODE_LABELS: Record<string, string> = {
  real: "真实行情",
  synthetic: "合成样本",
  mixed: "混合数据",
  empty: "暂无数据",
};

const METRIC_LABELS: Record<string, string> = {
  total_return: "总收益",
  annualized_return: "年化收益",
  benchmark_return: "基准收益",
  excess_return: "累计收益差（百分点）",
  relative_return: "相对净值收益",
  tracking_error: "年化跟踪误差",
  information_ratio: "信息比率",
  annualized_active_return: "日主动收益均值年化",
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
  total_days: "回测交易日数",
  years: "折算交易年数",
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
  "benchmark_max_drawdown",
  "volatility",
  "turnover",
  "avg_turnover",
  "total_cost",
  "cost",
  "win_rate",
]);

const COUNT_METRICS = new Set([
  "trade_count",
  "order_count",
  "num_trades",
  "num_orders",
  "total_days",
]);

const STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  succeeded: "已完成",
  failed: "失败",
  interrupted: "已中断",
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

export function factorLabel(key: string, definitions?: Record<string, {label:string;name:string}>): string {
  const definition = definitions?.[key];
  if (definition) return definition.label + (definition.name !== key ? ` [${key}]` : '');
  return FACTOR_LABELS[key] ?? key;
}

export function overviewModeLabel(mode: string | null | undefined): string {
  if (!mode) return "模式未知";
  const key = mode.trim().toLowerCase();
  return OVERVIEW_MODE_LABELS[key] ?? mode;
}

export function metricLabel(key: string): string {
  return METRIC_LABELS[key] ?? key;
}

export function formatMetricValue(
  key: string,
  value: number | null | undefined,
): string {
  if (!isFiniteNumber(value)) return "—";
  if (key === 'excess_return') return `${(value * 100).toFixed(2)}个百分点`;
  if (COUNT_METRICS.has(key)) {
    return Math.round(value).toLocaleString("zh-CN");
  }
  if (PERCENT_METRICS.has(key)) {
    return formatPercent(value);
  }
  if (["sharpe_ratio", "calmar_ratio", "profit_loss_ratio", "information_ratio"].includes(key)) return value.toFixed(2);
  if (["years", "total_cost_amount"].includes(key)) return formatNumber(value, 2);
  return formatNumber(value, 4);
}

export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

export function statusClass(status: string): string {
  return STATUS_LABELS[status] ? `status-${status}` : "status-unknown";
}

export function scoreUnitLabel(unit: string | undefined): string {
  if (!unit) return "后端评分口径";
  if (unit === "percentile_0_100") return "0–100 排序百分位";
  return unit;
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

export function tradeSideLabel(side: string): string {
  if (side === "buy") return "买入";
  if (side === "sell") return "卖出";
  return side;
}
