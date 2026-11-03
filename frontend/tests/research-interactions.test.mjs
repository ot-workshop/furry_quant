import assert from "node:assert/strict";
import { beforeEach, afterEach, test } from "node:test";
import { createDOM } from "./helpers/dom.mjs";

// Component interactions run in an isolated DOM with controlled API fixtures.
// Charts are omitted; native Qlib and real HTTP are checked by the live suite.
const { components, React, act, createRoot } = await createDOM();
const { App, ResearchPage, BacktestPage, EvaluationPage } = components;

const clone = value => structuredClone(value);
const response = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
const listing = items => ({ items, total: items.length });
function defaults() {
  return { name: "原配置", data: { mode: "snapshot", snapshot_id: "data-full", codes: ["600000.SH", "000001.SZ", "600036.SH", "000858.SZ"] },
    factors: [{ name: "momentum", weight: .4, window: 20 }, { name: "value", weight: .3, window: null }, { name: "volatility", weight: .3, window: 10 }],
    backtest: { start: "2026-03-02", end: "2026-08-31", initial_cash: 100000, topk: 3, n_drop: 1, risk_degree: .9, open_cost: .001, close_cost: .001, min_cost: 5,
      rebalance_frequency:'daily',cost_mode:'legacy',commission_rate:.0003,tax_mode:'historical',stamp_tax_rate:.0005,transfer_fee_rate:.00001,impact_cost:0 },
    evaluation: { horizons: [1, 5, 10], groups: 3, min_cross_section: 3 } };
}
function options() {
  return { defaults: defaults(), limits: { max_codes: 50, max_queued_tasks: 20 }, benchmarks: [{ code: "000300.SH", name: "沪深300" }],
    factor_definitions: [{ name: "momentum", label: "动量", description: "动量", default_window: 20 }, { name: "value", label: "盈利收益率", description: "估值", default_window: null }, { name: "volatility", label: "低波动", description: "波动", default_window: 10 }],
    snapshots: [{ id: "data-full", label: "原始四股", start: "2025-01-01", end: "2026-09-30", trading_days: 424, benchmark: "000300.SH", validation_mode: "fallback", source: "tushare_baostock",
      codes: [{ code: "600000.SH", name: "浦发" }, { code: "000001.SZ", name: "平安" }, { code: "600036.SH", name: "招商" }, { code: "000858.SZ", name: "五粮液" }] }] };
}

test('industry switch reaches POST and reuse restores the selected mode',async()=>{
  apiOptions.industry_data={id:'a'.repeat(64),source:'fixture',instruments:20,rows:30};
  await mount(ResearchPage,{onOpenResult(){},onResultsChanged(){}});
  await waitFor(()=>assert.ok(document.getElementById('research-neutralization')));
  await input('research-neutralization','industry');
  await click(button('创建研究任务'));await waitFor(()=>assert.equal(posted.length,1));
  assert.deepEqual(posted[0].neutralization,{mode:'industry',industry_version:'a'.repeat(64)});
  await input('research-neutralization','off');await click(button('再用此配置'));
  assert.equal(document.getElementById('research-neutralization').value,'industry');
});

test('historical mode submits dynamic pool, preserves config reuse and opens the actual last signal date',async()=>{
  const dates=[];for(let d=new Date('2025-01-01T00:00:00Z');d<=new Date('2026-09-30T00:00:00Z');d.setUTCDate(d.getUTCDate()+1))
    if(![0,6].includes(d.getUTCDay()))dates.push(d.toISOString().slice(0,10));
  apiOptions.historical_index={id:'history-test',start:'2025-03-03',end:'2026-09-30',data_start:'2025-01-01',
    trading_dates:dates,codes_count:493,note:'月度快照下一交易日，非精确公告生效日'};
  taskFactory=request=>{const t=completed(request);t.result.factor_as_of='2026-08-28';return t;};
  const opened=[];await mount(ResearchPage,{onOpenResult:(...args)=>opened.push(args),onResultsChanged(){}});
  await waitFor(()=>assert.ok(document.getElementById('research-source-historical')));
  await click(document.getElementById('research-source-historical'));
  assert.match(document.getElementById('research-historical-info').textContent,/调出持仓持续尝试卖出/);
  await input('research-topk','20');await input('research-factor-window-momentum','30');
  await click(button('创建研究任务'));
  await waitFor(()=>assert.equal(posted.length,1));
  assert.deepEqual(posted[0].data,{mode:'index_warehouse',warehouse_id:'history-test'});
  assert.equal(posted[0].backtest.topk,20);assert.equal(posted[0].factors[0].window,30);
  await click(button('查看评分'));assert.deepEqual(opened[0],['factors','factor-target','2026-08-28']);
  await input('research-factor-window-momentum','60');await click(button('再用此配置'));
  assert.equal(document.getElementById('research-factor-window-momentum').value,'30');
  assert.equal(document.getElementById('research-source-historical').checked,true);
});
function completed(request = defaults()) {
  return { id: "task-target", name: request.name, request: clone(request), status: "succeeded", stage: "finished", message: "计算完成", created_at: "2026-10-31T01:00:00Z", progress: { completed: 4, total: 4 }, error: null,
    result: { data_version: "subset-target", factor_run_id: "factor-target", backtest_run_id: "backtest-target", evaluation_run_id: "evaluation-target" } };
}

