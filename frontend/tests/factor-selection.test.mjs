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
const { App, ConfigProvider } = await import("antd");
const { correlation, page, selection, forms, experiments, drafts, routes } = await load({
  correlation: "./src/evaluation/CorrelationAnalysis", page: "./src/pages/ExperimentNewPage", selection: "./src/api/factorSelection",
  forms: "./src/research/form", experiments: "./src/research/experiments", drafts: "./src/research/selection-draft", routes: "./src/app/routes",
});
const parameters = { horizon: 1, cutoff: "2025-02-28", factor_count: 1, min_ic: 0, min_ir: 0, max_correlation: .6, min_dates: 20, start: null };
const reference = { evaluation_id: "selection-source", selection_id: "factor-selection-fixture", parameters };
const selected = { reference, parameters, evaluation_id: reference.evaluation_id, selection_id: reference.selection_id, status: "optimal", selected_keys: ["momentum"], stages: { input: 2, quality_passed: 1, dominance_removed: 0, milp_candidates: 1 }, candidates: [{ key: "momentum", rank_ic: { mean: .05, std: .02, ir: 2.5, valid_dates: 22 }, last_label_exit: parameters.cutoff, unmatured_dates: 2, status: "selected", reasons: ["质量及相关性约束通过，MILP 入选"] }], methodology: [], message: "已找到满足条件的最优因子子集", max_selected_correlation: null, mean_selected_correlation: null };
const config = options();
const seed = {
  research: { ...config.defaults, neutralization: { mode: "off" }, factors: [{ name: "momentum", weight: 1, window: 20 }], evaluation: { horizons: [1], groups: 3, min_cross_section: 3 } },
  reference, selection: selected, warnings: ["所有比较分段均晚于推荐截止日"],
  financial_data: config.financial_data, industry_data: config.industry_data, financial_coverage: config.snapshots[0].financial_coverage,
  candidates: [{ id: "milp", name: "MILP 等权组合", factors: [{ name: "momentum", window: 20, weight: 1 }] }, { id: "baseline", name: "原因子组合", factors: [{ name: "value", window: null, weight: 1 }] }],
  periods: { development: { start: "2025-03-03", end: "2025-03-31" }, validation: { start: "2025-04-01", end: "2025-05-14" }, test: { start: "2025-05-15", end: "2025-06-30" } },
};
const requests = [];
let rejectDraft = false;
globalThis.fetch = async (url, init = {}) => {
  const parsed = new URL(String(url), dom.window.location.origin);
  requests.push({ path: parsed.pathname, params: parsed.searchParams, body: init.body && JSON.parse(init.body) });
  const json = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
  if (parsed.pathname.endsWith("/selection/draft")) return rejectDraft ? json({ detail: "来源行情版本不可用" }, 422) : json(seed);
  if (parsed.pathname.endsWith("/selection")) return json(selected);
  if (parsed.pathname.endsWith("/analysis")) return json({ correlations: { names: ["momentum", "value"], cells: [], pair: { daily: [] } } });
  if (parsed.pathname.endsWith("/research/options")) return json(config);
  if (parsed.pathname.endsWith("/research/experiments")) return json({ id: "task-fixture", name: "因子筛选对照" }, 202);
  return json({ detail: "unmocked" }, 404);
};
async function waitFor(check) {
  const started = Date.now();
  for (;;) { try { return check(); } catch (error) { if (Date.now() - started > 4000) throw error; await act(() => new Promise((resolve) => setTimeout(resolve, 20))); } }
}
async function mount(Component, props) {
  const root = createRoot(document.getElementById("root"));
  await act(async () => root.render(React.createElement(ConfigProvider, null, React.createElement(App, null, React.createElement(Component, props)))));
  return async () => { await act(async () => root.unmount()); };
}
function button(label) { return [...document.querySelectorAll("button")].find((node) => node.textContent.trim() === label); }
async function setValue(selector, value) {
  const input = document.querySelector(selector);
  assert.ok(input, selector);
  await act(async () => { Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, "value").set.call(input, value); input.dispatchEvent(new dom.window.Event("input", { bubbles: true })); input.dispatchEvent(new dom.window.Event("change", { bubbles: true })); });
}

