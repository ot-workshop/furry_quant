// Real React events against the real local report API. Does not start downloads.
import assert from 'node:assert/strict';
import {writeFile} from 'node:fs/promises';
import {createDOM} from '../tests/helpers/dom.mjs';
const arg=name=>process.argv[process.argv.indexOf(name)+1],base=arg('--base');
assert.match(base,/^http:\/\/127\.0\.0\.1:\d+$/);
const {dom,components,React,act,createRoot}=await createDOM({url:base});
const original=globalThis.fetch,requests=[];
globalThis.fetch=async(url,init={})=>{requests.push({url:String(url),method:init.method??'GET'});return original(new URL(url,base),init);};
const root=createRoot(document.getElementById('root'));
async function until(check){const deadline=Date.now()+15000;while(!check()){
  if(Date.now()>deadline)throw new Error(document.body.textContent);
  await act(async()=>new Promise(resolve=>setTimeout(resolve,30)));
}}
async function find(code){
  const input=document.querySelector('[aria-label="筛选报告股票"]');
  await act(async()=>{Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set.call(input,code);input.dispatchEvent(new window.Event('input',{bubbles:true}));});
  return document.querySelector('[aria-label="自动处理报告"]').textContent;
}
try{
  await act(async()=>root.render(React.createElement(components.IndexDownloadPanel)));
  await until(()=>document.body.textContent.includes('493'));
  assert.equal(requests.filter(r=>r.url.endsWith('/report')).length,0);
  await act(async()=>[...document.querySelectorAll('button')].find(b=>b.textContent==='查看自动处理报告').click());
  await until(()=>document.querySelector('[aria-label="筛选报告股票"]'));
  const kaisai=await find('688065.SH');
  assert.match(kaisai,/官方确认全天停牌/);assert.match(kaisai,/盘中停牌保留真实成交/);
  assert.match(kaisai,/2023-06-15/);assert.match(kaisai,/2023-06-21/);assert.match(kaisai,/单源检查通过/);
  const guangqi=await find('601238.SH');
  assert.match(guangqi,/官方停复牌状态存在歧义/);assert.match(guangqi,/独立交易状态确认停牌/);
  assert.match(guangqi,/2026-09-15/);
  const rongbai=await find('688005.SH');assert.match(rongbai,/官方停复牌状态存在歧义/);
  const response=await globalThis.fetch('/api/v1/research/index-download/report/download');
  assert.equal(response.status,200);assert.match(response.headers.get('content-disposition'),/attachment/);
  const report=await response.json();assert.equal(report.summary.total,493);assert.equal(report.summary.passed,493);
  assert.equal(report.summary.needs_review,0);assert.equal(report.stocks.length,493);
  assert.equal(requests.filter(r=>r.method!=='GET').length,0);
  await writeFile(arg('--output'),JSON.stringify({status:'passed',summary:report.summary,kaisai,guangqi,rongbai,requests},null,2));
  console.log('HANDLING_REPORT_LIVE_PASSED');
}finally{await act(async()=>root.unmount());dom.window.close();globalThis.fetch=original;}
