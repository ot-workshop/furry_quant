import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';
import { createDOM } from './helpers/dom.mjs';

const { components: { BacktestPage }, React, act, createRoot } = await createDOM();
let root, detail;
const metric = key => document.querySelector(`[data-metric="${key}"]`);
const json = value => new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } });

beforeEach(() => {
  document.body.innerHTML = '<div id="root"></div>';
  root = createRoot(document.getElementById('root'));
  detail = { id: 'selected-backtest', name: '已保存的真实回测', status: 'succeeded', strategy: 'Qlib',
    start_date: '2025-03-03', end_date: '2026-09-30', is_synthetic: false, created_at: '2026-10-31',
    metrics: { total_return: .15, annualized_return: .1, volatility: .2, sharpe_ratio: .75, max_drawdown: -.12,
      calmar_ratio: .58, win_rate: .6, profit_loss_ratio: 1.25, avg_turnover: .08, total_cost: .02,
      total_cost_amount: 2000, trade_count: 100, total_days: 388, years: 388 / 252 },
    performance: { version: 'daily-nav-performance-v1', available: true,
      benchmark_metrics: { total_return: .08, annualized_return: .05, volatility: .22, sharpe_ratio: .2, max_drawdown: -.2, calmar_ratio: .1, win_rate: .51, profit_loss_ratio: .9 },
      drawdown: { peak_date: '2025-03-05', trough_date: '2025-04-07' }, benchmark_drawdown: { peak_date: '2025-03-04', trough_date: '2025-04-08' },
      annual_returns: [{year: 2025, start_date: '2025-03-03', end_date: '2025-12-31', trading_days: 207, strategy_return: .1, benchmark_return: .07},
        {year: 2026, start_date: '2026-01-05', end_date: '2026-09-30', trading_days: 181, strategy_return: 1.15/1.1-1, benchmark_return: 1.08/1.07-1}],
      methodology: {trading_days_per_year: 252, annual_risk_free_rate: .03, std_ddof: 1} }
  };
  globalThis.fetch = async url => {
    const path = new URL(url, window.location.href).pathname;
    if (path.endsWith('/backtests')) return json({items: [detail], total: 1});
    if (path === '/api/v1/backtests/selected-backtest') return json(detail);
    if (path.endsWith('/equity')) return json({items: []});
    return json({items: [], total: 0, offset: 0, limit: 50});
  };
});
afterEach(async () => { await act(async () => root.unmount()); });
async function mount() {
  await act(async () => root.render(React.createElement(BacktestPage, {selectedBacktestId: detail.id, onSelectBacktest() {}})));
  const end = Date.now() + 2000;
  while (!metric('sharpe_ratio')) {
    if (Date.now() > end) throw Error('Performance table did not render');
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 5)); });
  }
}

test('saved results display old-style annual chart and full strategy/benchmark metric table', async () => {
  await mount();
  assert.ok(document.querySelector('.performance-layout .performance-annual'));
  assert.ok(document.querySelector('.performance-layout .performance-table'));
  assert.deepEqual([...metric('volatility').querySelectorAll('td')].map(cell => cell.textContent), ['20.00%', '22.00%']);
  assert.deepEqual([...metric('sharpe_ratio').querySelectorAll('td')].map(cell => cell.textContent), ['0.75', '0.20']);
  assert.ok(metric('calmar_ratio').textContent.includes('卡玛比率'));
  assert.ok(metric('win_rate').textContent.includes('日收益胜率'));
  assert.ok(metric('profit_loss_ratio').textContent.includes('1.25'));
  assert.ok(metric('max_drawdown_date').textContent.includes('2025-04-07'));
  const years = JSON.parse(document.querySelector('[data-testid="annual-returns"]').dataset.items);
  assert.deepEqual(years, detail.performance.annual_returns);
  assert.ok(document.querySelector('.performance-summary').textContent.includes('无风险年利率假设3.00%'));
  assert.ok(document.querySelector('.performance-summary').textContent.includes('均非逐笔交易胜率'));
  assert.ok(document.querySelector('a[href="/api/v1/backtests/selected-backtest/download"]'));
});

test('total-return source and independent relative metrics are visible',async()=>{
  detail.benchmark_name='沪深300全收益指数（H00300.CSI）';
  detail.benchmark={code:'H00300.CSI',return_type:'gross_total_return',start:'2025-02-28',end:'2026-09-30',observations:389};
  Object.assign(detail.metrics,{relative_return:1.15/1.08-1,tracking_error:.12,information_ratio:.75});
  await mount();
  const text=document.querySelector('[data-testid="benchmark-description"]').textContent;
  assert.match(text,/H00300.CSI/);assert.match(text,/官方Tushare/);assert.match(text,/分红再投资/);
  for(const label of ['相对净值收益','年化跟踪误差','信息比率'])assert.ok(document.querySelector('.performance-extras').textContent.includes(label));
});

test('undefined ratios stay dashes while zero volatility and no trades stay zero', async () => {
  detail.metrics = {...detail.metrics, volatility: 0, sharpe_ratio: null, calmar_ratio: null, profit_loss_ratio: null, trade_count: 0};
  await mount();
  assert.equal(metric('volatility').querySelector('td').textContent, '0.00%');
  assert.equal(metric('sharpe_ratio').querySelector('td').textContent, '—');
  assert.equal(metric('calmar_ratio').querySelector('td').textContent, '—');
  assert.equal(metric('profit_loss_ratio').querySelector('td').textContent, '—');
  assert.ok([...document.querySelectorAll('.metric-card')].some(card => card.textContent.includes('成交笔数') && card.querySelector('.metric-value').textContent === '0'));
});

test('incomplete NAV or benchmark does not become fabricated annual bars or zero risk', async () => {
  detail.performance.available = false;
  detail.performance.annual_returns = [];
  detail.performance.benchmark_metrics = {};
  detail.metrics = {sharpe_ratio: null, volatility: null};
  await mount();
  assert.ok(document.querySelector('.performance-summary .status-note').textContent.includes('无法计算'));
  assert.equal(document.querySelector('[data-testid="annual-returns"]'), null);
  assert.equal(metric('volatility').querySelector('td').textContent, '—');
  assert.deepEqual([...metric('sharpe_ratio').querySelectorAll('td')].map(cell => cell.textContent), ['—', '—']);
});
