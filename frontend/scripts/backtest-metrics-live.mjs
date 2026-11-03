// Explicit read-only integration check against a local API and saved runs.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createDOM } from '../tests/helpers/dom.mjs';
const arg = name => process.argv[process.argv.indexOf(name) + 1];
const base = arg('--base'), idsPath = arg('--ids');
if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(base ?? '') || !idsPath) throw Error('Pass local --base and --ids JSON file');
const ids = JSON.parse(await readFile(idsPath, 'utf8'));
const { dom, components, React, act, createRoot } = await createDOM({ url: base });
const originalFetch = globalThis.fetch;
globalThis.fetch = (url, options) => originalFetch(new URL(url, base), options);
const root = createRoot(document.getElementById('root'));
const expectedNumber = value => value == null ? '—' : value.toFixed(2);
async function waitFor(check) {
  const deadline = Date.now() + 30000;
  while (true) { try { return check(); } catch (error) { if (Date.now() > deadline) throw error; await act(async () => { await new Promise(r => setTimeout(r, 20)); }); } }
}
try {
  for (const id of [...ids, ids[0]]) {
    const response = await originalFetch(`${base}/api/v1/backtests/${id}`);
    assert.equal(response.status, 200);
    const detail = await response.json();
    await act(async () => root.render(React.createElement(components.BacktestPage, {selectedBacktestId: id, onSelectBacktest() { throw Error('Unexpected result fallback'); }})));
    await waitFor(() => assert.ok(document.querySelector(`a[href="/api/v1/backtests/${id}/download"]`)));
    await waitFor(() => assert.ok(document.querySelector('[data-metric="sharpe_ratio"]')));
    for (const key of ['total_return', 'annualized_return', 'volatility', 'max_drawdown', 'win_rate', 'sharpe_ratio', 'calmar_ratio', 'profit_loss_ratio']) {
      const cells = [...document.querySelectorAll(`[data-metric="${key}"] td`)];
      const percent = ['total_return', 'annualized_return', 'volatility', 'max_drawdown', 'win_rate'].includes(key);
      const format = value => value == null ? '—' : percent ? `${(value * 100).toFixed(2)}%` : expectedNumber(value);
      assert.deepEqual(cells.map(cell => cell.textContent), [format(detail.metrics[key]), format(detail.performance.benchmark_metrics[key])]);
    }
    const annual = JSON.parse(document.querySelector('[data-testid="annual-returns"]').dataset.items);
    assert.deepEqual(annual, detail.performance.annual_returns);
    assert.ok(document.querySelector('[data-metric="max_drawdown_date"]').textContent.includes(detail.performance.drawdown.trough_date ?? '—'));
    const download = await originalFetch(`${base}/api/v1/backtests/${id}/download`).then(r => r.json());
    assert.deepEqual(download.summary.metrics, detail.metrics);
    assert.deepEqual(download.performance, detail.performance);
  }
  process.stdout.write(`PERFORMANCE_UI_PASSED: ${ids.length} saved/native runs, old-result reopen and download consistency\n`);
} finally { await act(async () => root.unmount()); dom.window.close(); globalThis.fetch = originalFetch; }
