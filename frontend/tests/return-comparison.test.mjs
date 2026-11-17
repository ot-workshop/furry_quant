import assert from "node:assert/strict";
import {after, test} from "node:test";
import {JSDOM} from "jsdom";
import {load} from "./helpers/load.mjs";
import {options} from "./fixtures.mjs";

const dom=new JSDOM('<div id="root"></div>',{url:"http://127.0.0.1:8000/",pretendToBeVisual:true});
for(const name of ["window","document","navigator","HTMLElement","Element","Node","Event","MouseEvent","KeyboardEvent","getComputedStyle","SVGElement","ShadowRoot"])
  Object.defineProperty(globalThis,name,{value:name==="window"?dom.window:dom.window[name],configurable:true,writable:true});
const computed=dom.window.getComputedStyle.bind(dom.window);
dom.window.getComputedStyle=element=>computed(element);globalThis.getComputedStyle=dom.window.getComputedStyle;
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
dom.window.matchMedia=()=>({matches:false,addEventListener(){},removeEventListener(){},addListener(){},removeListener(){}});
globalThis.ResizeObserver=class{observe(){} unobserve(){} disconnect(){}};dom.window.ResizeObserver=globalThis.ResizeObserver;
dom.window.scrollTo=()=>{};
const React=await import("react");const {act}=React;const {createRoot}=await import("react-dom/client");const {ConfigProvider,App}=await import("antd");
const {form,experiments,newPage,detailPage}=await load({form:"./src/research/form",experiments:"./src/research/experiments",newPage:"./src/pages/ReturnComparisonNew",detailPage:"./src/pages/ReturnComparisonDetail"});
const config=options();
const candidates=[{id:"base",name:"基准",factors:form.factorRows(config.defaults.factors,config.factor_definitions),neutralization:{mode:"off"}},
 {id:"neutral",name:"行业对照",factors:form.factorRows(config.defaults.factors,config.factor_definitions),neutralization:{mode:"industry"}}];
const posted=[];
globalThis.fetch=async(url,init={})=>{
  const path=String(url).replace("/api/v1","");
  if(path==="/research/experiments"){
    const body=JSON.parse(init.body);posted.push(body);
    return new Response(JSON.stringify({id:"task-return-comparison",kind:"experiment",name:body.name,status:"queued",request:body}),{status:202,headers:{"Content-Type":"application/json"}});
  }
  if(path.endsWith("/equity"))return new Response(JSON.stringify({items:[{date:"2025-03-03",equity:1,benchmark:1,drawdown:0,cost:0}]}),{headers:{"Content-Type":"application/json"}});
  throw Error(`Unexpected request ${path}`);
};
async function waitFor(check){const start=Date.now();for(;;){try{return check();}catch(e){if(Date.now()-start>5000)throw e;await act(()=>new Promise(resolve=>setTimeout(resolve,30)));}}}
async function mount(component){const root=createRoot(document.getElementById("root"));await act(async()=>root.render(React.createElement(ConfigProvider,null,React.createElement(App,null,component))));return ()=>act(async()=>root.unmount());}

test("return comparison preserves the interval, shared costs and independent candidate industry settings",()=>{
  const state=form.defaultForm(config);
  const built=experiments.buildReturnComparison(state,config,candidates,"base",true);
  assert.ok(built.request,built.errors.join("\n"));
  assert.deepEqual(built.request.plan.periods,{analysis:{start:state.start,end:state.end}});
  assert.deepEqual(built.request.plan.comparison,{baseline_id:"base",sample_mode:"common"});
  assert.equal(built.request.plan.candidates[0].neutralization.mode,"off");
  assert.equal(built.request.plan.candidates[1].neutralization.mode,"industry");
  assert.equal(built.request.backtest.impact_cost,state.impactCost/100);
  assert.equal("factor_selection" in built.request,false);
  assert.equal(experiments.buildReturnComparison(state,config,[candidates[0],{...candidates[0],id:"duplicate"}],"base",true).request,null);
  assert.equal(experiments.buildReturnComparison(state,config,candidates,"missing",true).request,null);
});

