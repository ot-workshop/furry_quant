import assert from 'node:assert/strict';
import {writeFile} from 'node:fs/promises';
import {createDOM} from '../tests/helpers/dom.mjs';
const base=process.argv[process.argv.indexOf('--base')+1],planId=process.argv[process.argv.indexOf('--plan')+1];
const {components,React,act,createRoot}=await createDOM({url:base});
const savedFetch=globalThis.fetch;let submitted;
globalThis.fetch=async(url,init={})=>{
 const target=new URL(url,base),response=await savedFetch(target,{...init,headers:{...init.headers,Origin:base}});
 if(init.method==='POST'){const value=await response.clone().json();assert.ok(response.ok,JSON.stringify(value));if(target.pathname.endsWith('/research/tasks'))submitted=value;}
 return response;
};
async function get(path){const response=await savedFetch(base+path);assert.ok(response.ok,path);return response.json();}
const root=createRoot(document.getElementById('root'));
const button=(text,parent=document)=>[...parent.querySelectorAll('button')].find(n=>n.textContent.trim()===text);
async function click(node){assert.ok(node);await act(async()=>node.click());}
async function waitFor(check){const end=Date.now()+90000;for(;;){try{return check();}catch(error){if(Date.now()>end)throw error;await act(async()=>new Promise(resolve=>setTimeout(resolve,100)));}}}
async function input(id,value){const node=document.getElementById(id);assert.ok(node,id);const proto=node.tagName==='SELECT'?HTMLSelectElement.prototype:HTMLInputElement.prototype;
 await act(async()=>{Object.getOwnPropertyDescriptor(proto,'value').set.call(node,String(value));node.dispatchEvent(new Event(node.tagName==='SELECT'?'change':'input',{bubbles:true}));});}
try{
 await act(async()=>root.render(React.createElement(React.StrictMode,null,React.createElement(components.App))));
 await click([...document.querySelectorAll('.nav-button')].find(n=>n.textContent.includes('数据管理')));
 const row=await waitFor(()=>{const row=document.querySelector(`[data-plan-id="${planId}"]`);assert.ok(row);return row;});
 const resume=button('续传此计划',row);
 if(resume)await click(resume);else assert.match(row.textContent,/已完成/);
 let state,job;const deadline=Date.now()+300000;
 do{assert.ok(Date.now()<deadline,'resume timeout');await act(async()=>new Promise(resolve=>setTimeout(resolve,250)));state=await get('/api/v1/research/market-download');job=state.jobs.find(j=>j.id===planId);}while(!job||job.status==='running'||state.worker_running);
 assert.equal(job.status,'complete',JSON.stringify(job));assert.equal(job.completed,301);assert.equal(job.downloaded_rows,0);
 const version=state.versions.find(v=>v.id===job.version_id);assert.ok(version);
 await click([...document.querySelectorAll('.nav-button')].find(n=>n.textContent.includes('新建研究')));
 const options=await get('/api/v1/research/options'),choice=options.historical_indices.find(o=>o.shared_version===version.id);assert.ok(choice);
 await waitFor(()=>assert.ok(document.getElementById('research-source-historical')));
 await click(document.getElementById('research-source-historical'));await input('research-warehouse-version',choice.id);
 for(const def of options.factor_definitions){const node=document.getElementById('research-factor-'+def.name);if(node&&node.checked!==['momentum','value'].includes(def.name))await click(node);}
 const name='共享行情与增量更新（小范围验收）';await input('research-name',name);
 await input('research-factor-window-momentum',5);await input('research-factor-weight-momentum',60);await input('research-factor-weight-value',40);
 await input('research-bt-start','2025-03-03');await input('research-bt-end','2025-05-30');await input('research-topk',10);await input('research-neutralization','off');
 await input('research-rebalance-frequency','weekly');
 if(!document.getElementById('research-horizons'))await click(button('展开高级设置'));
 await input('research-horizons','1');await input('research-groups',2);await input('research-min-cross',3);
 await click(button('创建研究任务'));await waitFor(()=>assert.ok(submitted));
 let task=submitted;const until=Date.now()+300000;
 while(['queued','running'].includes(task.status)){assert.ok(Date.now()<until,'research timeout');await act(async()=>new Promise(resolve=>setTimeout(resolve,250)));task=await get('/api/v1/research/tasks/'+task.id);}
 assert.equal(task.status,'succeeded',JSON.stringify(task));assert.equal(task.request.data.warehouse_id,choice.id);
 await waitFor(()=>assert.ok(button('查看回测',[...document.querySelectorAll('.research-task')].find(n=>n.textContent.includes(name)))));
 await click(button('查看回测',[...document.querySelectorAll('.research-task')].find(n=>n.textContent.includes(name))));
 await waitFor(()=>assert.ok(document.querySelector(`a[href="/api/v1/backtests/${task.result.backtest_run_id}/download"]`)));
 await click([...document.querySelectorAll('.nav-button')].find(n=>n.textContent.includes('新建研究')));
 const old=await waitFor(()=>{const row=[...document.querySelectorAll('.research-task')].find(n=>n.textContent.includes('我的研究（修复复跑）'));assert.ok(row);return row;});
 await click(button('查看回测',old));await waitFor(()=>assert.ok(document.querySelector('a[href="/api/v1/backtests/historical-qlib-fb1ef729c765/download"]')));
 await writeFile(new URL('../../.agent-work/shared-main-live-evidence.json',import.meta.url),JSON.stringify({status:'passed',resumed_job:job,version,task,new_result_opened:true,old_result_opened:true},null,2));
 process.stdout.write('SHARED_MAIN_RESUME_RESEARCH_PASSED\n');
}finally{await act(async()=>root.unmount());}
