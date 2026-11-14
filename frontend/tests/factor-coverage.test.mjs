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
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };
dom.window.ResizeObserver = globalThis.ResizeObserver;
dom.window.scrollTo = () => {};
globalThis.requestAnimationFrame = (callback) => setTimeout(callback, 0);
globalThis.cancelAnimationFrame = (id) => clearTimeout(id);
const React = await import("react");
const { act } = React;
const { createRoot } = await import("react-dom/client");
const { ConfigProvider } = await import("antd");
const { hints, coverage, forms, experiments } = await load({ experiments: "./src/pages/ExperimentNewPage", hints: "./src/components/FactorDataHint", coverage: "./src/research/factor-coverage", forms: "./src/research/form" });
const config = options();
const definition = config.factor_definitions.find((d) => d.name === "momentum");
const state = forms.defaultForm(config);
const row = state.factors.find((r) => r.name === "momentum");
let fail = false;
let missing = 1;
const calls = [];
globalThis.fetch = async (url, init) => {
  if (String(url).endsWith("/research/options")) return new Response(JSON.stringify(config), { headers: { "Content-Type": "application/json" } });
  calls.push(JSON.parse(init.body));
  return new Response(JSON.stringify(fail ? { detail: "预热历史不足" } : {
    as_of: "2025-02-28", execution_date: "2025-03-03", total: 3, available: 3 - missing, unavailable: missing,
    reasons: [{ code: "history", label: "历史窗口不足或滚动样本无效", count: 1 }],
    examples: [{ name: "测试股票", code: "600000.SH", reason: "history" }],
    notes: ["缺失值不会补成零；样本减少可能改变行业分布、因子相关性和回测结果。"],
  }), { status: fail ? 422 : 200, headers: { "Content-Type": "application/json" } });
};
async function waitFor(check) {
  const start = Date.now();
  for (;;) { try { return check(); } catch (error) { if (Date.now() - start > 4000) throw error; await act(() => new Promise((resolve) => setTimeout(resolve, 25))); } }
}
async function mount() {
  const root = createRoot(document.getElementById("root"));
  const render = async (overrides = {}) => { await act(async () => root.render(React.createElement(ConfigProvider, null,
    React.createElement(hints.FactorDataHint, { definition, options: config, form: state, row, ...overrides })))); };
  await render();
  return { render, close: async () => act(async () => root.unmount()) };
}

test("hint loads on click, shows actual missing counts and reloads after changing the factor window", async () => {
  const before = calls.length;
  const mounted = await mount();
  try {
    assert.equal(calls.length, before);
    const button = document.querySelector('button[aria-label="动量数据说明"]');
    assert.ok(button && button.querySelector('[aria-label="info-circle"]'));
    assert.equal(button.classList.contains("factor-data-hint-warning"), false);
    await act(async () => button.click());
    await waitFor(() => assert.ok(document.body.textContent.includes("可评分 2 / 3 只；暂不可评分 1 只")));
    assert.ok(button.querySelector('[aria-label="exclamation-circle"]'));
    assert.equal(button.querySelector('[aria-label="info-circle"]'), null);
    assert.equal(button.classList.contains("factor-data-hint-warning"), true);
    assert.equal(calls.at(-1).factor.window, 20);
    assert.ok(document.body.textContent.includes("2025-02-28"));
    assert.ok(document.body.textContent.includes("样本减少可能改变行业分布"));
    await mounted.render({ row: { ...row, window: 60 } });
    await waitFor(() => assert.equal(calls.at(-1).factor.window, 60));
    assert.equal(calls.length, before + 2);
  } finally { await mounted.close(); }
});

test("hover opens the same explanation; failed coverage is not shown as zero missing", async () => {
  fail = true;
  const mounted = await mount();
  try {
    const button = document.querySelector('button[aria-label="动量数据说明"]');
    await act(async () => button.dispatchEvent(new dom.window.MouseEvent("mouseover", { bubbles: true })));
    await waitFor(() => assert.ok(document.body.textContent.includes("预热历史不足")));
    assert.equal(document.body.textContent.includes("暂不可评分 0 只"), false);
    assert.ok(button.querySelector('[aria-label="info-circle"]'));
    assert.equal(button.classList.contains("factor-data-hint-warning"), false);
    fail = false;
    const retry = [...document.querySelectorAll("button")].find((b) => b.textContent.replace(/\s/g, "") === "重试");
    await act(async () => retry.click());
    await waitFor(() => assert.ok(document.body.textContent.includes("暂不可评分 1 只")));
  } finally { await mounted.close(); fail = false; }
});

test("complete coverage keeps the information icon; changing stocks clears a previous warning", async () => {
  const mounted = await mount();
  try {
    const button = document.querySelector('button[aria-label="动量数据说明"]');
    await act(async () => button.click());
    await waitFor(() => assert.ok(button.querySelector('[aria-label="exclamation-circle"]')));
    missing = 0;
    await mounted.render({ form: { ...state, codes: ["600000.SH", "600036.SH"] } });
    await waitFor(() => assert.ok(document.body.textContent.includes("可评分 3 / 3 只；暂不可评分 0 只")));
    assert.ok(button.querySelector('[aria-label="info-circle"]'));
    assert.equal(button.querySelector('[aria-label="exclamation-circle"]'), null);
    assert.equal(button.classList.contains("factor-data-hint-warning"), false);
  } finally { await mounted.close(); missing = 1; }
});

test("coverage request preserves custom stocks, source versions, filters and all window parameters", () => {
  const request = coverage.coverageRequest(state, config, { ...row, key: "momentum_2", window: 60, skip: 5, baselineWindow: 30 });
  assert.deepEqual(request.data.codes, state.codes);
  assert.equal(request.factor.id, "momentum_2");
  assert.equal(request.factor.window, 60);
  assert.equal(request.factor.skip, 5);
  assert.equal(request.factor.baseline_window, 30);
  assert.equal(request.financial_version, config.financial_data.version);
  assert.equal("weight" in request.factor, false);
  assert.throws(() => coverage.coverageRequest({ ...state, start: "" }, config, row), /开始日期/);
});

test("comparison dropdown notes do not select the factor and preserve searchable labels", async () => {
  const root = createRoot(document.getElementById("root"));
  try {
    await act(async () => root.render(React.createElement(ConfigProvider, null, React.createElement(experiments.default))));
    await waitFor(() => assert.ok(document.body.textContent.includes("候选方案")));
    const card = [...document.querySelectorAll(".ant-card")].find((element) => element.querySelector(".ant-card-head-title")?.textContent === "候选方案");
    const select = card.querySelector(".ant-select");
    const input = select.querySelector('input[role="combobox"]');
    await act(async () => input.dispatchEvent(new dom.window.MouseEvent("mousedown", { bubbles: true })));
    await waitFor(() => assert.ok(document.querySelector('.ant-select-dropdown button[aria-label="净资产收益率数据说明"]')));
    const before = select.querySelectorAll(".ant-select-selection-item").length;
    const button = document.querySelector('.ant-select-dropdown button[aria-label="净资产收益率数据说明"]');
    await act(async () => button.click());
    await waitFor(() => assert.ok(document.body.textContent.includes("暂不可评分 1 只")));
    assert.equal(select.querySelectorAll(".ant-select-selection-item").length, before);
    assert.equal(calls.at(-1).factor.name, "roe_ttm");
  } finally { await act(async () => root.unmount()); }
});

after(() => { dom.window.close(); setTimeout(() => process.exit(), 200).unref(); });
