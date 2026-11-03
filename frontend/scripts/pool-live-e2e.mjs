// Opt-in online check. Actual React events, local HTTP queue and native Qlib.
import assert from 'node:assert/strict';
import { readFile, writeFile } from 'node:fs/promises';
import { createDOM } from '../tests/helpers/dom.mjs';

const arg = name => process.argv[process.argv.indexOf(name) + 1];
const base = arg('--base'), casesPath = arg('--cases'), output = arg('--output');
if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(base ?? '') || !casesPath || !output) throw Error('Pass local --base, --cases and --output');
const cases = JSON.parse(await readFile(casesPath, 'utf8'));
const { dom, components, React, act, createRoot } = await createDOM({ url: base });
const originalFetch = globalThis.fetch, calls = [], submissions = [], evidence = [], rejections = [];
globalThis.fetch = async (url, init = {}) => {
  const target = new URL(url, base); calls.push(target.pathname + target.search);
  const response = await originalFetch(target, { ...init, headers: { ...init.headers, Origin: base } });
  if (target.pathname.endsWith('/research/tasks') && init.method === 'POST') submissions.push({ request: JSON.parse(init.body), task: await response.clone().json(), status: response.status });
  return response;
};
const root = createRoot(document.getElementById('root'));
async function waitFor(assertion, ms = 900000) {
  const until = Date.now() + ms;
  while (true) { try { return assertion(); } catch (error) { if (Date.now() > until) throw error; await act(async () => { await new Promise(r => setTimeout(r, 50)); }); } }
}
const button = (text, parent = document) => { const found = [...parent.querySelectorAll('button')].find(b => b.textContent.trim() === text); assert.ok(found, text); return found; };
const click = async node => { assert.ok(node); await act(async () => node.click()); };
async function input(id, value) {
  const node = document.getElementById(id); assert.ok(node, id);
  const proto = node.tagName === 'TEXTAREA' ? dom.window.HTMLTextAreaElement.prototype : node.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
  await act(async () => { Object.getOwnPropertyDescriptor(proto, 'value').set.call(node, String(value)); node.dispatchEvent(new Event(node.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true })); });
}
async function page() {
  if (!document.getElementById('research-name')) await click([...document.querySelectorAll('.nav-button')].find(b => b.textContent.includes('新建研究')));
  await waitFor(() => assert.ok(document.getElementById('research-name')), 30000);
}
const card = name => { const el = [...document.querySelectorAll('.research-task')].find(a => a.querySelector('.research-task-title')?.textContent === name); assert.ok(el, name); return el; };
async function results(entry) {
  const r = entry.task.result;
  await page(); let offset = calls.length;
  await click(button('查看评分', card(entry.name)));
  await waitFor(() => assert.equal(document.getElementById('factor-run')?.value, r.factor_run_id), 30000);
  await waitFor(() => assert.equal(document.getElementById('score-date')?.value, entry.request.backtest.end), 30000);
  await waitFor(() => assert.ok(calls.slice(offset).some(p => p.startsWith(`/api/v1/factor-runs/${r.factor_run_id}/scores?`))), 30000);
  const instruments = [...document.getElementById('instrument-select').options].map(o => o.value).filter(Boolean);
  assert.deepEqual(instruments.sort(), entry.request.data.codes.map(c => c.slice(-2) + c.slice(0, 6)).sort());
  if (entry.request.factors.length >= 10) {
    await waitFor(() => assert.equal(document.querySelectorAll('[data-testid="factor-calculation-details"] [data-factor-id]').length, entry.request.factors.length), 30000);
    for (const spec of entry.request.factors) assert.ok(document.querySelector(`[data-factor-id="${spec.id ?? spec.name}"]`));
  }
  await page(); offset = calls.length;
  await click(button('查看回测', card(entry.name)));
  await waitFor(() => assert.ok(document.querySelector(`a[href="/api/v1/backtests/${r.backtest_run_id}/download"]`)), 30000);
  assert.ok(calls.slice(offset).includes(`/api/v1/backtests/${r.backtest_run_id}`));
  await page(); await click(button('查看因子评估', card(entry.name)));
  await waitFor(() => assert.equal(document.getElementById('evaluation-select')?.value, r.evaluation_run_id), 30000);
  await waitFor(() => assert.ok(document.querySelector(`a[href="/api/v1/evaluations/${r.evaluation_run_id}/download"]`)), 30000);
  await page();
}
try {
  await act(async () => root.render(React.createElement(components.App)));
  for (const variant of cases) {
    try {
      await page();
      const source = variant.source_case ? evidence.find(e => e.key === variant.source_case)?.task?.result?.data_version : variant.snapshot_id;
      if (variant.source_case) assert.ok(source, 'Source case did not succeed');
      await click(document.getElementById(source ? 'research-source-snapshot' : 'research-source-download'));
      await input('research-name', variant.name);
      if (source) {
        await waitFor(() => assert.ok([...document.getElementById('research-snapshot').options].some(o => o.value === source)), 30000);
        await input('research-snapshot', source);
        for (const label of [...document.querySelectorAll('.research-stock-list label')]) {
          const wanted = variant.codes.includes(label.querySelector('.research-check-code').textContent);
          if (label.querySelector('input').checked !== wanted) await click(label.querySelector('input'));
        }
      } else {
        await input('research-download-codes', variant.codes.join(', '));
        await input('research-data-start', variant.data_start ?? '2025-01-01'); await input('research-data-end', variant.data_end ?? '2026-09-30');
        await input('research-validation-mode', 'fallback');
      }
      for (const spec of variant.factors.filter(f => f.id)) {
        if (!document.getElementById(`research-factor-${spec.id}`)) {
          const source = document.getElementById(`research-factor-${spec.name}`).closest('.research-factor-card');
          await click(button('添加同类不同窗口', source));
          assert.ok(document.getElementById(`research-factor-${spec.id}`));
        }
      }
      for (const box of [...document.querySelectorAll('.research-factor-head input[type="checkbox"]')]) {
        const name = box.id.replace('research-factor-', '');
        const spec = variant.factors.find(f => (f.id ?? f.name) === name);
        if (box.checked !== Boolean(spec)) await click(box);
        if (spec) {
          await input(`research-factor-weight-${name}`, spec.weight * 100);
          if (document.getElementById(`research-factor-window-${name}`)) await input(`research-factor-window-${name}`, spec.window);
          if (document.getElementById(`research-factor-skip-${name}`)) await input(`research-factor-skip-${name}`, spec.skip ?? 0);
          if (document.getElementById(`research-factor-baseline-${name}`)) await input(`research-factor-baseline-${name}`, spec.baseline_window);
        }
      }
      await input('research-bt-start', variant.start ?? '2025-05-06'); await input('research-bt-end', variant.end ?? '2026-09-30');
      await input('research-topk', variant.topk ?? 2); await input('research-initial-cash', 180000);
      if (!document.getElementById('research-n-drop')) await click(button('展开高级设置'));
      for (const [id, value] of Object.entries({ 'research-n-drop': 1, 'research-risk-degree': 85, 'research-open-cost': .1, 'research-close-cost': .15, 'research-min-cost': 5, 'research-horizons': '1, 3, 7', 'research-groups': 3, 'research-min-cross': 3 })) await input(id, value);
      const submit = button('创建研究任务');
      assert.equal(submit.disabled, false, document.querySelector('.research-error-box')?.textContent);
      const beforeCount = variant.expect_rejection ? await originalFetch(`${base}/api/v1/research/tasks`).then(r => r.json()).then(r => r.total) : null;
      const previous = submissions.length; await click(submit);
      await waitFor(() => assert.equal(submissions.length, previous + 1), 30000);
      const submitted = submissions.at(-1);
      if (variant.expect_rejection) {
        assert.equal(submitted.status, 422);
        assert.match(submitted.task.detail, /共同有效1\/2只.*成交额异动\[amount_anomaly\] 1\/2只/);
        await waitFor(() => assert.ok(document.body.textContent.includes(submitted.task.detail)), 30000);
        assert.equal(await originalFetch(`${base}/api/v1/research/tasks`).then(r => r.json()).then(r => r.total), beforeCount);
        rejections.push({key:variant.key, request:submitted.request, status:submitted.status, detail:submitted.task.detail});
        console.log(JSON.stringify({key:variant.key, stage:'expected-preflight-rejection', detail:submitted.task.detail}));
        continue;
      }
      assert.ok(submitted.task.id, JSON.stringify(submitted.task));
      let last, finished = false;
      const deadline = Date.now() + 900000;
      while (Date.now() < deadline) {
        const task = await originalFetch(`${base}/api/v1/research/tasks/${submitted.task.id}`).then(r => r.json());
        const stage = task.status + '/' + task.stage;
        if (stage !== last) { console.log(JSON.stringify({ key: variant.key, task: task.id, stage })); last = stage; }
        if (!['queued', 'running'].includes(task.status)) {
          const entry = { key: variant.key, name: variant.name, request: submitted.request, task };
          evidence.push(entry); await writeFile(output, JSON.stringify({ cases: evidence, preflight_rejections:rejections }, null, 2));
          assert.equal(task.status, variant.expect_failure ? 'failed' : 'succeeded', task.error);
          if (!variant.expect_failure) { await waitFor(() => assert.ok(card(entry.name).querySelector('.research-result')), 30000); await results(entry); }
          else { assert.ok(task.error_details?.issues.length, task.error); assert.equal(task.result, null); }
          finished = true; break;
        }
        await act(async () => { await new Promise(r => setTimeout(r, 500)); });
      }
      assert.ok(finished, `Timed out waiting for ${variant.key}`);
    } catch (error) { console.log(JSON.stringify({ key: variant.key, check_error: error.message })); throw error; }
  }
  const successes = evidence.filter(e => e.task.status === 'succeeded');
  await results(successes[0]); await results(successes.at(-1));
  await writeFile(output, JSON.stringify({ status: 'passed', cases: evidence, preflight_rejections:rejections }, null, 2));
  console.log('POOL_LIVE_PASSED');
} finally { await act(async () => root.unmount()); dom.window.close(); globalThis.fetch = originalFetch; }
