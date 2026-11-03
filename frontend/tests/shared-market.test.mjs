import assert from 'node:assert/strict';
import {test} from 'node:test';
import {createDOM} from './helpers/dom.mjs';
const {components,React,act,createRoot}=await createDOM();
const empty={worker_running:false,registered_codes:2,jobs:[],versions:[]};
const button=text=>[...document.querySelectorAll('button')].find(n=>n.textContent===text);
async function change(id,value){const node=document.getElementById(id);const prototype=node.tagName==='TEXTAREA'?window.HTMLTextAreaElement.prototype:node.tagName==='SELECT'?HTMLSelectElement.prototype:HTMLInputElement.prototype;
  await act(async()=>{Object.getOwnPropertyDescriptor(prototype,'value').set.call(node,value);node.dispatchEvent(new Event(node.tagName==='SELECT'?'change':'input',{bubbles:true}));});}
function plan(spec){return {id:'a'.repeat(24),spec,start:spec.start,end:spec.end,data_start:'2024-12-01',codes_count:2,reused_rows:10,missing_rows:2,estimated_quote_requests:8,restricted:true,note:'fixture',tasks:[]};}

test('download preview requires confirmation; edits invalidate it and pass selected range',async()=>{
 const calls=[];globalThis.fetch=async(url,init={})=>{const body=init.body?JSON.parse(init.body):undefined;calls.push([url,body]);
   return new Response(JSON.stringify(url.endsWith('/preview')?plan(body):url.endsWith('/start')?{status:'queued'}:empty));};
 const root=createRoot(document.getElementById('root'));
 try{
  await act(async()=>root.render(React.createElement(components.SharedMarketPanel)));
  assert.equal(calls.filter(c=>c[1]).length,0);
  await change('shared-scope','all_market');await change('shared-codes','600036.sh 600519.sh');await change('shared-start','2024-01-01');await change('shared-warmup','21');
  await act(async()=>button('预览覆盖与增量方案').click());
  const preview=calls.find(c=>c[0].endsWith('/preview'))[1];assert.deepEqual(preview.codes,['600036.SH','600519.SH']);assert.equal(preview.start,'2024-01-01');assert.equal(preview.warmup,21);
  assert.ok(document.querySelector('[data-testid="shared-plan"]'));assert.equal(calls.filter(c=>c[0].endsWith('/start')).length,0);
  await change('shared-end','2025-02-28');assert.equal(document.querySelector('[data-testid="shared-plan"]'),null);
  await act(async()=>button('预览覆盖与增量方案').click());await act(async()=>button('确认方案并下载 / 更新').click());
  assert.deepEqual(calls.find(c=>c[0].endsWith('/start'))[1],{plan_id:'a'.repeat(24)});
 }finally{await act(async()=>root.unmount());}
});

test('late preview cannot restore confirmation after parameters changed',async()=>{
 let resolvePreview;globalThis.fetch=async(url,init={})=>url.endsWith('/preview')?new Promise(resolve=>{resolvePreview=()=>resolve(new Response(JSON.stringify(plan(JSON.parse(init.body)))));}):new Response(JSON.stringify(empty));
 const root=createRoot(document.getElementById('root'));
 try{
  await act(async()=>root.render(React.createElement(components.SharedMarketPanel)));
  await act(async()=>button('预览覆盖与增量方案').click());await change('shared-start','2022-01-01');
  await act(async()=>resolvePreview());assert.equal(document.querySelector('[data-testid="shared-plan"]'),null);
 }finally{await act(async()=>root.unmount());}
});
