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
]) {
  Object.defineProperty(globalThis, name, {
    value: name === "window" ? dom.window : dom.window[name],
    configurable: true,
    writable: true,
  });
}
const computedStyle = dom.window.getComputedStyle.bind(dom.window);
dom.window.getComputedStyle = (element) => computedStyle(element);
globalThis.getComputedStyle = dom.window.getComputedStyle;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
dom.window.matchMedia = () => ({
  matches: false,
  addEventListener() {},
  removeEventListener() {},
  addListener() {},
  removeListener() {},
});
globalThis.matchMedia = dom.window.matchMedia;
const trackedChannels = [];
class TrackedMessageChannel extends globalThis.MessageChannel {
  constructor() {
    super();
    trackedChannels.push(this);
  }
}
globalThis.MessageChannel = TrackedMessageChannel;
dom.window.MessageChannel = TrackedMessageChannel;
globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
};
dom.window.ResizeObserver = globalThis.ResizeObserver;
dom.window.scrollTo = () => {};
globalThis.requestAnimationFrame = (callback) => setTimeout(callback, 0);
globalThis.cancelAnimationFrame = (id) => clearTimeout(id);

let summaryHandler = async () =>
  new Response(
    JSON.stringify({
      database: null,
      counts: { fixed: 0, historical: 0, market: 0 },
      as_of: "2026-11-22T00:00:00Z",
    }),
    { headers: { "Content-Type": "application/json" } },
  );
let dataStatusHandler = async () =>
  new Response(
    JSON.stringify({
      snapshots: [],
      historical_indices: [],
      market_universes: [],
      database: null,
    }),
    { headers: { "Content-Type": "application/json" } },
  );

globalThis.fetch = async (url) => {
  const path = String(url).replace("/api/v1", "");
  const json = (value, status = 200) =>
    new Response(JSON.stringify(value), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  if (path === "/dashboard/summary") return summaryHandler();
  if (path === "/data-status") return dataStatusHandler();
  if (path === "/research/tasks") return json({ items: [], total: 0 });
  if (path === "/backtests") return json({ items: [], total: 0 });
  return json({ detail: "not mocked" }, 404);
};

const React = await import("react");
const { act } = React;
const { createRoot } = await import("react-dom/client");
const { App, ConfigProvider } = await import("antd");
const { dashboard, databaseUi, hookMod } = await load({
  dashboard: "./src/pages/DashboardPage",
  databaseUi: "./src/pages/data/Database",
  hookMod: "./src/hooks/useApiResource",
});

async function waitFor(check) {
  const started = Date.now();
  for (;;) {
    try {
      return check();
    } catch (error) {
      if (Date.now() - started > 4000) throw error;
      await act(() => new Promise((resolve) => setTimeout(resolve, 20)));
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
        React.createElement(App, null, React.createElement(component)),
      ),
    ),
  );
  return async () => act(async () => root.unmount());
}

