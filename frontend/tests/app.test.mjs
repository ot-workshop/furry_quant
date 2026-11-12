import assert from "node:assert/strict";
import { after, test } from "node:test";
import { JSDOM } from "jsdom";
import { load } from "./helpers/load.mjs";
import { options } from "./fixtures.mjs";

const dom = new JSDOM('<div id="root"></div>', { url: "http://127.0.0.1:8000/", pretendToBeVisual: true });
for (const name of ["window", "document", "navigator", "HTMLElement", "Element", "Node", "Event", "MouseEvent", "KeyboardEvent", "getComputedStyle", "SVGElement", "ShadowRoot"])
  Object.defineProperty(globalThis, name, { value: name === "window" ? dom.window : dom.window[name], configurable: true, writable: true });
const computedStyle = dom.window.getComputedStyle.bind(dom.window);
dom.window.getComputedStyle = (element) => computedStyle(element);
globalThis.getComputedStyle = dom.window.getComputedStyle;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
dom.window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
globalThis.matchMedia = dom.window.matchMedia;
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
dom.window.ResizeObserver = globalThis.ResizeObserver;
dom.window.scrollTo = () => {};
globalThis.requestAnimationFrame = (callback) => setTimeout(callback, 0);
globalThis.cancelAnimationFrame = (id) => clearTimeout(id);

const React = await import("react");
const { act } = React;
const { createRoot } = await import("react-dom/client");
const { app, theme } = await load({ app: "./src/App", theme: "./src/app/theme" });

const posted = [];
let tasks = [];
const downloadPosts = [];
let failFinancialPreview = false;
globalThis.fetch = async (url, init = {}) => {
  const path = String(url).replace("/api/v1", "");
  const json = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
  if (path === "/research/options") {
    const value = options();
    value.defaults.backtest.start = "2025-01-10"; // too early for a 20-day momentum window
    return json(value);
  }
  if (path === "/research/tasks" && init.method === "POST") {
    const body = JSON.parse(init.body);
    posted.push({ body, key: init.headers["Idempotency-Key"] });
    const task = { id: "task-" + "a".repeat(32), kind: "research", name: body.name, status: "queued", stage: "queued", message: "等待运行", created_at: "2026-01-01T00:00:00Z", started_at: null, finished_at: null, progress: { completed: 0, total: 4 }, request: body, result: null, error: null };
    tasks = [task];
    return json(task, 202);
  }
  if (path === "/research/tasks") return json({ items: tasks, total: tasks.length });
  if (path === "/data-status") return json({ snapshots: [], database: null });
  if (path === "/research/industry-download") return json({ worker_running: false, version: null, versions: [], jobs: [] });
  if (path === "/research/financial-download") return json({ worker_running: false, version: null, versions: [], jobs: [], market_versions: [{ id: "market-a", scope: "custom", start: "2024-01-01", end: "2025-12-31", codes_count: 3, restricted: false }] });
  if (path === "/research/financial-download/preview") {
    if (failFinancialPreview) return json({ detail: "测试：财务接口暂时不可用" }, 503);
    downloadPosts.push({ path, body: JSON.parse(init.body) });
    return json({ id: "f".repeat(24), spec: JSON.parse(init.body), codes: [{ code: "600000.SH", rows: 0 }], skipped: 0, estimated_requests: 9, requests_per_stock: 9, note: "按报表续传", point_in_time_certified: false });
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
    root.render(React.createElement(theme.ThemeProvider, null, React.createElement(app.default)));
  });
  return { container, root };
}

const button = (container, text) =>
  [...container.querySelectorAll("button")].find((node) => node.textContent.replace(/\s/g, "").includes(text));

test("dashboard points to data management when no dataset exists", async () => {
  const { container, root } = await mount("/");
  await waitFor(() => assert.match(container.textContent, /还没有可用于研究的数据集/));
  assert.ok(button(container, "去下载行情"));
  await act(async () => root.unmount());
});

test("industry data tab renders without a published version", async () => {
  const { container, root } = await mount("/data?tab=industry");
  await waitFor(() => assert.match(container.textContent, /还没有行业数据/));
  assert.ok(button(container, "预览下载"));
  await act(async () => root.unmount());
});

test("financial download previews the chosen scope, invalidates edits and queues that plan", async () => {
  downloadPosts.length = 0;
  const { container, root } = await mount("/data?tab=financial");
  await waitFor(() => assert.equal(button(container, "预览要下载的股票").disabled, false));
  await act(async () => button(container, "预览要下载的股票").click());
  await waitFor(() => assert.ok(button(container, "确认下载")));
  assert.deepEqual(downloadPosts[0].body, { market_version: "market-a", refresh_days: 7, full_refresh: false });
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
    await waitFor(() => assert.equal(button(container, "预览要下载的股票").disabled, false));
    await act(async () => button(container, "预览要下载的股票").click());
    await waitFor(() => assert.match(container.textContent, /财务接口暂时不可用/));
    assert.equal(button(container, "确认下载"), undefined);
  } finally {
    failFinancialPreview = false;
    await act(async () => root.unmount());
  }
});

test("new research validates, submits once and lands on the task centre", async () => {
  window.localStorage.clear();
  const { container, root } = await mount("/research/new");
  await waitFor(() => assert.ok(button(container, "提交研究")));
  // The default start leaves too little warm-up history: submission stays disabled with a reason.
  assert.match(container.textContent, /最早可从/);
  const submit = button(container, "提交研究");
  assert.equal(submit.disabled, true);
  await act(async () => button(container, "使用该日期").click());
  await waitFor(() => assert.equal(button(container, "提交研究").disabled, false));
  await act(async () => button(container, "提交研究").click());
  await waitFor(() => assert.equal(posted.length, 1));
  assert.equal(posted[0].body.data.snapshot_id, "shared-a");
  assert.ok(/^research-[A-Za-z0-9-]+$/.test(posted[0].key));
  await waitFor(() => assert.equal(window.location.pathname, "/tasks"));
  await waitFor(() => assert.match(container.textContent, /排队中/));
  await act(async () => root.unmount());
});

after(() => {
  dom.window.close();
  // React's scheduler leaves a MessagePort open in jsdom; exit once results are reported.
  setTimeout(() => process.exit(), 200).unref();
});
