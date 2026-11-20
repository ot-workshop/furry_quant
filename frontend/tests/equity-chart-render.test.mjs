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
  observe() {} unobserve() {} disconnect() {}
};
dom.window.ResizeObserver = globalThis.ResizeObserver;
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
        width: 900,
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
