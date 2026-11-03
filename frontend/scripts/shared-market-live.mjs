import assert from 'node:assert/strict';
import {writeFile} from 'node:fs/promises';
import {createDOM} from '../tests/helpers/dom.mjs';
const base=process.argv[process.argv.indexOf('--base')+1];
const {components,React,act,createRoot}=await createDOM({url:base});
const originalFetch=globalThis.fetch,previews=[],starts=[],tasks=[];
globalThis.fetch=async(url,init={})=>{
 const target=new URL(url,base),response=await originalFetch(target,{...init,headers:{...init.headers,Origin:base}});
 if(init.method==='POST'){
  const result=await response.clone().json();assert.ok(response.ok,JSON.stringify(result));
  if(target.pathname.endsWith('/market-download/preview'))previews.push(result);
  if(target.pathname.endsWith('/market-download/start'))starts.push(result);
  if(target.pathname.endsWith('/research/tasks'))tasks.push(result);
 }
 return response;
};
async function get(path){const r=await originalFetch(base+path);assert.ok(r.ok,path);return r.json();}
const root=createRoot(document.getElementById('root'));
const button=text=>[...document.querySelectorAll('button')].find(n=>n.textContent.trim()===text);
async function click(n){assert.ok(n);await act(async()=>n.click());}
async function waitFor(check,timeout=90000){const until=Date.now()+timeout;for(;;){try{return check();}catch(error){if(Date.now()>until)throw error;await act(async()=>new Promise(resolve=>setTimeout(resolve,100)));}}}
async function input(id,value){const n=document.getElementById(id);assert.ok(n,id);const proto=n.tagName==='TEXTAREA'?window.HTMLTextAreaElement.prototype:n.tagName==='SELECT'?HTMLSelectElement.prototype:HTMLInputElement.prototype;
 await act(async()=>{Object.getOwnPropertyDescriptor(proto,'value').set.call(n,String(value));n.dispatchEvent(new Event(n.tagName==='SELECT'?'change':'input',{bubbles:true}));});}
