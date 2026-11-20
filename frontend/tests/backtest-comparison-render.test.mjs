import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";
import { load } from "./helpers/load.mjs";

const dom = new JSDOM('<div id="root"></div>', {
  url: "http://127.0.0.1:8000/compare",
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
  "location",
  "history",
]) {
  Object.defineProperty(globalThis, name, {
    value: name === "window" ? dom.window : dom.window[name],
    configurable: true,
    writable: true,
  });
}
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

const backtests = {
  items: [
    {
      id: "benchmark-report",
      name: "沪深300全收益对照",
      status: "succeeded",
      start_date: "2025-03-03",
      end_date: "2026-11-13",
      created_at: "2026-11-19T00:00:00+00:00",
    },
    {
      id: "market-qlib-source",
      name: "原始执行",
      status: "succeeded",
      start_date: "2025-03-03",
      end_date: "2026-11-13",
      created_at: "2026-11-19T00:00:00+00:00",
    },
  ],
  total: 2,
};

const side = (id, name, benchmark, excess, code) => ({
  id,
  name,
  status: "succeeded",
  start_date: "2025-03-03",
  end_date: "2026-11-13",
  created_at: "2026-11-19T00:00:00+00:00",
  benchmark: { code, name: benchmark, return_type: "gross_total_return", series_sha256: "aae48286" },
  metrics: { total_return: -0.16201678769416406, benchmark_return: benchmark === "全收益" ? 0.08187265424399648 : 0.032452314299515006, excess_return: excess, max_drawdown: -0.3257 },
  execution_run_id: id === "benchmark-report" ? "market-qlib-source" : null,
  performance_version: "daily-nav-performance-v3",
});

function comparison(baseline, candidate) {
  const forward = baseline === "benchmark-report";
  const first = forward
    ? side("benchmark-report", "沪深300全收益对照", "全收益", -0.24388944193816054, "H00300.CSI")
    : side("market-qlib-source", "原始执行", "价格", -0.19446910199367906, "000300.SH");
  const second = forward
    ? side("market-qlib-source", "原始执行", "价格", -0.19446910199367906, "000300.SH")
    : side("benchmark-report", "沪深300全收益对照", "全收益", -0.24388944193816054, "H00300.CSI");
  const direction = forward ? 1 : -1;
  return {
    baseline: first,
    candidate: second,
    direction: "candidate_minus_baseline",
    conclusion: "两次结果来自同一执行结果，只有评价基准不同",
    conclusion_states_cause: true,
    comparability: {
      comparable: true,
      interval: "same",
      metric_version: "same",
      reasons: [],
      baseline_interval: ["2025-03-03", "2026-11-13"],
      candidate_interval: ["2025-03-03", "2026-11-13"],
    },
    metrics: [
      { key: "total_return", label: "策略总收益", unit: "percent", baseline: -0.162, candidate: -0.162, delta: 0, comparable: true },
      {
        key: "benchmark_return",
        label: "基准收益",
        unit: "percent",
        baseline: first.metrics.benchmark_return,
        candidate: second.metrics.benchmark_return,
        delta: direction * 4.942033994448147,
        comparable: true,
      },
      {
        key: "excess_return",
        label: "收益差（超额收益）",
        unit: "percent",
        baseline: first.metrics.excess_return,
        candidate: second.metrics.excess_return,
        delta: -direction * 4.942033994448147,
        comparable: true,
      },
    ],
    dimensions: [
      {
        key: "benchmark",
        label: "评价基准",
        status: "different",
        fields: [
          { label: "基准代码", baseline: first.benchmark.code, candidate: second.benchmark.code, status: "different" },
          { label: "序列版本", baseline: "official-benchmark-close-v1", candidate: null, status: "unknown" },
        ],
      },
      {
        key: "factors",
        label: "因子与参数",
        status: "same",
        fields: [{ label: "因子与权重", baseline: "momentum 50%", candidate: "momentum 50%", status: "same" }],
      },
    ],
    relation: {
      status: "same_execution",
      execution_pointer: "market-qlib-source",
      evidence: [{ label: "派生报告记录的执行来源", value: "market-qlib-source" }],
      explanation:
        "两次结果来自同一执行结果，策略总收益完全相同；只有评价基准不同，已存基准收益差 +4.9420 个百分点，报表超额收益随之变化 -4.9420 个百分点。",
      derived_run_id: "benchmark-report",
      source_run_id: "market-qlib-source",
      benchmark_delta_pp: direction * 4.942033994448147,
      excess_delta_pp: -direction * 4.942033994448147,
    },
    not_implemented: ["最早评分差异或最早成交差异的定位：首版不扫描全量净值与成交明细"],
  };
}

