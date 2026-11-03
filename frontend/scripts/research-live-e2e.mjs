// Explicit integration check: real React form events -> real local API -> Qlib.
// Uses an isolated DOM; it does not claim graphical browser/Canvas validation.
import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { createDOM } from "../tests/helpers/dom.mjs";

const base = process.argv[process.argv.indexOf("--base") + 1];
if (!base || !/^http:\/\/(127\.0\.0\.1|localhost):\d+$/.test(base)) throw new Error("Pass --base http://127.0.0.1:8001 for a local QA service");
const { components, React, act, createRoot } = await createDOM({ url: base });
const realFetch = globalThis.fetch;
const calls = [], submissions = [];
globalThis.fetch = async (url, init = {}) => {
  const absolute = new URL(url, base);
  calls.push(absolute.pathname + absolute.search);
  const response = await realFetch(absolute, { ...init, headers: { ...init.headers, Origin: base } });
  if (absolute.pathname.endsWith("/research/tasks") && init.method === "POST") {
    const task = await response.clone().json();
    submissions.push({ request: JSON.parse(init.body), task });
  }
  return response;
};
const root = createRoot(document.getElementById("root"));
async function tick() { await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)); }); }
async function waitFor(assertion, timeout = 120000) {
  const until = Date.now() + timeout;
  while (true) { try { return assertion(); } catch (error) { if (Date.now() >= until) throw error; await tick(); } }
}
function button(text, parent = document) {
  const node = [...parent.querySelectorAll("button")].find(el => el.textContent.trim() === text);
  assert.ok(node, "Missing button " + text); return node;
}
async function click(node) { await act(async () => node.click()); }
async function input(id, value) {
  const el = document.getElementById(id); assert.ok(el, "Missing field " + id);
  await act(async () => {
    Object.getOwnPropertyDescriptor(el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype, "value").set.call(el, String(value));
    el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  });
}
async function check(id, enabled) {
  const el = document.getElementById(id); assert.ok(el);
  if (el.checked !== enabled) await click(el);
}
async function researchPage() {
  if (!document.getElementById("research-name")) {
    const nav = [...document.querySelectorAll(".nav-button")].find(el => el.textContent.includes("新建研究"));
    await click(nav);
  }
  await waitFor(() => assert.ok(document.getElementById("research-name")));
}
function taskArticle(name) {
  const article = [...document.querySelectorAll(".research-task")].find(el => el.querySelector(".research-task-title")?.textContent === name);
  assert.ok(article, "Missing task " + name); return article;
}
async function openTaskResults(entry) {
  const result = entry.task.result;
  await researchPage();
  let start = calls.length;
  await click(button("查看评分", taskArticle(entry.name)));
  await waitFor(() => assert.equal(document.getElementById("factor-run")?.value, result.factor_run_id));
  await waitFor(() => assert.equal(document.getElementById("score-date")?.value, entry.request.backtest.end));
  await waitFor(() => assert.ok(calls.slice(start).some(p => p.includes(`/factor-runs/${result.factor_run_id}/scores?`))));
  const scoreCalls = calls.slice(start).filter(p => /\/factor-runs\/[^/]+\/scores\?/.test(p));
  assert.ok(scoreCalls.every(p => p.includes(`/${result.factor_run_id}/scores?`) && new URL(p, base).searchParams.get("date") === entry.request.backtest.end), scoreCalls.join("\n"));
  const pool = [...document.getElementById("instrument-select").options].map(o => o.value).filter(Boolean);
  assert.deepEqual(pool.sort(), entry.request.data.codes.map(c => c.slice(-2) + c.slice(0, 6)).sort());
  await waitFor(() => assert.ok(document.querySelector('[data-testid="factor-history"]')));
  const historyDates = JSON.parse(document.querySelector('[data-testid="factor-history"]').dataset.dates);
  assert.ok(historyDates.length > 0 && historyDates.every(date => date <= entry.request.backtest.end));
  await researchPage(); start = calls.length;
  await click(button("查看回测", taskArticle(entry.name)));
  await waitFor(() => assert.ok(calls.slice(start).some(p => p === `/api/v1/backtests/${result.backtest_run_id}`)));
  await waitFor(() => assert.ok(document.querySelector(`a[href="/api/v1/backtests/${result.backtest_run_id}/download"]`)));
  assert.ok(!calls.slice(start).some(p => /^\/api\/v1\/backtests\/[^/]+$/.test(p) && p !== `/api/v1/backtests/${result.backtest_run_id}`));
  await researchPage(); start = calls.length;
  await click(button("查看因子评估", taskArticle(entry.name)));
  await waitFor(() => assert.equal(document.getElementById("evaluation-select")?.value, result.evaluation_run_id));
  await waitFor(() => assert.ok(document.querySelector(`a[href="/api/v1/evaluations/${result.evaluation_run_id}/download"]`)));
  await waitFor(() => assert.ok(calls.slice(start).includes(`/api/v1/evaluations/${result.evaluation_run_id}`)));
  assert.ok(!calls.slice(start).some(p => /^\/api\/v1\/evaluations\/[^/]+$/.test(p) && p !== `/api/v1/evaluations/${result.evaluation_run_id}`));
  await researchPage();
}