test('monthly scheduling and itemized costs reach POST and survive task reuse',async()=>{
  await mount(ResearchPage,{onOpenResult(){},onResultsChanged(){}});
  await waitFor(()=>assert.ok(document.getElementById('research-rebalance-frequency')));
  await input('research-rebalance-frequency','monthly');await click(button('展开高级设置'));
  await input('research-cost-mode','itemized');
  assert.equal(document.getElementById('research-open-cost'),null);
  await input('research-commission-rate','0.02');await input('research-tax-mode','fixed');
  await input('research-stamp-tax-rate','0.05');await input('research-transfer-fee-rate','0.001');
  await input('research-impact-cost','0.15');
  await click(button('创建研究任务'));await waitFor(()=>assert.equal(posted.length,1));
  const bt=posted[0].backtest;
  assert.equal(bt.rebalance_frequency,'monthly');assert.equal(bt.cost_mode,'itemized');
  assert.equal(bt.commission_rate,.0002);assert.equal(bt.stamp_tax_rate,.0005);assert.equal(bt.transfer_fee_rate,.00001);assert.equal(bt.impact_cost,.0015);
  await input('research-rebalance-frequency','weekly');await input('research-impact-cost','0');
  await click(button('再用此配置'));
  assert.equal(document.getElementById('research-rebalance-frequency').value,'monthly');
  assert.equal(document.getElementById('research-cost-mode').value,'itemized');
  assert.equal(document.getElementById('research-impact-cost').value,'0.15');
});

test('comparison benchmark reaches the real form POST and survives task reuse',async()=>{
  apiOptions.comparison_benchmarks=[...apiOptions.benchmarks,{code:'H00300.CSI',name:'沪深300全收益指数',coverage:{start:'2004-12-31',end:'2026-10-30',rows:5300}}];
  await mount(ResearchPage,{onOpenResult(){},onResultsChanged(){}});
  await waitFor(()=>assert.ok(document.getElementById('research-comparison-benchmark')));
  await input('research-comparison-benchmark','H00300.CSI');
  assert.match(document.body.textContent,/5300条/);
  await click(button('创建研究任务'));await waitFor(()=>assert.equal(posted.length,1));
  assert.equal(posted[0].backtest.benchmark,'H00300.CSI');
  await input('research-comparison-benchmark','');await click(button('再用此配置'));
  assert.equal(document.getElementById('research-comparison-benchmark').value,'H00300.CSI');
});

test('internal evaluation failure shows its actual stage and error type with config reuse',async()=>{
  const failed=completed();failed.status='failed';failed.result=null;failed.progress={completed:3,total:4};
  failed.error='因子评估内存不足，已保留任务参数，请重试。';
  failed.error_details={kind:'runtime',job_id:failed.id,issues:[],stage:'evaluation',exception_type:'MemoryError',
    trace:[{file:'evaluation.py',function:'evaluate_factors',line:150}]};
  tasks=[failed];await mount(ResearchPage,{onOpenResult(){},onResultsChanged(){}});
  await waitFor(()=>assert.ok(document.querySelector('.research-task-error')));
  assert.match(document.querySelector('.research-task-error').textContent,/因子评估内存不足/);
  const details=[...document.querySelectorAll('details')].find(n=>n.querySelector('summary')?.textContent==='查看失败明细');
  assert.ok(details);assert.match(details.textContent,/失败阶段：因子评估/);assert.match(details.textContent,/MemoryError/);
  assert.match(details.textContent,/evaluation.py:150/);
  await click(button('再用此配置'));assert.equal(document.getElementById('research-name').value,failed.name);
});

