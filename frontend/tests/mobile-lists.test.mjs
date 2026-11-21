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
  "location",
  "history",
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

const media = { viewport: 390, listeners: new Set() };
dom.window.matchMedia = (query) => ({
  media: query,
  get matches() {
    const max = /\(max-width:\s*(\d+)px\)/.exec(query);
    return max ? media.viewport <= Number(max[1]) : false;
  },
  onchange: null,
  addEventListener(type, fn) {
    if (type === "change") media.listeners.add(fn);
  },
  removeEventListener(type, fn) {
    if (type === "change") media.listeners.delete(fn);
  },
  addListener(fn) {
    media.listeners.add(fn);
  },
  removeListener(fn) {
    media.listeners.delete(fn);
  },
  dispatchEvent() {
    return true;
  },
});
function setViewport(w) {
  media.viewport = w;
  for (const fn of [...media.listeners]) fn({ matches: true });
}

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

const backtestsFixture = {
  items: [
    {
      id: "bt-real-zero-1",
      name: "超长回测标题测试手机两行卡片布局与徽标右对齐（2020-03-02→2026-11-13）",
      status: "succeeded",
      strategy: "DynamicUniverseTopkStrategy",
      start_date: "2020-03-02",
      end_date: "2026-11-13",
      created_at: "2026-11-21T06:00:00Z",
      is_synthetic: true,
      metrics: { total_return: 0, max_drawdown: -0.05 },
    },
    {
      id: "bt-missing-2",
      name: "空指标对照回测",
      status: "succeeded",
      strategy: "TopkDropoutStrategy",
      start_date: "2024-01-02",
      end_date: "2024-06-28",
      created_at: "2026-11-20T06:00:00Z",
      is_synthetic: true,
      metrics: null,
    },
  ],
  total: 2,
};

const evaluationsFixture = {
  items: [
    {
      id: "eval-1",
      name: "多因子超长评估标题用于验证手机卡片折行与因子标签对齐",
      is_synthetic: true,
      created_at: "2026-11-21T06:00:00Z",
      data_version: "snap-1",
      factor_run_id: "fr-1",
      factor_config: [
        { name: "momentum", window: 20, weight: 1 },
        {
          name: "超长自定义多因子测试名称用于验证手机卡片内标签自动换行与两行截断Alpha001",
          window: 60,
          weight: 0.5,
        },
      ],
      evaluation: { horizons: [1, 5], quantiles: 5 },
      start: "2020-03-02",
      end: "2026-11-13",
      sample_size: 4820,
      provenance: {},
    },
  ],
  total: 1,
};

globalThis.fetch = async (url) => {
  const path = String(url).replace("/api/v1", "");
  const json = (value) =>
    new Response(JSON.stringify(value), {
      headers: { "Content-Type": "application/json" },
    });
  if (path === "/data-status")
    return json({
      snapshots: [],
      historical_indices: [],
      market_universes: [],
      database: null,
    });
  if (path === "/research/tasks") return json({ items: [], total: 0 });
  if (path === "/backtests") return json(backtestsFixture);
  if (path === "/evaluations") return json(evaluationsFixture);
  if (path === "/factor-runs")
    return json({
      items: [
        {
          id: "fr-1",
          name: "测试评分批次",
          as_of: "2026-11-13",
          universe: "snap-1",
          sample_size: 50,
          scheme: "equal",
          data_version: "snap-1",
          is_synthetic: true,
          factor_config: [{ name: "momentum", window: 20, weight: 1 }],
        },
      ],
      total: 1,
    });
  if (path.startsWith("/factor-runs/fr-1/scores"))
    return json({
      items: [
        {
          instrument: "000001.SZ",
          name: "平安银行",
          industry: "银行",
          date: "2026-11-13",
          raw: { momentum: 0.1 },
          scores: { momentum: 80 },
          composite: 80,
          rank: 1,
        },
      ],
      total: 1,
      offset: 0,
      limit: 50,
      metadata: {
        weights: { momentum: 1 },
        score_unit: "percentile_0_100",
        factor_definitions: {
          momentum: {
            id: "momentum",
            name: "momentum",
            label: "动量",
            family: "price",
            direction: "higher",
            required_fields: ["close"],
            formula: "",
            params: {},
            lookback: 20,
          },
        },
      },
    });
  throw new Error(`Unexpected request ${path}`);
};

const React = await import("react");
const { act } = React;
const { createRoot } = await import("react-dom/client");
const { App, ConfigProvider } = await import("antd");
const { dashboard, backtests, evaluations, factors, tasksUi } = await load({
  dashboard: "./src/pages/DashboardPage",
  backtests: "./src/pages/BacktestListPage",
  evaluations: "./src/pages/EvaluationListPage",
  factors: "./src/pages/FactorsPage",
  tasksUi: "./src/components/tasks",
});

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

async function mount(component, props = {}) {
  const root = createRoot(document.getElementById("root"));
  await act(async () =>
    root.render(
      React.createElement(
        ConfigProvider,
        null,
        React.createElement(App, null, React.createElement(component, props)),
      ),
    ),
  );
  return async () => act(async () => root.unmount());
}

