import assert from "node:assert/strict";
import { after, test } from "node:test";
import { JSDOM } from "jsdom";
import { load } from "./helpers/load.mjs";

const dom = new JSDOM('<div id="root"></div>', { url: "http://127.0.0.1:8000/", pretendToBeVisual: true });
for (const name of ["window", "document", "navigator", "HTMLElement", "Element", "Node", "Event", "MouseEvent", "KeyboardEvent", "getComputedStyle", "SVGElement", "ShadowRoot"])
  Object.defineProperty(globalThis, name, { value: name === "window" ? dom.window : dom.window[name], configurable: true, writable: true });
const computedStyle = dom.window.getComputedStyle.bind(dom.window);
dom.window.getComputedStyle = (element) => computedStyle(element);
globalThis.getComputedStyle = dom.window.getComputedStyle;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
dom.window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} });
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
dom.window.ResizeObserver = globalThis.ResizeObserver;
dom.window.scrollTo = () => {};
globalThis.requestAnimationFrame = (callback) => setTimeout(callback, 0);
globalThis.cancelAnimationFrame = (id) => clearTimeout(id);

const React = await import("react");
const { act } = React;
const { createRoot } = await import("react-dom/client");
const { App, ConfigProvider } = await import("antd");
const { details, market, unified } = await load({ details: "./src/pages/data/MarketJobDetails", market: "./src/pages/data/MarketDownload", unified: "./src/pages/data/UnifiedUpdates" });
const job = {
  id: "a".repeat(24), spec: { scope: "all_market", start: "2020-01-01", end: "2025-03-10", codes: [], warmup: 127 },
  status: "partial", completed: 2, total: 3, reused_rows: 100, downloaded_rows: 10,
  message: "有待复核数据，未切换当前版本；可续传", updated_at: "2025-03-10T20:00:00Z",
  failures: [{ code: "600000.SH", category: "source_conflict", reason: "两源行情冲突", dates: ["2025-03-05"], date_count: 1,
    action: "核对来源后续传，原始数据保留。", evidence: { capture: "captures/test", quarantine_file: "quarantine/asset.json" } }],
  repairs: [{ code: "000029.SZ", action: "located_pre_halt_quote", dates: ["2019-12-31"], reason: "找到停牌前报价，未更改研究起点。", source: "tushare:daily", validation_level: "single_source_passed" }],
};
let posted = [];
globalThis.fetch = async (url, init = {}) => {
  const path = String(url).replace("/api/v1", "");
  const json = (value) => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });
  if (init.method === "POST") { posted.push({ path, body: JSON.parse(init.body) }); return json({ id: "task-test", status: "queued" }); }
  if (path === "/research/market-download") return json({ worker_running: false, versions: [], registered_codes: 3, jobs: [job] });
  if (path === "/research/data-updates") return json({ schedule: null, market_versions: [], jobs: [{ id: "b".repeat(24), status: "partial", total: 1, completed: 0, updated_at: job.updated_at,
    steps: { market: { status: "partial", message: job.message, result: job } } }] });
  throw new Error(`Unexpected API ${path}`);
};
async function waitFor(check) {
  const start = Date.now();
  for (;;) { try { return check(); } catch (error) { if (Date.now() - start > 4000) throw error; await act(() => new Promise((resolve) => setTimeout(resolve, 25))); } }
}
async function mount(component, props = {}) {
  const root = createRoot(document.getElementById("root"));
  await act(async () => root.render(React.createElement(ConfigProvider, null, React.createElement(App, null, React.createElement(component, props)))));
  return async () => act(async () => root.unmount());
}

for (const [name, component, id] of [["market download", market.default, job.id], ["unified update", unified.default, "b".repeat(24)]]) {
  test(`${name} expands failure dates and automatic actions, then resumes the same plan`, async () => {
    posted = [];
    const close = await mount(component);
    try {
      await waitFor(() => assert.ok(document.querySelector(".ant-table-row-expand-icon")));
      await act(async () => document.querySelector(".ant-table-row-expand-icon").click());
      assert.ok(document.body.textContent.includes("异常日期：2025-03-05"));
      assert.ok(document.body.textContent.includes("找到停牌前报价，未更改研究起点"));
      assert.ok(document.body.textContent.includes("单源检查通过"));
      assert.ok(document.body.textContent.includes("官方 Tushare 日行情"));
      assert.ok(document.body.textContent.includes("captures/test/quarantine/asset.json"));
      assert.equal(document.body.textContent.includes("双源核验通过"), false);
      const resume = [...document.querySelectorAll("button")].find((button) => button.textContent.replace(/\s/g, "") === "续传");
      await act(async () => resume.click());
      await waitFor(() => assert.equal(posted.length, 1));
      assert.equal(posted[0].body.plan_id, id);
    } finally { await close(); }
  });
}

test("old failure records and completed jobs with only processing records remain readable", async () => {
  assert.equal(details.hasMarketDetails({ failures: [], repairs: job.repairs }), true);
  assert.equal(details.hasMarketDetails(undefined), false);
  const close = await mount(details.default, { failures: [{ code: "600000.SH", reason: "旧下载记录" }] });
  try {
    assert.ok(document.body.textContent.includes("600000.SH：旧下载记录"));
    assert.equal(document.body.textContent.includes("undefined"), false);
    assert.equal(document.body.textContent.includes("异常日期"), false);
  } finally { await close(); }
});

after(() => { dom.window.close(); setTimeout(() => process.exit(), 200).unref(); });