let root, tasks, posted, urls, taskFactory, apiOptions, customFetch;
beforeEach(() => {
  document.body.innerHTML = '<div id="root"></div>';
  window.localStorage.clear();
  root = createRoot(document.getElementById("root"));
  tasks = []; posted = []; urls = []; taskFactory = completed; apiOptions = options(); customFetch = null;
  globalThis.fetch = async (url, init = {}) => {
    const parsed = new URL(url, window.location.href);
    urls.push(parsed.pathname + parsed.search);
    if (customFetch) { const answer = customFetch(parsed, init); if (answer) return answer; }
    if (parsed.pathname.endsWith("/research/options")) return response(apiOptions);
    if (parsed.pathname.endsWith("/research/tasks")) {
      if (init.method === "POST") { const body = JSON.parse(init.body); posted.push(body); const task = taskFactory(body); tasks = [task, ...tasks]; return response(task, 202); }
      return response(listing(tasks));
    }
    if (parsed.pathname.endsWith("/overview")) return response({ mode: "real", notice: "研究数据", factor_runs: 2, backtests: 2, instruments: 4 });
    return response({ detail: "未配置的测试接口：" + parsed.pathname }, 404);
  };
});
afterEach(async () => { await act(async () => root.unmount()); });
async function mount(Component, props = {}) { await act(async () => { root.render(React.createElement(Component, props)); }); }
async function tick() { await act(async () => { await new Promise(resolve => setTimeout(resolve, 5)); }); }
async function waitFor(assertion, timeout = 2000) {
  const deadline = Date.now() + timeout;
  while (true) { try { return assertion(); } catch (error) { if (Date.now() >= deadline) throw error; await tick(); } }
}
function button(text) { const found = [...document.querySelectorAll("button")].find(el => el.textContent.trim() === text); assert.ok(found, "缺少按钮：" + text); return found; }
async function click(el) { await act(async () => el.click()); }
async function input(id, value) {
  const el = document.getElementById(id); assert.ok(el, "缺少输入：" + id);
  await act(async () => {
    const prototype = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype :
      el.tagName==='TEXTAREA' ? window.HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value").set.call(el, value);
    el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? "change" : "input", { bubbles: true }));
  });
}

function tenDefinitions() {
  return [ ['momentum',20,0], ['value',null,null], ['volatility',10,null], ['book_to_price',null,null], ['size',null,null],
    ['reversal',21,1], ['medium_momentum',126,21], ['downside_volatility',20,null], ['max_drawdown',60,null], ['amount_anomaly',20,null,60] ]
    .map(([name,default_window,default_skip,default_baseline_window]) => ({name,label:name,description:name,default_window,default_skip,default_baseline_window}));
}

function datePlanFixture() {
  const dates=[];
  for(let d=new Date('2021-01-01T00:00:00Z');d<=new Date('2023-12-31T00:00:00Z');d.setUTCDate(d.getUTCDate()+1)) {
    const day=d.toISOString().slice(0,10);
    if(![0,6].includes(d.getUTCDay()) && day!=='2022-01-03')dates.push(day);
  }
  const history=dates.filter(d=>d<'2022-01-04'),future=dates.filter(d=>d>='2022-01-01');
  return {required_history:127,requested_backtest_start:'2022-01-03',first_trading_day:'2022-01-04',
    calendars:[{start:'2021-01-01',end:'2023-12-31',trading_dates:dates}],
    proposals:[{mode:'extend_history',data_start:history.at(-127),data_end:'2026-09-30',backtest_start:'2022-01-04',backtest_end:'2026-09-30',history_days:127},
      {mode:'delay_backtest',data_start:'2022-01-01',data_end:'2026-09-30',backtest_start:future[127],backtest_end:'2026-09-30',history_days:127}]};
}

function automaticFixture(body) {
  const plan=datePlanFixture();
  const required=Math.max(...body.factors.map(f=>['value','size','book_to_price'].includes(f.name) ? 0 :
    f.name==='amount_anomaly' ? f.window+f.baseline_window-1 : f.window))+1;
  const prior=plan.calendars[0].trading_dates.filter(d=>d<'2022-01-04');
  const start=prior.at(-required),end='2023-12-29';
  return {...plan,required_history:required,proposals:[{mode:'auto_range',data_start:start,data_end:end,
    backtest_start:'2022-01-04',backtest_end:end,history_days:required,warmup_start:start,warmup_end:prior.at(-1)}]};
}