test("mobile viewports render two-line cards instead of tables and preserve 0 vs missing", async () => {
  setViewport(390);
  const closeDash = await mount(dashboard.default);
  try {
    await waitFor(() => assert.ok(document.querySelector(".list-card")));
    assert.equal(document.querySelectorAll("table").length, 0);
    const cards = [...document.querySelectorAll(".list-card")];
    assert.equal(cards.length, 2);
    assert.match(cards[0].textContent, /0\.00%/);
    assert.match(cards[1].textContent, /—/);
  } finally {
    await closeDash();
  }

  const closeBt = await mount(backtests.default);
  try {
    await waitFor(() => assert.ok(document.querySelector(".list-card")));
    assert.equal(document.querySelectorAll("table").length, 0);
    assert.match(document.body.textContent, /2020-03-02 ~ 2026-11-13/);
    assert.match(
      document.querySelector(".list-card-subbadges")?.textContent || "",
      /动态条件股票池/,
    );
  } finally {
    await closeBt();
  }

  const closeEv = await mount(evaluations.default);
  try {
    await waitFor(() => assert.ok(document.querySelector(".list-card")));
    assert.equal(document.querySelectorAll("table").length, 0);
    assert.match(document.body.textContent, /4820 只/);
    const clampedTag = document.querySelector(".list-card .tag-text-clamp");
    assert.ok(clampedTag, "Expected >40-char factor tag to clamp with title");
    assert.match(
      clampedTag.closest(".ant-tag")?.getAttribute("title") || "",
      /超长自定义多因子/,
    );
  } finally {
    await closeEv();
  }

  const closeFactorsMobile = await mount(factors.default, {
    search: new URLSearchParams(),
  });
  try {
    await waitFor(() =>
      assert.match(
        document.querySelector(
          ".ant-select-content, .ant-select-selection-item",
        )?.textContent || "",
        /1 个因子 · 50 只/,
      ),
    );
  } finally {
    await closeFactorsMobile();
  }
});

test("TaskTable resetKey resets page on filter change and preserves page during list growth", async () => {
  setViewport(390);
  const makeTasks = (count) =>
    Array.from({ length: count }, (_, idx) => ({
      id: `t-${idx}`,
      kind: "research",
      name: `任务 ${idx + 1}`,
      status: "succeeded",
      stage: "finished",
      message: "计算完成",
      created_at: `2026-11-21T00:${String(idx % 60).padStart(2, "0")}:00Z`,
      progress: { completed: 4, total: 4 },
      request: {
        name: `任务 ${idx + 1}`,
        data: { mode: "market_warehouse" },
        factors: [{ name: "momentum", window: 20, weight: 1 }],
        backtest: { start: "2024-01-02", end: "2024-06-28" },
      },
      result: null,
      error: null,
      error_details: null,
    }));

  const root = createRoot(document.getElementById("root"));
  const renderTasks = async (count, resetKey) => {
    await act(async () =>
      root.render(
        React.createElement(
          ConfigProvider,
          null,
          React.createElement(
            App,
            null,
            React.createElement(tasksUi.TaskTable, {
              tasks: makeTasks(count),
              onChanged: () => {},
              resetKey,
            }),
          ),
        ),
      ),
    );
  };
  try {
    await renderTasks(22, "all");
    await waitFor(() =>
      assert.ok(document.querySelector(".ant-pagination-item-2")),
    );
    await act(async () => {
      const target =
        document.querySelector(".ant-pagination-item-2 a") ??
        document.querySelector(".ant-pagination-item-2");
      target.click();
    });
    assert.ok(
      document.querySelector(
        ".ant-pagination-item-2.ant-pagination-item-active",
      ),
    );

    await renderTasks(23, "all");
    assert.ok(
      document.querySelector(
        ".ant-pagination-item-2.ant-pagination-item-active",
      ),
      "Background polling growth should keep active page 2",
    );

    await renderTasks(23, "succeeded");
    assert.ok(
      document.querySelector(
        ".ant-pagination-item-1.ant-pagination-item-active",
      ),
      "Changing resetKey should reset pagination to page 1",
    );
  } finally {
    await act(async () => root.unmount());
  }
});

test("desktop viewports keep tables inside TableScroll and empty TaskTable shows an empty state", async () => {
  setViewport(1440);
  const closeBt = await mount(backtests.default);
  try {
    await waitFor(() => assert.ok(document.querySelector("table")));
    assert.equal(document.querySelectorAll(".list-card").length, 0);
    assert.ok(document.querySelector(".table-scroll"));
  } finally {
    await closeBt();
  }

  const closeFactors = await mount(factors.default, {
    search: new URLSearchParams(),
  });
  try {
    await waitFor(() =>
      assert.ok(document.querySelector(".table-scroll table")),
    );
    assert.equal(
      document.querySelector('[aria-label="横截面排名表"]')?.tagName,
      "DIV",
    );
  } finally {
    await closeFactors();
  }

  const closeEmptyTasks = await mount(tasksUi.TaskTable, {
    tasks: [],
    onChanged: () => {},
  });
  try {
    assert.match(document.body.textContent, /没有符合条件的任务/);
  } finally {
    await closeEmptyTasks();
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
