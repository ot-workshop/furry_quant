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
const { panel } = await load({ panel: "./src/components/backtest" });

let requestedDates = [];
globalThis.fetch = async (url) => {
  const path = String(url).replace("/api/v1", "");
  const position = path.match(/^\/backtests\/run\/positions(?:\?|$)/);
  if (position) {
    const date = new URL(String(url), "http://127.0.0.1").searchParams.get(
      "date",
    );
    requestedDates.push(date);
    return new Response(JSON.stringify({ items: [], total: 0 }), {
      headers: { "Content-Type": "application/json" },
    });
  }
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

async function mount(component) {
  const root = createRoot(document.getElementById("root"));
  await act(async () =>
    root.render(
      React.createElement(
        ConfigProvider,
        null,
        React.createElement(App, null, component),
      ),
    ),
  );
  return () => act(async () => root.unmount());
}

test("legacy start_date equity point can still query positions", async () => {
  const close = await mount(
    React.createElement(panel.PositionsPanel, {
      runId: "run",
      dates: ["2025-03-03", "2025-03-04"],
      date: null,
      onDate: () => {},
    }),
  );
  try {
    await waitFor(() => assert.deepEqual(requestedDates, ["2025-03-04"]));
  } finally {
    await close();
  }
});

after(() => {
  dom.window.close();
  setTimeout(() => process.exit(), 200).unref();
});