for(const size of [2,7,15]) test(`automatic download preview and confirmation preserve pool ${size}, windows and weights`,async()=>{
  const codes=['000776.SZ','002156.SZ','600026.SH','600028.SH','600030.SH','600150.SH','600276.SH',
    '600999.SH','601088.SH','601318.SH','601319.SH','601601.SH','601628.SH','601857.SH','603259.SH'].slice(0,size);
  apiOptions.defaults.data={mode:'download',codes,start:'2025-01-01',end:'2026-09-30',benchmark:'000300.SH',validation_mode:'fallback'};
  const d=apiOptions.defaults;d.backtest={...d.backtest,start:'2022-01-03',end:'2023-12-31',topk:Math.min(3,size)};
  let plannedBody;
  customFetch=(url,init)=>{
    if(url.pathname.endsWith('/date-plan')) {plannedBody=JSON.parse(init.body);return response(automaticFixture(plannedBody));}
    return null;
  };
  await mount(ResearchPage,{onOpenResult(){},onResultsChanged(){}});
  await waitFor(()=>assert.ok(document.getElementById('research-auto-dates')));
  assert.equal(document.getElementById('research-auto-dates').checked,true);
  assert.equal(document.getElementById('research-data-start').disabled,true);
  await input('research-factor-window-momentum',String(30+size));
  await input('research-factor-weight-momentum','60');
  await click(document.getElementById('research-factor-value'));
  await input('research-factor-weight-volatility','40');
  await input('research-factor-window-volatility',String(5+size));
  await click(button('预览下载方案'));
  await waitFor(()=>assert.ok(document.getElementById('research-confirm-download')));
  assert.equal(posted.length,0);
  assert.equal(plannedBody.auto_range,true);
  assert.equal('data_start' in plannedBody,false);
  assert.equal('data_end' in plannedBody,false);
  assert.equal(document.getElementById('research-bt-start').value,'2022-01-03');
  assert.match(document.getElementById('research-download-confirmation').textContent,/预热：.*至.*回测：/);
  const proposal=automaticFixture(plannedBody).proposals[0];
  await click(document.getElementById('research-confirm-download'));
  await waitFor(()=>assert.equal(posted.length,1));
  assert.deepEqual(posted[0].data,{...d.data,start:proposal.data_start,end:proposal.data_end});
  assert.deepEqual(posted[0].factors,[{name:'momentum',weight:.6,window:30+size},{name:'volatility',weight:.4,window:5+size}]);
  assert.deepEqual(posted[0].backtest,{...d.backtest,start:proposal.backtest_start,end:proposal.backtest_end});
});

test('changing pool, window, weight, dates or costs invalidates download confirmation',async()=>{
  const d=apiOptions.defaults;
  d.data={mode:'download',codes:d.data.codes,start:'2025-01-01',end:'2026-09-30',benchmark:'000300.SH',validation_mode:'fallback'};
  d.backtest.start='2022-01-03';d.backtest.end='2023-12-31';
  customFetch=(url,init)=>url.pathname.endsWith('/date-plan') ? response(automaticFixture(JSON.parse(init.body))) : null;
  await mount(ResearchPage,{onOpenResult(){},onResultsChanged(){}});
  await waitFor(()=>assert.ok(document.getElementById('research-auto-dates')));
  for(const [id,value] of [['research-download-codes',d.data.codes.slice(0,3).join(',')],
    ['research-factor-window-momentum','60'],['research-factor-weight-momentum','40.0'],
    ['research-bt-end','2023-12-30'],['research-initial-cash','150000'],['research-rebalance-frequency','weekly']]) {
    await click(button('预览下载方案'));
    await waitFor(()=>assert.ok(document.getElementById('research-confirm-download')));
    await input(id,value);
    assert.equal(document.getElementById('research-confirm-download'),null,id);
    assert.equal(posted.length,0);
  }
});

test('automatic preview ignores late calendar responses after a pool edit and does not download on errors',async()=>{
  const d=apiOptions.defaults;
  d.data={mode:'download',codes:d.data.codes,start:'2025-01-01',end:'2026-09-30',benchmark:'000300.SH',validation_mode:'fallback'};
  d.backtest.start='2022-01-03';d.backtest.end='2023-12-31';
  let finish,body;
  customFetch=(url,init)=>url.pathname.endsWith('/date-plan') ? (body=JSON.parse(init.body),new Promise(resolve=>{finish=resolve;})) : null;
  await mount(ResearchPage,{onOpenResult(){},onResultsChanged(){}});
  await waitFor(()=>assert.ok(document.getElementById('research-auto-dates')));
  await click(button('预览下载方案'));
  await input('research-download-codes',d.data.codes.slice(0,3).join(','));
  await act(async()=>finish(response(automaticFixture(body))));
  assert.equal(document.getElementById('research-confirm-download'),null);
  assert.match(document.body.textContent,/其他配置已修改/);
  customFetch=url=>url.pathname.endsWith('/date-plan') ? response({detail:'交易日历暂时不可用'},503) : null;
  await click(button('预览下载方案'));
  await waitFor(()=>assert.match(document.body.textContent,/交易日历暂时不可用/));
  assert.equal(posted.length,0);
});

