import assert from "node:assert/strict";
import { after, test } from "node:test";
import { JSDOM } from "jsdom";
import { load } from "./helpers/load.mjs";

const dom = new JSDOM('<div id="root"></div>', {
  url: "http://127.0.0.1:8000/",
  pretendToBeVisual: true,
});
for (const name of [
  "window",
  "document",
  "navigator",
  "HTMLElement",
  "Element",
  "Node",
  "Event",
  "MouseEvent",
  "KeyboardEvent",
  "getComputedStyle",
  "SVGElement",
  "ShadowRoot",
])
  Object.defineProperty(globalThis, name, {
    value: name === "window" ? dom.window : dom.window[name],
    configurable: true,
    writable: true,
  });
const computed = dom.window.getComputedStyle.bind(dom.window);
dom.window.getComputedStyle = (element) => computed(element);
globalThis.getComputedStyle = dom.window.getComputedStyle;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
dom.window.matchMedia = () => ({
  matches: false,
  addEventListener() {},
  removeEventListener() {},
  addListener() {},
  removeListener() {},
});
globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
};
dom.window.ResizeObserver = globalThis.ResizeObserver;
dom.window.scrollTo = () => {};
globalThis.requestAnimationFrame = (callback) => setTimeout(callback, 0);
globalThis.cancelAnimationFrame = (id) => clearTimeout(id);

const React = await import("react");
const { act } = React;
const { createRoot } = await import("react-dom/client");
const { ConfigProvider, App } = await import("antd");
const { page } = await load({ page: "./src/pages/ExperimentDetailPage" });

let impactCost;
const selection = {
  candidate_id: "a",
  score: 0.01,
  metric: "validation_excess_return",
  ranking: [{ candidate_id: "a", score: 0.01 }],
  tie_break: "candidate_id_ascending",
};
const result = {
  metadata: {
    id: "split-display",
    name: "显示回归",
    created_at: "2026-11-20T00:00:00Z",
    data_version: "fixture",
    dataset_id: null,
    is_synthetic: false,
    selected_candidate: "a",
    validation_kind: "retrospective_split",
  },
  plan: {
    name: "显示回归",
    selection_metric: "validation_excess_return",
    periods: {
      development: { start: "2025-01-02", end: "2025-01-17" },
      validation: { start: "2025-01-20", end: "2025-02-04" },
      test: { start: "2025-02-05", end: "2025-02-28" },
    },
    candidates: [
      {
        id: "a",
        name: "方案",
        factors: [{ name: "momentum", weight: 1, window: 5 }],
      },
      { id: "b", name: "对照", factors: [{ name: "value", weight: 1 }] },
    ],
  },
  selection,
  shared_parameters: {
    universe_label: "固定池",
    codes: ["600000.SH", "000001.SZ"],
    benchmark: "000300.SH",
    backtest: {
      initial_cash: 100000,
      topk: 1,
      n_drop: 1,
      risk_degree: 0.9,
      open_cost: 0.001,
      close_cost: 0.001,
      min_cost: 5,
      rebalance_frequency: "daily",
    },
  },
  rows: [],
  limitations: [],
};

const metrics = {
  total_return: 0,
  benchmark_return: 0,
  excess_return: 0,
  max_drawdown: 0,
  total_cost_amount: 0,
  avg_turnover: 0,
};
result.rows = [
  { candidate_id: "a", period: "validation", trading_days: 2, metrics },
  { candidate_id: "b", period: "validation", trading_days: 2, metrics },
  { candidate_id: "a", period: "test", trading_days: 2, metrics },
];

globalThis.fetch = async (url) => {
  const path = String(url).replace("/api/v1", "");
  if (path === "/experiments/split-display")
    return new Response(JSON.stringify(result), {
      headers: { "Content-Type": "application/json" },
    });
  if (path.endsWith("/equity") || path.includes("/positions"))
    return new Response(JSON.stringify({ items: [], total: 0 }), {
      headers: { "Content-Type": "application/json" },
    });
  throw Error(`Unexpected request ${path}`);
};

async function waitFor(check) {
  const start = Date.now();
  for (;;) {
    try {
      return check();
    } catch (error) {
      if (Date.now() - start > 5000) throw error;
      await act(() => new Promise((resolve) => setTimeout(resolve, 30)));
    }
  }
}

async function renderCase() {
  const root = createRoot(document.getElementById("root"));
  await act(async () =>
    root.render(
      React.createElement(
        ConfigProvider,
        null,
        React.createElement(
          App,
          null,
          React.createElement(page.default, { id: "split-display" }),
        ),
      ),
    ),
  );
  await waitFor(() =>
    assert.ok(document.body.textContent.includes("冲击成本")),
  );
  const labels = [...document.querySelectorAll(".ant-descriptions-item-label")];
  const label = labels.find((node) => node.textContent.trim() === "冲击成本");
  assert.ok(label);
  const value = label
    .closest(".ant-descriptions-item")
    ?.querySelector(".ant-descriptions-item-content");
  return {
    text: value?.textContent.trim(),
    close: () => act(async () => root.unmount()),
  };
}

test("missing impact cost renders as missing", async () => {
  const view = await renderCase();
  try {
    assert.equal(view.text, "—");
  } finally {
    await view.close();
  }
});

test("real zero impact cost stays zero", async () => {
  result.shared_parameters.backtest.impact_cost = 0;
  const view = await renderCase();
  try {
    assert.equal(view.text, "0.000%");
  } finally {
    await view.close();
  }
});

after(() => {
  dom.window.close();
  setTimeout(() => process.exit(), 200).unref();
});
