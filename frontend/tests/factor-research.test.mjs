import assert from "node:assert/strict";
import { test } from "node:test";
import { load } from "./helpers/load.mjs";
import { options, tradingDates } from "./fixtures.mjs";

const { form, experiments, charts } = await load({
  form: "./src/research/form",
  experiments: "./src/research/experiments",
  charts: "./src/research/chart-options",
});

function workspace(mode = "snapshot") {
  const config = options();
  const pool = {
    id: "pool-test",
    label: "动态测试",
    start: "2025-02-03",
    end: "2025-06-30",
    data_start: "2025-01-02",
    codes_count: 55,
    codes: ["600000.SH", "000001.SZ", "600519.SH"],
    trading_dates: tradingDates("2025-01-02", "2025-06-30"),
    financial_coverage: { covered: 3, total: 55 },
    listing_coverage: { covered: 55, total: 55 },
    st_coverage: { covered: 55, total: 55 },
  };
  config.historical_indices = [pool];
  config.market_universes = [pool];
  const state = {
    ...form.defaultForm(config),
    sourceMode: mode,
    warehouseId: pool.id,
    marketWarehouseId: pool.id,
    start: "2025-03-03",
    end: "2025-06-30",
    filtersEnabled: false,
    neutralization: "off",
    horizons: [1, 3],
  };
  const candidates = ["momentum", "value"].map((name, index) => ({
    id: `plan_${index}`,
    name: name,
    factors: form.factorRows(
      [{ name, weight: 1, ...(name === "momentum" ? { window: 5 } : {}) }],
      config.factor_definitions,
    ),
  }));
  return {
    config,
    state,
    candidates,
    splits: { validation: "2025-04-01", test: "2025-05-15" },
  };
}

for (const mode of ["snapshot", "index_warehouse", "market_warehouse"]) {
  test(`comparison payload preserves ${mode} pool, dates, costs and candidate windows`, () => {
    const { config, state, candidates, splits } = workspace(mode);
    const built = experiments.buildExperiment(
      state,
      config,
      candidates,
      splits,
    );
    assert.ok(built.request, built.errors.join("\n"));
    assert.equal(built.request.data.mode, mode);
    assert.equal(built.request.plan.candidates[0].factors[0].window, 5);
    assert.equal(built.request.plan.candidates[1].factors[0].weight, 1);
    assert.equal(
      built.request.plan.periods.validation.start,
      splits.validation,
    );
    assert.equal(built.request.plan.periods.development.end, "2025-03-31");
    assert.equal(built.request.backtest.initial_cash, state.initialCash);
    assert.equal("start" in built.request.backtest, false);
  });
}

test("dynamic comparison preserves shared filters and neutralization while changing weights", () => {
  const { config, state, candidates, splits } = workspace("market_warehouse");
  state.filtersEnabled = true;
  state.universeFilters = {
    ...state.universeFilters,
    exclude_st: false,
    min_listing_days: 0,
    min_market_cap: 3e9,
  };
  state.neutralization = "industry";
  candidates[0].factors = form.factorRows(
    [
      { name: "momentum", window: 10, weight: 0.4 },
      { name: "value", weight: 0.6 },
    ],
    config.factor_definitions,
  );
  const built = experiments.buildExperiment(state, config, candidates, splits);
  assert.ok(built.request, built.errors.join("\n"));
  assert.equal(built.request.data.filters.min_market_cap, 3e9);
  assert.equal(built.request.neutralization.mode, "industry");
  assert.equal(
    built.request.neutralization.industry_version,
    config.industry_data.id,
  );
  assert.deepEqual(
    built.request.plan.candidates[0].factors.map((factor) => factor.weight),
    [0.4, 0.6],
  );
});

