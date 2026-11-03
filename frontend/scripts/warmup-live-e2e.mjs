// Reproduce the user's actual failed download, then recover using explicit UI actions.
import assert from 'node:assert/strict';
import {readFile,writeFile} from 'node:fs/promises';
import {createDOM} from '../tests/helpers/dom.mjs';
const arg=name=>process.argv[process.argv.indexOf(name)+1];
const base=arg('--base'), context=JSON.parse(await readFile(arg('--context'),'utf8'));
assert.match(base,/^http:\/\/127\.0\.0\.1:\d+$/);
const originalFetch=globalThis.fetch;
const headers={'Content-Type':'application/json',Origin:base,'Idempotency-Key':'warmup-original-rejection'};
const before=await originalFetch(base+'/api/v1/research/tasks').then(r=>r.json());
const rejected=await originalFetch(base+'/api/v1/research/tasks',{method:'POST',headers,body:JSON.stringify(context.original)});
assert.equal(rejected.status,422);
const rejection=await rejected.json();
assert.ok(rejection.detail.includes(`${context.required}个`) && rejection.detail.includes(`${context.available}个`),rejection.detail);
assert.equal((await originalFetch(base+'/api/v1/research/tasks').then(r=>r.json())).total,before.total);
const {dom,components,React,act,createRoot}=await createDOM({url:base});
const posted=[],calls=[];
globalThis.fetch=async (url,init={})=>{
  const target=new URL(url,base);calls.push(target.pathname);
  const response=await originalFetch(target,{...init,headers:{...init.headers,Origin:base}});
  if (init.method==='POST' && target.pathname.endsWith('/research/tasks')) posted.push({request:JSON.parse(init.body),task:await response.clone().json()});
  return response;
};
const root=createRoot(document.getElementById('root'));
const button=(text,parent=document)=>{const b=[...parent.querySelectorAll('button')].find(b=>b.textContent.trim()===text);assert.ok(b,text);return b;};
const click=async el=>{assert.ok(el);await act(async()=>el.click());};
async function wait(check,timeout=30000){const end=Date.now()+timeout;while(true){try{return check();}catch(error){if(Date.now()>end)throw error;await act(async()=>new Promise(r=>setTimeout(r,50)));}}}
async function page(){if(!document.getElementById('research-name'))await click([...document.querySelectorAll('.nav-button')].find(b=>b.textContent.includes('新建研究')));await wait(()=>assert.ok(document.getElementById('research-name')));}
const taskCard=name=>{const c=[...document.querySelectorAll('.research-task')].find(c=>c.querySelector('.research-task-title')?.textContent===name);assert.ok(c,name);return c;};
try {
  await act(async()=>root.render(React.createElement(components.App)));
  await wait(()=>assert.ok(taskCard(context.seed_name)));
  await wait(()=>assert.ok(document.getElementById('research-name')));
  await wait(()=>assert.equal(button('再用此配置',taskCard(context.seed_name)).disabled,false));
  await click(button('再用此配置',taskCard(context.seed_name)));
  assert.ok(document.getElementById('research-warmup').textContent.includes(`${context.required}个`));
  await click(button('创建研究任务'));assert.equal(posted.length,0);
  await click(document.getElementById('research-use-cached-snapshot'));
  assert.equal(document.getElementById('research-source-snapshot').checked,true);
  await click(document.getElementById('research-use-warmup-start'));
  assert.equal(document.getElementById('research-bt-start').value,context.earliest);
  await click(button('创建研究任务'));
  await wait(()=>assert.equal(posted.length,1));
  const submission=posted[0];assert.ok(submission.task.id,JSON.stringify(submission.task));
  const sorted=specs=>[...specs].sort((a,b)=>(a.id??a.name).localeCompare(b.id??b.name));
  assert.deepEqual(sorted(submission.request.factors),sorted(context.original.factors));
  assert.deepEqual([...submission.request.data.codes].sort(),[...context.original.data.codes].sort());
  assert.equal(submission.request.data.mode,'snapshot');
  assert.equal(submission.request.data.snapshot_id,context.snapshot_id);
  assert.deepEqual(submission.request.backtest,{...context.original.backtest,start:context.earliest});
  let task;
  const deadline=Date.now()+500000;
  while(Date.now()<deadline){
    await act(async()=>{task=await originalFetch(base+'/api/v1/research/tasks/'+submission.task.id).then(r=>r.json());await new Promise(r=>setTimeout(r,250));});
    if(!['queued','running'].includes(task.status))break;
  }
  assert.equal(task.status,'succeeded',task.error);
  await wait(()=>assert.ok(taskCard(context.seed_name).querySelector('.research-result')));
  await click(button('查看评分',taskCard(context.seed_name)));
  await wait(()=>assert.equal(document.getElementById('factor-run')?.value,task.result.factor_run_id));
  await wait(()=>assert.equal(document.getElementById('score-date')?.value,submission.request.backtest.end));
  await wait(()=>assert.equal(document.querySelectorAll('[data-testid="factor-calculation-details"] [data-factor-id]').length,10));
  assert.equal([...document.getElementById('instrument-select').options].filter(o=>o.value).length,context.original.data.codes.length);
  await page();await click(button('查看回测',taskCard(context.seed_name)));
  await wait(()=>assert.ok(document.querySelector(`a[href="/api/v1/backtests/${task.result.backtest_run_id}/download"]`)));
  await page();await click(button('查看因子评估',taskCard(context.seed_name)));
  await wait(()=>assert.equal(document.getElementById('evaluation-select')?.value,task.result.evaluation_run_id));
  await writeFile(arg('--output'),JSON.stringify({status:'passed',rejection,required:context.required,available:context.available,
    earliest:context.earliest,request:submission.request,task},null,2));
  console.log('WARMUP_UI_PASSED');
} finally {await act(async()=>root.unmount());dom.window.close();globalThis.fetch=originalFetch;}
