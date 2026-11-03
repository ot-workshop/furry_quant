import assert from 'node:assert/strict';
import test from 'node:test';
import {createDOM} from './helpers/dom.mjs';
const {dom,components,React,act,createRoot}=await createDOM();
const state=()=>({status:'partial',worker_running:false,pause_requested:false,total:493,verified:1,validation_counts:{single_source_passed:1,dual_source_verified:0,secondary_fallback_passed:0},failed:2,raw_captures:3,
  plan:{start:'2020-01-02',end:'2026-10-30',data_start:'2019-06-28',warmup_sessions:127,snapshots:83},
  budget:{day:'2026-11-01',requests:[{source:'baostock',api:'sdk_message',count:20}],holds:[]},
  limits:{baostock_daily_requests:10000,baostock_per_minute:60},failures:[{code:'000001.SZ',reason:'BaoStock复权待核验'}]});
const response=(s,status=200)=>new Response(JSON.stringify(s),{status,headers:{'Content-Type':'application/json'}});
test('warehouse panel separates captures and verification, starts explicitly, and pauses cooperatively',async()=>{
  document.body.innerHTML='<div id="root"></div>';const root=createRoot(document.getElementById('root'));
  const posts=[];let current=state();
  globalThis.fetch=async(url,init={})=>{
    if(init.method==='POST'){
      posts.push({url,body:JSON.parse(init.body)});
      current={...current,worker_running:true,pause_requested:String(url).endsWith('/pause')};
    }
    return response(current);
  };
  try{
    await act(async()=>root.render(React.createElement(components.IndexDownloadPanel)));
    assert.equal(posts.length,0);
    assert.match(document.body.textContent,/主源四类原始响应已保存 3 \/ 493 只；已通过检查 1 只；待复核 2 只/);
    assert.match(document.body.textContent,/2019-06-28/);
    assert.match(document.body.textContent,/单源检查通过 1 只；双源核验通过 0 只；BaoStock补缺通过 0 只/);
    const buttons=[...document.querySelectorAll('button')];
    await act(async()=>buttons[0].click());
    assert.deepEqual(posts[0],{url:'/api/v1/research/index-download/start',body:{}});
    assert.equal(buttons[0].disabled,true);
    await act(async()=>buttons[1].click());
    assert.match(posts[1].url,/\/pause$/);
    assert.match(document.body.textContent,/当前股票处理后停止/);
  }finally{await act(async()=>root.unmount());}
});
test('warehouse start failure remains visible and does not claim a running job',async()=>{
  document.body.innerHTML='<div id="root"></div>';const root=createRoot(document.getElementById('root'));
  globalThis.fetch=async(url,init={})=>init.method==='POST'?response({detail:'无法启动下载进程'},503):response(state());
  try{
    await act(async()=>root.render(React.createElement(components.IndexDownloadPanel)));
    await act(async()=>document.querySelector('button').click());
    assert.match(document.querySelector('[role="alert"]').textContent,/无法启动下载进程/);
    assert.equal(document.querySelector('button').disabled,false);
    assert.doesNotMatch(document.querySelector('[role="status"]').textContent,/后台下载/);
  }finally{await act(async()=>root.unmount());}
});

