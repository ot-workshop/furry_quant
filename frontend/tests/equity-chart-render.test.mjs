import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { after, test } from "node:test";
import { JSDOM } from "jsdom";
import { load } from "./helpers/load.mjs";

const dom = new JSDOM('<div id="root"></div>', {
  url: "http://127.0.0.1:8000/backtests/chart-anchor",
  pretendToBeVisual: true,
});
for (const name of [
  "window", "document", "navigator", "HTMLElement", "Element", "Node",
  "Event", "MouseEvent", "getComputedStyle", "SVGElement", "ShadowRoot",
  "location", "history",
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
  addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
});
globalThis.ResizeObserver = class {
  constructor(callback) {
    this.callback = callback;
    resizeObservers.push(this);
  }
  observe() {} unobserve() {} disconnect() {}
};
const resizeObservers = [];
globalThis.resizeObservers = resizeObservers;
function fireResize() {
  for (const observer of resizeObservers) observer.callback([]);
}
dom.window.ResizeObserver = globalThis.ResizeObserver;
// jsdom has no layout: report the probe width for the chart host element so the
// chart's own width-driven axis logic sees a realistic canvas.
let probeWidth = 900;
Object.defineProperty(dom.window.HTMLElement.prototype, "clientWidth", {
  get() {
    return this.classList?.contains("chart") ? probeWidth : 0;
  },
  configurable: true,
});
dom.window.scrollTo = () => {};
globalThis.requestAnimationFrame = (callback) => setTimeout(callback, 0);
globalThis.cancelAnimationFrame = (id) => clearTimeout(id);

const React = await import("react");
const { act } = React;
const { createRoot } = await import("react-dom/client");
const { ConfigProvider, App } = await import("antd");
const echarts = await import("echarts/core");
const chartTypes = await import("echarts/charts");
const components = await import("echarts/components");
const renderers = await import("echarts/renderers");

// Use the real chart, registry and event handlers. Only the drawing surface is
// SVG, since jsdom has no canvas; production must register its own markPoint.
echarts.use(renderers.SVGRenderer);
echarts.setPlatformAPI({
  measureText: (text) => ({ width: String(text).length * 7 }),
});
const localRequire = createRequire(new URL("../package.json", import.meta.url));
const modules = {
  "echarts/core": {
    ...echarts,
    init(element, theme, options) {
      return echarts.init(element, theme, {
        ...options,
        renderer: "svg",
        width: probeWidth,
        height: Number.parseFloat(element.style.height) || 360,
      });
    },
  },
  "echarts/charts": chartTypes,
  "echarts/components": components,
  "echarts/renderers": renderers,
};
const { charts, workspace, routes } = await load({
  charts: "./src/components/charts",
  workspace: "./src/pages/BacktestDetailPage",
  routes: "./src/app/routes",
}, {
  stubCharts: false,
  requireModule: (name) => modules[name] ?? localRequire(name),
});

const items = [
  { date: "2025-02-28", equity: 1, benchmark: 1, drawdown: 0, cost: 0 },
  { date: "2025-03-03", equity: 1.01, benchmark: 1, drawdown: 0, cost: 0 },
  { date: "2025-03-04", equity: 1.02, benchmark: 1, drawdown: 0, cost: 0 },
];

async function waitFor(check) {
  const start = Date.now();
  for (;;) {
    try {
      return check();
    } catch (error) {
      if (Date.now() - start > 5000) throw error;
      await act(() => new Promise((resolve) => setTimeout(resolve, 20)));
    }
  }
}

function provider(child) {
  return React.createElement(ConfigProvider, null,
    React.createElement(App, null, child));
}

const LABEL_WIDTH = 7;