test("form controls submit distinct missing policies and industry switches without requiring split dates",async()=>{
  const close=await mount(React.createElement(newPage.default,{options:config}));
  try{
    const checkbox=[...document.querySelectorAll('input[type="checkbox"]')].find(e=>e.closest("label")?.textContent.includes("固定相同股票样本"));
    assert.equal(checkbox.checked,true);await act(async()=>checkbox.click());
    const controls=[...document.querySelectorAll('[aria-label="盈利收益率缺失处理"]')];assert.equal(controls.length,2);
    await act(async()=>controls[1].closest(".ant-select").querySelector('input[role="combobox"]').dispatchEvent(new MouseEvent("mousedown",{bubbles:true})));
    await waitFor(()=>assert.ok(document.querySelector('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option[title="缺失时剔除"]')));
    await act(async()=>document.querySelector('.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option[title="缺失时剔除"]').click());
    const switches=[...document.querySelectorAll('button[role="switch"]')];
    assert.equal(switches.at(-1).getAttribute("aria-checked"),"true");await act(async()=>switches.at(-1).click());
    const submit=[...document.querySelectorAll("button")].find(b=>b.textContent.includes("运行收益对照"));
    assert.equal(submit.disabled,false);await act(async()=>submit.click());
    await waitFor(()=>assert.equal(posted.length,1));
    assert.equal(posted[0].plan.comparison.sample_mode,"independent");
    assert.equal(posted[0].plan.candidates[1].neutralization.mode,"off");
    assert.equal(posted[0].plan.candidates[1].factors.find(f=>f.name==="value").missing_policy,"exclude");
    assert.equal(posted[0].plan.candidates[0].factors.find(f=>f.name==="value").missing_policy,"zero");
    assert.equal(window.location.pathname,"/tasks");
  }finally{await close();}
});

test("result presents both sample runs, differences and links to the matching daily scores",async()=>{
  const metrics={total_return:.1,benchmark_return:.02,excess_return:.08,max_drawdown:-.05,total_cost_amount:100,avg_turnover:.1};
  const rows=candidates.flatMap(c=>["independent","common"].map(sample=>({candidate_id:c.id,period:"analysis",sample,backtest_run_id:`bt-${c.id}-${sample}`,factor_run_id:`f-${c.id}-${sample}`,evaluation_run_id:`e-${c.id}-${sample}`,metrics,coverage:{signal_days:2,scored_days:2,min_scored:2,max_scored:3},trading_days:2})));
  const result={metadata:{id:"return-comparison-fixture",name:"历史对照",created_at:"2026-11-18T00:00:00Z",validation_kind:"return_comparison"},plan:{name:"历史对照",periods:{analysis:{start:"2025-03-03",end:"2025-06-30"}},comparison:{baseline_id:"base",sample_mode:"common"},candidates:candidates.map(c=>({...c,factors:config.defaults.factors}))},shared_parameters:{},rows,comparisons:[{candidate_id:"base",independent_difference:0,common_difference:0,sample_difference:0},{candidate_id:"neutral",independent_difference:.02,common_difference:.01,sample_difference:.01}],sample_days:[{date:"2025-03-03",pool:3,common:2,candidates:{base:3,neutral:2}}],limitations:["不进行样本外选优"]};
  result.shared_parameters={backtest:{initial_cash:100000,topk:3,risk_degree:.9,open_cost:.001,close_cost:.001,impact_cost:.0005,rebalance_frequency:"weekly"},benchmark:"000300.SH"};
  const close=await mount(React.createElement(detailPage.default,{result}));
  try{
    await waitFor(()=>assert.ok(document.body.textContent.includes("样本变化差额")));
    const links=[...document.querySelectorAll("a")].map(a=>a.getAttribute("href"));
    assert.ok(links.includes("/backtests/bt-neutral-common"));
    assert.ok(links.some(href=>href.includes("run=f-base-common")&&href.includes("date=2025-03-03")));
    const notes=[...document.querySelectorAll("button")].find(b=>b.textContent.includes("口径与限制"));
    if(notes)await act(async()=>notes.click());
    assert.ok(!document.body.textContent.includes("选定："));
  }finally{await close();}
});

after(()=>{dom.window.close();setTimeout(()=>process.exit(),200).unref();});
