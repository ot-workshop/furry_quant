// Actual React -> HTTP queue -> native Qlib -> frozen industry before/after views.
import assert from 'node:assert/strict';
import {writeFile} from 'node:fs/promises';
import {createDOM} from '../tests/helpers/dom.mjs';
const base=process.argv[process.argv.indexOf('--base')+1];
assert.match(base,/^http:\/\/(127\.0\.0\.1|localhost):\d+$/);
const {components,React,act,createRoot}=await createDOM({url:base});
const originalFetch=globalThis.fetch,posted=[];
globalThis.fetch=async(url,init={})=>{
 const absolute=new URL(url,base),response=await originalFetch(absolute,{...init,headers:{...init.headers,Origin:base}});
 if(init.method==='POST'&&absolute.pathname.endsWith('/research/tasks')){
   const task=await response.clone().json();assert.ok(response.ok,JSON.stringify(task));posted.push({request:JSON.parse(init.body),task});
 }
 return response;
};
const root=createRoot(document.getElementById('root'));
async function waitFor(check){const end=Date.now()+300000;for(;;){try{return check();}catch(error){if(Date.now()>end)throw error;await act(async()=>new Promise(resolve=>setTimeout(resolve,75)));}}}
async function click(node){assert.ok(node,'Missing click target');await act(async()=>node.click());}
const button=(text,parent=document)=>[...parent.querySelectorAll('button')].find(node=>node.textContent.trim()===text);
async function input(id,value){const node=document.getElementById(id);assert.ok(node,id);await act(async()=>{
 const proto=node instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype;
 Object.getOwnPropertyDescriptor(proto,'value').set.call(node,String(value));node.dispatchEvent(new Event(node instanceof HTMLSelectElement?'change':'input',{bubbles:true}));
});}
async function research(){if(!document.getElementById('research-name'))await click([...document.querySelectorAll('.nav-button')].find(n=>n.textContent.includes('新建研究')));await waitFor(()=>assert.ok(document.getElementById('research-name')));}
const get=async path=>{const response=await originalFetch(base+path);assert.ok(response.ok,path);return response.json();};
const options=await get('/api/v1/research/options');assert.ok(options.industry_data?.id);
const snapshots=[3,5].map(n=>options.snapshots.find(s=>s.codes.length===n&&(n===5||s.codes.some(c=>c.code==='000858.SZ'))));
assert.ok(snapshots.every(Boolean));const evidence=[],stamp=Date.now();
try{
 await act(async()=>root.render(React.createElement(React.StrictMode,null,React.createElement(components.App))));
 for(const pool of ['fixed3','fixed5','historical'])for(const mode of ['off','industry','off']){
  await research();const name=`行业验收-${stamp}-${pool}-${mode}-${evidence.length}`;await input('research-name',name);
  if(pool!=='historical'){await click(document.getElementById('research-source-snapshot'));await input('research-snapshot',snapshots[pool==='fixed3'?0:1].id);await click(button('全选'));}
  else await click(document.getElementById('research-source-historical'));
  for(const def of options.factor_definitions){const node=document.getElementById('research-factor-'+def.name);if(node&&node.checked!==['momentum','value','volatility'].includes(def.name))await click(node);}
  await input('research-factor-window-momentum',pool==='fixed3'?12:20);await input('research-factor-window-volatility',pool==='fixed3'?8:10);
  await input('research-factor-weight-momentum',40);await input('research-factor-weight-value',40);await input('research-factor-weight-volatility',20);
  await input('research-bt-start','2025-03-03');await input('research-bt-end','2025-05-30');await input('research-topk',pool==='historical'?10:2);
  await input('research-rebalance-frequency',pool==='historical'?'weekly':'monthly');await input('research-comparison-benchmark','000300.SH');
  await input('research-neutralization',mode);
  if(!document.getElementById('research-risk-degree'))await click(button('展开高级设置'));
  await input('research-n-drop',1);await input('research-horizons','5');await input('research-groups',3);await input('research-min-cross',3);
  await input('research-cost-mode','itemized');await input('research-impact-cost',.01);
  await click(button('创建研究任务'));await waitFor(()=>assert.equal(posted.length,evidence.length+1));
  const item=posted.at(-1);assert.equal(item.request.neutralization.mode,mode);assert.equal(item.request.neutralization.industry_version,options.industry_data.id);
  await waitFor(()=>{const article=[...document.querySelectorAll('.research-task')].find(n=>n.textContent.includes(name));assert.ok(article);assert.ok(!article.textContent.includes('运行失败'),article.textContent);assert.ok(button('查看回测',article));});
  const task=await get('/api/v1/research/tasks/'+item.task.id);assert.equal(task.status,'succeeded');
  const result=await get(`/api/v1/backtests/${task.result.backtest_run_id}/download`);
  const diagnostics=await get(`/api/v1/factor-runs/${task.result.factor_run_id}/industries`);
  assert.equal(diagnostics.neutralization.mode,mode);assert.ok(diagnostics.items.length);
  if(mode==='industry')for(const row of diagnostics.items)for(const value of Object.values(row.residual_mean))assert.ok(Math.abs(value)<1e-9);
  const article=[...document.querySelectorAll('.research-task')].find(n=>n.textContent.includes(name));await click(button('查看回测',article));
  await waitFor(()=>assert.ok(document.querySelector(`a[href="/api/v1/backtests/${task.result.backtest_run_id}/download"]`)));
  await waitFor(()=>assert.match(document.querySelector('[data-testid="industry-diagnostics"]')?.textContent??'',/现金/));
  await research();const card=[...document.querySelectorAll('.research-task')].find(n=>n.textContent.includes(name));await click(button('查看评分',card));
  await waitFor(()=>assert.match(document.querySelector('[data-testid="industry-diagnostics"]')?.textContent??'',/处理前平均分/));
  if(mode==='industry')await waitFor(()=>assert.match(document.querySelector('[data-testid="factor-calculation-details"]')?.textContent??'',/处理前综合分/));
  await research();await click(button('再用此配置',[...document.querySelectorAll('.research-task')].find(n=>n.textContent.includes(name))));
  assert.equal(document.getElementById('research-neutralization').value,mode);
  evidence.push({pool,mode,task,result,diagnostics});process.stdout.write(`PASSED ${pool} ${mode}\n`);
 }
 for(const pool of ['fixed3','fixed5','historical']){
   const [a,on,b]=evidence.filter(e=>e.pool===pool);
   assert.equal(a.task.result.factor_run_id,b.task.result.factor_run_id);
   assert.equal(a.task.result.backtest_run_id,b.task.result.backtest_run_id);
   for(const key of ['trades','positions','equity'])assert.deepEqual(a.result[key],b.result[key]);
   assert.notDeepEqual(a.result.trades,on.result.trades);
 }
 await writeFile(new URL('../../.agent-work/industry-live-evidence.json',import.meta.url),JSON.stringify({status:'passed',cases:evidence.map(({result,diagnostics,...r})=>({...r,checks:{account_days:result.equity.length-1,trades:result.trades.length,sectors:diagnostics.items.length,coverage:diagnostics.coverage}}))},null,2));
 process.stdout.write('INDUSTRY_LIVE_PASSED\n');
}finally{await act(async()=>root.unmount());}