/** Bounding boxes of the rendered date labels, in canvas pixels. */
function dateLabelBoxes() {
  const svg = document.querySelector(".chart svg");
  assert.ok(svg, "chart svg is rendered");
  return [...svg.querySelectorAll("text")]
    .filter((node) => /^\d{4}-\d{2}-\d{2}$/.test(node.textContent.trim()))
    .map((node) => {
      const content = node.textContent.trim();
      const anchor = node.getAttribute("text-anchor") || "start";
      const transform = /translate\(([-\d.]+)/.exec(
        node.getAttribute("transform") || "",
      );
      const x = transform ? Number(transform[1]) : 0;
      const width = content.length * LABEL_WIDTH;
      const left =
        anchor === "middle" ? x - width / 2 : anchor === "end" ? x - width : x;
      return { content, left, right: left + width };
    })
    .sort((a, b) => a.left - b.left);
}

test("both dated ends keep the full date inside a narrow equity chart", async () => {
  probeWidth = 318;
  const series = Array.from({ length: 416 }, (_, index) => {
    const date = new Date(Date.UTC(2025, 2, 3) + index * 2 * 86400000)
      .toISOString()
      .slice(0, 10);
    return { date, equity: 1 + index / 1000, benchmark: 1, drawdown: 0, cost: 0 };
  });
  const root = createRoot(document.getElementById("root"));
  try {
    await act(async () => root.render(provider(
      React.createElement(charts.EquityChart, { items: series }),
    )));
    const boxes = await waitFor(() => {
      const found = dateLabelBoxes();
      assert.ok(found.length >= 2);
      return found;
    });
    assert.equal(boxes[0].content, series[0].date);
    assert.equal(boxes[boxes.length - 1].content, series[series.length - 1].date);
    for (const box of boxes) {
      assert.ok(box.left >= 0, `${box.content} starts inside the canvas`);
      assert.ok(box.right <= probeWidth, `${box.content} ends inside the canvas`);
    }
    for (let index = 1; index < boxes.length; index += 1)
      assert.ok(
        boxes[index].left >= boxes[index - 1].right,
        `${boxes[index].content} does not overlap ${boxes[index - 1].content}`,
      );
    const chart = echarts.getInstanceByDom(document.querySelector(".chart"));
    const label = chart.getOption().xAxis[0].axisLabel;
    assert.equal(label.showMinLabel, true);
    assert.equal(label.showMaxLabel, true);
    assert.equal(label.alignMinLabel, "left");
    assert.equal(label.alignMaxLabel, "right");
    const aria = document.querySelector('[role="img"]').getAttribute("aria-label");
    assert.ok(aria.includes(series[0].date));
    assert.ok(aria.includes(series[series.length - 1].date));
  } finally {
    await act(async () => root.unmount());
  }
});

test("a short dated range keeps both ends readable on a narrow chart", async () => {
  probeWidth = 318;
  const series = [
    { date: "2025-02-28", equity: 1, benchmark: 1, drawdown: 0, cost: 0 },
    { date: "2025-03-03", equity: 1.01, benchmark: 1, drawdown: 0, cost: 0 },
    { date: "2025-03-04", equity: 1.02, benchmark: 1, drawdown: 0, cost: 0 },
    { date: "2025-03-05", equity: 1.03, benchmark: 1, drawdown: 0, cost: 0 },
    { date: "2025-03-06", equity: 1.04, benchmark: 1, drawdown: 0, cost: 0 },
    { date: "2025-03-07", equity: 1.05, benchmark: 1, drawdown: 0, cost: 0 },
  ];
  const root = createRoot(document.getElementById("root"));
  try {
    await act(async () => root.render(provider(
      React.createElement(charts.EquityChart, { items: series }),
    )));
    const boxes = await waitFor(() => {
      const found = dateLabelBoxes();
      assert.ok(found.length >= 2);
      return found;
    });
    assert.equal(boxes[0].content, "2025-02-28");
    assert.equal(boxes[boxes.length - 1].content, "2025-03-07");
    for (const box of boxes) {
      assert.ok(box.left >= 0 && box.right <= probeWidth, `${box.content} stays inside`);
    }
    for (let index = 1; index < boxes.length; index += 1)
      assert.ok(boxes[index].left >= boxes[index - 1].right);
  } finally {
    await act(async () => root.unmount());
  }
});

test("real ECharts renders the anchor and updates it when only anchorDate changes", async () => {
  const root = createRoot(document.getElementById("root"));
  const render = (anchorDate) => act(async () => root.render(provider(
    React.createElement(charts.EquityChart, { items, anchorDate }),
  )));
  try {
    await render(null);
    assert.ok(document.querySelector(".chart svg"));
    assert.ok(!document.querySelector(".chart svg").textContent.includes("期初基准点"));
    await render("2025-02-28");
    await waitFor(() => assert.ok(
      document.querySelector(".chart svg").textContent.includes("期初基准点"),
    ));
    const chart = echarts.getInstanceByDom(document.querySelector(".chart"));
    assert.equal(chart.getOption().series[0].data.length, items.length);
    await render(null);
    await waitFor(() => assert.ok(
      !document.querySelector(".chart svg").textContent.includes("期初基准点"),
    ));
  } finally {
    await act(async () => root.unmount());
  }
});

test("a width change keeps the legend series the reader turned off", async () => {
  probeWidth = 318;
  const root = createRoot(document.getElementById("root"));
  try {
    await act(async () => root.render(provider(
      React.createElement(charts.EquityChart, { items }),
    )));
    const chart = await waitFor(() => {
      const instance = echarts.getInstanceByDom(document.querySelector(".chart"));
      assert.ok(instance);
      return instance;
    });
    await act(async () => chart.dispatchAction({ type: "legendUnSelect", name: "基准净值" }));
    await waitFor(() => assert.equal(chart.getOption().legend[0].selected["基准净值"], false));
    // A viewport change rebuilds the option from scratch (notMerge) — the choice
    // the reader made must survive that rebuild.
    probeWidth = 236;
    await act(async () => fireResize());
    await waitFor(() => assert.equal(chart.getOption().legend[0].selected["基准净值"], false));
    assert.equal(chart.getOption().legend[0].selected["策略净值"], true);
  } finally {
    await act(async () => root.unmount());
  }
});

for (const withAnchor of [true, false]) {
  test(`real chart clicks select positions, with ${withAnchor ? "an initial anchor" : "a legacy first trading day"}`, async () => {
    history.replaceState(null, "", "/backtests/chart-anchor");
    const requestedDates = [];
    const detail = {
      id: "chart-anchor", name: "净值点击回归", status: "succeeded",
      strategy: "TopkDropoutStrategy", start_date: "2025-03-03", end_date: "2025-03-04",
      created_at: "2025-03-05T00:00:00Z", metrics: {}, config: {}, limitations: [],
    };
    globalThis.fetch = async (url) => {
      const path = String(url).replace("/api/v1", "");
      let body;
      if (path === "/backtests/chart-anchor") body = detail;
      else if (path === "/backtests/chart-anchor/equity")
        body = { items: withAnchor ? items : items.slice(1) };
      else if (path.startsWith("/backtests/chart-anchor/positions?")) {
        requestedDates.push(new URL(path, location.href).searchParams.get("date"));
        body = { items: [], total: 0 };
      } else throw Error(`Unexpected request ${path}`);
      return new Response(JSON.stringify(body), {
        headers: { "Content-Type": "application/json" },
      });
    };
    function Workspace() {
      const route = routes.useRoute();
      return React.createElement(workspace.default, {
        id: route.params[0], tab: route.params[1] ?? "overview",
      });
    }
    const root = createRoot(document.getElementById("root"));
    try {
      await act(async () => root.render(provider(React.createElement(Workspace))));
      const chart = await waitFor(() => {
        const element = document.querySelector('[aria-label^="策略净值"]');
        const instance = element && echarts.getInstanceByDom(element);
        assert.ok(instance);
        assert.equal(instance.getOption().series[0].data.length, withAnchor ? 3 : 2);
        return instance;
      });
      if (withAnchor) {
        // Dispatch through the actual ECharts instance and production Chart handler.
        await act(async () => chart.trigger("click", { name: "2025-02-28", data: 1 }));
        assert.equal(location.pathname, "/backtests/chart-anchor");
        assert.deepEqual(requestedDates, []);
      }
      await act(async () => chart.trigger("click", { name: "2025-03-03", data: 1.01 }));
      await waitFor(() => assert.deepEqual(requestedDates, ["2025-03-03"]));
      assert.equal(location.pathname, "/backtests/chart-anchor/positions");
    } finally {
      await act(async () => root.unmount());
    }
  });
}

after(() => dom.window.close());
