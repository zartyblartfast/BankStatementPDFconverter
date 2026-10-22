'use strict';
const fs=require('fs'),vm=require('vm'),assert=require('assert');
class Element{
  constructor(){this.listeners={};this.children=[];this.dataset={};this.value='';this.textContent='';this.disabled=false;}
  addEventListener(name,fn){(this.listeners[name]??=[]).push(fn);}
  async emit(name){for(const fn of this.listeners[name]||[])await fn();}
  replaceChildren(...nodes){this.children=nodes;}
  appendChild(node){this.children.push(node);return node;}
  add(node){this.children.push(node);}
  showModal(){this.open=true;}
  close(){this.open=false;this.emit('close');}
  focus(){this.focused=true;}
}
(async()=>{
 const ids=['rulesStatus','ruleInspect','ruleInspectClose','ruleInspectKey','ruleInspectMatches','ruleInspectHistory','ruleEdit','ruleEditTitle','ruleEditKey','ruleEditMode','ruleEditCategory','ruleEditSubcategory','ruleEditState','ruleEditCategories','ruleEditStateField','ruleEditHint','ruleEditStatus','ruleEditCancel','ruleEditSave'];
 const els=Object.fromEntries(ids.map(id=>[id,new Element()]));
 const row=new Element();row.dataset={key:'SHOP <literal>',category:'Fixture',subcategory:'Old',enabled:'false'};
 const controls=Object.fromEntries(['.rule-category','.rule-subcategory','.rule-edit','.rule-toggle','.rule-inspect'].map(name=>[name,new Element()]));row.querySelector=name=>controls[name];controls['.rule-category'].value='Fixture';
 const actions=[];let result={cancelled:true},reloads=0;
 const detail={ok:true,key:'SHOP <literal>',rule:{category:'Fixture',subcategory:'Old',enabled:false},matches:[{txn_id:'9223372036854775807',date:'2026-01-01',description:'<script>literal</script>',category:'Fixture',subcategory:'Old'}],history:[{id:'latest',at:'now',before:{category:'Fixture',subcategory:'Old',enabled:true},after:{category:'Fixture',subcategory:'Old',enabled:false},can_undo:true},{id:'earlier',at:'then',before:null,after:{category:'Fixture',subcategory:'Old',enabled:true},can_undo:false}]};
 const context={window:{ruleActions:{preview:async payload=>{actions.push(payload);return result;}},location:{reload(){reloads++;}}},document:{getElementById:id=>els[id],querySelectorAll:selector=>selector==='.rule-row'?[row]:[],createElement:()=>new Element()},Option:class{constructor(text,value){this.textContent=text;this.value=value;}},CAT_TREE:{Fixture:['Old','Test']},encodeURIComponent,Error,fetch:async()=>({ok:true,json:async()=>detail})};
 vm.runInNewContext(fs.readFileSync('static/rules.js','utf8'),context);
 await controls['.rule-edit'].emit('click');els.ruleEditSubcategory.value='Test';await els.ruleEditSave.emit('click');
 assert.equal(actions[0].enabled,false);assert.equal(actions[0].subcategory,'Test');assert(els.rulesStatus.textContent.includes('not saved'));await els.ruleEditCancel.emit('click');
 await controls['.rule-edit'].emit('click');els.ruleEditMode.value='state';await els.ruleEditMode.emit('change');els.ruleEditState.value='true';await els.ruleEditSave.emit('click');assert.equal(actions[1].enabled,true);assert.equal(actions[1].subcategory,'Old');await els.ruleEditCancel.emit('click');
 await controls['.rule-inspect'].emit('click');assert(els.ruleInspect.open);assert.equal(els.ruleInspectMatches.children[0].children[0].textContent,'9223372036854775807');assert.equal(els.ruleInspectMatches.children[0].children[2].textContent,'<script>literal</script>');
 assert.equal(els.ruleInspectHistory.children[0].children.length,1);assert.equal(els.ruleInspectHistory.children[1].children.length,0);
 result={saved:true};await els.ruleInspectHistory.children[0].children[0].emit('click');await new Promise(r=>setImmediate(r));assert.equal(actions[2].undo_id,'latest');assert.equal(reloads,0);assert(els.rulesStatus.textContent.startsWith('Rule change saved.'));assert(els.rulesStatus.textContent.includes('Refresh this page to see the updated list.'));
 // An obsolete current destination is not in CAT_TREE. Disable must send
 // the row's exact saved pair, not a dropdown fallback or pending edit.
 row.dataset={key:'SHOP <literal>',category:'Removed',subcategory:'Old',enabled:'true'};
 await controls['.rule-edit'].emit('click');els.ruleEditCategory.value='Fixture';els.ruleEditSubcategory.value='Test';els.ruleEditMode.value='state';await els.ruleEditMode.emit('change');els.ruleEditState.value='false';
 result={cancelled:true};
 await els.ruleEditSave.emit('click');
 assert.equal(actions[3].category,'Removed');assert.equal(actions[3].subcategory,'Old');assert.equal(actions[3].enabled,false);
 result={unknown:true,error:'Unknown save'};await els.ruleEditSave.emit('click');const count=actions.length;await els.ruleEditSave.emit('click');assert.equal(actions.length,count);assert(els.rulesStatus.textContent.includes('unknown'));
 console.log('rules_page_probe: real edit/disabled preservation/re-enable/evidence/history/targeted undo/unknown freeze passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
