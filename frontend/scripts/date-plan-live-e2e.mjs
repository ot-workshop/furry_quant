// Actual React -> local calendar API -> explicit apply -> isolated paused queue.
import assert from 'node:assert/strict';
import {readFile,writeFile} from 'node:fs/promises';
import {createDOM} from '../tests/helpers/dom.mjs';
const arg=n=>process.argv[process.argv.indexOf(n)+1];
const base=arg('--base'),context=JSON.parse(await readFile(arg('--context'),'utf8'));
assert.match(base,/^http:\/\/127\.0\.0\.1:\d+$/);
const {dom,components,React,act,createRoot}=await createDOM({url:base});
const originalFetch=globalThis.fetch,posted=[],plans=[];
globalThis.fetch=async(url,init={})=>{
  const target=new URL(url,base),response=await originalFetch(target,{...init,headers:{...init.headers,Origin:base}});
  if(init.method==='POST' && target.pathname.endsWith('/tasks'))posted.push({request:JSON.parse(init.body),task:await response.clone().json()});
  if(init.method==='POST' && target.pathname.endsWith('/date-plan'))plans.push(await response.clone().json());
  return response;
};
const root=createRoot(document.getElementById('root'));
const click=async el=>{assert.ok(el);await act(async()=>el.click());};
async function wait(check){const until=Date.now()+30000;while(true){try{return check();}catch(e){if(Date.now()>until)throw e;await act(async()=>new Promise(r=>setTimeout(r,50)));}}}
const button=text=>{const b=[...document.querySelectorAll('button')].find(b=>b.textContent.trim()===text);assert.ok(b,text);return b;};
async function input(id,value){const node=document.getElementById(id);assert.ok(node,id);const proto=node.tagName==='TEXTAREA'?dom.window.HTMLTextAreaElement.prototype:HTMLInputElement.prototype;await act(async()=>{Object.getOwnPropertyDescriptor(proto,'value').set.call(node,String(value));node.dispatchEvent(new Event('input',{bubbles:true}));});}
try{
  await act(async()=>root.render(React.createElement(components.App)));
  await wait(()=>assert.ok(document.getElementById('research-name')));
  await click(document.getElementById('research-source-download'));
  await click(document.getElementById('research-auto-dates'));
  await input('research-download-codes',context.request.data.codes.join(','));
  await input('research-data-start','2022-01-01');await input('research-data-end','2026-09-30');
  await input('research-bt-start','2022-01-03');await input('research-bt-end','2026-09-30');
  for(const spec of context.request.factors){const key=spec.name,box=document.getElementById('research-factor-'+key);if(!box.checked)await click(box);await input('research-factor-weight-'+key,spec.weight*100);}
  await click(button('创建研究任务'));assert.equal(posted.length,0);
  const evidence=[];
  for(const mode of ['extend_history','delay_backtest']){
    await input('research-data-start','2022-01-01');await input('research-bt-start','2022-01-03');
    await click(document.getElementById('research-plan-dates'));
    await wait(()=>assert.ok(document.getElementById('research-apply-'+mode)));
    const plan=plans.at(-1),proposal=plan.proposals.find(p=>p.mode===mode);
    assert.equal(plan.first_trading_day,'2022-01-04');assert.equal(plan.required_history,127);
    assert.deepEqual(plan,context.plan);
    assert.equal(document.getElementById('research-data-start').value,'2022-01-01');
    assert.equal(document.getElementById('research-bt-start').value,'2022-01-03');
    await click(document.getElementById('research-apply-'+mode));
    await click(button('创建研究任务'));await wait(()=>assert.equal(posted.length,evidence.length+1));
    const entry=posted.at(-1);assert.equal(entry.task.status,'queued',JSON.stringify(entry.task));
    assert.equal(entry.request.data.start,proposal.data_start);
    assert.equal(entry.request.backtest.start,proposal.backtest_start);
    assert.equal(entry.request.backtest.end,'2026-09-30');
    assert.deepEqual([...entry.request.data.codes].sort(),[...context.request.data.codes].sort());
    const sorted=specs=>[...specs].sort((a,b)=>a.name.localeCompare(b.name));
    assert.deepEqual(sorted(entry.request.factors),sorted(context.request.factors));
    evidence.push({mode,proposal,request:entry.request,task_id:entry.task.id});
  }
  await writeFile(arg('--output'),JSON.stringify({status:'passed',cases:evidence},null,2));
  console.log('DATE_PLAN_REACT_PASSED');
}finally{await act(async()=>root.unmount());dom.window.close();globalThis.fetch=originalFetch;}