const requested = [];
globalThis.fetch = async (url) => {
  const parsed = new URL(String(url), "http://127.0.0.1:8000");
  const path = parsed.pathname.replace("/api/v1", "");
  if (path === "/backtests")
    return new Response(JSON.stringify(backtests), { headers: { "Content-Type": "application/json" } });
  if (path === "/backtest-comparisons") {
    const baseline = parsed.searchParams.get("baseline_run_id");
    const candidate = parsed.searchParams.get("candidate_run_id");
    requested.push({ baseline, candidate });
    return new Response(JSON.stringify(comparison(baseline, candidate)), {
      headers: { "Content-Type": "application/json" },
    });
  }
  throw Error(`Unexpected request ${path}`);
};

const React = await import("react");
const { act } = React;
const { createRoot } = await import("react-dom/client");
const { ConfigProvider, App } = await import("antd");
const { page } = await load({ page: "./src/pages/BacktestComparisonPage" });

async function waitFor(check) {
  const start = Date.now();
  for (;;) {
    try {
      return check();
    } catch (error) {
      if (Date.now() - start > 5000) throw error;
      await act(() => new Promise((resolve) => setTimeout(resolve, 25)));
    }
  }
}

async function choose(selectIndex, optionText) {
  await waitFor(() => assert.ok(document.querySelectorAll(".ant-select").length >= 2));
  const selects = [...document.querySelectorAll(".ant-select")];
  const target = selects[selectIndex];
  assert.ok(target, `select #${selectIndex} exists`);
  await act(async () =>
    target.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })),
  );
  await waitFor(() =>
    assert.ok(document.querySelector(".ant-select-dropdown:not(.ant-select-dropdown-hidden)")),
  );
  const option = [...document.querySelectorAll(".ant-select-item-option")].find((node) =>
    node.textContent.includes(optionText),
  );
  assert.ok(option, `option ${optionText} is listed`);
  await act(async () => option.dispatchEvent(new MouseEvent("click", { bubbles: true })));
}

test("the comparison page shows the evidence, keeps unknown apart from equal and swaps sides", async () => {
  const root = createRoot(document.getElementById("root"));
  await act(async () =>
    root.render(
      React.createElement(ConfigProvider, null, React.createElement(App, null, React.createElement(page.default))),
    ),
  );
  try {
    await waitFor(() => assert.ok(document.body.textContent.includes("选择对照")));
    assert.ok(document.body.textContent.includes("尚未开始对照"));

    await choose(0, "沪深300全收益对照");
    await choose(1, "原始执行");
    const start = [...document.querySelectorAll("button")].find((node) =>
      node.textContent.includes("开始对照"),
    );
    assert.ok(start);
    await act(async () => start.dispatchEvent(new MouseEvent("click", { bubbles: true })));

    await waitFor(() => assert.ok(document.body.textContent.includes("两次结果来自同一执行结果，只有评价基准不同")));
    assert.deepEqual(requested, [{ baseline: "benchmark-report", candidate: "market-qlib-source" }]);
    const text = document.body.textContent;
    assert.ok(text.includes("+4.942 pp"), "benchmark delta is in percentage points with a sign");
    assert.ok(text.includes("-4.942 pp"), "excess delta points the other way");
    assert.ok(text.includes("0.000 pp"), "total return delta is exactly zero");
    // Unknown evidence shows an em dash for the missing side, never a zero.
    assert.ok(text.includes("序列版本"));
    assert.ok(text.includes("未知"));
    assert.ok(!text.includes("official-benchmark-close-v1 0"));

    // Swapping A/B asks the other way round and flips the reported direction.
    const swap = [...document.querySelectorAll("button")].find((node) =>
      node.textContent.includes("交换 A/B"),
    );
    assert.ok(swap);
    await act(async () => swap.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    await waitFor(() =>
      assert.deepEqual(requested.at(-1), {
        baseline: "market-qlib-source",
        candidate: "benchmark-report",
      }),
    );
    await waitFor(() => assert.ok(document.body.textContent.includes("-4.942 pp")));
  } finally {
    await act(async () => root.unmount());
  }
});
