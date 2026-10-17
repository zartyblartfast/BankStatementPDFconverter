'use strict';
// Execute the shipped controller; no sort implementation is duplicated here.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
class Element {
  constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.listeners={};this.dataset={};this.attrs={};this.value='';this.disabled=false;this._text='';}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}
  set innerHTML(_){throw Error('Unsafe HTML rendering');}
  addEventListener(type,fn){(this.listeners[type]??=[]).push(fn);}
  async emit(type){for(const fn of this.listeners[type]||[])await fn({});}
  replaceChildren(...children){this._text='';this.children=[...children];}
  appendChild(child){this.children=this.children.filter(x=>x!==child);this.children.push(child);return child;}
  add(child){this.appendChild(child);}
  setAttribute(name,value){this.attrs[name]=value;}
  getAttribute(name){return this.attrs[name];}
  showModal(){this.open=true;}
  close(){this.open=false;this.emit('close');}
  focus(){this.focused=true;}
}
const ids=['rulesStatus','rulesAccount','rulesBody','suggestionsBody','ruleInspect','ruleInspectClose','ruleInspectKey','ruleInspectMatches','ruleInspectHistory','ruleEdit','ruleEditTitle','ruleEditKey','ruleEditMode','ruleEditCategory','ruleEditSubcategory','ruleEditState','ruleEditCategories','ruleEditStateField','ruleEditHint','ruleEditStatus','ruleEditCancel','ruleEditSave'];
const els=Object.fromEntries(ids.map(id=>[id,new Element()]));els.rulesAccount.value='BETA';els.rulesAccount.dataset.applied='BETA';
const ruleHeaders=['key','category','subcategory','state'];
const matchHeaders=['date','description','category','subcategory','account'];
function headers(fields){return fields.map(field=>{const th=new Element('th'),button=new Element('button');button.type='button';button.dataset.sort=field;button.textContent=field;button.parentElement=th;th.appendChild(button);return button;});}
const rh=headers(ruleHeaders),mh=headers(matchHeaders),sh=headers(['key','category']);
function row(key,category,subcategory,state){const r=new Element('tr');r.dataset={key,category,subcategory,state,enabled:state==='Enabled'?'true':'false'};
  r.controls=Object.fromEntries(['.rule-category','.rule-subcategory','.rule-edit','.rule-toggle','.rule-inspect'].map(x=>[x,new Element()]));
  r.controls['.rule-category'].value=category;r.querySelector=x=>r.controls[x];return r;}
const rows=[row('Z SHOP','B','Z','Disabled'),row('A <literal>','A','Y','Enabled'),row('A <literal>','A','Y','Enabled')];
els.rulesBody.replaceChildren(...rows);
const suggestions=[row('Z SUGGESTION','','',''),row('A SUGGESTION','','',''),row('UNCATEGORISED','','','')];
suggestions[0].dataset.sortCategory='A';suggestions[0].dataset.sortSubcategory='Z';
suggestions[1].dataset.sortCategory='Z';suggestions[1].dataset.sortSubcategory='A';
suggestions[2].dataset.sortCategory='Uncategorised';suggestions[2].dataset.sortSubcategory='';
delete suggestions[2].controls['.rule-edit']; // Category unknown: inspect/link only, never Add.
els.suggestionsBody.replaceChildren(...suggestions);
const urls=[],actions=[];
const detail={ok:true,key:'A <literal>',rule:null,matches:[
 {txn_id:'9223372036854775807',date:'2026-02-01',description:'Z <script>literal</script>',category:'Z',subcategory:'B',account_name:'Second & account',account_id:'BETA'},
 {txn_id:'9007199254740993',date:'2026-01-01',description:'A',category:'A',subcategory:'A',account_name:'First',account_id:'ALPHA'},
 {txn_id:'9007199254740995',date:'2026-01-01',description:'A',category:'A',subcategory:'A',account_name:'First',account_id:'ALPHA'}],history:[]};
