// Actual React form -> HTTP worker -> native execution -> immutable benchmark views.
import assert from 'node:assert/strict';
import {writeFile} from 'node:fs/promises';
import {createDOM} from '../tests/helpers/dom.mjs';
const base=process.argv[process.argv.indexOf('--base')+1];
assert.match(base,/^http:\/\/(127\.0\.0\.1|localhost):\d+$/);
const {components,React,act,createRoot}=await createDOM({url:base});
const originalFetch=globalThis.fetch,posted=[];
globalThis.fetch=async(url,init={})=>{
 const absolute=new URL(url,base),response=await originalFetch(absolute,{...init,headers:{...init.headers,Origin:base}});
 if(init.method==='POST'&&absolute.pathname.endsWith('/research/tasks'))posted.push({request:JSON.parse(init.body),task:await response.clone().json()});
 return response;
};
const root=createRoot(document.getElementById('root'));
async function waitFor(check){const end=Date.now()+300000;for(;;){try{return check();}catch(error){if(Date.now()>end)throw error;await act(async()=>new Promise(resolve=>setTimeout(resolve,50)));}}}
async function click(node){assert.ok(node,'Missing click target');await act(async()=>node.click());}
const button=(text,parent=document)=>[...parent.querySelectorAll('button')].find(node=>node.textContent.trim()===text);
async function input(id,value){const node=document.getElementById(id);assert.ok(node,id);await act(async()=>{
 const proto=node instanceof HTMLSelectElement?HTMLSelectElement.prototype:HTMLInputElement.prototype;
 Object.getOwnPropertyDescriptor(proto,'value').set.call(node,String(value));node.dispatchEvent(new Event(node instanceof HTMLSelectElement?'change':'input',{bubbles:true}));
});}
async function research(){if(!document.getElementById('research-name'))await click([...document.querySelectorAll('.nav-button')].find(n=>n.textContent.includes('新建研究')));await waitFor(()=>assert.ok(document.getElementById('research-name')));}
const options=await originalFetch(base+'/api/v1/research/options').then(r=>r.json());
assert.ok(options.comparison_benchmarks.some(item=>item.code==='H00300.CSI'&&item.coverage?.rows>=5300));
const fixed=options.snapshots.filter(s=>s.codes.length>=3&&s.codes.length<=15&&s.start<='2025-03-03'&&s.end>='2025-05-30'&&s.codes.every(c=>/^(60|00|30)/.test(c.code))).sort((a,b)=>a.codes.length-b.codes.length)[0];
assert.ok(fixed);
const evidence=[],stamp=Date.now();
try{
 await act(async()=>root.render(React.createElement(React.StrictMode,null,React.createElement(components.App))));
 for(const mode of ['historical','fixed'])for(const code of ['000300.SH','H00300.CSI']){
  await research();const name=`基准验收-${stamp}-${mode}-${code}`;await input('research-name',name);
  if(mode==='fixed'){await click(document.getElementById('research-source-snapshot'));await input('research-snapshot',fixed.id);await click(button('全选'));}
  else await click(document.getElementById('research-source-historical'));
  for(const def of options.factor_definitions){const node=document.getElementById('research-factor-'+def.name);if(node&&node.checked!==['momentum','value','volatility'].includes(def.name))await click(node);}
  await input('research-factor-window-momentum',20);await input('research-factor-window-volatility',10);
  await input('research-factor-weight-momentum',40);await input('research-factor-weight-value',40);await input('research-factor-weight-volatility',20);
  await input('research-bt-start','2025-03-03');await input('research-bt-end','2025-05-30');
  await input('research-topk',mode==='historical'?10:2);await input('research-initial-cash',100000);
  await input('research-rebalance-frequency',mode==='historical'?'weekly':'monthly');await input('research-comparison-benchmark',code);
  if(!document.getElementById('research-risk-degree'))await click(button('展开高级设置'));
  await input('research-n-drop',1);await input('research-horizons','5');await input('research-groups',3);await input('research-min-cross',3);
  await input('research-cost-mode','itemized');await input('research-impact-cost',.01);
  await click(button('创建研究任务'));await waitFor(()=>assert.equal(posted.length,evidence.length+1));
  const item=posted.at(-1);assert.equal(item.request.backtest.benchmark,code);
  let task=item.task;await waitFor(()=>{const article=[...document.querySelectorAll('.research-task')].find(n=>n.textContent.includes(name));assert.ok(article);assert.ok(!article.textContent.includes('运行失败'),article.textContent);assert.ok(button('查看回测',article));});
  task=await originalFetch(base+'/api/v1/research/tasks/'+task.id).then(r=>r.json());assert.equal(task.status,'succeeded',JSON.stringify(task));
  const article=[...document.querySelectorAll('.research-task')].find(n=>n.textContent.includes(name));await click(button('查看回测',article));
  await waitFor(()=>assert.match(document.querySelector('[data-testid="benchmark-description"]')?.textContent??'',new RegExp(code.replace('.','\\.'))));
  assert.ok(document.body.textContent.includes('信息比率'));assert.ok(document.body.textContent.includes('年化跟踪误差'));
  const response=await originalFetch(`${base}/api/v1/backtests/${task.result.backtest_run_id}/download`);assert.equal(response.status,200);
  const result=await response.json();evidence.push({mode,code,task,result});
  await research();await click(button('再用此配置',[...document.querySelectorAll('.research-task')].find(n=>n.textContent.includes(name))));
  assert.equal(document.getElementById('research-comparison-benchmark').value,code);
 }
 for(const mode of ['historical','fixed']){
  const [a,b]=evidence.filter(item=>item.mode===mode);
  assert.equal(a.task.result.factor_run_id,b.task.result.factor_run_id);assert.equal(a.task.result.evaluation_run_id,b.task.result.evaluation_run_id);
  assert.equal(a.result.metadata.execution_run_id,b.result.metadata.execution_run_id);
  for(const key of ['trades','orders','positions','execution_events'])assert.deepEqual(a.result[key],b.result[key]);
  const strategy=e=>e.map(({benchmark,...point})=>point);
  assert.deepEqual(strategy(a.result.equity),strategy(b.result.equity));
  assert.notEqual(a.result.summary.metrics.benchmark_return,b.result.summary.metrics.benchmark_return);
 }
 // Reopen the earlier task after later tasks have completed.
 const old=evidence[0];await research();await click(button('查看回测',[...document.querySelectorAll('.research-task')].find(n=>n.textContent.includes(old.task.name))));
 await waitFor(()=>assert.ok(document.querySelector(`a[href="/api/v1/backtests/${old.task.result.backtest_run_id}/download"]`)));
 await writeFile(new URL('../../.agent-work/benchmark-live-evidence.json',import.meta.url),JSON.stringify({status:'passed',cases:evidence.map(({result,...entry})=>({...entry,checks:{account_days:result.equity.length-1,trades:result.trades.length,execution_run_id:result.metadata.execution_run_id,benchmark_return:result.summary.metrics.benchmark_return,relative_return:result.summary.metrics.relative_return,tracking_error:result.summary.metrics.tracking_error,information_ratio:result.summary.metrics.information_ratio}}))},null,2));
 process.stdout.write('BENCHMARK_LIVE_PASSED\n');
}finally{await act(async()=>root.unmount());}
