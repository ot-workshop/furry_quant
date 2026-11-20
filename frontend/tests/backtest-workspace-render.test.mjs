import assert from "node:assert/strict";
import { after, test } from "node:test";
import { JSDOM } from "jsdom";
import { load } from "./helpers/load.mjs";

const dom = new JSDOM('<div id="root"></div>', {
  url: "http://127.0.0.1:8000/backtests/legacy-anchor",
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
const { workspace } = await load({
  workspace: "./src/pages/BacktestDetailPage",
});
const markers = [];
class MockChart extends dom.window.HTMLElement {
  setOption(option) {
    if (option.series?.[0]?.markPoint) markers.push(option.series[0].markPoint);
  }
  resize() {}
  dispose() {}
}
dom.window.HTMLElement.prototype.setOption = MockChart.prototype.setOption;
dom.window.HTMLElement.prototype.resize = MockChart.prototype.resize;
dom.window.HTMLElement.prototype.dispose = MockChart.prototype.dispose;

const detail = {
  id: "legacy-anchor",
  name: "锚点显示回归",
  status: "succeeded",
  strategy: "TopkDropoutStrategy",
  start_date: "2025-03-03",
  end_date: "2025-03-04",
  created_at: "2025-03-05T00:00:00Z",
  is_synthetic: false,
  metrics: {},
  config: {},
  trades: [],
  positions: [],
  orders: [],
  equity: [],
  qlib_report: [],
  limitations: [],
};
const equity = {
  items: [
    { date: "2025-02-28", equity: 1, benchmark: 1, drawdown: 0, cost: 0 },
    { date: "2025-03-03", equity: 1.01, benchmark: 1, drawdown: 0, cost: 0 },
    { date: "2025-03-04", equity: 1.02, benchmark: 1, drawdown: 0, cost: 0 },
  ],
};
const positionDates = [];
globalThis.fetch = async (url) => {
  const path = String(url).replace("/api/v1", "");
  if (path === "/backtests/legacy-anchor")
    return new Response(JSON.stringify(detail), {
      headers: { "Content-Type": "application/json" },
    });
  if (path === "/backtests/legacy-anchor/equity")
    return new Response(JSON.stringify(equity), {
      headers: { "Content-Type": "application/json" },
    });
  const match = path.match(
    /^\/backtests\/legacy-anchor\/positions\?date=([^&]+)/,
  );
  if (match) {
    positionDates.push(decodeURIComponent(match[1]));
    return new Response(JSON.stringify({ items: [], total: 0 }), {
      headers: { "Content-Type": "application/json" },
    });
  }
  if (path === "/backtests/legacy-anchor/trades")
    return new Response(JSON.stringify({ items: [], total: 0 }), {
      headers: { "Content-Type": "application/json" },
    });
  if (path === "/backtests/legacy-anchor/costs")
    return new Response(JSON.stringify({}), {
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

test("backtest workspace keeps the anchor on chart and out of position dates", async () => {
  const root = createRoot(document.getElementById("root"));
  await act(async () =>
    root.render(
      React.createElement(
        ConfigProvider,
        null,
        React.createElement(
          App,
          null,
          React.createElement(workspace.default, {
            id: "legacy-anchor",
            tab: "positions",
          }),
        ),
      ),
    ),
  );
  try {
    await waitFor(() => assert.deepEqual(positionDates, ["2025-03-04"]));
    const dropdown = document.querySelector(".ant-select");
    assert.ok(dropdown);
    await act(async () =>
      dropdown.dispatchEvent(new MouseEvent("mousedown", { bubbles: true })),
    );
    await waitFor(() =>
      assert.ok(
        document.querySelector(
          ".ant-select-dropdown:not(.ant-select-dropdown-hidden)",
        ),
      ),
    );
    const options = [
      ...document.querySelectorAll(
        ".ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option",
      ),
    ].map((option) => option.textContent.trim());
    assert.ok(options.includes("2025-03-03"));
    assert.ok(options.includes("2025-03-04"));
    assert.ok(!options.includes("2025-02-28"));
  } finally {
    await act(async () => root.unmount());
  }
});

after(() => {
  dom.window.close();
  setTimeout(() => process.exit(), 200).unref();
});
