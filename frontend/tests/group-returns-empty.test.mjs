import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";
import { load } from "./helpers/load.mjs";

const dom = new JSDOM('<div id="root"></div>', {
  url: "http://127.0.0.1:8000/evaluations/chart",
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

echarts.use([
  renderers.SVGRenderer,
  components.GraphicComponent,
  components.GridComponent,
  components.TooltipComponent,
  components.LegendComponent,
  components.MarkLineComponent,
  components.MarkPointComponent,
  components.DataZoomComponent,
]);
echarts.setPlatformAPI({
  measureText: (text) => ({ width: String(text).length * 7 }),
});
const localRequire = (await import("node:module")).createRequire(
  new URL("../package.json", import.meta.url),
);
const modules = {
  "echarts/core": {
    ...echarts,
    init(element, theme, options) {
      return echarts.init(element, theme, {
        ...options,
        renderer: "svg",
        width: 480,
        height: Number.parseFloat(element.style.height) || 200,
      });
    },
  },
  "echarts/charts": chartTypes,
  "echarts/components": components,
  "echarts/renderers": renderers,
};
const { charts } = await load(
  { charts: "./src/components/charts" },
  {
    stubCharts: false,
    requireModule: (name) => modules[name] ?? localRequire(name),
  },
);

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

async function render(groups) {
  const root = createRoot(document.getElementById("root"));
  await act(async () =>
    root.render(
      React.createElement(
        ConfigProvider,
        null,
        React.createElement(
          App,
          null,
          React.createElement(charts.GroupReturnsChart, { groups }),
        ),
      ),
    ),
  );
  await waitFor(() => assert.ok(document.querySelector(".chart svg")));
  const chart = echarts.getInstanceByDom(document.querySelector(".chart"));
  return { root, chart };
}

const seriesValues = (chart) =>
  chart
    .getOption()
    .series[0].data.map((row) =>
      row && typeof row === "object" ? row.value : row,
    );

const groups = (values) =>
  values.map((value, index) => ({ group: index + 1, mean_return: value }));

test("no finite group return says so inside the plot and never fills zeros", async () => {
  const { root, chart } = await render(groups([null, null, null]));
  try {
    await waitFor(() =>
      assert.ok(
        document
          .querySelector(".chart svg")
          .textContent.includes("暂无可展示的分组收益"),
      ),
    );
    assert.deepEqual(seriesValues(chart), [null, null, null]);
    assert.ok(
      document.body.textContent.includes("共 3 组均无有效收益，未按 0 显示"),
    );
    assert.ok(
      document
        .querySelector('[role="img"]')
        .getAttribute("aria-label")
        .includes("暂无可展示的数据"),
    );
  } finally {
    await act(async () => root.unmount());
  }
});

test("real zeros stay real values and get no empty-state message", async () => {
  const { root, chart } = await render(groups([0, 0, 0]));
  try {
    assert.ok(
      !document.querySelector(".chart svg").textContent.includes("暂无可展示"),
    );
    assert.deepEqual(seriesValues(chart), [0, 0, 0]);
    assert.ok(!document.body.textContent.includes("未按 0 显示"));
  } finally {
    await act(async () => root.unmount());
  }
});

test("a partially missing group keeps the valid bars and notes the gap", async () => {
  const { root, chart } = await render(groups([0.01, null, -0.02]));
  try {
    assert.ok(
      !document.querySelector(".chart svg").textContent.includes("暂无可展示"),
    );
    assert.deepEqual(seriesValues(chart), [0.01, null, -0.02]);
    assert.ok(
      document.body.textContent.includes("有 1 组缺少有效收益，未按 0 显示"),
    );
  } finally {
    await act(async () => root.unmount());
  }
});

test("a chart with every group populated shows neither message", async () => {
  const { root } = await render(groups([0.01, 0.02, -0.01]));
  try {
    assert.ok(
      !document.querySelector(".chart svg").textContent.includes("暂无可展示"),
    );
    assert.ok(!document.body.textContent.includes("未按 0 显示"));
  } finally {
    await act(async () => root.unmount());
  }
});
