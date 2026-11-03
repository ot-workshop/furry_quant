// Actual React -> local date planner -> explicit confirmation -> paused queue.
import assert from 'node:assert/strict';
import {readFile,writeFile} from 'node:fs/promises';
import {createDOM} from '../tests/helpers/dom.mjs';
const arg=name=>process.argv[process.argv.indexOf(name)+1];
const base=arg('--base'),context=JSON.parse(await readFile(arg('--context'),'utf8'));
assert.match(base,/^http:\/\/127\.0\.0\.1:\d+$/);
const {dom,components,React,act,createRoot}=await createDOM({url:base});
const originalFetch=globalThis.fetch,posted=[],plans=[];
globalThis.fetch=async(url,init={})=>{
  const target=new URL(url,base),answer=await originalFetch(target,{...init,headers:{...init.headers,Origin:base}});
  if(init.method==='POST' && target.pathname.endsWith('/tasks'))posted.push({request:JSON.parse(init.body),task:await answer.clone().json()});
  if(init.method==='POST' && target.pathname.endsWith('/date-plan'))plans.push({input:JSON.parse(init.body),output:await answer.clone().json()});
  return answer;
};
const root=createRoot(document.getElementById('root'));
const sorted=items=>[...items].sort((a,b)=>(a.id??a.name).localeCompare(b.id??b.name));
const click=async node=>{assert.ok(node);await act(async()=>node.click());};
const button=text=>[...document.querySelectorAll('button')].find(node=>node.textContent.trim()===text);
async function wait(check){const until=Date.now()+45000;while(true){try{return check();}catch(error){if(Date.now()>until)throw error;await act(async()=>new Promise(resolve=>setTimeout(resolve,50)));}}}
async function input(id,value){const node=document.getElementById(id);assert.ok(node,id);const prototype=node.tagName==='TEXTAREA'?dom.window.HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
  await act(async()=>{Object.getOwnPropertyDescriptor(prototype,'value').set.call(node,String(value));node.dispatchEvent(new Event('input',{bubbles:true}));});}
try {
  await act(async()=>root.render(React.createElement(components.App)));
  await wait(()=>assert.ok(document.getElementById('research-name')));
  await click(document.getElementById('research-source-download'));
  assert.equal(document.getElementById('research-auto-dates').checked,true);
  await input('research-bt-start',context.request.backtest.start);
  await input('research-bt-end',context.request.backtest.end);
  for(const spec of context.request.factors){const key=spec.name,box=document.getElementById('research-factor-'+key);if(!box.checked)await click(box);await input('research-factor-weight-'+key,spec.weight*100);}
  const evidence=[];
  for(const size of [2,15]) {
    const codes=context.request.data.codes.slice(0,size);
    await input('research-download-codes',codes.join(','));
    await input('research-topk',Math.min(size,3));
    await click(button('预览下载方案'));
    await wait(()=>assert.ok(document.getElementById('research-confirm-download')));
    assert.equal(posted.length,evidence.length);
    const plan=plans.at(-1),proposal=plan.output.proposals[0];
    assert.deepEqual({...plan.input,factors:sorted(plan.input.factors)},{auto_range:true,backtest_start:context.request.backtest.start,
      backtest_end:context.request.backtest.end,factors:sorted(context.request.factors)});
    assert.equal(proposal.mode,'auto_range');assert.equal(proposal.history_days,127);
    assert.ok(proposal.warmup_end<proposal.backtest_start);
    assert.ok(document.getElementById('research-download-confirmation').textContent.includes(proposal.warmup_start));
    await click(document.getElementById('research-confirm-download'));
    await wait(()=>assert.equal(posted.length,evidence.length+1));
    const task=posted.at(-1);assert.equal(task.task.status,'queued',JSON.stringify(task));
    assert.deepEqual(task.request.data.codes,codes);
    assert.deepEqual(sorted(task.request.factors),sorted(context.request.factors));
    assert.equal(task.request.data.start,proposal.data_start);assert.equal(task.request.data.end,proposal.data_end);
    assert.equal(task.request.backtest.start,proposal.backtest_start);assert.equal(task.request.backtest.end,proposal.backtest_end);
    evidence.push({size,plan,request:task.request,task_id:task.task.id});
  }
  await writeFile(arg('--output'),JSON.stringify({status:'passed',cases:evidence},null,2));
  console.log('AUTOMATIC_DOWNLOAD_LIVE_PASSED');
} finally {await act(async()=>root.unmount());dom.window.close();globalThis.fetch=originalFetch;}
