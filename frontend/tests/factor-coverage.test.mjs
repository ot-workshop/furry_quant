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
const { hints, coverage, forms, experiments, sections } = await load({ sections: "./src/research/sections/FactorSection", experiments: "./src/pages/ExperimentNewPage", hints: "./src/components/FactorDataHint", coverage: "./src/research/factor-coverage", forms: "./src/research/form" });
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
    as_of: "2025-02-28", execution_date: "2025-03-03", total: 3, available: 3 - missing - (calls.at(-1).factor.name === "gross_margin_ttm" ? 1 : 0), unavailable: missing,
    reasons: [{ code: "history", label: "历史窗口不足或滚动样本无效", count: 1 }],
    examples: [{ name: "测试股票", code: "600000.SH", reason: "history" }],
    notes: ["缺失值不会补成零；样本减少可能改变行业分布、因子相关性和回测结果。"],
    ...(calls.at(-1).factor.name === "value" ? { breakdown: { positive: 1, zero: 1, negative: 0 } } : {}),
    ...(calls.at(-1).factor.name === "gross_margin_ttm" ? { not_applicable: 1, not_applicable_score: 50 } : {}),
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

test("earnings policy selector changes the coverage request and shows real zero profits separately", async () => {
  const root = createRoot(document.getElementById("root"));
  let current;
  function Form() {
    const [form, setForm] = React.useState(state);
    current = form;
    return React.createElement(sections.FactorSection, { form, options: config, setForm,
      update: (patch) => setForm((value) => ({ ...value, ...patch })) });
  }
  try {
    await act(async () => root.render(React.createElement(ConfigProvider, null, React.createElement(Form))));
    const control = document.querySelector('[aria-label="盈利收益率缺失处理"]');
    assert.ok(control);
    const select = control.closest(".ant-select");
    const input = select.querySelector('input[role="combobox"]');
    await act(async () => input.dispatchEvent(new dom.window.MouseEvent("mousedown", { bubbles: true })));
    await waitFor(() => assert.ok(document.querySelector('.ant-select-item-option[title="缺失记50分"]')));
    await act(async () => document.querySelector('.ant-select-item-option[title="缺失记50分"]').click());
    await waitFor(() => assert.equal(current.factors.find((row) => row.name === "value").missingPolicy, "neutral"));
    const button = document.querySelector('button[aria-label="盈利收益率数据说明"]');
    await act(async () => button.click());
    await waitFor(() => assert.ok(document.body.textContent.includes("原始值可用 2 / 3 只；缺失或受限 1 只")));
    assert.ok(document.body.textContent.includes("零利润 1 只"));
    assert.equal(calls.at(-1).factor.missing_policy, "neutral");
    assert.equal(current.factors.find((row) => row.name === "value").weight, 50);
    assert.ok(button.querySelector('[aria-label="exclamation-circle"]'));
  } finally { await act(async () => root.unmount()); }
});

test("gross margin separates inapplicable coverage from genuine missing warnings", async () => {
  const gross = {...definition, name: "gross_margin_ttm", label: "毛利率TTM", default_window: null};
  const mounted = await mount();
  try {
    missing = 0;
    await mounted.render({definition: gross, row: {...row, key: "gross_margin_ttm", name: "gross_margin_ttm", window: null, missingPolicy: "exclude"}});
    const button = document.querySelector('button[aria-label="毛利率TTM数据说明"]');
    await act(async () => button.click());
    await waitFor(() => assert.ok(document.body.textContent.includes("行业不适用 1 只；该项记50分")));
    assert.ok(button.querySelector('[aria-label="info-circle"]'));
    assert.equal(button.classList.contains("factor-data-hint-warning"), false);
    assert.equal(calls.at(-1).factor.missing_policy, "exclude");
    missing = 1;
    await mounted.render({definition: gross, row: {...row, key: "gross_margin_ttm", name: "gross_margin_ttm", window: null, missingPolicy: "zero"}});
    await waitFor(() => assert.ok(button.querySelector('[aria-label="exclamation-circle"]')));
    assert.equal(button.classList.contains("factor-data-hint-warning"), true);
    assert.equal(calls.at(-1).factor.missing_policy, "zero");
  } finally { missing = 1; await mounted.close(); }
});

test("gross selector and request round trip retain policy, weights and pinned industry", async () => {
  const gross = {...definition, name: "gross_margin_ttm", label: "毛利率TTM", default_window: null, default_skip: null};
  const options = {...config, factor_definitions: [...config.factor_definitions, gross]};
  const form = forms.defaultForm(options);
  const target = form.factors.find((r) => r.name === "gross_margin_ttm");
  Object.assign(target, {enabled: true, weight: 50, missingPolicy: "exclude"});
  const root = createRoot(document.getElementById("root"));
  let current = form;
  function Form() {
    const [state, setForm] = React.useState(form);
    current = state;
    return React.createElement(sections.FactorSection, {form: state, options, setForm, update: (patch) => setForm((value) => ({...value, ...patch}))});
  }
  try {
    await act(async () => root.render(React.createElement(ConfigProvider, null, React.createElement(Form))));
    const control = document.querySelector('[aria-label="毛利率TTM缺失处理"]');
    assert.ok(control);
    await act(async () => control.closest(".ant-select").querySelector('input[role="combobox"]').dispatchEvent(new dom.window.MouseEvent("mousedown", {bubbles: true})));
    await waitFor(() => assert.ok(document.querySelector('.ant-select-item-option[title="缺失记50分"]')));
    await waitFor(() => assert.ok(document.querySelector('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option[title="缺失记50分"]')));
    await act(async () => document.querySelector('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option[title="缺失记50分"]').click());
    await waitFor(() => assert.equal(current.factors.find((r) => r.name === "gross_margin_ttm").missingPolicy, "neutral"));
    assert.equal(current.factors.find((r) => r.name === "gross_margin_ttm").weight, 50);
    const query = coverage.coverageRequest({...current, industryVersion: "b".repeat(64)}, options, current.factors.find((r) => r.name === "gross_margin_ttm"));
    assert.equal(query.industry_version, "b".repeat(64));
    assert.equal(query.factor.missing_policy, "neutral");
    const restored = forms.factorRows([{name: "gross_margin_ttm", weight: 0.5, missing_policy: "exclude"}], options.factor_definitions);
    assert.equal(restored.find((r) => r.name === "gross_margin_ttm").missingPolicy, "exclude");
  } finally { await act(async () => root.unmount()); }
});

after(() => { dom.window.close(); setTimeout(() => process.exit(), 200).unref(); });
