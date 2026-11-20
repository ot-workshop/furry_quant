import assert from "node:assert/strict";
import { after, test } from "node:test";
import { JSDOM } from "jsdom";
import { load } from "./helpers/load.mjs";

const { charts } = await load({ charts: "./src/research/chart-options" });

const palette = {
  text: "#eee",
  muted: "#aaa",
  border: "#333",
  split: "#222",
  surface: "#141414",
  primary: "#5b8cff",
  series: ["#5b8cff", "#aaa", "#a58bff", "#ffa940"],
};

test("horizontalIntervalOption renders interval segment, mean point, and zero reference", () => {
  const option = charts.horizontalIntervalOption(-0.064883, -0.102766, -0.027, palette);
  assert.equal(option.xAxis.type, "value");
  assert.ok(option.xAxis.min <= -0.102766);
  assert.ok(option.xAxis.max >= 0);
  assert.equal(option.series.length, 2);
  assert.equal(option.series[0].type, "line");
  assert.deepEqual(option.series[0].data, [
    [-0.102766, "Rank IC"],
    [-0.027, "Rank IC"],
  ]);
  assert.deepEqual(option.series[0].markLine.data, [{ xAxis: 0 }]);
  assert.equal(option.series[1].type, "scatter");
  assert.deepEqual(option.series[1].data, [[-0.064883, "Rank IC"]]);
});

test("FactorEvidencePanel keeps null raw availability fields and partial date coverage as unknown instead of zero", async () => {
  const dom = new JSDOM('<div id="ev-root"></div>', { url: "http://127.0.0.1:8000/", pretendToBeVisual: true });
  for (const name of ["window", "document", "navigator", "HTMLElement", "Element", "Node", "Event", "MouseEvent", "KeyboardEvent", "getComputedStyle", "SVGElement", "ShadowRoot"])
    Object.defineProperty(globalThis, name, { value: name === "window" ? dom.window : dom.window[name], configurable: true, writable: true });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  dom.window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
  globalThis.matchMedia = dom.window.matchMedia;
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
  dom.window.ResizeObserver = globalThis.ResizeObserver;
  dom.window.scrollTo = () => {};
  globalThis.requestAnimationFrame = (cb) => setTimeout(cb, 0);
  globalThis.cancelAnimationFrame = (id) => clearTimeout(id);

  const React = await import("react");
  const { act } = React;
  const { createRoot } = await import("react-dom/client");
  const { panel } = await load({ panel: "./src/evaluation/FactorEvidencePanel" });

  const prevFetch = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        factor: "value",
        horizon: 10,
        status: "no_valid_observations",
        status_label: "无有效观测",
        status_reason: "所选因子在有效信号日内无有效成熟观测。",
        range: { start: "2025-01-02", end: "2025-02-05", cutoff: "2025-02-20", latest_exit_date: null },
        lag: { method: "newey_west_bartlett", lag: 9, default_lag: 9, is_shorter_than_default: false, min_dates_required: 20, finite_sample_correction: "n*(n-1)", confidence_level: 0.95, z_critical: 1.959964 },
        counts: { signal_dates: 25, valid_rank_ic_dates: 0, matured_dates: 0, unmatured_dates: 0, missing_exit_date_dates: 0, excluded_matured_dates: 25, missing_daily_rows: 0, lag_pairs: 0, paired_observations_on_valid_ic_days: 0 },
        rank_ic: { mean: null, sample_std: null, se: null, ci_low: null, ci_high: null, variance: null },
        exclusions: { ic: [], labels: [] },
        label_coverage: { scored_stock_days: 100, valid_stock_days: 100, excluded_stock_days: 0 },
        raw_availability: {
          available: true,
          coverage_status: "partial",
          factor_run_id: "factors-partial",
          matched_signal_dates: 1,
          total_signal_dates: 25,
          universe_stock_days: 4,
          eligible_stock_days: 4,
          raw_valid_stock_days: null,
          raw_missing_stock_days: null,
          imputed_stock_days: null,
          not_applicable_stock_days: null,
          not_applicable_scored_stock_days: null,
          missing_policy: null,
          component_imputations: [],
        },
        common_sample: { available: false, policy: null, sha256: null, label: "未知" },
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );

  const rootEl = dom.window.document.getElementById("ev-root");
  const root = createRoot(rootEl);
  try {
    await act(async () => {
      root.render(
        React.createElement(panel.FactorEvidencePanel, {
          horizon: "10",
          result: {
            metadata: { id: "eval-partial", name: "partial", is_synthetic: true, created_at: "2026-11-20T00:00:00Z", start: "2025-01-02", end: "2025-02-20", sample_size: 4, data_version: "v1", factor_run_id: "factors-partial", evaluation: { horizons: [10], groups: 3, min_cross_section: 3 }, provenance: {} },
            methodology: { ic_engine: "", label: "", signal_range: "", groups: "", statistics: "", directions: {}, limitations: [] },
            signal_coverage: [],
            labels: {},
            factors: { value: { horizons: { "10": { ic_mean: null, ic_ir: null, ic_positive_rate: null, rank_ic_mean: null, rank_ic_ir: null, rank_ic_positive_rate: null, long_short_mean: null, daily: [], groups: [] } } } },
            correlations: { method: "daily Spearman", pairs: [] },
          },
        }),
      );
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 80));
    });
    const text = rootEl.textContent || "";
    assert.ok(text.includes("1 / 25 交易日"), text);
    assert.ok(text.includes("未知（旧结果未保存原始观测字段）"), text);
    assert.ok(text.includes("未知（未保存填补字段）"), text);
    assert.ok(!text.includes("0 只·日（无填补规则）"), text);
  } finally {
    await act(async () => {
      root.unmount();
    });
    globalThis.fetch = prevFetch;
    dom.window.close();
  }
});

after(() => {});
