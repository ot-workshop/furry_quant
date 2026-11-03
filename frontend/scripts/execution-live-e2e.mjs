// Real React events -> HTTP queue -> native Qlib; charts use DOM test substitutes.
import assert from 'node:assert/strict';
import {writeFile,mkdir} from 'node:fs/promises';
import {createDOM} from '../tests/helpers/dom.mjs';
const base=process.argv[process.argv.indexOf('--base')+1];
assert.match(base,/^http:\/\/(127\.0\.0\.1|localhost):\d+$/);
const {components,React,act,createRoot}=await createDOM({url:base});
const originalFetch=globalThis.fetch,calls=[],submitted=[];
globalThis.fetch=async(url,init={})=>{
 const absolute=new URL(url,base);calls.push(absolute.pathname+absolute.search);
 const response=await originalFetch(absolute,{...init,headers:{...init.headers,Origin:base}});
 if(init.method==='POST'&&absolute.pathname.endsWith('/research/tasks'))submitted.push({request:JSON.parse(init.body),task:await response.clone().json()});
 return response;
};
const root=createRoot(document.getElementById('root'));
async function waitFor(check){const end=Date.now()+240000;for(;;){try{return check();}catch(error){if(Date.now()>end)throw error;await act(async()=>new Promise(resolve=>setTimeout(resolve,35)));}}}
async function click(node){assert.ok(node,'Missing target');await act(async()=>node.click());}
function button(text,parent=document){return [...parent.querySelectorAll('button')].find(n=>n.textContent.trim()===text);}
async function input(id,value){const node=document.getElementById(id);assert.ok(node,id);await act(async()=>{
 const proto=node instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
 Object.getOwnPropertyDescriptor(proto,'value').set.call(node,String(value));node.dispatchEvent(new Event(node instanceof HTMLSelectElement ? 'change' : 'input',{bubbles:true}));
});}
async function research(){if(!document.getElementById('research-name'))await click([...document.querySelectorAll('.nav-button')].find(n=>n.textContent.includes('新建研究')));await waitFor(()=>assert.ok(document.getElementById('research-name')));}
function article(name){return [...document.querySelectorAll('.research-task')].find(n=>n.querySelector('.research-task-title')?.textContent===name);}
async function open(entry){
 const r=entry.task.result;await research();await click(button('查看评分',article(entry.name)));
 await waitFor(()=>assert.equal(document.getElementById('factor-run')?.value,r.factor_run_id));
 await waitFor(()=>assert.equal(document.getElementById('score-date')?.value,r.factor_as_of??entry.request.backtest.end));
 await research();await click(button('查看回测',article(entry.name)));
 await waitFor(()=>assert.ok(document.querySelector(`a[href="/api/v1/backtests/${r.backtest_run_id}/download"]`)));
 await waitFor(()=>assert.ok(document.querySelector('section[aria-label="调仓与交易费用"] table')));
 const text=document.querySelector('section[aria-label="调仓与交易费用"]').textContent;
 assert.ok(text.includes(entry.request.backtest.rebalance_frequency==='daily' ? '每日' : entry.request.backtest.rebalance_frequency==='weekly' ? '每周首个交易日' : '每月首个交易日'));
 assert.match(text,/券商佣金/);assert.match(text,/冲击成本/);
 await research();await click(button('查看因子评估',article(entry.name)));
 await waitFor(()=>assert.equal(document.getElementById('evaluation-select')?.value,r.evaluation_run_id));
 await waitFor(()=>assert.ok(document.querySelector(`a[href="/api/v1/evaluations/${r.evaluation_run_id}/download"]`)));
 await research();
}
const options=await originalFetch(base+'/api/v1/research/options').then(r=>r.json());
const fixed=options.snapshots.filter(s=>s.codes.length>=3&&s.codes.length<=15&&s.start<='2025-03-03'&&s.end>='2025-05-30'&&s.codes.every(c=>/^(60|00|30)/.test(c.code))).sort((a,b)=>a.codes.length-b.codes.length)[0];
assert.ok(fixed,'Need a real fixed-pool snapshot');
const variants=[
 {key:'daily-legacy',frequency:'daily',mode:'legacy',impact:0,commission:.03},
 {key:'weekly-itemized',frequency:'weekly',mode:'itemized',impact:.1,commission:.03},
 {key:'monthly-itemized',frequency:'monthly',mode:'itemized',impact:.1,commission:.03},
 {key:'monthly-impact',frequency:'monthly',mode:'itemized',impact:.3,commission:.03},
 {key:'monthly-commission',frequency:'monthly',mode:'itemized',impact:.1,commission:.2},
 {key:'fixed-weekly',frequency:'weekly',mode:'itemized',impact:.1,commission:.03,fixed:true},
 {key:'fixed-monthly-custom',frequency:'monthly',mode:'itemized',impact:.2,commission:.05,fixed:true,tax:'fixed',stamp:.07,transfer:.002},
];
const evidence=[],stamp=Date.now();
try{
 await act(async()=>root.render(React.createElement(React.StrictMode,null,React.createElement(components.App))));
 for(const v of variants){
  await research();const name=`调仓费用验收-${stamp}-${v.key}`;
  await input('research-name',name);
  if(v.fixed){await click(document.getElementById('research-source-snapshot'));await input('research-snapshot',fixed.id);await click(button('全选'));}
  else await click(document.getElementById('research-source-historical'));
  for(const def of options.factor_definitions){const node=document.getElementById('research-factor-'+def.name);if(node&&node.checked!==['momentum','value','volatility'].includes(def.name))await click(node);}
  await input('research-factor-window-momentum',20);await input('research-factor-window-volatility',10);
  await input('research-factor-weight-momentum',40);await input('research-factor-weight-value',40);await input('research-factor-weight-volatility',20);
  await input('research-bt-start',v.fixed?'2025-03-03':'2023-06-01');await input('research-bt-end',v.fixed?'2025-05-30':'2023-10-31');
  await input('research-topk',v.fixed?3:10);await input('research-initial-cash',1000000);await input('research-rebalance-frequency',v.frequency);
  if(!document.getElementById('research-risk-degree'))await click(button('展开高级设置'));
  await input('research-n-drop',v.fixed?1:2);await input('research-horizons','5');await input('research-groups',v.fixed?3:5);await input('research-min-cross',v.fixed?3:20);
  await input('research-cost-mode',v.mode);await input('research-impact-cost',v.impact);await input('research-min-cost',5);
  if(v.mode==='itemized'){
   await input('research-commission-rate',v.commission);await input('research-tax-mode',v.tax??'historical');
   if(v.tax==='fixed'){await input('research-stamp-tax-rate',v.stamp);await input('research-transfer-fee-rate',v.transfer);}
  }
  else {await input('research-open-cost',.1);await input('research-close-cost',.1);}
  const count=submitted.length;await click(button('创建研究任务'));await waitFor(()=>assert.equal(submitted.length,count+1));
  const item=submitted.at(-1);assert.ok(item.task.id,JSON.stringify(item.task));
  await waitFor(()=>{assert.ok(article(name));assert.ok(!article(name).querySelector('.research-task-error'),article(name).textContent);assert.equal(article(name).querySelector('.status-pill').textContent.trim(),'已完成');});
  const task=await originalFetch(base+'/api/v1/research/tasks/'+item.task.id).then(r=>r.json());assert.equal(task.status,'succeeded',task.error);
  const detail=await originalFetch(base+'/api/v1/backtests/'+task.result.backtest_run_id).then(r=>r.json());
  assert.equal(detail.rebalance_policy.frequency,v.frequency);assert.equal(detail.execution_costs.cost_mode,v.mode);
  assert.equal(detail.execution_costs.impact_cost,v.impact/100);assert.equal(detail.config.exchange_kwargs.impact_cost,v.impact/100);
  if(v.mode==='itemized'){assert.equal(detail.execution_costs.commission_rate,v.commission/100);assert.ok(detail.cost_summary.commission>0);assert.equal(detail.cost_summary.legacy_fee,0);}
  if(v.tax==='fixed'){assert.equal(detail.execution_costs.tax_mode,'fixed');assert.equal(detail.execution_costs.stamp_tax_rate,v.stamp/100);assert.equal(detail.execution_costs.transfer_fee_rate,v.transfer/100);}
  const entry={key:v.key,name,request:item.request,task,detail};await open(entry);evidence.push(entry);
  await mkdir(new URL('../../.agent-work/',import.meta.url),{recursive:true});
  await writeFile(new URL('../../.agent-work/execution-web-evidence.json',import.meta.url),JSON.stringify({status:evidence.length===variants.length?'passed':'in_progress',cases:evidence},null,2));
  console.log(JSON.stringify({case:v.key,status:'passed',task_id:task.id,backtest_run_id:task.result.backtest_run_id,schedule:detail.rebalance_policy.scheduled_count,cost:detail.cost_summary.total}));
 }
 assert.equal(new Set(evidence.slice(0,5).map(e=>e.task.result.factor_run_id)).size,1);
 assert.equal(new Set(evidence.map(e=>e.task.result.backtest_run_id)).size,evidence.length);
 await open(evidence[0]);await open(evidence.at(-1));
 console.log('EXECUTION_WEB_WORKFLOW_PASSED');
}finally{await act(async()=>root.unmount());}
