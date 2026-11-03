import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import ts from "typescript";

// Compile the production form helpers with the existing TypeScript dependency.
// The module has type-only imports and no React, DOM or network initialization.
const source = readFileSync(new URL("../src/pages/researchForm.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } });
const loaded = { exports: {} };
new Function("exports", "module", compiled.outputText)(loaded.exports, loaded);
const { buildDefaultForm, buildRequest, mergeDraft, parseCodes, warmupInfo, matchingSnapshot } = loaded.exports;

function options() {
  return {
    snapshots: [{ id: "joint-test", start: "2025-01-01", end: "2026-09-30", benchmark: "000300.SH", validation_mode: "fallback",
      codes: [{ code: "600000.SH", name: "浦发" }, { code: "000001.SZ", name: "平安" }, { code: "600036.SH", name: "招商" }] }],
    factor_definitions: [{ name: "momentum", default_window: 20 }, { name: "value", default_window: null }, { name: "volatility", default_window: 10 }],
    benchmarks: [{ code: "000300.SH", name: "沪深300" }], limits: { max_codes: 50, max_queued_tasks: 20 },
    defaults: { name: "我的研究", data: { mode: "snapshot", snapshot_id: "joint-test", codes: ["600000.SH", "000001.SZ", "600036.SH"] },
      factors: [{ name: "momentum", weight: .4, window: 20 }, { name: "value", weight: .3, window: null }, { name: "volatility", weight: .3, window: 10 }],
      backtest: { start: "2025-03-03", end: "2026-09-30", initial_cash: 100000, topk: 3, n_drop: 1, risk_degree: .9, open_cost: .001, close_cost: .001, min_cost: 5,
        rebalance_frequency:'daily',cost_mode:'legacy',commission_rate:.0003,tax_mode:'historical',stamp_tax_rate:.0005,transfer_fee_rate:.00001,impact_cost:0 },
      evaluation: { horizons: [1, 5, 10], groups: 3, min_cross_section: 3 } }
  };
}

test("server defaults round-trip without changing fee units or factor weights", () => {
  const o = options();
  const result = buildRequest(buildDefaultForm(o), o);
  assert.deepEqual(result.errors, []);
  assert.deepEqual(result.request, o.defaults);
});

test('industry toggle and pinned history survive pool changes, draft and task reuse',()=>{
  const o=options();o.industry_data={id:'a'.repeat(64)};
  const base=buildDefaultForm(o),form={...base,neutralizationMode:'industry'};
  const result=buildRequest(form,o);assert.deepEqual(result.errors,[]);
  assert.deepEqual(result.request.neutralization,{mode:'industry',industry_version:'a'.repeat(64)});
  assert.equal(mergeDraft(base,form,o).neutralizationMode,'industry');
  assert.equal(loaded.exports.formFromRequest(result.request,o).form.industryVersion,'a'.repeat(64));
  const download=buildRequest({...form,sourceMode:'download',downloadCodes:'600000.SH,000001.SZ',topk:'2'},o);
  assert.equal(download.request.neutralization.mode,'industry');
  const off=buildRequest({...form,neutralizationMode:'off'},o);
  assert.equal(off.request.neutralization.mode,'off');
  assert.deepEqual(off.request.factors,result.request.factors);
});

test('independent comparison benchmark survives all source modes, draft and task reuse',()=>{
  const o=options();o.comparison_benchmarks=[...o.benchmarks,{code:'H00300.CSI',name:'沪深300全收益指数'}];
  const form={...buildDefaultForm(o),comparisonBenchmark:'H00300.CSI'};
  const result=buildRequest(form,o);assert.deepEqual(result.errors,[]);
  assert.equal(result.request.backtest.benchmark,'H00300.CSI');
  assert.equal(result.request.data.mode,'snapshot');
  assert.equal(loaded.exports.formFromRequest(result.request,o).form.comparisonBenchmark,'H00300.CSI');
  assert.equal(mergeDraft(buildDefaultForm(o),form,o).comparisonBenchmark,'H00300.CSI');
  const download=buildRequest({...form,sourceMode:'download',downloadCodes:'600000.SH,000001.SZ',topk:'2'},o);
  assert.deepEqual(download.errors,[]);assert.equal(download.request.data.benchmark,'000300.SH');
  assert.equal(download.request.backtest.benchmark,'H00300.CSI');
  assert.match(buildRequest({...form,comparisonBenchmark:'invalid'},o).errors.join(''),/评价基准/);
});

test('weekly and itemized costs preserve units through drafts and old/new task reuse',()=>{
  const o=options(),form={...buildDefaultForm(o),rebalanceFrequency:'weekly',costMode:'itemized',commissionRate:'0.02',taxMode:'fixed',stampTaxRate:'0.05',transferFeeRate:'0.001',impactCost:'0.1'};
  const result=buildRequest(form,o);assert.deepEqual(result.errors,[]);
  const bt=result.request.backtest;
  assert.deepEqual({f:bt.rebalance_frequency,c:bt.cost_mode,commission:bt.commission_rate,stamp:bt.stamp_tax_rate,transfer:bt.transfer_fee_rate,impact:bt.impact_cost},
    {f:'weekly',c:'itemized',commission:.0002,stamp:.0005,transfer:.00001,impact:.001});
  const reused=loaded.exports.formFromRequest(result.request,o).form;
  for(const key of ['rebalanceFrequency','costMode','commissionRate','taxMode','stampTaxRate','transferFeeRate','impactCost'])assert.equal(reused[key],form[key]);
  assert.equal(mergeDraft(buildDefaultForm(o),{...form,rebalanceFrequency:'monthly'},o).rebalanceFrequency,'monthly');
  const old=structuredClone(result.request);for(const key of ['rebalance_frequency','cost_mode','commission_rate','tax_mode','stamp_tax_rate','transfer_fee_rate','impact_cost'])delete old.backtest[key];
  const legacy=loaded.exports.formFromRequest(old,o).form;assert.equal(legacy.costMode,'legacy');assert.equal(legacy.rebalanceFrequency,'daily');assert.equal(legacy.impactCost,'0');
  assert.match(buildRequest({...form,commissionRate:'70',stampTaxRate:'40'},o).errors.join(''),/合计比例/);
  assert.match(buildRequest({...form,impactCost:'-0.1'},o).errors.join(''),/冲击成本/);
});

test('historical pool payload pins the warehouse and bypasses the fixed-pool stock count',()=>{
  const o=options(),dates=[];
  for(let day=new Date('2024-01-01T00:00:00Z');day<=new Date('2026-09-30T00:00:00Z');day.setUTCDate(day.getUTCDate()+1))
    if(![0,6].includes(day.getUTCDay()))dates.push(day.toISOString().slice(0,10));
  o.historical_index={id:'warehouse-v1',start:'2024-03-01',end:'2026-09-30',data_start:'2024-01-01',trading_dates:dates};
  const form={...buildDefaultForm(o),sourceMode:'index_warehouse',warehouseId:'warehouse-v1',topk:'100'};
  const result=buildRequest(form,o);
  assert.deepEqual(result.errors,[]);assert.equal(result.codeCount,300);
  assert.deepEqual(result.request.data,{mode:'index_warehouse',warehouse_id:'warehouse-v1'});
  assert.equal(result.request.backtest.topk,100);
  const reused=loaded.exports.formFromRequest(result.request,o);
  assert.equal(reused.form.sourceMode,'index_warehouse');assert.equal(reused.form.topk,'100');
  assert.equal(reused.form.warehouseId,'warehouse-v1');
  form.warehouseId='old';assert.match(buildRequest(form,o).errors.join(''),/版本已变化/);
  form.warehouseId='warehouse-v1';form.btStart='2024-03-02';assert.match(buildRequest(form,o).errors.join(''),/交易日/);
});

test("custom pool, windows, weights, allocation and fees reach the correct payload", () => {
  const o = options();
  const form = buildDefaultForm(o);
  form.selectedCodes = ["000001.SZ", "600036.SH"];
  form.topk = "2";
  form.initialCash = "150000";
  form.riskDegree = "80";
  form.openCost = "0.2";
  form.closeCost = "0.3";
  form.factors.value.enabled = false;
  form.factors.momentum = { enabled: true, weight: "60", window: "10" };
  form.factors.volatility = { enabled: true, weight: "40", window: "20" };
  const result = buildRequest(form, o);
  assert.deepEqual(result.errors, []);
  assert.deepEqual(result.request.data.codes, ["000001.SZ", "600036.SH"]);
  assert.deepEqual(result.request.factors, [{ name: "momentum", weight: .6, window: 10 }, { name: "volatility", weight: .4, window: 20 }]);
  assert.equal(result.request.backtest.risk_degree, .8);
  assert.equal(result.request.backtest.open_cost, .002);
  assert.equal(result.request.backtest.close_cost, .003);
});

test("stock formats normalize and alias duplicates cannot be downloaded", () => {
  assert.deepEqual(parseCodes("sh600000，000001.sz\nSZ000001"), ["600000.SH", "000001.SZ", "000001.SZ"]);
  const o = options();
  const form = buildDefaultForm(o);
  form.sourceMode = "download";
  form.downloadCodes = "SH600000,600000.SH";
  assert.equal(buildRequest(form, o).request, null);
  form.downloadCodes = "hello,600000.SH";
  assert.equal(buildRequest(form, o).request, null);
});

test("unsupported boards and instruments fail before creating a research task", () => {
  const o = options();
  for (const code of ['688981.SH', '689009.SH', '430047.BJ', '510300.SH', '159915.SZ', '900901.SH', '200002.SZ', '000300.SH']) {
    const form = buildDefaultForm(o);
    form.sourceMode = 'download';
    form.downloadCodes = `${code},600900.SH`;
    form.topk = '2';
    const result = buildRequest(form, o);
    assert.equal(result.request, null);
    assert.ok(result.errors.some(error => error.includes('尚未适配') && error.includes(code)));
  }
});

test("weight totals and zero weights match strict backend acceptance", () => {
  const o = options();
  const form = buildDefaultForm(o);
  for (const factor of Object.values(form.factors)) factor.weight = "33.33";
  assert.equal(buildRequest(form, o).request, null);
  form.factors.momentum.weight = "33.34";
  assert.deepEqual(buildRequest(form, o).errors, []);
  form.factors.momentum.weight = "0";
  form.factors.value.weight = "50";
  form.factors.volatility.weight = "50";
  assert.equal(buildRequest(form, o).request, null);
});

test("invalid lookbacks, costs, position counts and diagnostics are rejected", () => {
  const o = options();
  for (const mutate of [
    f => { f.factors.momentum.window = "253"; },
    f => { f.nDrop = "4"; }, f => { f.topk = "4"; },
    f => { f.openCost = "100"; }, f => { f.horizons = "253"; },
    f => { f.groups = "11"; }, f => { f.minCrossSection = "2"; },
    f => { f.groups = "4"; f.minCrossSection = "3"; }
  ]) {
    const form = buildDefaultForm(o);
    mutate(form);
    assert.equal(buildRequest(form, o).request, null);
  }
});

test("draft restore ignores unknown stocks and malformed structures", () => {
  const o = options();
  const base = buildDefaultForm(o);
  assert.deepEqual(mergeDraft(base, null, o), base);
  const draft = mergeDraft(base, { selectedCodes: ["600000.SH", "not-in-snapshot"], factors: { momentum: null }, initialCash: 150000 }, o);
  assert.deepEqual(draft.selectedCodes, ["600000.SH"]);
  assert.equal(draft.initialCash, "150000");
  assert.deepEqual(draft.factors.momentum, base.factors.momentum);
});

test("warm-up uses both nonoverlapping amount windows and total momentum span", () => {
  const o = options();
  const dates = [];
  for (let d = new Date('2025-01-01T00:00:00Z'); d <= new Date('2026-09-30T00:00:00Z'); d.setUTCDate(d.getUTCDate() + 1)) {
    if (![0, 6].includes(d.getUTCDay())) dates.push(d.toISOString().slice(0, 10));
  }
  o.snapshots[0].trading_dates = dates;
  o.factor_definitions.push(
    { name: 'amount_anomaly', default_window: 20, default_baseline_window: 60 },
    { name: 'medium_momentum', default_window: 126, default_skip: 21 },
  );
  const form = buildDefaultForm(o);
  for (const row of Object.values(form.factors)) row.enabled = false;
  Object.assign(form.factors.amount_anomaly, { enabled: true, weight: '100' });
  form.btStart = dates[79];
  assert.ok(buildRequest(form, o).errors.some(e => e.includes('预热不足')));
  form.btStart = dates[80];
  assert.deepEqual(buildRequest(form, o).errors, []);
  form.factors.amount_anomaly.baselineWindow = '120';
  assert.ok(buildRequest(form, o).errors.some(e => e.includes('预热不足')));
  form.factors.amount_anomaly.enabled = false;
  Object.assign(form.factors.medium_momentum, { enabled: true, weight: '100' });
  form.btStart = dates[127];
  assert.deepEqual(buildRequest(form, o).errors, []);
  form.factors.medium_momentum.skip = '126';
  assert.ok(buildRequest(form, o).errors.some(e => e.includes('剔除天数')));
});

test('download preflight uses cached calendar across stock pools and never borrows earlier data', () => {
  const o = options();
  const dates=[];
  for (let d=new Date('2025-01-01T00:00:00Z');d<=new Date('2026-09-30T00:00:00Z');d.setUTCDate(d.getUTCDate()+1)) {
    if (![0,6].includes(d.getUTCDay())) dates.push(d.toISOString().slice(0,10));
  }
  o.snapshots[0].trading_dates=dates;
  o.factor_definitions.push({name:'medium_momentum',default_window:126,default_skip:21});
  const f=buildDefaultForm(o);
  f.sourceMode='download';f.downloadCodes='600900.SH,300750.SZ';f.topk='2';
  for (const row of Object.values(f.factors)) row.enabled=false;
  Object.assign(f.factors.medium_momentum,{enabled:true,weight:'100'});
  assert.match(buildRequest(f,o).errors.join(' '),/预热不足.*127个/);
  assert.equal(warmupInfo(f,o).earliest,dates[127]);
  f.btStart=dates[127];
  assert.deepEqual(buildRequest(f,o).errors,[]);
  f.dataStart=dates[70];
  assert.match(buildRequest(f,o).errors.join(' '),/预热不足/);
  assert.equal(warmupInfo(f,o).earliest,dates[197]);
  delete o.snapshots[0].trading_dates;
  f.dataStart='2025-01-01';f.btStart='2025-03-03';
  assert.match(buildRequest(f,o).errors.join(' '),/61个自然日/);
  assert.equal(warmupInfo(f,o).earliest,null);
  f.dataStart='2024-01-01';
  assert.deepEqual(buildRequest(f,o).errors,[]);
  assert.equal(warmupInfo(f,o).known,false);
});

test('cached data reuse prefers the current download version and exact pool', () => {
  const o=options(), f=buildDefaultForm(o);
  f.sourceMode='download';f.downloadCodes=o.defaults.data.codes.join(',');
  o.snapshots.push({...o.snapshots[0],id:'current-download',is_current_download:true});
  assert.equal(matchingSnapshot(f,o).id,'current-download');
  o.snapshots.unshift({...o.snapshots[1],id:'larger-current',codes:[...o.snapshots[0].codes,{code:'600900.SH',name:'长电'}]});
  assert.equal(matchingSnapshot(f,o).id,'current-download');
});