const context={document:{getElementById:id=>els[id],createElement:tag=>new Element(tag),querySelectorAll:selector=>selector==='.rule-row'?[...rows,...suggestions]:selector==='#suggestionsTable [data-sort]'?sh:selector==='#rulesTable [data-sort]'?rh:selector==='#ruleMatchesTable [data-sort]'?mh:[]},window:{location:{reload(){throw Error('Sort must not reload or mutate');}},ruleActions:{preview:async payload=>{actions.push(payload);return {cancelled:true};}}},CAT_TREE:{A:['Y'],B:['Z']},Option:class extends Element{constructor(text,value){super('option');this.textContent=text;this.value=value;}},fetch:async url=>{urls.push(url);return {ok:true,json:async()=>detail};},encodeURIComponent,Error};
(async()=>{
 vm.runInNewContext(fs.readFileSync('static/rules.js','utf8'),context);
 // Suggested assignments sort independently; displayed categories never become a saved rule.
 for(const [index,button] of sh.entries()){
   await button.emit('click');assert.equal(button.parentElement.getAttribute('aria-sort'),'ascending');
   assert.deepEqual(els.suggestionsBody.children.map(r=>r.dataset.key),[
     ['A SUGGESTION','UNCATEGORISED','Z SUGGESTION'],
     ['Z SUGGESTION','UNCATEGORISED','A SUGGESTION'],
     ['UNCATEGORISED','A SUGGESTION','Z SUGGESTION']][index]);
   await button.emit('click');assert.equal(button.parentElement.getAttribute('aria-sort'),'descending');
   assert(button.textContent.endsWith('↓'));
 }
 assert.equal(actions.length,0);assert.deepEqual(els.rulesBody.children,rows);
 await suggestions[0].controls['.rule-edit'].emit('click');
 assert.equal(els.ruleEditTitle.textContent,'Add rule');assert.equal(els.ruleEditCategory.value,'');
 assert(els.ruleEditMode.disabled);assert.equal(actions.length,0);
 await els.ruleEditCancel.emit('click');assert.equal(actions.length,0);
 // All required sorts execute in both directions with stable original-order ties.
 await rows[0].controls['.rule-edit'].emit('click');
 assert(els.ruleEdit.open,'Edit rule opens a dialog without mutation');
 els.ruleEditSubcategory.value='pending';
 for(const [index,button] of rh.entries()){
   await button.emit('click');assert.equal(button.parentElement.getAttribute('aria-sort'),'ascending');assert(button.textContent.endsWith('↑'));
   assert.deepEqual(els.rulesBody.children.map(r=>r.dataset.key),index===3?['Z SHOP','A <literal>','A <literal>']:['A <literal>','A <literal>','Z SHOP']);
   if(index!==3)assert.strictEqual(els.rulesBody.children[0],rows[1]);
   await button.emit('click');assert.equal(button.parentElement.getAttribute('aria-sort'),'descending');assert(button.textContent.endsWith('↓'));
 }
 assert.equal(els.ruleEditSubcategory.value,'pending');
 await els.ruleEditCancel.emit('click');assert(!els.ruleEdit.open);
 assert(els.rulesStatus.textContent.includes('not saved'));assert.equal(els.rulesAccount.value,'BETA');assert.equal(actions.length,0);
 // Editing the selector without submitting must not relabel the current applied view.
 els.rulesAccount.value='EMPTY';
 await rows[1].controls['.rule-inspect'].emit('click');assert(urls[0].includes('account_id=BETA'));assert(urls[0].includes('key=A%20%3Cliteral%3E'));
 assert.equal(els.ruleInspectMatches.children.length,3);assert(els.ruleInspectMatches.textContent.includes('9223372036854775807'));assert(els.ruleInspectMatches.textContent.includes('<script>literal</script>'));assert(els.ruleInspectMatches.textContent.includes('Second & account'));
 for(const button of mh){
   await button.emit('click');assert.equal(button.parentElement.getAttribute('aria-sort'),'ascending');
   assert(els.ruleInspectMatches.children[0].textContent.includes('9007199254740993'));
   assert(els.ruleInspectMatches.children[1].textContent.includes('9007199254740995'));
   await button.emit('click');assert.equal(button.parentElement.getAttribute('aria-sort'),'descending');assert(els.ruleInspectMatches.children[0].textContent.includes('9223372036854775807'));
 }
 assert.equal(actions.length,0);assert.equal(urls.length,1);
 // Native buttons provide Enter/Space activation; verify actual template semantics.
 const template=fs.readFileSync('templates/rules.html','utf8');
 for(const field of new Set([...ruleHeaders,...matchHeaders]))assert(new RegExp('<button[^>]*type="button"[^>]*data-sort="'+field+'"').test(template),'native keyboard button '+field);
 assert(template.includes('aria-sort="none"'));
 await rows[1].controls['.rule-edit'].emit('click');
 els.ruleEditCategory.value='B';await els.ruleEditCategory.emit('change');els.ruleEditSubcategory.value='Z';
 await els.ruleEditSave.emit('click');
 assert.equal(actions.length,1);assert.deepEqual(JSON.parse(JSON.stringify(actions[0])),{key:'A <literal>',category:'B',subcategory:'Z',enabled:true});
 assert(els.ruleEdit.open,'Confirmation cancel returns to unsaved edit');assert.equal(els.ruleEditSubcategory.value,'Z');
 assert(els.ruleEditStatus.textContent.includes('not saved'));
 await els.ruleEditCancel.emit('click');
 // State-only disable uses saved obsolete pair, never unsaved category fields.
 rows[1].dataset.category='Removed';rows[1].dataset.subcategory='Old';
 await rows[1].controls['.rule-edit'].emit('click');els.ruleEditMode.value='state';await els.ruleEditMode.emit('change');els.ruleEditState.value='false';
 await els.ruleEditSave.emit('click');assert.equal(actions[1].category,'Removed');assert.equal(actions[1].subcategory,'Old');assert.equal(actions[1].enabled,false);
 await els.ruleEditCancel.emit('click');
 rows[1].dataset.enabled='false';
 await rows[1].controls['.rule-edit'].emit('click');els.ruleEditMode.value='state';await els.ruleEditMode.emit('change');els.ruleEditState.value='true';
 await els.ruleEditSave.emit('click');assert.equal(actions.length,2);assert(els.ruleEditStatus.textContent.includes('category'));
 // Editing a disabled rule remains disabled; enabling is always a separate change.
 els.ruleEditMode.value='category';await els.ruleEditMode.emit('change');els.ruleEditCategory.value='B';await els.ruleEditCategory.emit('change');els.ruleEditSubcategory.value='Z';
 await els.ruleEditSave.emit('click');assert.equal(actions[2].enabled,false);
 await els.ruleEditCancel.emit('click');
 rows[1].dataset.category='';rows[1].dataset.subcategory='';
 await rows[1].controls['.rule-edit'].emit('click');assert.equal(els.ruleEditTitle.textContent,'Add rule');assert(els.ruleEditMode.disabled);
 els.ruleEditCategory.value='B';await els.ruleEditCategory.emit('change');els.ruleEditSubcategory.value='Z';
 await els.ruleEditSave.emit('click');assert.equal(actions[3].enabled,true);
 console.log('rules_display_probe: 11 ascending/descending stable sorts, native buttons/aria/arrows, filter/pending selection, literal text/exact IDs, no mutation passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
