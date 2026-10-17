'use strict';
/* Execute real shared approval + real search/exception controllers. */
const fs=require('fs'),vm=require('vm'),assert=require('assert');
class Element {
  constructor(){this.listeners={};this.children=[];this.dataset={csrf:'fixture'};this.textContent='';this.value='';this.checked=false;this.disabled=false;this.open=false;this.style={};this.classList={add:()=>{this.resolved=true;}};}
  addEventListener(type,fn){(this.listeners[type]??=[]).push(fn);}
  async emit(type,event={}){for(const fn of this.listeners[type]||[])await fn({preventDefault(){},...event});}
  replaceChildren(...nodes){this.children=nodes;}
  appendChild(node){this.children.push(node);return node;}
  add(node){this.children.push(node);}
  showModal(){this.open=true;}
  close(){this.open=false;this.emit('close');}
  focus(){}
}
const flush=()=>new Promise(r=>setImmediate(r));
function setup(kind){
  const ids=['rulePreview','rulePreviewSummary','rulePreviewMatches','rulePreviewApprove','rulePreviewCancel','rulePreviewStatus','searchRecategory','searchRecategoryForm','searchRecategoryCategory','searchRecategorySubcategory','searchRecategoryStatus','searchRecategorySave','searchRecategoryTransaction','searchRecategoryClose','searchRecategoryFuture','manageCategories'];
  const els=Object.fromEntries(ids.map(id=>[id,new Element()]));
  const button=new Element(),row=new Element(),cat=new Element(),sub=new Element(),future=new Element(),status=new Element(),box=new Element(),badge=new Element();
  button.dataset={txn:'9223372036854775807',oldSubcategory:'9007199254740993',category:'Fixture',subcategory:'Old',exc:'9223372036854775807'};
  row.querySelector=selector=>({'.cat-select':cat,'.sub-select':sub,'.remember-cb':future,'.correction-status':status,'.row-select':box}[selector]||{textContent:selector});
  row.querySelectorAll=()=>[button,cat,sub,future,box];button.closest=()=>row;
  const calls=[],alerts=[];let queue=[],reloads=0;
  const context={window:{location:{href:'http://127.0.0.1/transactions?q=kept&page=3',reload(){reloads++;}}},document:{getElementById:id=>id.startsWith('exc-')?row:els[id],querySelector:()=>badge,querySelectorAll:selector=>selector==='.recat-search'||selector==='.apply-btn'?[button]:row.resolved?[]:[row],createElement:()=>new Element(),addEventListener(){}},Option:class{constructor(text,value){this.textContent=text;this.value=value;}},CAT_TREE:{Fixture:['Old','Test']},alert:text=>alerts.push(text),updateSelection(){},JSON,BigInt,Error,fetch:async(url,options)=>{calls.push({url,body:options.body});const data=queue.shift();if(!data)throw Error('Unexpected write '+url);return {ok:data.ok!==false,json:async()=>data};}};
  vm.runInNewContext(fs.readFileSync('static/rule_actions.js','utf8'),context);
  vm.runInNewContext(fs.readFileSync(kind==='search'?'static/search_recategorisation.js':'static/exception_corrections.js','utf8'),context);
  return {els,button,row,cat,sub,future,status,calls,alerts,context,setQueue(value){queue=value;},reloads:()=>reloads};
}
const preview={ok:true,preview_token:'held',key:'SYNTHETIC',old:{category:'Fixture',subcategory:'Old',enabled:true},proposed:{category:'Fixture',subcategory:'Test',enabled:true},noop:false,matches:[]};
(async()=>{
  for(const kind of ['search','exception'])for(const mode of ['only','cancel','failure','disabled']){
    const app=setup(kind),{els}=app;
    if(kind==='search'){
      els.searchRecategoryFuture.checked=true;
      await app.button.emit('click');assert.equal(els.searchRecategoryFuture.checked,false);
      els.searchRecategoryCategory.value='Fixture';els.searchRecategorySubcategory.value='Test';els.searchRecategoryFuture.checked=mode!=='only';
    }else{app.cat.value='Fixture';app.sub.value='Test';app.future.checked=mode!=='only';}
    const queue=[{ok:true,warning:'Category saved; reports need refresh.'}];
    if(mode!=='only')queue.push(mode==='disabled'?{ok:false,error:'Key is disabled. Re-enable in Rules.'}:preview);
    if(mode==='failure')queue.push({ok:false,error:'Rule outcome unknown.'});
    app.setQueue(queue);
    const pending=kind==='search'?els.searchRecategoryForm.emit('submit'):app.button.emit('click');
    await flush();
    if(mode==='cancel')await els.rulePreviewCancel.emit('click');
    if(mode==='failure')await els.rulePreviewApprove.emit('click');
    await pending;
    assert(app.calls[0].body.includes('9223372036854775807'));
    assert(!app.calls[0].body.includes('remember_rule'));
    assert(!app.calls[0].body.includes('merchant_core'));
    if(kind==='search'){
      assert(app.calls[0].body.includes('9007199254740993'));
      assert.equal(app.reloads(),1);assert(app.context.window.location.href.endsWith('?q=kept&page=3'));
      if(mode==='disabled')assert(app.alerts.some(x=>x.includes('Category saved. Future rule was not saved')));
      if(mode==='failure')assert(app.alerts.some(x=>x.includes('Category saved. Future rule outcome unknown')));
      assert(app.alerts.includes('Category saved; reports need refresh.'));
    }else{
      assert(app.row.resolved);assert(app.button.disabled);
      assert(app.status.textContent.includes('Category saved'));
      assert(app.status.textContent.includes('reports need refresh'));
      if(mode==='failure')assert(app.status.textContent.includes('unknown'));
      if(mode==='disabled')assert(app.status.textContent.includes('disabled'));
    }
    assert.equal(app.calls.length,mode==='only'?1:mode==='failure'?3:2);
  }
  console.log('correction_rules_probe: 8 real-controller scope/cancel/failure/disabled flows, warnings, exact IDs and filter reload passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