test("a longer candidate window and invalid split days prevent submission", () => {
  const { config, state, candidates, splits } = workspace("index_warehouse");
  candidates[0].factors.find((row) => row.name === "momentum").window = 126;
  const tooEarly = experiments.buildExperiment(
    state,
    config,
    candidates,
    splits,
  );
  assert.equal(tooEarly.request, null);
  assert.match(tooEarly.errors.join(" "), /预热|历史不足/);
  candidates[0].factors.find((row) => row.name === "momentum").window = 5;
  const invalid = experiments.buildExperiment(state, config, candidates, {
    ...splits,
    validation: "2025-04-05",
  });
  assert.equal(invalid.request, null);
  assert.match(invalid.errors.join(" "), /交易日/);
});

const palette = {
  text: "#eee",
  muted: "#aaa",
  border: "#333",
  split: "#222",
  surface: "#141414",
  primary: "#5b8cff",
  series: ["#5b8cff", "#aaa", "#a58bff", "#ffa940"],
};

test("heatmap renders complete aligned matrix, keeps real zero, and patterns uncomputable cells", () => {
  const option = charts.correlationChartOption(
    ["a", "b", "c"],
    [
      { first: "a", second: "a", value: 1, valid_dates: 55 },
      { first: "a", second: "b", value: 0, valid_dates: 55 },
      { first: "b", second: "b", value: 1, valid_dates: 55 },
      { first: "c", second: "c", value: null, valid_dates: 0 },
    ],
    { a: "动量", b: "估值", c: "常数" },
    palette,
  );
  assert.equal(option.visualMap.seriesIndex, 0);
  assert.equal(option.visualMap.min, -1);
  assert.equal(option.visualMap.max, 1);
  assert.equal(option.visualMap.inRange.color[1], palette.surface);
  assert.deepEqual(option.xAxis.data, ["a", "b", "c"]);
  assert.deepEqual(option.yAxis.data, ["a", "b", "c"]);
  assert.equal(option.yAxis.inverse, true);
  assert.equal(option.series.length, 2);
  assert.equal(option.series[0].data.length + option.series[1].data.length, 9);

  const zeroCell = option.series[0].data.find(
    (item) => item.first === "a" && item.second === "b",
  );
  assert.deepEqual(zeroCell.value, [0, 1, 0]);
  assert.equal(zeroCell.noSample, false);
  assert.match(
    option.tooltip.formatter({ data: zeroCell }),
    /相关性：0\.0000\n有效日期：55/,
  );

  const missingCell = option.series[1].data.find(
    (item) => item.first === "c" && item.second === "c",
  );
  assert.deepEqual(missingCell.value, [2, 2, 0]);
  assert.equal(missingCell.noSample, true);
  assert.equal(option.series[1].itemStyle.borderType, "dashed");
  assert.ok(option.series[1].itemStyle.decal);
  assert.equal(option.series[1].label.formatter(), "—");
  assert.match(
    option.tooltip.formatter({ data: missingCell }),
    /无法计算相关系数（有效日期不足，或因子在区间内没有变化）\n有效日期：0/,
  );
});

test("chart metrics use backend fields without connecting missing values or annualizing IR", () => {
  const points = [
    {
      date: "2025-01-02",
      ic: 0.1,
      rank_ic: 0.2,
      ic_mean: null,
      rank_ic_mean: null,
      ic_ir: null,
      rank_ic_ir: null,
    },
    {
      date: "2025-01-03",
      ic: null,
      rank_ic: null,
      ic_mean: 0.1,
      rank_ic_mean: 0.2,
      ic_ir: 1.5,
      rank_ic_ir: 2.5,
    },
  ];
  const daily = charts.icChartOption(points, "daily", palette);
  const rolling = charts.icChartOption(points, "mean", palette);
  const ir = charts.icChartOption(points, "ir", palette);
  assert.deepEqual(daily.series[0].data, [0.1, null]);
  assert.deepEqual(rolling.series[1].data, [null, 0.2]);
  assert.deepEqual(ir.series[1].data, [null, 2.5]);
  assert.equal(ir.series[1].connectNulls, false);
  assert.equal(daily.yAxis.min, -1);
  assert.equal(ir.yAxis.min, undefined);
});