for(const mode of ['extend_history','delay_backtest']) test(`2022 no-calendar date planning applies ${mode} explicitly and preserves POST parameters`,async()=>{
  apiOptions.factor_definitions=tenDefinitions();
  const d=apiOptions.defaults;
  d.data={mode:'download',start:'2022-01-01',end:'2026-09-30',codes:d.data.codes,benchmark:'000300.SH',validation_mode:'fallback'};
  d.backtest.start='2022-01-03';d.backtest.end='2026-09-30';
  d.factors=tenDefinitions().map(f=>({name:f.name,weight:.1,window:f.default_window,
    ...(f.default_skip!=null && f.name!=='momentum'?{skip:f.default_skip}:{}),...(f.default_baseline_window!=null?{baseline_window:f.default_baseline_window}:{})}));
  let planningCalls=0;
  const plan=datePlanFixture();
  customFetch=(url,init)=>url.pathname.endsWith('/date-plan') ? (planningCalls++,response(plan)) : null;
  await mount(ResearchPage,{onOpenResult(){},onResultsChanged(){}});
  await waitFor(()=>assert.ok(document.getElementById('research-auto-dates')));
  await click(document.getElementById('research-auto-dates'));
  await waitFor(()=>assert.ok(document.getElementById('research-plan-dates')));
  await click(button('创建研究任务'));assert.equal(posted.length,0);
  await click(document.getElementById('research-plan-dates'));
  await waitFor(()=>assert.ok(document.getElementById(`research-apply-${mode}`)));
  assert.equal(planningCalls,1);assert.equal(posted.length,0);
  assert.equal(document.getElementById('research-data-start').value,'2022-01-01');
  assert.equal(document.getElementById('research-bt-start').value,'2022-01-03');
  assert.ok(document.getElementById('research-date-proposals').textContent.includes('非交易日'));
  await click(document.getElementById(`research-apply-${mode}`));
  const selected=plan.proposals.find(p=>p.mode===mode);
  await click(button('创建研究任务'));await waitFor(()=>assert.equal(posted.length,1));
  assert.deepEqual(posted[0].factors,d.factors);
  assert.deepEqual(posted[0].data,{...d.data,start:selected.data_start});
  assert.deepEqual(posted[0].backtest,{...d.backtest,start:selected.backtest_start,end:selected.backtest_end});
});

test('late date plan cannot overwrite edited window or dates and service errors stay visible',async()=>{
  apiOptions.defaults.data={mode:'download',start:'2022-01-01',end:'2026-09-30',codes:apiOptions.defaults.data.codes,benchmark:'000300.SH',validation_mode:'fallback'};
  apiOptions.defaults.backtest.start='2022-01-03';
  let finish;
  customFetch=url=>url.pathname.endsWith('/date-plan') ? new Promise(resolve=>{finish=resolve;}) : null;
  await mount(ResearchPage,{onOpenResult(){},onResultsChanged(){}});
  await waitFor(()=>assert.ok(document.getElementById('research-auto-dates')));
  await click(document.getElementById('research-auto-dates'));
  await waitFor(()=>assert.ok(document.getElementById('research-plan-dates')));
  await click(document.getElementById('research-plan-dates'));
  await input('research-factor-window-momentum','60');
  await act(async()=>finish(response(datePlanFixture())));
  await waitFor(()=>assert.ok(document.body.textContent.includes('查询期间日期或因子已变化')));
  assert.equal(document.getElementById('research-apply-extend_history'),null);
  assert.equal(document.getElementById('research-factor-window-momentum').value,'60');
  customFetch=url=>url.pathname.endsWith('/date-plan') ? response({detail:'交易日历暂时不可用'},503) : null;
  await click(document.getElementById('research-plan-dates'));
  await waitFor(()=>assert.ok(document.body.textContent.includes('交易日历暂时不可用')));
  assert.equal(posted.length,0);
});