const handlingReport=()=>({schema_version:'index-handling-report-v1',source_id:'fixture',status:'complete',source_updated_at:'2026-11-01T00:00:00Z',
  generated_at:'2026-11-01T00:00:01Z',scope:'当前冻结历史仓库',summary:{total:3,passed:2,needs_review:1,pending:0,auto_handled:1,action_counts:{},validation_counts:{}},
  rules:['无法确认的全天停牌不会直接前填'],stocks:[
    {code:'688065.SH',name:'凯赛生物',validation_level:'single_source_passed',result:'单源检查通过',outcome:'auto_handled',actions:[
      {rule:'intraday_halt',title:'盘中停牌保留真实成交',reason:'仅下午停牌',handling:'保留真实成交，不生成全天停牌估值行',dates:['2023-06-15'],kind:'handled',evidence:[{source:'官方Tushare',api:'suspend_d',file:'suspension-raw/example.parquet',sha256:'verified-hash'}]}]},
    {code:'600000.SH',name:'待复核股票',validation_level:null,result:'待复核，未发布',outcome:'needs_review',actions:[
      {rule:'unresolved',title:'自动处理未能确认',reason:'两源冲突',handling:'保留证据，当前股票不进入已通过数据',dates:[],kind:'unresolved',evidence:[]}]},
    {code:'000001.SZ',name:'普通股票',validation_level:'single_source_passed',result:'单源检查通过',outcome:'passed',actions:[]},
  ]});

test('handling report loads explicitly, shows actual decisions, filters and downloads without starting jobs',async()=>{
  document.body.innerHTML='<div id="root"></div>';const root=createRoot(document.getElementById('root'));
  const calls=[];
  globalThis.fetch=async(url,init={})=>{calls.push({url,method:init.method??'GET'});return response(String(url).endsWith('/report')?handlingReport():state());};
  try{
    await act(async()=>root.render(React.createElement(components.IndexDownloadPanel)));
    assert.equal(calls.filter(r=>r.url.endsWith('/report')).length,0);
    await act(async()=>[...document.querySelectorAll('button')].find(b=>b.textContent==='查看自动处理报告').click());
    const section=document.querySelector('[aria-label="自动处理报告"]');
    assert.match(section.textContent,/单源检查通过/);assert.match(section.textContent,/盘中停牌保留真实成交/);
    assert.match(section.textContent,/2023-06-15/);assert.match(section.textContent,/verified-hash/);
    assert.equal(section.querySelector('a[download]').getAttribute('href'),'/api/v1/research/index-download/report/download');
    assert.doesNotMatch(section.textContent,/普通股票/);
    const input=section.querySelector('[aria-label="筛选报告股票"]');
    await act(async()=>{Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set.call(input,'凯赛');input.dispatchEvent(new window.Event('input',{bubbles:true}));});
    assert.match(section.textContent,/符合条件 1 只/);assert.doesNotMatch(section.textContent,/待复核股票/);
    await act(async()=>{Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set.call(input,'');input.dispatchEvent(new window.Event('input',{bubbles:true}));});
    const select=section.querySelector('[aria-label="报告显示范围"]');
    await act(async()=>{select.value='review';select.dispatchEvent(new window.Event('change',{bubbles:true}));});
    assert.match(section.textContent,/待复核股票/);assert.doesNotMatch(section.textContent,/凯赛生物/);
    assert.equal(calls.filter(r=>r.method==='POST').length,0);
  }finally{await act(async()=>root.unmount());}
});

test('handling report errors can be retried and ordinary stocks can be inspected',async()=>{
  document.body.innerHTML='<div id="root"></div>';const root=createRoot(document.getElementById('root'));let failed=true;
  globalThis.fetch=async(url)=>String(url).endsWith('/report')?(failed?response({detail:'报告读取失败'},503):response(handlingReport())):response(state());
  try{
    await act(async()=>root.render(React.createElement(components.IndexDownloadPanel)));
    const button=()=>[...document.querySelectorAll('button')].find(b=>b.textContent==='查看自动处理报告');
    await act(async()=>button().click());assert.match(document.body.textContent,/报告读取失败/);
    failed=false;await act(async()=>button().click());
    const select=document.querySelector('[aria-label="报告显示范围"]');
    await act(async()=>{select.value='all';select.dispatchEvent(new window.Event('change',{bubbles:true}));});
    assert.match(document.body.textContent,/普通股票/);
    assert.match(document.body.textContent,/未发现需要自动修复的问题/);
    assert.equal(document.querySelector('[role="alert"]'),null);
  }finally{await act(async()=>root.unmount());}
});
