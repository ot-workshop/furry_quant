export function tradingDates(start, end) {
  const dates = [];
  for (let day = new Date(`${start}T00:00:00Z`); day <= new Date(`${end}T00:00:00Z`); day.setUTCDate(day.getUTCDate() + 1))
    if (![0, 6].includes(day.getUTCDay())) dates.push(day.toISOString().slice(0, 10));
  return dates;
}

export function options() {
  return {
    defaults: {
      name: "我的研究",
      data: { mode: "snapshot", snapshot_id: "shared-a", codes: ["600000.SH", "000001.SZ", "600036.SH"] },
      factors: [
        { name: "momentum", weight: 0.5, window: 20 },
        { name: "value", weight: 0.5, window: null },
      ],
      backtest: {
        start: "2025-03-03",
        end: "2025-06-30",
        initial_cash: 100000,
        topk: 2,
        n_drop: 1,
        risk_degree: 0.9,
        open_cost: 0.001,
        close_cost: 0.001,
        min_cost: 5,
        rebalance_frequency: "daily",
        cost_mode: "legacy",
        commission_rate: 0.0003,
        tax_mode: "historical",
        stamp_tax_rate: 0.0005,
        transfer_fee_rate: 0.00001,
        impact_cost: 0.0005,
      },
      evaluation: { horizons: [1, 5], groups: 3, min_cross_section: 3 },
    },
    limits: { max_codes: 50, max_queued_tasks: 20 },
    benchmarks: [{ code: "000300.SH", name: "沪深300" }],
    comparison_benchmarks: [{ code: "000300.SH", name: "沪深300" }],
    factor_definitions: [
      { name: "momentum", label: "动量", description: "过去收益", default_window: 20, default_skip: 0 },
      { name: "value", label: "盈利收益率", description: "1/PE", default_window: null },
      { name: "volatility", label: "低波动", description: "波动率", default_window: 10 },
      {
        name: "roe_ttm",
        label: "净资产收益率",
        description: "TTM 归母净利润 / 平均归母权益",
        default_window: null,
        family: "fundamental",
        family_label: "基本面",
        direction: "higher",
        requires_financials: true,
      },
    ],
    snapshots: [
      {
        id: "shared-a",
        label: "官方共享行情 · 3只",
        start: "2025-01-02",
        end: "2025-06-30",
        trading_days: tradingDates("2025-01-02", "2025-06-30").length,
        trading_dates: tradingDates("2025-01-02", "2025-06-30"),
        is_subset: false,
        benchmark: "000300.SH",
        validation_mode: "fallback",
        source: "official_tushare_shared",
        financial_coverage: { covered: 3, total: 3 },
        codes: [
          { code: "600000.SH", name: "浦发银行" },
          { code: "000001.SZ", name: "平安银行" },
          { code: "600036.SH", name: "招商银行" },
        ],
      },
    ],
    historical_indices: [],
    industry_data: null,
    financial_data: {
      version: "a".repeat(24),
      codes: 3,
      rows: 36,
      quarantined: 0,
      latest_report_period: "2025-03-31",
      earliest_report_period: "2022-12-31",
      calendar_start: "2022-01-04",
      calendar_end: "2025-12-31",
      policy: "test policy",
      point_in_time_certified: false,
    },
  };
}
