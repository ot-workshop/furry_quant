import assert from 'node:assert/strict';
import {test} from 'node:test';
import {createDOM} from './helpers/dom.mjs';
const {components,React,act,createRoot}=await createDOM();
test('chronology guards remain distinct from PIT certification and capacity limits',async()=>{
 const root=createRoot(document.getElementById('root'));
 try{
  const audit={version:'fixture',point_in_time_certified:false,valuation_fields_used:['pe_ttm'],note:'供应商历史版本未认证',
    items:[{key:'valuation',label:'PE/PB',status:'unverified',detail:'历史首次发布时间未认证'},
           {key:'signal',label:'信号时序',status:'guarded',detail:'开盘前可用'},
           {key:'capacity',label:'成交容量',status:'not_modeled',detail:'固定比例费用不代表成交容量'}],
    capacity_diagnostics:{valid_stock_days:2,missing_lookback_stock_days:1,max_prior_turnover_ratio:.25,above_ten_percent:1,note:'只用于诊断'}};
  await act(async()=>root.render(React.createElement(components.TemporalAuditPanel,{audit})));
  assert.match(document.body.textContent,/历史公开时点尚未完全认证/);
  assert.match(document.body.textContent,/已设程序约束/);assert.match(document.body.textContent,/未建模/);
  assert.match(document.body.textContent,/25\.00%/);assert.match(document.body.textContent,/缺少预热的有1个/);
 }finally{await act(async()=>root.unmount());}
});
test('legacy results do not acquire invented audit certifications',async()=>{
 const root=createRoot(document.getElementById('root'));
 try{
  await act(async()=>root.render(React.createElement(components.TemporalAuditPanel,{})));
  assert.match(document.body.textContent,/旧结果未保存本轮核验清单/);
  assert.equal(document.querySelector('[data-testid="temporal-audit"] table'),null);
 }finally{await act(async()=>root.unmount());}
});
