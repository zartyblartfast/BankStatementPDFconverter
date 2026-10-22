'use strict';
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const data=JSON.parse(fs.readFileSync(0,'utf8'));
class Element{
 constructor(){this.listeners={};this.dataset={};this.children=[];this.value='';this.textContent='';this.isConnected=true;}
 addEventListener(n,f){(this.listeners[n]??=[]).push(f)}
 async emit(n){for(const f of this.listeners[n]||[])await f({preventDefault(){}})}
 replaceChildren(...x){this.children=x;this.value=''} add(x){this.children.push(x)} appendChild(x){this.children.push(x)}
 showModal(){this.open=true} close(){this.open=false;this.emit('close')} focus(){this.focused=true} remove(){this.isConnected=false}
}
const ids='rulesStatus ruleEdit ruleEditTitle ruleEditKey ruleEditMode ruleEditCategory ruleEditSubcategory ruleEditState ruleEditCategories ruleEditStateField ruleEditHint ruleEditStatus ruleEditCancel ruleEditSave rulePreview rulePreviewSummary rulePreviewMatches rulePreviewApprove rulePreviewCancel rulePreviewStatus rulesReviewTitle exceptionRulesTable rulesAllDone'.split(' ');
const els=Object.fromEntries(ids.map(id=>[id,new Element()]));els.rulePreview.dataset.csrf='synthetic';
function row(key){const r=new Element();r.dataset={key,category:'',subcategory:'',enabled:'false'};r.button=new Element();r.querySelector=s=>s==='.rule-edit'?r.button:null;return r}
const rows=[row('SYNTHETIC'),row('NEXT')],calls=[];
let reloads=0,failConfirm=false,failPreview=false;
const ctx={document:{getElementById:id=>els[id]||null,querySelectorAll:s=>s==='.rule-row'?rows.filter(r=>r.isConnected):[],createElement:()=>new Element()},window:{location:{search:'',href:'http://127.0.0.1/exceptions',reload(){reloads++}}},CAT_TREE:data.suggestion.categories,Option:class{constructor(t,v){this.value=v}},URL,URLSearchParams,BigInt,Error,encodeURIComponent,
 fetch:async(url,options)=>{calls.push({url,options});if(failConfirm&&url.endsWith('/confirm'))throw Error('Connection lost; outcome unknown');if(failPreview&&url.endsWith('/preview'))return {ok:false,json:async()=>({ok:false,error:'Reopen this suggestion; evidence changed.'})};return {ok:true,json:async()=>url.includes('/suggestion')?data.suggestion:url.endsWith('/preview')?data.preview:{ok:true,changed:true}}}};
vm.runInNewContext(fs.readFileSync('static/rule_actions.js','utf8'),ctx);
vm.runInNewContext(fs.readFileSync(process.env.RULES_JS||'static/rules.js','utf8'),ctx);
const tick=()=>new Promise(r=>setImmediate(r));
(async()=>{
 await rows[0].button.emit('click');assert(els.ruleEdit.open);assert.equal(els.ruleEditCategory.value,'Fixture');
 const saving=els.ruleEditSave.emit('click');await tick();assert(els.rulePreview.open);
 assert(!calls.some(c=>c.url.endsWith('/confirm')),'no save before explicit approval');
 await els.rulePreviewApprove.emit('click');await saving;await tick();
 assert.equal(reloads,0,'saving must not reload or navigate away');
 assert(!els.ruleEdit.open);assert(!els.rulePreview.open);assert(els.rulesStatus.textContent.startsWith('Rule saved for SYNTHETIC.'));
 assert(!rows[0].isConnected);assert(rows[1].isConnected);assert(els.rulesStatus.focused);assert.equal(calls.filter(c=>c.url.includes('/suggestion')).length,1,'do not auto-open next');
 await rows[1].button.emit('click');assert(els.ruleEdit.open);await els.ruleEditCancel.emit('click');assert(rows[1].isConnected);assert(rows[1].button.focused);
 await rows[1].button.emit('click');failPreview=true;await els.ruleEditSave.emit('click');assert(els.ruleEditStatus.textContent.includes('Reopen'));assert(rows[1].isConnected);assert(!els.rulePreview.open);await els.ruleEditCancel.emit('click');failPreview=false;
 await rows[1].button.emit('click');failConfirm=true;const unknown=els.ruleEditSave.emit('click');await tick();await els.rulePreviewApprove.emit('click');await unknown;
 assert(els.rulesStatus.textContent.startsWith('Rule outcome unknown.'));assert(rows[1].isConnected);assert(els.ruleEditSave.disabled);assert(els.rulePreviewApprove.disabled);assert.equal(reloads,0);
 const count=calls.length;await rows[1].button.emit('click');assert.equal(calls.length,count,'unknown outcomes block another action until reload');
 console.log(JSON.stringify({checks:'inline save, explicit approval, saved acknowledgement, no reload, no auto-next, cancel/focus, stale preview, uncertain confirmation locks retry',confirm_calls:calls.filter(c=>c.url.endsWith('/confirm')).length}));
})().catch(e=>{console.error(e);process.exitCode=1});