test("DashboardPage separates loading, error+retry, disconnected DB, and empty market states", async () => {
  let resolvePending;
  summaryHandler = () =>
    new Promise((resolve) => {
      resolvePending = resolve;
    });

  const closeLoading = await mount(dashboard.default);
  try {
    // Loading state shows skeletons without false disconnected/empty notice.
    assert.ok(document.querySelector(".ant-statistic-skeleton"));
    assert.doesNotMatch(
      document.body.textContent,
      /数据库未连接|暂无行情|状态读取失败/,
    );
    await act(async () => {
      resolvePending(
        new Response(JSON.stringify({ detail: "工作台摘要暂时无法读取" }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        }),
      );
    });

    await waitFor(() =>
      assert.match(document.body.textContent, /状态读取失败/),
    );
    const retryBtn = [...document.querySelectorAll("button")].find((btn) =>
      btn.textContent.replace(/\s+/g, "").includes("重试"),
    );
    assert.ok(retryBtn, "Expected retry button on Database error");
    assert.ok(retryBtn, "Expected retry button on summary error");

    // Manual retry recovers cleanly into populated state.
    summaryHandler = async () =>
      new Response(
        JSON.stringify({
          database: {
            securities: 5453,
            coverage_start: "2015-12-04",
            coverage_end: "2026-11-19",
          },
          counts: { fixed: 1, historical: 0, market: 1 },
          as_of: "2026-11-22T00:00:00Z",
        }),
        { headers: { "Content-Type": "application/json" } },
      );
    await act(async () => {
      retryBtn.click();
    });
    await waitFor(() => {
      assert.match(document.body.textContent, /5,453/);
      assert.match(document.body.textContent, /2015-12-04 ~ 2026-11-19/);
      assert.match(
        document.body.textContent,
        /固定股票池 1 · 沪深300历史成分 0 · 条件筛选 1/,
      );
    });
  } finally {
    await closeLoading();
  }

  // 200 with database: null -> disconnected label only.
  summaryHandler = async () =>
    new Response(
      JSON.stringify({
        database: null,
        counts: { fixed: 0, historical: 0, market: 0 },
        as_of: "2026-11-22T00:00:00Z",
      }),
      { headers: { "Content-Type": "application/json" } },
    );
  const closeDisconnected = await mount(dashboard.default);
  try {
    await waitFor(() =>
      assert.match(document.body.textContent, /数据库未连接/),
    );
    assert.doesNotMatch(document.body.textContent, /暂无行情/);
  } finally {
    await closeDisconnected();
  }

  // 200 with connected database, null coverage_start, and invalid manifests excluded from valid count.
  summaryHandler = async () =>
    new Response(
      JSON.stringify({
        database: { securities: 0, coverage_start: null, coverage_end: null },
        counts: { fixed: 1, historical: 0, market: 0, invalid: 2 },
        as_of: "2026-11-22T00:00:00Z",
      }),
      { headers: { "Content-Type": "application/json" } },
    );
  const closeNoMarket = await mount(dashboard.default);
  try {
    await waitFor(() => assert.match(document.body.textContent, /暂无行情/));
    assert.match(document.body.textContent, /已跳过 2 个无效清单/);
    assert.doesNotMatch(document.body.textContent, /数据库未连接/);
  } finally {
    await closeNoMarket();
  }
});

test("Database page separates error+retry from disconnected DB and empty market bars", async () => {
  dataStatusHandler = async () =>
    new Response(JSON.stringify({ detail: "暂时不可读" }), {
      status: 503,
      headers: { "Content-Type": "application/json" },
    });
  const closeDb = await mount(databaseUi.default);
  try {
    await waitFor(() => assert.match(document.body.textContent, /加载失败/));
    dataStatusHandler = async () =>
      new Response(
        JSON.stringify({
          snapshots: [],
          historical_indices: [],
          market_universes: [],
          database: {
            schema_version: "postgres-market-v1",
            row_counts: { daily_bar: 0 },
            releases: 0,
            revisions: 0,
            datasets: 0,
            securities: 0,
            coverage_start: null,
            coverage_end: null,
            checks: {},
            latest_release: null,
          },
        }),
        { headers: { "Content-Type": "application/json" } },
      );
    const retryBtn = [...document.querySelectorAll("button")].find((btn) =>
      btn.textContent.replace(/\s+/g, "").includes("重试"),
    );
    assert.ok(retryBtn, "Expected retry button on Database error");
    await act(async () => {
      retryBtn.click();
    });
    await waitFor(() => assert.match(document.body.textContent, /暂无行情/));
    assert.doesNotMatch(document.body.textContent, /未连接数据库/);
  } finally {
    await closeDb();
  }
});

test("useApiResource retryOnErrorMs recovers automatically after transient failure", async () => {
  let attempts = 0;
  function Probe() {
    const state = hookMod.useApiResource(
      async () => {
        attempts += 1;
        if (attempts === 1) throw new Error("暂不可用");
        return { ok: true };
      },
      [],
      { retryOnErrorMs: 30 },
    );
    return React.createElement(
      "div",
      null,
      state.data ? "RECOVERED" : state.error ? `ERR:${state.error}` : "WAIT",
    );
  }
  const close = await mount(Probe);
  try {
    await waitFor(() =>
      assert.match(document.body.textContent, /ERR:暂不可用/),
    );
    await waitFor(() => assert.match(document.body.textContent, /RECOVERED/));
    assert.equal(attempts, 2);
  } finally {
    await close();
  }
});

after(() => {
  for (const channel of trackedChannels) {
    channel.port1.close();
    channel.port2.close();
  }
  trackedChannels.length = 0;
  dom.window.close();
  setTimeout(() => process.exit(), 200).unref();
});
