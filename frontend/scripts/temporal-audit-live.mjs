import assert from 'node:assert/strict';
import {writeFile} from 'node:fs/promises';
import {createDOM} from '../tests/helpers/dom.mjs';
const base=process.argv[process.argv.indexOf('--base')+1];
const {components,React,act,createRoot}=await createDOM({url:base});const originalFetch=globalThis.fetch,posted=[];
globalThis.fetch=async(url,init={})=>{
 const target=new URL(url,base),response=await originalFetch(target,{...init,headers:{...init.headers,Origin:base}});
 if(init.method==='POST'&&target.pathname.endsWith('/research/tasks')){
  const task=await response.clone().json();assert.ok(response.ok,JSON.stringify(task));posted.push(task);
 }return response;
};
const get=async path=>{const r=await originalFetch(base+path);assert.ok(r.ok,path);return r.json();};
const root=createRoot(document.getElementById('root')),button=(text,parent=document)=>[...parent.querySelectorAll('button')].find(n=>n.textContent.trim()===text);
async function click(n){assert.ok(n);await act(async()=>n.click());}
async function waitFor(check){const end=Date.now()+60000;for(;;){try{return check();}catch(error){if(Date.now()>end)throw error;await act(async()=>new Promise(resolve=>setTimeout(resolve,50)));}}}
async function input(id,value){const n=document.getElementById(id);assert.ok(n,id);await act(async()=>{
 const proto=n instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype;
 Object.getOwnPropertyDescriptor(proto,'value').set.call(n,String(value));n.dispatchEvent(new Event(n instanceof HTMLSelectElement?'change':'input',{bubbles:true}));
});}
async function research(){if(!document.getElementById('research-name'))await click([...document.querySelectorAll('.nav-button')].find(n=>n.textContent.includes('新建研究')));await waitFor(()=>assert.ok(document.getElementById('research-name')));}
const options=await get('/api/v1/research/options'),snapshot=options.snapshots.find(s=>s.codes.length===5);
const evidence=[],stamp=Date.now();
try{
 await act(async()=>root.render(React.createElement(React.StrictMode,null,React.createElement(components.App))));
 for(const [pool,neutralization] of [['fixed','off'],['historical','off'],['historical','industry']]){
  await research();const name=`时点核验-${stamp}-${pool}-${neutralization}`;await input('research-name',name);
  if(pool==='fixed'){await click(document.getElementById('research-source-snapshot'));await input('research-snapshot',snapshot.id);await click(button('全选'));}
  else await click(document.getElementById('research-source-historical'));
  for(const def of options.factor_definitions){const n=document.getElementById('research-factor-'+def.name);if(n&&n.checked!==['momentum','value','volatility'].includes(def.name))await click(n);}
  await input('research-factor-window-momentum',20);await input('research-factor-window-volatility',10);
  await input('research-factor-weight-momentum',40);await input('research-factor-weight-value',40);await input('research-factor-weight-volatility',20);
  await input('research-bt-start','2025-03-03');await input('research-bt-end','2025-05-30');await input('research-topk',pool==='fixed'?2:10);
  await input('research-rebalance-frequency',pool==='fixed'?'monthly':'weekly');await input('research-comparison-benchmark','H00300.CSI');
  await input('research-neutralization',neutralization);
  if(!document.getElementById('research-horizons'))await click(button('展开高级设置'));
  await input('research-horizons','5');await input('research-groups',3);await input('research-min-cross',3);
  await input('research-cost-mode','itemized');await input('research-impact-cost',.01);
  await click(button('创建研究任务'));await waitFor(()=>assert.equal(posted.length,evidence.length+1));
  let task=posted.at(-1);const end=Date.now()+300000;
  while(['queued','running'].includes(task.status)){
    assert.ok(Date.now()<end,'task timeout');await act(async()=>new Promise(resolve=>setTimeout(resolve,250)));
    task=await get('/api/v1/research/tasks/'+task.id);
  }
  assert.equal(task.status,'succeeded',JSON.stringify(task));
  const result=await get('/api/v1/backtests/'+task.result.backtest_run_id+'/download');
  const audit=result.metadata.temporal_audit;assert.equal(audit.point_in_time_certified,false);
  assert.equal(audit.items.find(i=>i.key==='capacity').status,'not_modeled');assert.ok(audit.capacity_diagnostics.valid_stock_days>0);
  await waitFor(()=>assert.ok(button('查看回测',[...document.querySelectorAll('.research-task')].find(n=>n.textContent.includes(name)))));
  await click(button('查看回测',[...document.querySelectorAll('.research-task')].find(n=>n.textContent.includes(name))));
  await waitFor(()=>assert.match(document.querySelector('[data-testid="temporal-audit"]')?.textContent??'',/成交容量未建模/));
  assert.match(document.querySelector('[data-testid="temporal-audit"]').textContent,/PE\/PB/);
  await research();await click(button('查看评分',[...document.querySelectorAll('.research-task')].find(n=>n.textContent.includes(name))));
  await waitFor(()=>assert.match(document.querySelector('[data-testid="temporal-audit"]')?.textContent??'',/历史公开时点尚未完全认证/));
  if(pool==='historical')assert.match(document.querySelector('[data-testid="temporal-audit"]').textContent,/月度权重快照/);
  await research();await click(button('查看因子评估',[...document.querySelectorAll('.research-task')].find(n=>n.textContent.includes(name))));
  await waitFor(()=>assert.match(document.querySelector('[data-testid="temporal-audit"]')?.textContent??'',/历史公开时点尚未完全认证/));
  evidence.push({pool,neutralization,task,checks:{trades:result.trades.length,capacity:audit.capacity_diagnostics,pit_certified:audit.point_in_time_certified}});
  process.stdout.write(`PASSED ${pool} ${neutralization}\n`);
 }
 await writeFile(new URL('../../.agent-work/temporal-live-evidence.json',import.meta.url),JSON.stringify({status:'passed',cases:evidence},null,2));
 process.stdout.write('TEMPORAL_LIVE_PASSED\n');
}finally{await act(async()=>root.unmount());}