test("correlation matrix precedes selection; parameters apply and edits invalidate handoff", async () => {
  const unmount = await mount(correlation.CorrelationAnalysis, { result: { metadata: { id: reference.evaluation_id, start: "2025-01-02", end: "2025-06-30" }, factors: { momentum: {}, value: {}, composite: {} } }, horizon: "1" });
  try {
    await waitFor(() => assert.ok(button("筛选组合")));
    const text = document.body.textContent;
    assert.ok(text.indexOf("平均秩相关矩阵") < text.indexOf("组合筛选"));
    await setValue('[aria-label="筛选因子数量"]', "1");
    await setValue('[aria-label="最低平均 Rank IC"]', "0.03");
    await act(async () => button("筛选组合").click());
    await waitFor(() => assert.equal(button("创建分段比较").disabled, false));
    const sent = requests.filter((row) => row.path.endsWith("/selection")).at(-1);
    assert.equal(sent.params.get("min_ic"), "0.03");
    assert.equal(sent.params.get("factor_count"), "1");
    assert.ok(document.body.textContent.includes("质量预筛"));
    await setValue('[aria-label="组合相关性上限"]', "0.4");
    assert.equal(button("创建分段比较").disabled, true);
    assert.ok(document.body.textContent.includes("参数已修改"));
  } finally { await unmount(); }
});

test("seeded comparison locks source and MILP factors, preserves reference and submits post-cutoff periods", async () => {
  const unmount = await mount(page.default, { selection: JSON.stringify(reference) });
  try {
    await waitFor(() => assert.ok(button("提交方案比较")));
    await waitFor(() => assert.equal(button("提交方案比较").disabled, false));
    assert.ok(document.body.textContent.includes("组合筛选截止日：2025-02-28"));
    const milpName = [...document.querySelectorAll("input")].find((input) => input.value === "MILP 等权组合");
    assert.equal(milpName.disabled, true);
    await act(async () => button("提交方案比较").click());
    const sent = requests.filter((row) => row.path.endsWith("/research/experiments")).at(-1).body;
    assert.deepEqual(sent.factor_selection, reference);
    assert.equal(sent.plan.candidates[0].id, "milp");
    assert.equal(sent.plan.candidates[0].factors[0].window, 20);
    assert.equal(sent.plan.candidates[0].factors[0].weight, 1);
    assert.ok(Object.values(sent.plan.periods).every((period) => period.start > reference.parameters.cutoff));
    assert.equal(requests.find((row) => row.path.endsWith("/selection/draft")).params.get("selection_id"), reference.selection_id);
  } finally { await unmount(); }
});

test("invalid source is an error and cannot silently load the default pool", async () => {
  rejectDraft = true;
  const unmount = await mount(page.default, { selection: JSON.stringify(reference) });
  try {
    await waitFor(() => assert.ok(document.body.textContent.includes("来源行情版本不可用")));
    assert.equal(button("提交方案比较"), undefined);
  } finally { await unmount(); rejectDraft = false; }
});

test("selection draft pins source versions and request builder prevents overlap", () => {
  const old = { ...seed, financial_data: { ...config.financial_data, version: "old-financial" }, industry_data: { ...config.industry_data, id: "old-industry" } };
  const merged = drafts.selectionOptions(config, old);
  assert.equal(merged.financial_data.version, "old-financial");
  assert.equal(merged.industry_data.id, "old-industry");
  const form = forms.formFromRequest(seed.research, config).form;
  const candidates = seed.candidates.map((row) => ({ ...row, factors: forms.factorRows(row.factors, config.factor_definitions) }));
  const split = { validation: seed.periods.validation.start, test: seed.periods.test.start };
  assert.deepEqual(experiments.buildExperiment(form, config, candidates, split, reference).request.factor_selection, reference);
  const invalid = experiments.buildExperiment({ ...form, start: "2025-02-28" }, config, candidates, split, reference);
  assert.equal(invalid.request, null);
  assert.ok(invalid.errors.some((message) => message.includes("晚于推荐截止日")));
  const encoded = routes.paths.newExperiment(JSON.stringify(reference));
  assert.deepEqual(selection.parseSelectionReference(new URL(encoded, "http://localhost").searchParams.get("selection")), reference);
  assert.throws(() => selection.parseSelectionReference('{"evaluation_id":1}'));
});

after(() => {
  dom.window.close();
  // Match the app suite: React's jsdom scheduler retains a MessagePort.
  setTimeout(() => process.exit(), 200).unref();
});