async function dataPage(){await click([...document.querySelectorAll('.nav-button')].find(n=>n.textContent.includes('数据管理')));await waitFor(()=>assert.ok(document.getElementById('shared-scope')));}
const cases=[
 {scope:'csi300',start:'2025-03-03',end:'2025-05-30',warmup:21,codes:['600036.SH','600519.SH'],label:'沪深300子集复用'},
 {scope:'all_market',start:'2025-03-03',end:'2025-05-30',warmup:21,codes:['600036.SH','600519.SH'],label:'换全市场子集复用'},
 {scope:'custom',start:'2025-03-03',end:'2025-05-30',warmup:21,codes:['600036.SH','600519.SH','600238.SH','000151.SZ'],label:'新增仓库外股票'},
 {scope:'custom',start:'2026-10-23',end:'2026-11-02',warmup:21,codes:['600036.SH','600519.SH'],label:'补最近交易日'},
 {scope:'csi300',start:'2025-03-03',end:'2025-05-30',warmup:21,codes:[],label:'完整历史成分版本与动态回测'},
];
const evidence=[];const stamp=Date.now();
try{
 await act(async()=>root.render(React.createElement(React.StrictMode,null,React.createElement(components.App))));
 for(const [index,spec] of cases.entries()){
  await dataPage();await input('shared-scope',spec.scope);await input('shared-start',spec.start);await input('shared-end',spec.end);await input('shared-warmup',spec.warmup);await input('shared-codes',spec.codes.join(', '));
  await click(button('预览覆盖与增量方案'));await waitFor(()=>assert.equal(previews.length,index+1));
  const plan=previews.at(-1);assert.deepEqual(plan.spec.codes,[...spec.codes].sort());assert.equal(plan.spec.start,spec.start);assert.equal(plan.spec.warmup,spec.warmup);
  if(index<2)assert.equal(plan.missing_rows,0);
  if(index===3)assert.ok([0,2].includes(plan.missing_rows)); // A replay must reuse the two rows captured on first execution.
  assert.equal(starts.length,index);await waitFor(()=>assert.ok(button('确认方案并下载 / 更新')));
  await click(button('确认方案并下载 / 更新'));await waitFor(()=>assert.equal(starts.length,index+1));
  let state,job;const until=Date.now()+300000;
  do{assert.ok(Date.now()<until,'download timeout');await act(async()=>new Promise(resolve=>setTimeout(resolve,250)));state=await get('/api/v1/research/market-download');job=state.jobs.find(j=>j.id===plan.id);}while(!job||job.status==='running'||state.worker_running);
  assert.equal(job.status,'complete',JSON.stringify(job));const version=state.versions.find(v=>v.id===job.version_id);if(index<4)assert.ok(version.snapshot_id,JSON.stringify(version));
  await click([...document.querySelectorAll('.nav-button')].find(n=>n.textContent.includes('新建研究')));
  await waitFor(()=>assert.ok(document.getElementById('research-name')));
  const options=await get('/api/v1/research/options');
  if(index<4){
   await waitFor(()=>assert.ok(document.querySelector(`#research-snapshot option[value="${version.snapshot_id}"]`)));
   await click(document.getElementById('research-source-snapshot'));await input('research-snapshot',version.snapshot_id);await click(button('全选'));
  }else{
   const choice=options.historical_indices.find(o=>o.shared_version===version.id);assert.ok(choice);
   await click(document.getElementById('research-source-historical'));await input('research-warehouse-version',choice.id);
  }
  for(const def of options.factor_definitions){const node=document.getElementById('research-factor-'+def.name);if(node&&node.checked!==['momentum','value'].includes(def.name))await click(node);}
  await input('research-factor-window-momentum',5);await input('research-factor-weight-momentum',60);await input('research-factor-weight-value',40);
  await input('research-name',`共享行情验收-${stamp}-${index}`);await input('research-bt-start',spec.start);await input('research-bt-end',spec.end);await input('research-topk',2);await input('research-neutralization','off');
  if(!document.getElementById('research-horizons'))await click(button('展开高级设置'));
  await input('research-horizons','1');await input('research-groups',2);await input('research-min-cross',3);
  await click(button('创建研究任务'));await waitFor(()=>assert.equal(tasks.length,index+1));
  let task=tasks.at(-1);const timeout=Date.now()+180000;
  while(['queued','running'].includes(task.status)){assert.ok(Date.now()<timeout);await act(async()=>new Promise(resolve=>setTimeout(resolve,250)));task=await get('/api/v1/research/tasks/'+task.id);}
  assert.equal(task.status,'succeeded',JSON.stringify(task));if(index<4)assert.equal(task.result.data_version,version.snapshot_id);
  const result=await get('/api/v1/backtests/'+task.result.backtest_run_id+'/download');
  evidence.push({label:spec.label,plan,job,version,task,backtest:result});
  process.stdout.write(`PASSED ${spec.label}: reused=${job.reused_rows}, new=${job.downloaded_rows}\n`);
 }
 assert.deepEqual(evidence[0].backtest.trades,evidence[1].backtest.trades);
 assert.ok(evidence[0].backtest.equity.length>0);
 assert.deepEqual(evidence[0].backtest.equity,evidence[1].backtest.equity);
 const fullResponse=await globalThis.fetch('/api/v1/research/market-download/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({scope:'all_market',start:'2025-03-03',end:'2025-05-30',warmup:21,codes:[]})});
 const fullPreview=await fullResponse.json();assert.ok(fullPreview.codes_count>4000);assert.equal(fullPreview.restricted,false);
 await writeFile(new URL('../../.agent-work/shared-live-evidence.json',import.meta.url),JSON.stringify({status:'passed',cases:evidence,full_market_preview:{codes:fullPreview.codes_count,reused_rows:fullPreview.reused_rows,missing_rows:fullPreview.missing_rows,estimated_quote_requests:fullPreview.estimated_quote_requests,download_started:false}},null,2));
 process.stdout.write('SHARED_MARKET_LIVE_PASSED\n');
}finally{await act(async()=>root.unmount());}
