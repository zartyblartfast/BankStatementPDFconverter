'use strict';
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const fixture=JSON.parse(fs.readFileSync(0,'utf8'));
class Element {
 constructor(){this.listeners={};this.children=[];this.dataset={};this.value='';this.textContent='';this.disabled=false;}
 addEventListener(n,fn){(this.listeners[n]??=[]).push(fn);}
 async emit(n){for(const fn of this.listeners[n]||[])await fn({preventDefault(){}});}
 replaceChildren(...nodes){this.children=nodes;this.value='';}
 appendChild(n){this.children.push(n);return n;}
 add(n){this.children.push(n);}
 showModal(){this.open=true;}
 close(){this.open=false;this.emit('close');}
 focus(){this.focused=true;}
}
function setup(search,suggestion=fixture.suggestion){
 const ids=['rulesStatus','ruleInspect','ruleInspectClose','ruleInspectKey','ruleInspectMatches','ruleInspectHistory','ruleEdit','ruleEditTitle','ruleEditKey','ruleEditMode','ruleEditCategory','ruleEditSubcategory','ruleEditState','ruleEditCategories','ruleEditStateField','ruleEditHint','ruleEditStatus','ruleEditCancel','ruleEditSave','rulePreview','rulePreviewSummary','rulePreviewMatches','rulePreviewApprove','rulePreviewCancel','rulePreviewStatus'];
 const els=Object.fromEntries(ids.map(id=>[id,new Element()]));els.rulePreview.dataset.csrf='synthetic';
 function row(key){const r=new Element();r.dataset={key,category:'',subcategory:'',enabled:'false'};r.controls={'.rule-edit':new Element(),'.rule-inspect':new Element()};r.querySelector=s=>r.controls[s];return r;}
 const rows=[row('SYNTHETIC EXTRA'),row('SYNTHETIC')],calls=[],replaced=[];
 const context={document:{getElementById:id=>els[id],querySelectorAll:s=>s==='.rule-row'?rows:[],createElement:()=>new Element()},
  window:{location:{search,href:'http://127.0.0.1/rules'+search,reload(){throw Error('No auto-save');}},history:{state:null,replaceState:(_s,_t,url)=>replaced.push(String(url))}},
  URL,URLSearchParams,encodeURIComponent,Error,BigInt,CAT_TREE:{Obsolete:['Old']},Option:class {constructor(t,v){this.textContent=t;this.value=v;}},
  fetch:async(url,options)=>{calls.push({url,options});if(url.startsWith('/api/rules/suggestion'))return {ok:true,json:async()=>suggestion};if(url==='/api/rules/preview')return {ok:true,json:async()=>fixture.preview};throw Error('Unexpected write '+url);}};
 vm.runInNewContext(fs.readFileSync('static/rule_actions.js','utf8'),context);
 vm.runInNewContext(fs.readFileSync('static/rules.js','utf8'),context);
 return {els,rows,calls,replaced};
}
const tick=()=>new Promise(r=>setImmediate(r));
(async()=>{
 const s=setup('?key=SYNTHETIC&intent=add_rule&account_id=ALPHA');await tick();
 assert(s.els.ruleEdit.open,'View suggestion must directly open editable proposal');
 assert.equal(s.els.ruleEditKey.textContent.startsWith('SYNTHETIC ·'),true,'exact key, not substring');
 assert.equal(s.els.ruleEditCategory.value,'Fixture');assert.equal(s.els.ruleEditSubcategory.value,'Test');
 assert.equal(s.calls.length,1);assert(!s.calls[0].url.includes('account_id'));
 assert.equal(s.replaced.length,1);assert(!s.replaced[0].includes('intent='));
 assert.equal(s.els.ruleEditMode.disabled,true);assert(s.els.ruleEditHint.textContent.includes('all accounts'));
 const save=s.els.ruleEditSave.emit('click');await tick();
 assert(s.els.rulePreview.open,'Existing explicit confirmation remains required');
 assert.equal(s.calls.length,2);const payload=JSON.parse(s.calls[1].options.body);
 assert.deepEqual(payload,{key:'SYNTHETIC',category:'Fixture',subcategory:'Test',enabled:true,suggestion_revision:fixture.suggestion.suggestion_revision});
 await s.els.rulePreviewCancel.emit('click');await save;
 assert(s.els.ruleEdit.open);await s.els.ruleEditCancel.emit('click');assert(!s.els.ruleEdit.open);
 assert(!s.calls.some(c=>c.url.includes('/confirm')));
 // Plain filters never launch actions; repeated/malformed intents fail closed.
 for(const query of ['?key=SYNTHETIC','?q=SYNTHETIC','?key=SYNTHETIC&key=SYNTHETIC&intent=add_rule','?key=SYNTHETIC&intent=add_rule&intent=add_rule','?key=SYN&intent=add_rule']){
  const x=setup(query);await tick();assert(!x.els.ruleEdit.open,query);assert.equal(x.calls.length,0,query);
 }
 // Conflicts/missing/obsolete/no evidence leave both fields blank, disclose counts and block preview without a choice.
 for(const assignments of [ [{category:'Fixture',subcategory:'Test',count:1,valid:true},{category:'Other',subcategory:'Old',count:1,valid:false}], [{category:'',subcategory:'',count:2,valid:false}], [] ]){
  const data={...fixture.suggestion,proposal:null,assignments,explanation:'No unanimous current assignment across all accounts. Make an explicit choice.'};
  const x=setup('?key=SYNTHETIC&intent=add_rule',data);await tick();
  assert(x.els.ruleEdit.open);assert.equal(x.els.ruleEditCategory.value,'');assert.equal(x.els.ruleEditSubcategory.value,'');assert(x.els.ruleEditHint.textContent.includes('explicit choice'));
  if(assignments.length)assert(x.els.ruleEditHint.textContent.includes('(2)')||x.els.ruleEditHint.textContent.includes('(1)'));
  await x.els.ruleEditSave.emit('click');assert.equal(x.calls.length,1);
  await x.els.ruleEditCancel.emit('click');
 }
 // Manual Add is the same fresh evidence path, not a second workflow.
 const manual=setup('?key=SYNTHETIC');await manual.rows[1].controls['.rule-edit'].emit('click');
 assert.equal(manual.els.ruleEditCategory.value,'Fixture');await manual.els.ruleEditCancel.emit('click');
 console.log(JSON.stringify({payload,checks:'direct exact-key one-shot, all-account fresh defaults, choice required, explicit preview/cancel, no auto-save'}));
})().catch(e=>{console.error(e);process.exitCode=1;});