const stamp = Date.now();
const cases = [
  { key: "baseline", momentum: 10, weight: 60 },
  { key: "weights", momentum: 10, weight: 20 },
  { key: "window", momentum: 60, weight: 60 },
  { key: "pool", momentum: 10, weight: 60, codes: ["000001.SZ", "000858.SZ", "600036.SH"] },
  { key: "end-date", momentum: 10, weight: 60, end: "2026-08-31" },
];
const evidence = [];
try {
  await act(async () => root.render(React.createElement(React.StrictMode, null, React.createElement(components.App))));
  for (const variant of cases) {
    await researchPage();
    const name = `流程回归-${stamp}-${variant.key}`;
    await input("research-name", name);
    await click(button("全选"));
    if (variant.codes) {
      for (const label of [...document.querySelectorAll(".research-stock-list label")]) {
        const code = label.querySelector(".research-check-code").textContent;
        if (!variant.codes.includes(code)) await click(label.querySelector("input"));
      }
    }
    await check("research-factor-value", false);
    await check("research-factor-momentum", true);
    await check("research-factor-volatility", true);
    await input("research-factor-window-momentum", variant.momentum);
    await input("research-factor-window-volatility", 20);
    await input("research-factor-weight-momentum", variant.weight);
    await input("research-factor-weight-volatility", 100 - variant.weight);
    await input("research-bt-start", "2026-03-02");
    await input("research-bt-end", variant.end ?? "2026-09-30");
    await input("research-topk", 2);
    await input("research-initial-cash", 150000);
    if (!document.getElementById("research-risk-degree")) await click(button("展开高级设置"));
    for (const [id, value] of Object.entries({ "research-n-drop": 1, "research-risk-degree": 80, "research-open-cost": .2, "research-close-cost": .3, "research-min-cost": 8 })) await input(id, value);
    const before = submissions.length;
    await click(button("创建研究任务"));
    await waitFor(() => assert.equal(submissions.length, before + 1));
    const submitted = submissions.at(-1);
    assert.ok(submitted.task.id, JSON.stringify(submitted.task));
    assert.equal(submitted.request.factors.find(f => f.name === "momentum").window, variant.momentum);
    assert.equal(submitted.request.factors.find(f => f.name === "momentum").weight, variant.weight / 100);
    await waitFor(() => {
      const article = taskArticle(name);
      assert.ok(!article.querySelector(".research-task-error"), article.textContent);
      assert.equal(article.querySelector(".status-pill").textContent.trim(), "已完成");
    });
    const task = await realFetch(`${base}/api/v1/research/tasks/${submitted.task.id}`).then(r => r.json());
    assert.equal(task.status, "succeeded", task.error);
    const entry = { key: variant.key, name, request: submitted.request, task };
    await openTaskResults(entry);
    evidence.push(entry);
    console.log(JSON.stringify({ case: variant.key, status: "passed", task_id: task.id, ...task.result }));
  }
  // Re-open an older task after newer runs have been published.
  await openTaskResults(evidence[0]);
  await openTaskResults(evidence.at(-1));
  await mkdir(new URL("../../.agent-work/", import.meta.url), { recursive: true });
  await writeFile(new URL("../../.agent-work/research-live-evidence.json", import.meta.url), JSON.stringify({ status: "passed", cases: evidence }, null, 2));
  console.log("LIVE_WORKFLOW_PASSED: form events, 5 Qlib tasks and exact result navigation");
} finally {
  await act(async () => root.unmount());
}
