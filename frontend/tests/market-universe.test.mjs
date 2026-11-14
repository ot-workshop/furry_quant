import assert from "node:assert/strict";
import { test } from "node:test";
import { load } from "./helpers/load.mjs";
import { options, tradingDates } from "./fixtures.mjs";

const { form: f } = await load({ form: "./src/research/form" });

function marketOptions() {
  const value = options();
  value.market_universes = [{
    id: "market-saved", label: "沪深A股全市场", full_market: true,
    start: "2025-01-02", end: "2025-06-30", data_start: "2025-01-02",
    trading_dates: tradingDates("2025-01-02", "2025-06-30"), codes_count: 5500,
    financial_coverage: { covered: 300, total: 5500 },
    st_coverage: { covered: 5500, total: 5500 }, listing_coverage: { covered: 5500, total: 5500 },
    codes: ["600000.SH", "000001.SZ", "688001.SH"],
  }];
  return value;
}

test("dynamic universe preserves filter values, factor weights, windows and rebalance frequency", () => {
  const opts = marketOptions();
  const form = { ...f.defaultForm(opts), sourceMode: "market_warehouse", marketWarehouseId: "market-saved",
    universeFilters: { ...f.defaultUniverseFilters(), min_listing_days: 120, min_market_cap: 3e9,
      max_market_cap: 9e10, min_avg_amount: 5e7, amount_window: 40, boards: ["main", "star"], exclude_st: false,
      industries: ["801780.SI"], industry_version: opts.industry_data.id },
    topk: 30, rebalanceFrequency: "weekly", filtersEnabled: true,
  };
  const built = f.buildRequest(form, opts);
  assert.deepEqual(built.errors, []);
  assert.equal(built.request.data.mode, "market_warehouse");
  assert.deepEqual(built.request.data.filters, form.universeFilters);
  assert.equal(built.request.backtest.topk, 30);
  assert.equal(built.request.backtest.rebalance_frequency, "weekly");
  const restored = f.formFromRequest(built.request, opts);
  assert.deepEqual(restored.warnings, []);
  assert.deepEqual(f.buildRequest(restored.form, opts).request, built.request);
});

test("liquidity window participates in warmup and invalid ranges block submission", () => {
  const opts = marketOptions();
  const form = { ...f.defaultForm(opts), sourceMode: "market_warehouse", marketWarehouseId: "market-saved",
    filtersEnabled: true,
    universeFilters: { ...f.defaultUniverseFilters(), min_avg_amount: 1e7, amount_window: 60 },
  };
  assert.equal(f.requiredHistory(form, opts.factor_definitions), 60);
  assert.match(f.buildRequest(form, opts).errors.join(), /历史预热不足/);
  const repaired = { ...form, start: f.earliestStart(form, opts) };
  assert.deepEqual(f.buildRequest(repaired, opts).errors, []);
  const invalid = { ...repaired, universeFilters: { ...form.universeFilters, min_market_cap: 2e9, max_market_cap: 1e9 } };
  assert.match(f.buildRequest(invalid, opts).errors.join(), /下限不能大于上限/);
});

test("stock pool and common filters stay independent across all three sources", () => {
  const opts = marketOptions();
  opts.historical_indices = [{ ...opts.market_universes[0], id: "index-saved", codes_count: 300 }];
  Object.assign(opts.snapshots[0], { st_coverage: { covered: 3, total: 3 }, listing_coverage: { covered: 3, total: 3 } });
  const base = { ...f.defaultForm(opts), filtersEnabled: true,
    universeFilters: { ...f.defaultUniverseFilters(), min_listing_days: 30, amount_window: 25, min_avg_amount: 1e6 } };
  for (const sourceMode of ["snapshot", "index_warehouse", "market_warehouse"]) {
    const form = { ...base, sourceMode, warehouseId: "index-saved", marketWarehouseId: "market-saved" };
    const built = f.buildRequest(form, opts);
    assert.deepEqual(built.errors, []);
    assert.deepEqual(built.request.data.filters, base.universeFilters);
    const restored = f.formFromRequest(built.request, opts);
    assert.equal(restored.form.sourceMode, sourceMode);
    assert.equal(restored.form.filtersEnabled, true);
    assert.deepEqual(f.buildRequest(restored.form, opts).request, built.request);
    const disabled = f.buildRequest({ ...form, filtersEnabled: false }, opts);
    assert.equal("filters" in disabled.request.data, false);
    assert.equal(f.formFromRequest(disabled.request, opts).form.filtersEnabled, false);
  }
});

test("custom stocks use shared warehouse data, roundtrip and survive draft restore", () => {
  const opts = marketOptions();
  const form = { ...f.defaultForm(opts), sourceMode: "snapshot", snapshotId: "market-saved",
    codes: ["000001.SZ", "688001.SH"], filtersEnabled: true };
  const built = f.buildRequest(form, opts);
  assert.deepEqual(built.errors, []);
  assert.equal(built.request.data.mode, "market_warehouse");
  assert.deepEqual(built.request.data.codes, form.codes);
  assert.equal(f.poolSize(form, opts), 2);
  assert.equal(f.customStocks(form, opts).length, 3);
  assert.deepEqual(f.dataRange(form, opts), { start: "2025-01-02", end: "2025-06-30", dataStart: "2025-01-02" });
  const restored = f.formFromRequest(built.request, opts);
  assert.equal(restored.form.sourceMode, "snapshot");
  assert.equal(restored.form.snapshotId, "market-saved");
  assert.deepEqual(f.buildRequest(restored.form, opts).request, built.request);
  const draft = f.mergeDraft(f.defaultForm(opts), form, opts);
  assert.deepEqual(draft.codes, form.codes);
  assert.equal(draft.snapshotId, "market-saved");
  const invalid = f.buildRequest({ ...form, codes: ["600000.SH", "999999.SH"] }, opts);
  assert.match(invalid.errors.join(), /覆盖范围/);
});

test("missing historical state blocks the relevant filters but never disables other conditions", () => {
  const opts = marketOptions();
  const form = { ...f.defaultForm(opts), filtersEnabled: true };
  assert.match(f.buildRequest(form, opts).errors.join(), /历史名称|历史上市日期/);
  const adjusted = { ...form, universeFilters: { ...form.universeFilters, exclude_st: false, min_listing_days: 0, min_avg_amount: 1e6 } };
  assert.deepEqual(f.buildRequest(adjusted, opts).errors, []);
  assert.equal(f.buildRequest(adjusted, opts).request.data.filters.min_avg_amount, 1e6);
});

test("market-only data is usable and stale versions do not silently switch to another dataset", () => {
  const opts = marketOptions();
  opts.snapshots = [];
  const form = f.defaultForm(opts);
  assert.equal(form.sourceMode, "market_warehouse");
  assert.equal(f.poolSize(form, opts), 5500);
  assert.deepEqual(f.financialCoverage(form, opts), { covered: 300, total: 5500 });
  assert.deepEqual(f.buildRequest(form, opts).errors, []);
  assert.equal(f.marketOption(opts, "not-available"), undefined);
  const request = f.buildRequest(form, opts).request;
  request.data.warehouse_id = "not-available";
  const restored = f.formFromRequest(request, opts);
  assert.match(restored.warnings.join(), /不可用/);
  assert.equal(f.buildRequest(restored.form, opts).request, null);
});
