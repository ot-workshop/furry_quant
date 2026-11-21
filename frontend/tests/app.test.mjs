import assert from "node:assert/strict";
import { after, test } from "node:test";
import { JSDOM } from "jsdom";
import { load } from "./helpers/load.mjs";
import { options, tradingDates } from "./fixtures.mjs";

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
const { app, theme } = await load({
  app: "./src/App",
  theme: "./src/app/theme",
});

const posted = [];
let marketEnabled = false;
let indexEnabled = false;
const marketDataset = {
  id: "market-browser",
  label: "沪深A股全市场",
  full_market: true,
  start: "2025-01-02",
  end: "2025-06-30",
  data_start: "2025-01-02",
  codes_count: 5500,
  trading_days: 128,
  trading_dates: tradingDates("2025-01-02", "2025-06-30"),
  financial_coverage: { covered: 300, total: 5500 },
  note: "按历史生命周期与每日条件筛选",
  listing_coverage: { covered: 5500, total: 5500 },
  st_coverage: { covered: 5500, total: 5500 },
  codes: ["600000.SH", "000001.SZ", "688001.SH"],
};
let tasks = [];
const downloadPosts = [];
let failFinancialPreview = false;
let industryStatus = {
  worker_running: false,
  version: null,
  versions: [],
  jobs: [],
};
globalThis.fetch = async (url, init = {}) => {
  const path = String(url).replace("/api/v1", "");
  const json = (value, status = 200) =>
    new Response(JSON.stringify(value), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  if (path === "/research/options") {
    const value = options();
    if (marketEnabled) value.market_universes = [marketDataset];
    if (indexEnabled) {
      value.historical_indices = [
        {
          ...marketDataset,
          id: "csi300-browser",
          label: "沪深300历史成分",
          codes_count: 3,
        },
      ];
      value.snapshots[0].st_coverage = { covered: 3, total: 3 };
      value.snapshots[0].listing_coverage = { covered: 3, total: 3 };
    }
    value.defaults.backtest.start = "2025-01-10"; // too early for a 20-day momentum window
    return json(value);
  }
  if (path === "/research/tasks" && init.method === "POST") {
    const body = JSON.parse(init.body);
    posted.push({ body, key: init.headers["Idempotency-Key"] });
    const task = {
      id: "task-" + "a".repeat(32),
      kind: "research",
      name: body.name,
      status: "queued",
      stage: "queued",
      message: "等待运行",
      created_at: "2026-01-01T00:00:00Z",
      started_at: null,
      finished_at: null,
      progress: { completed: 0, total: 4 },
      request: body,
      result: null,
      error: null,
    };
    tasks = [task];
    return json(task, 202);
  }
  if (path === "/research/tasks")
    return json({ items: tasks, total: tasks.length });
  if (path === "/research/data-updates")
    return json({ jobs: [], schedule: null, market_versions: [] });
  if (path === "/research/backups" && init.method === "POST") {
    downloadPosts.push({ path, body: JSON.parse(init.body) });
    return json({ id: "task-maintenance", status: "queued" });
  }
  if (path === "/research/backups")
    return json({
      backups: [
        {
          id: "backup-" + "c".repeat(24),
          created_at: "2026-11-12T10:00:00Z",
          files: 80,
          bytes: 102400,
          format: "finance-quant-backup-v2",
        },
      ],
      jobs: [],
      backup_root: "/backups",
      restore_root: "/restores",
      coverage: ["数据库", "行情", "财务", "行业", "下载检查点"],
    });
  if (path === "/research/data-updates/preview") {
    downloadPosts.push({ path, body: JSON.parse(init.body) });
    return json({
      id: "b".repeat(24),
      spec: JSON.parse(init.body),
      stages: ["market", "financial", "industry", "benchmarks"],
      plans: {},
      note: "串行更新",
    });
  }
  if (
    path === "/research/data-updates/start" ||
    path === "/research/data-updates/schedule"
  ) {
    downloadPosts.push({ path, body: JSON.parse(init.body) });
    return json({ status: "queued" });
  }
  if (path === "/data-status")
    return json({
      snapshots: [],
      database: null,
      market_universes: marketEnabled ? [marketDataset] : [],
    });
  if (path === "/dashboard/summary")
    return json({
      database: null,
      counts: { fixed: 0, historical: 0, market: marketEnabled ? 1 : 0 },
      as_of: "2026-11-22T00:00:00Z",
    });
  if (path === "/research/industry-download") return json(industryStatus);
  if (path === "/research/financial-download")
    return json({
      worker_running: false,
      version: null,
      versions: [],
      jobs: [],
      market_versions: [
        {
          id: "market-a",
          scope: "custom",
          start: "2024-01-01",
          end: "2025-12-31",
          codes_count: 3,
          restricted: false,
        },
      ],
    });
  if (path === "/research/financial-download/preview") {
    if (failFinancialPreview)
      return json({ detail: "测试：财务接口暂时不可用" }, 503);
    downloadPosts.push({ path, body: JSON.parse(init.body) });
    return json({
      id: "f".repeat(24),
      spec: JSON.parse(init.body),
      codes: [{ code: "600000.SH", rows: 0 }],
      skipped: 0,
      estimated_requests: 9,
      requests_per_stock: 9,
      note: "按报表续传",
      point_in_time_certified: false,
    });
  }
  if (path === "/research/financial-download/start") {
    downloadPosts.push({ path, body: JSON.parse(init.body) });
    return json({ task_id: "download-task", status: "queued" }, 202);
  }
  if (path === "/backtests") return json({ items: [], total: 0 });
  return json({ detail: "not mocked" }, 404);
};

async function waitFor(check, timeout = 4000) {
  const started = Date.now();
  for (;;) {
    try {
      return check();
    } catch (error) {
      if (Date.now() - started > timeout) throw error;
      await act(() => new Promise((resolve) => setTimeout(resolve, 25)));
    }
  }
}

async function mount(path) {
  dom.window.history.pushState(null, "", path);
  const container = document.getElementById("root");
  const root = createRoot(container);
  await act(async () => {
    root.render(
      React.createElement(
        theme.ThemeProvider,
        null,
        React.createElement(app.default),
      ),
    );
  });
  return { container, root };
}

const button = (container, text) =>
  [...container.querySelectorAll("button")].find((node) =>
    node.textContent.replace(/\s/g, "").includes(text),
  );

test("dashboard points to data management when no dataset exists", async () => {
  const { container, root } = await mount("/");
  await waitFor(() =>
    assert.match(container.textContent, /还没有可用于研究的数据集/),
  );
  assert.ok(button(container, "去下载行情"));
  await act(async () => root.unmount());
});

test("industry data tab renders without a published version", async () => {
  const { container, root } = await mount("/data?tab=industry");
  await waitFor(() => assert.match(container.textContent, /还没有行业数据/));
  assert.ok(button(container, "预览下载"));
  await act(async () => root.unmount());
});

test("empty industry classifications remain visible while all three levels stay supported", async () => {
  const empty = [
    {
      taxonomy: "SW2021",
      level: "L3",
      code: "851544.SI",
      name: "互联网药店",
      reason: "暂无A股成员",
    },
  ];
  const excluded = [
    {
      taxonomy: "SW2021",
      level: "L3",
      sector_code: "851711.SI",
      code: "T00018.SH",
      in_date: "20000719",
      out_date: null,
      reason: "不支持的供应商股票代码；原始记录已保留，未纳入研究",
    },
  ];
  const version = {
    id: "a".repeat(64),
    rows: 100,
    instruments: 20,
    levels: {
      L1: { label: "一级", rows: 40, sectors: 31, instruments: 20 },
      L2: { label: "二级", rows: 30, sectors: 134, instruments: 20 },
      L3: { label: "三级", rows: 30, sectors: 346, instruments: 20 },
    },
    empty_sectors: empty,
    excluded_members: excluded,
    data_asof: "2026-11-13",
  };
  industryStatus = {
    worker_running: false,
    version,
    versions: [{ ...version, current: true }],
    jobs: [
      {
        id: "b".repeat(24),
        status: "complete",
        total: 1746,
        completed: 1746,
        levels: ["L1", "L2", "L3"],
        failures: [],
        empty_sectors: empty,
        excluded_members: excluded,
        message: "行业数据已发布；1个分类暂无A股成员，已正常跳过",
      },
    ],
  };
  const { container, root } = await mount("/data?tab=industry");
  try {
    await waitFor(() => assert.match(container.textContent, /暂无A股成员/));
    const levels = [...container.querySelectorAll('input[type="checkbox"]')];
    assert.equal(levels.length, 3);
    assert.ok(levels.every((node) => node.checked && !node.disabled));
    const header = container.querySelector(".ant-collapse-header");
    await act(async () => header.click());
    await waitFor(() => assert.match(container.textContent, /互联网药店/));
    await act(async () =>
      container.querySelectorAll(".ant-collapse-header")[1].click(),
    );
    await waitFor(() => assert.match(container.textContent, /T00018.SH/));
    await act(async () =>
      container.querySelector(".ant-table-row-expand-icon").click(),
    );
    await waitFor(() =>
      assert.equal(
        [...container.querySelectorAll("td")].filter(
          (node) => node.textContent === "互联网药店",
        ).length,
        2,
      ),
    );
    assert.match(container.textContent, /1746\/1746/);
    assert.match(container.textContent, /已完成/);
    assert.doesNotMatch(
      container.textContent,
      /部分完成|No objects to concatenate/,
    );
    assert.equal(button(container, "续传"), undefined);
  } finally {
    await act(async () => root.unmount());
    industryStatus = {
      worker_running: false,
      version: null,
      versions: [],
      jobs: [],
    };
  }
});

test("financial download previews the chosen scope, invalidates edits and queues that plan", async () => {
  downloadPosts.length = 0;
  const { container, root } = await mount("/data?tab=financial");
  await waitFor(() =>
    assert.equal(button(container, "预览要下载的股票").disabled, false),
  );
  await act(async () => button(container, "预览要下载的股票").click());
  await waitFor(() => assert.ok(button(container, "确认下载")));
  assert.deepEqual(downloadPosts[0].body, {
    market_version: "market-a",
    refresh_days: 7,
    full_refresh: false,
  });
  const fullRefresh = container.querySelector('button[role="switch"]');
  await act(async () => fullRefresh.click());
  assert.equal(button(container, "确认下载"), undefined);
  await act(async () => button(container, "预览要下载的股票").click());
  await waitFor(() => assert.ok(button(container, "确认下载")));
  assert.equal(downloadPosts[1].body.full_refresh, true);
  await act(async () => button(container, "确认下载").click());
  await waitFor(() => assert.equal(downloadPosts.length, 3));
  assert.deepEqual(downloadPosts[2].body, { plan_id: "f".repeat(24) });
  await act(async () => root.unmount());
});

test("failed preview shows its error and never enables download", async () => {
  failFinancialPreview = true;
  const { container, root } = await mount("/data?tab=financial");
  try {
    await waitFor(() =>
      assert.equal(button(container, "预览要下载的股票").disabled, false),
    );
    await act(async () => button(container, "预览要下载的股票").click());
    await waitFor(() =>
      assert.match(container.textContent, /财务接口暂时不可用/),
    );
    assert.equal(button(container, "确认下载"), undefined);
  } finally {
    failFinancialPreview = false;
    await act(async () => root.unmount());
  }
});

test("unified update previews all components and submits exactly the confirmed plan", async () => {
  downloadPosts.length = 0;
  const { container, root } = await mount("/data?tab=updates");
  await waitFor(() => assert.ok(button(container, "预览统一更新")));
  await act(async () => button(container, "预览统一更新").click());
  await waitFor(() => assert.ok(button(container, "确认统一更新")));
  assert.equal(downloadPosts[0].body.market.scope, "csi300");
  assert.equal(downloadPosts[0].body.financial.refresh_days, 7);
  assert.deepEqual(downloadPosts[0].body.industry.levels, ["L1", "L2", "L3"]);
  assert.deepEqual(downloadPosts[0].body.benchmarks, ["H00300.CSI"]);
  await act(async () => button(container, "确认统一更新").click());
  await waitFor(() => assert.equal(downloadPosts.length, 2));
  assert.deepEqual(downloadPosts[1].body, { plan_id: "b".repeat(24) });
  await act(async () => button(container, "保存自动更新计划").click());
  await waitFor(() => assert.equal(downloadPosts.length, 3));
  assert.equal(downloadPosts[2].body.enabled, false);
  assert.equal(downloadPosts[2].body.hour, 19);
  await act(async () => root.unmount());
});

test("backup UI queues create, verify and an explicitly confirmed separate restore", async () => {
  downloadPosts.length = 0;
  const { container, root } = await mount("/data?tab=backups");
  await waitFor(() => assert.ok(button(container, "校验")));
  assert.match(container.textContent, /财务.*行业.*下载检查点/);
  await act(async () => button(container, "创建完整备份").click());
  await waitFor(() => assert.equal(downloadPosts.length, 1));
  assert.deepEqual(downloadPosts[0].body, { action: "create" });
  await act(async () => button(container, "校验").click());
  await waitFor(() => assert.equal(downloadPosts.length, 2));
  assert.equal(downloadPosts[1].body.action, "verify");
  await act(async () => button(container, "恢复到新环境").click());
  await waitFor(() =>
    assert.match(document.body.textContent, /确认恢复到新环境/),
  );
  assert.equal(downloadPosts.length, 2);
  await act(async () => button(document, "确认恢复").click());
  await waitFor(() => assert.equal(downloadPosts.length, 3));
  assert.deepEqual(downloadPosts[2].body, {
    action: "restore",
    backup_id: "backup-" + "c".repeat(24),
    database: downloadPosts[2].body.database,
  });
  assert.match(downloadPosts[2].body.database, /^fq_restore_[a-z0-9_]+$/);
  await act(async () => root.unmount());
});

test("new research validates, submits once and lands on the task centre", async () => {
  window.localStorage.clear();
  const { container, root } = await mount("/research/new");
  await waitFor(() => assert.ok(button(container, "提交研究")));
  assert.doesNotMatch(container.textContent, /财务数据时间口径|首次观测/);
  // The default start leaves too little warm-up history: submission stays disabled with a reason.
  assert.match(container.textContent, /最早可从/);
  const submit = button(container, "提交研究");
  assert.equal(submit.disabled, true);
  await act(async () => button(container, "使用该日期").click());
  await waitFor(() =>
    assert.equal(button(container, "提交研究").disabled, false),
  );
  await act(async () => button(container, "提交研究").click());
  await waitFor(() => assert.equal(posted.length, 1));
  assert.equal(posted[0].body.data.snapshot_id, "shared-a");
  assert.equal("financial_policy" in posted[0].body, false);
  assert.ok(/^research-[A-Za-z0-9-]+$/.test(posted[0].key));
  await waitFor(() => assert.equal(window.location.pathname, "/tasks"));
  await waitFor(() => assert.match(container.textContent, /排队中/));
  await act(async () => root.unmount());
});

test("experiment form no longer offers a financial time mode", async () => {
  const { container, root } = await mount("/experiments/new");
  await waitFor(() => assert.ok(button(container, "提交方案比较")));
  assert.doesNotMatch(container.textContent, /财务数据时间口径|首次观测/);
  await act(async () => root.unmount());
});

test("market dataset action selects its version, edits conditions and submits the displayed configuration", async () => {
  marketEnabled = true;
  posted.length = 0;
  const { container, root } = await mount("/data?tab=datasets");
  try {
    await waitFor(() => assert.ok(button(container, "用于研究")));
    await act(async () => button(container, "用于研究").click());
    await waitFor(() => assert.match(container.textContent, /实际数据覆盖/));
    assert.match(window.location.search, /market=market-browser/);
    assert.match(container.textContent, /沪深A股全市场/);
    assert.doesNotMatch(container.textContent, /新功能|NEW/);
    const label = [...container.querySelectorAll(".ant-checkbox-wrapper")].find(
      (node) => node.textContent.includes("排除 ST"),
    );
    assert.ok(label);
    assert.equal(label.querySelector("input").checked, true);
    await act(async () => label.querySelector("input").click());
    await waitFor(() =>
      assert.equal(button(container, "提交研究").disabled, false),
    );
    await act(async () => button(container, "提交研究").click());
    await waitFor(() => assert.equal(posted.length, 1));
    assert.equal(posted[0].body.data.mode, "market_warehouse");
    assert.equal(posted[0].body.data.warehouse_id, "market-browser");
    assert.equal(posted[0].body.data.filters.exclude_st, false);
    assert.equal(posted[0].body.data.filters.min_listing_days, 180);
    await waitFor(() => assert.match(container.textContent, /全市场/));
  } finally {
    marketEnabled = false;
    await act(async () => root.unmount());
  }
});

test("pool selection has its own section and the same filters survive switching all three pools", async () => {
  marketEnabled = true;
  indexEnabled = true;
  posted.length = 0;
  window.localStorage.clear();
  const { container, root } = await mount(
    "/research/new?market=market-browser",
  );
  try {
    await waitFor(() => assert.match(container.textContent, /实际数据覆盖/));
    assert.doesNotMatch(container.textContent, /全市场与条件筛选|新功能|NEW/);
    const filterCard = [...container.querySelectorAll(".ant-card")].find(
      (node) =>
        node.querySelector(".ant-card-head-title")?.textContent === "筛选条件",
    );
    const dataCard = [...container.querySelectorAll(".ant-card")].find((node) =>
      node.textContent.includes("基础股票池"),
    );
    assert.ok(filterCard && dataCard && !dataCard.contains(filterCard));
    const stInput = () =>
      [...filterCard.querySelectorAll(".ant-checkbox-wrapper")]
        .find((node) => node.textContent.includes("排除 ST"))
        .querySelector("input");
    await act(async () => stInput().click());
    assert.equal(stInput().checked, false);
    for (const label of [
      "沪深300历史成分",
      "自选股票",
      "全市场",
      "沪深300历史成分",
    ]) {
      const choice = [
        ...container.querySelectorAll(".ant-segmented-item"),
      ].find((node) => node.textContent === label);
      assert.ok(choice, label);
      await act(async () => choice.click());
      assert.equal(stInput().checked, false);
      assert.match(container.textContent, /历史名称记录|历史上市日期/);
    }
    await waitFor(() =>
      assert.equal(button(container, "提交研究").disabled, false),
    );
    await act(async () => button(container, "提交研究").click());
    await waitFor(() => assert.equal(posted.length, 1));
    assert.equal(posted[0].body.data.mode, "index_warehouse");
    assert.equal(posted[0].body.data.filters.exclude_st, false);
    assert.equal(posted[0].body.data.filters.min_listing_days, 180);
  } finally {
    marketEnabled = false;
    indexEnabled = false;
    await act(async () => root.unmount());
  }
});

after(() => {
  dom.window.close();
  // React's scheduler leaves a MessagePort open in jsdom; exit once results are reported.
  setTimeout(() => process.exit(), 200).unref();
});
