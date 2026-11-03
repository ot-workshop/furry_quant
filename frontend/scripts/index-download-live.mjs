// Actual React panel reads the real local warehouse status; never starts a job.
import assert from 'node:assert/strict';
import {writeFile} from 'node:fs/promises';
import {createDOM} from '../tests/helpers/dom.mjs';
const arg=name=>process.argv[process.argv.indexOf(name)+1],base=arg('--base');
assert.match(base,/^http:\/\/127\.0\.0\.1:\d+$/);
const {dom,components,React,act,createRoot}=await createDOM({url:base});
const original=globalThis.fetch,requests=[];
globalThis.fetch=async(url,init={})=>{requests.push({url,method:init.method??'GET'});return original(new URL(url,base),init);};
const root=createRoot(document.getElementById('root'));
try{
  await act(async()=>root.render(React.createElement(components.IndexDownloadPanel)));
  const until=Date.now()+15000;
  while(!document.body.textContent.includes('493')){
    if(Date.now()>until)throw new Error(document.body.textContent);
    await act(async()=>new Promise(resolve=>setTimeout(resolve,50)));
  }
  const text=document.body.textContent;
  assert.match(text,/2019-06-28/);assert.match(text,/2020-01-02/);assert.match(text,/2026-10-30/);
  assert.match(text,/83 期/);assert.match(text,/串行/);assert.match(text,/核验通过/);
  assert.equal(requests.filter(r=>r.method==='POST').length,0);
  await writeFile(arg('--output'),JSON.stringify({status:'passed',text,requests},null,2));
  console.log('INDEX_DOWNLOAD_LIVE_PANEL_PASSED');
}finally{await act(async()=>root.unmount());dom.window.close();globalThis.fetch=original;}
