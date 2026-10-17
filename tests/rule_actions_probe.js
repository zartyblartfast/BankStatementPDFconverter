'use strict';
const assert=require('assert'),fs=require('fs'),vm=require('vm');
class Element {
  constructor(){this.listeners={};this.children=[];this.dataset={csrf:'fixture'};this.textContent='';this.disabled=false;this.open=false;}
  addEventListener(type,fn){(this.listeners[type]??=[]).push(fn);}
  async emit(type,event={}){for(const fn of this.listeners[type]||[])await fn({preventDefault(){},...event});}
  replaceChildren(...children){this.children=children;}
  appendChild(child){this.children.push(child);return child;}
  showModal(){this.open=true;}
  close(){this.open=false;this.emit('close');}
}
const ids=['rulePreview','rulePreviewSummary','rulePreviewMatches','rulePreviewApprove','rulePreviewCancel','rulePreviewStatus'];
const els=Object.fromEntries(ids.map(id=>[id,new Element()]));
let calls=[],responses=[];
const context={window:{},document:{getElementById:id=>els[id],createElement:()=>new Element()},JSON,BigInt,Error,fetch:async(url,options)=>{calls.push({url,body:options.body});const data=responses.shift();return {ok:data.ok!==false,json:async()=>data};}};
vm.runInNewContext(fs.readFileSync('static/rule_actions.js','utf8'),context);
const api=context.window.ruleActions;
assert.equal(api.encode({category:'literal __proto__'},{txn_id:'9223372036854775807'}),'{"category":"literal __proto__","txn_id":9223372036854775807}');
assert.throws(()=>api.encode({},{txn_id:'9223372036854775808'}));
assert.throws(()=>api.encode({},{txn_id:'01'}));
assert.equal(api.encode({category:'Fixture'},{exception_ids:['1','9223372036854775807']}),'{"category":"Fixture","exception_ids":[1,9223372036854775807]}');
const flush=()=>new Promise(r=>setImmediate(r));
(async()=>{
  responses.push({ok:true,preview_token:'one',key:'SHOP',old:{category:'Old',subcategory:'One',enabled:true},proposed:{category:'New',subcategory:'Two',enabled:true},noop:false,matches:[{txn_id:'9223372036854775807',date:'2026-01-01',description:'<literal>',category:'Old',subcategory:'One',account_id:'BETA',account_name:'Second <literal> account'}]});
  const cancelled=api.preview({category:'New',subcategory:'Two'},{txn_id:'9223372036854775807'});
  await assert.rejects(api.preview({key:'SHOP',category:'New',subcategory:'Two',enabled:true}),/existing rule preview/);
  await flush();assert(els.rulePreviewSummary.textContent.includes('Old / One'));assert(els.rulePreviewSummary.textContent.includes('New / Two'));
  assert(els.rulePreviewMatches.children[0].textContent.includes('9223372036854775807'));
  assert(els.rulePreviewMatches.children[0].textContent.includes('Second <literal> account'));
  assert(els.rulePreviewSummary.textContent.includes('across all accounts'));
  assert(els.rulePreviewSummary.textContent.includes('All-account evidence'));
  await els.rulePreviewCancel.emit('click');assert.equal((await cancelled).cancelled,true);assert.equal(calls.length,1);
  responses.push({ok:true,preview_token:'two',key:'SHOP',old:null,proposed:{category:'New',subcategory:'Two',enabled:true},noop:false,matches:[]},{ok:true,changed:true});
  const saved=api.preview({key:'SHOP',category:'New',subcategory:'Two',enabled:true});await flush();await els.rulePreviewApprove.emit('click');assert.equal((await saved).saved,true);assert.equal(calls.length,3);
  responses.push({ok:true,preview_token:'three',key:'SHOP',old:{category:'New',subcategory:'Two',enabled:true},proposed:{category:'New',subcategory:'Two',enabled:false},noop:false,matches:[]},{ok:false,error:'unknown outcome'});
  const failed=api.preview({key:'SHOP',category:'New',subcategory:'Two',enabled:false});await flush();await els.rulePreviewApprove.emit('click');assert.equal((await failed).unknown,true);assert(els.rulePreviewStatus.textContent.includes('unknown outcome'));assert(els.rulePreviewApprove.disabled);await els.rulePreviewCancel.emit('click');
  responses.push({ok:true,preview_token:'four',key:'SHOP',old:{category:'New',subcategory:'Two',enabled:true},proposed:{category:'New',subcategory:'Two',enabled:true},noop:true,matches:[]});
  const noop=api.preview({key:'SHOP',category:'New',subcategory:'Two',enabled:true});await flush();assert(els.rulePreviewApprove.disabled);await els.rulePreviewCancel.emit('click');assert.equal((await noop).noop,true);
  responses.push({ok:true,preview_token:'undo',key:'SHOP',old:{category:'New',subcategory:'Two',enabled:true},proposed:null,noop:false,matches:[]});
  const undone=api.preview({undo_id:'latest'});await flush();assert(els.rulePreviewSummary.textContent.includes('Undo this rule change'));assert(els.rulePreviewSummary.textContent.includes('remove the rule'));await els.rulePreviewCancel.emit('click');await undone;
  console.log('rule_actions_probe: int64, conflict evidence, cancel, approval, unknown freeze and no-op passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