test('failed ten-factor download can be corrected with cached data and explicit warm-up date without changing factors', async () => {
  apiOptions.factor_definitions=tenDefinitions();
  const dates=[];
  for (let d=new Date('2025-01-01T00:00:00Z');d<=new Date('2026-09-30T00:00:00Z');d.setUTCDate(d.getUTCDate()+1)) {
    if (![0,6].includes(d.getUTCDay())) dates.push(d.toISOString().slice(0,10));
  }
  apiOptions.snapshots[0].trading_dates=dates;
  const request=defaults();
  request.data={mode:'download',codes:request.data.codes,start:'2025-01-01',end:'2026-09-30',benchmark:'000300.SH',validation_mode:'fallback'};
  request.backtest.start='2025-03-03';request.backtest.end='2026-09-30';
  request.factors=tenDefinitions().map(d=>({name:d.name,weight:.1,window:d.default_window,
    ...(d.default_skip!=null && d.name!=='momentum' ? {skip:d.default_skip}:{}),
    ...(d.default_baseline_window!=null ? {baseline_window:d.default_baseline_window}:{})}));
  tasks=[{...completed(request),status:'failed',result:null,error:'历史预热不足'}];
  await mount(ResearchPage,{onOpenResult(){},onResultsChanged(){}});
  await waitFor(()=>assert.ok([...document.querySelectorAll('button')].find(b=>b.textContent==='再用此配置')));
  await click(button('再用此配置'));
  assert.match(document.getElementById('research-warmup').textContent,/预热不足.*127个/);
  await click(button('创建研究任务'));
  assert.equal(posted.length,0);
  await click(document.getElementById('research-use-cached-snapshot'));
  assert.equal(document.getElementById('research-source-snapshot').checked,true);
  await click(document.getElementById('research-use-warmup-start'));
  assert.equal(document.getElementById('research-bt-start').value,dates[127]);
  await click(button('创建研究任务'));
  await waitFor(()=>assert.equal(posted.length,1));
  assert.deepEqual(posted[0].factors,request.factors);
  assert.deepEqual(posted[0].data,{mode:'snapshot',snapshot_id:'data-full',codes:request.data.codes});
  assert.deepEqual(posted[0].backtest,{...request.backtest,start:dates[127]});
});

test('ten dynamic factor cards, duplicate windows, skip/baseline and reuse preserve the actual POST configuration', async () => {
  apiOptions.factor_definitions = tenDefinitions();
  await mount(ResearchPage, {onOpenResult() {},onResultsChanged() {}});
  await waitFor(() => assert.ok(document.getElementById('research-factor-size')));
  assert.equal(document.querySelectorAll('.research-factor-card').length,10);
  for (const definition of apiOptions.factor_definitions) {
    const box=document.getElementById('research-factor-'+definition.name);
    if (!box.checked) await click(box);
  }
  const card=document.getElementById('research-factor-momentum').closest('.research-factor-card');
  await click([...card.querySelectorAll('button')].find(b=>b.textContent==='添加同类不同窗口'));
  await input('research-factor-window-momentum_2','60');
  await input('research-factor-skip-momentum_2','5');
  await input('research-factor-window-amount_anomaly','6');
  await input('research-factor-baseline-amount_anomaly','12');
  await click(button('等权分配'));
  await click(button('创建研究任务'));
  await waitFor(()=>assert.equal(posted.length,1));
  assert.equal(posted[0].factors.length,11);
  assert.deepEqual(posted[0].factors.find(f=>f.id==='momentum_2'), {name:'momentum',id:'momentum_2',window:60,skip:5,weight:.0909});
  assert.equal(posted[0].factors.find(f=>f.name==='amount_anomaly').baseline_window,12);
  assert.ok(Math.abs(posted[0].factors.reduce((n,f)=>n+f.weight,0)-1)<1e-9);
  await input('research-factor-window-momentum_2','30');
  await click(button('再用此配置'));
  assert.equal(document.getElementById('research-factor-window-momentum_2').value,'60');
  assert.equal(document.getElementById('research-factor-skip-momentum_2').value,'5');
  assert.equal(document.getElementById('research-factor-baseline-amount_anomaly').value,'12');
});

test('excluded stock retains raw values, per-factor reasons and coverage in the detail view', async () => {
  const definitions={momentum:{id:'momentum',name:'momentum',label:'动量',family:'momentum',direction:'higher',required_fields:['close'],formula:'past return',params:{window:20,skip:0},lookback:20},
    book_to_price:{id:'book_to_price',name:'book_to_price',label:'账面价值收益率',family:'value',direction:'higher',required_fields:['pb'],formula:'1/PB',params:{},lookback:0}};
  const instrument={instrument:'SH600000',name:'浦发'};
  customFetch=url=> {
    if(url.pathname==='/api/v1/instruments')return response(listing([instrument]));
    if(url.pathname.includes('/scores'))return response({items:[],total:0,metadata:{factor_definitions:definitions,weights:{momentum:.5,book_to_price:.5},coverage:{universe:4,eligible:3,factors:{momentum:{valid:4,missing:0},book_to_price:{valid:3,missing:1}}}},
      diagnostics:[{date:'2026-08-31',instrument:'SH600000',raw:{momentum:.1,book_to_price:null},reasons:{book_to_price:'missing_or_nonpositive_pb'}}]});
    if(url.pathname.includes('/factor-history'))return response({items:[],scheme:'test'});
    return null;
  };
  const run={id:'factor',name:'test',as_of:'2026-08-31',available_dates:['2026-08-31'],scheme:'test',factor_definitions:definitions};
  await mount(components.StockFactorsPage,{runs:{data:listing([run]),loading:false,error:null,reload(){}},selectedRunId:'factor',onRunChange(){},selectedInstrument:instrument,onInstrumentChange(){}});
  await waitFor(()=>assert.ok(document.querySelector('[data-testid="factor-calculation-details"]')));
  const row=document.querySelector('[data-factor-id="book_to_price"]');
  assert.ok(row.textContent.includes('PB缺失或非正')&&row.textContent.includes('3/4只'));
  assert.ok(row.textContent.includes('—'));
  assert.ok(document.querySelector('[data-factor-id="momentum"]').textContent.includes('0.1'));
});

test("changing actual stock checkboxes, windows and weights reaches POST and exact result buttons", async () => {
  const opened = [];
  await mount(ResearchPage, { onOpenResult: (...args) => opened.push(args), onResultsChanged() {} });
  await waitFor(() => assert.ok(document.getElementById("research-factor-momentum")));
  const pool = [...document.querySelectorAll('input[type="checkbox"]')].find(el => el.closest("label")?.textContent.includes("600000.SH"));
  assert.ok(pool); await click(pool);
  await click(document.getElementById("research-factor-value"));
  await input("research-factor-window-momentum", "10");
  await input("research-factor-weight-momentum", "60");
  await input("research-factor-window-volatility", "20");
  await input("research-factor-weight-volatility", "40");
  await click(button("创建研究任务"));
  await waitFor(() => assert.equal(posted.length, 1));
  assert.deepEqual(posted[0].data.codes.sort(), ["000001.SZ", "000858.SZ", "600036.SH"]);
  assert.deepEqual(posted[0].factors, [{ name: "momentum", weight: .6, window: 10 }, { name: "volatility", weight: .4, window: 20 }]);
  await click(button("查看评分")); await click(button("查看回测")); await click(button("查看因子评估"));
  assert.deepEqual(opened, [["factors", "factor-target", "2026-08-31"], ["backtests", "backtest-target"], ["evaluations", "evaluation-target"]]);
});

test("offline task never claims a download stage", async () => {
  tasks = [completed()];
  await mount(ResearchPage, { onOpenResult() {}, onResultsChanged() {} });
  await waitFor(() => assert.ok(document.querySelector(".research-task")));
  const stages = document.querySelector(".research-task .research-stage-track");
  assert.ok(!stages.textContent.includes("下载数据"));
  assert.equal(stages.children.length, 4);
});

test("failed download shows concrete instrument and dates", async () => {
  const task = completed();
  task.status = 'failed'; task.error = '600150.SH 日估值缺失'; task.result = null;
  task.error_details = {job_id: 'download-first', issues: [{code: '600150.SH', api: 'daily_basic', category: 'missing_sessions', dates: ['2025-08-13', '2025-08-18'], description: '日估值：缺少交易日记录'}]};
  tasks = [task];
  await mount(ResearchPage, {onOpenResult() {}, onResultsChanged() {}});
  await waitFor(() => assert.ok(document.querySelector('.research-task-error')));
  assert.ok(document.querySelector('.research-task').textContent.includes('2025-08-18'));
  assert.ok(document.querySelector('.research-task').textContent.includes('查看失败明细'));
});

test("missing requested backtest must not silently switch to a newer run", async () => {
  const selected = [];
  customFetch = url => url.pathname.endsWith("/backtests") ? response(listing([{ id: "newer-run", created_at: "2026-10-31", metrics: {} }])) : null;
  await mount(BacktestPage, { selectedBacktestId: "missing-task-result", onSelectBacktest: id => selected.push(id) });
  await tick();
  assert.deepEqual(selected, []);
  assert.ok(urls.some(url => url.includes("/backtests/missing-task-result")));
  assert.ok(!urls.some(url => url.includes("/backtests/newer-run/")));
});

test("missing requested evaluation must not silently select the latest evaluation", async () => {
  customFetch = url => url.pathname.endsWith("/evaluations") ? response(listing([{ id: "newer-evaluation", evaluation: { horizons: [1] } }])) : null;
  await mount(EvaluationPage, { selectedEvaluationId: "missing-task-evaluation" });
  await tick();
  assert.ok(urls.some(url => url.endsWith("/evaluations/missing-task-evaluation")));
  assert.ok(!urls.some(url => url.endsWith("/evaluations/newer-evaluation")));
});

test("opening a task score uses its own pool and end date, not snapshot latest", async () => {
  const task = completed(); tasks = [task];
  let factorListCalls = 0;
  customFetch = url => {
    if (url.pathname.endsWith("/factor-runs")) {
      const result = listing([{ id: "factor-newer", name: "较新结果", as_of: "2026-09-30", available_dates: ["2026-09-30"] }, { id: "factor-target", name: "目标结果", as_of: "2026-09-30", available_dates: ["2026-08-31", "2026-09-30"], scheme: "test-scheme", sample_size: 3 }]);
      return factorListCalls++ ? new Promise(resolve => setTimeout(() => resolve(response(result)), 40)) : response(result);
    }
    if (url.pathname.endsWith("/instruments")) return response(listing([{ instrument: "SZ000001", name: "平安" }]));
    if (url.pathname.endsWith("/scores")) return response({ ...listing([]), metadata: { weights: {} } });
    if (url.pathname.endsWith("/factor-history")) return response(listing([{date: "2026-08-31", composite: 50}, {date: "2026-09-30", composite: 70}]));
    return null;
  };
  await mount(App);
  await waitFor(() => assert.ok(document.querySelector(".research-task")));
  await click(button("查看评分"));
  await waitFor(() => assert.ok(urls.some(url => url.includes("/factor-target/scores"))));
  const scoreRequests = urls.filter(url => url.includes("/factor-target/scores"));
  assert.ok(scoreRequests.every(url => new URL(url, window.location.href).searchParams.get("date") === "2026-08-31"), scoreRequests.join("\n"));
  assert.ok(urls.some(url => url.includes("/instruments?") && url.includes("run_id=factor-target")));
  assert.ok(!urls.some(url => url.includes("/factor-newer/scores")));
  await waitFor(() => assert.ok(document.querySelector('[data-testid="factor-history"]')));
  assert.deepEqual(JSON.parse(document.querySelector('[data-testid="factor-history"]').dataset.dates), ["2026-08-31"]);
});

test("switching snapshot reserves warm-up sessions for current windows", async () => {
  const dates = Array.from({ length: 31 }, (_, i) => new Date(Date.UTC(2026, 7, 3 + i))).filter(d => d.getUTCDay() !== 0 && d.getUTCDay() !== 6).map(d => d.toISOString().slice(0, 10));
  apiOptions.snapshots.push({ ...clone(apiOptions.snapshots[0]), id: "data-short", label: "较短快照", start: dates[0], end: dates.at(-1), trading_dates: dates, trading_days: dates.length });
  await mount(ResearchPage, { onOpenResult() {}, onResultsChanged() {} });
  await waitFor(() => assert.ok(document.getElementById("research-snapshot")));
  await input("research-snapshot", "data-short");
  assert.equal(document.getElementById("research-bt-start").value, dates[21]);
  assert.notEqual(document.getElementById("research-bt-start").value, dates[0]);
});

test("completion refreshes data options without losing edited draft", async () => {
  await mount(ResearchPage, { onOpenResult() {}, onResultsChanged() {} });
  await waitFor(() => assert.ok(document.getElementById("research-name")));
  await input("research-name", "保留这个名字");
  const callsBefore = urls.filter(url => url.endsWith("/research/options")).length;
  apiOptions.snapshots.push({ ...clone(apiOptions.snapshots[0]), id: "data-new", label: "新完成的数据" });
  await click(button("创建研究任务"));
  await waitFor(() => assert.ok(urls.filter(url => url.endsWith("/research/options")).length > callsBefore));
  assert.equal(document.getElementById("research-name").value, "保留这个名字");
  assert.ok([...document.getElementById("research-snapshot").options].some(option => option.value === "data-new"));
});

test('failed download refreshes published cache choices while preserving edited form', async () => {
  await mount(ResearchPage,{onOpenResult(){},onResultsChanged(){}});
  await waitFor(()=>assert.ok(document.getElementById('research-name')));
  await input('research-name','保留失败后的编辑');
  const previous=urls.filter(u=>u.endsWith('/research/options')).length;
  apiOptions.snapshots.push({...clone(apiOptions.snapshots[0]),id:'downloaded-before-failure',label:'已下载数据',is_current_download:true});
  const request=defaults();
  request.data={mode:'download',codes:request.data.codes,start:'2025-01-01',end:'2026-09-30',benchmark:'000300.SH',validation_mode:'fallback'};
  taskFactory=body=>({...completed({...request,name:body.name}),status:'failed',result:null,progress:{completed:1,total:5},error:'预热不足'});
  await click(button('创建研究任务'));
  await waitFor(()=>assert.ok(urls.filter(u=>u.endsWith('/research/options')).length>previous));
  assert.equal(document.getElementById('research-name').value,'保留失败后的编辑');
  assert.ok([...document.getElementById('research-snapshot').options].some(o=>o.value==='downloaded-before-failure'));
});
