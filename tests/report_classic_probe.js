const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
class Element {
  constructor(tag='div'){this.tagName=tag;this.style={};this.value='';this.checked=false;this.hidden=false;this.open=false;this.children=[];this.listeners={};this.textContent='';this.dataset={};this.attributes={};this.className='';}
  appendChild(child){child.parent=this;this.children.push(child);return child;}
  replaceChildren(...children){for(const c of this.children)c.parent=null;this.children=[];children.forEach(c=>this.appendChild(c));}
  addEventListener(event,fn){this.listeners[event]=fn;}
  setAttribute(k,v){this.attributes[k]=String(v);}
  after(child){const a=this.parent.children;child.parent=this.parent;a.splice(a.indexOf(this)+1,0,child);}
  remove(){if(this.parent){const a=this.parent.children;a.splice(a.indexOf(this),1);this.parent=null;}}
  getContext(){return {};}
  showModal(){this.open=true;} close(){this.open=false;}
}
const elements={};
for(const match of input.html.matchAll(/<([\w-]+)[^>]*\bid="([^"]+)"[^>]*>/g))elements[match[2]]=new Element(match[1]);
const document={listeners:{},getElementById:id=>elements[id]||null,createElement:tag=>new Element(tag),addEventListener(event,fn){this.listeners[event]=fn;}};
const charts=[];
const context=vm.createContext({document,window:{location:{protocol:input.protocol||'http:',pathname:'/report/raw/fixture',search:''}},Chart:class{constructor(_,config){charts.push(config);}destroy(){}},console});
vm.runInContext(fs.readFileSync(path.join(path.dirname(input.helper),'report-actions.js'),'utf8'),context);
vm.runInContext(fs.readFileSync(input.helper,'utf8'),context);
for(const script of input.scripts)vm.runInContext(script,context);
const state=()=>JSON.parse(vm.runInContext('JSON.stringify(LedgerClassicReport.state())',context));
const kind=vm.runInContext('REPORT_KIND',context);
const walk=e=>[e,...e.children.flatMap(walk)];
const all=()=>Object.values(elements).flatMap(walk);
const fire=e=>{assert.ok(e,'expected control');e.listeners.click({stopPropagation(){}});};
const expandAll=()=>{for(const e of all())if(e.tagName==='details'){e.open=true;if(e.listeners.toggle)e.listeners.toggle();}for(const e of all())if(e.className==='cat-row expandable')fire(e);for(const e of all())if(e.className==='sub-row expandable')fire(e);};
let s=state();
assert.equal(s.raw.debits_pence,input.scenario==='unsafe'?'9007199254742301':'1423');assert.equal(s.raw.credits_pence,'1200');
assert.equal(s.adjusted.debits_pence,input.scenario==='unsafe'?'9007199254741001':'123');
assert.equal(s.adjusted.credits_pence,'200');
assert.equal(s.transfers.in_pence,'1000');assert.equal(s.transfers.out_pence,'1000');
assert.equal(s.excluded_count,1);assert.equal(s.unclassified_count,input.scenario==='unsafe'?3:2);
assert.equal(all().filter(e=>e.className.includes('item-row')).length,0,'no auto-open raw list');
assert.ok(all().filter(e=>e.tagName==='details').every(e=>!e.open));
assert.match(elements.policySummary.textContent,/confirmed active principal/i);
assert.match(elements.policySummary.textContent,/transfers:/);
assert.match(elements.policyWarnings.textContent,/different posting dates/i);
if(kind==='income_vs_expense')assert.equal(charts.filter(c=>c.type==='doughnut').length,2);
else if(kind!=='expenditure_heatmap'){assert.equal(charts[0].type,'bar');assert.equal(charts[1].type,'doughnut');}
if(kind==='income_by_category'){
  assert.deepEqual(charts[1].data.labels,[input.scenario==='unsafe'?'constructor':'Own Accounts'],'income distribution is by source, not parent category');
  assert.ok(elements.tableBody.children[0].children[0].textContent.includes(input.scenario==='unsafe'?'constructor':'Own Accounts'));
}
if(kind==='expenditure_heatmap'){
  assert.equal(elements.heatmapWrap.children[0].className,'hm-table','retain original heatmap layout');
  assert.ok(all().some(e=>e.className==='hm-cell clickable'));
  fire(all().find(e=>e.className==='hm-cell clickable'));
}
if(kind==='expenditure_heatmap'){
  assert.ok(document.listeners.keydown,'heatmap retains Escape dismissal');
  document.listeners.keydown({key:'Escape'});assert.equal(elements.popupOverlay.style.display,'none');
  fire(all().find(e=>e.className==='hm-cell clickable'));
}
expandAll();
assert.ok(all().some(e=>e.textContent.includes('keep </ScRiPt> &')),'notes remain literal data');
assert.ok(all().some(e=>e.textContent.includes('Confirmed paired')));
assert.ok(all().some(e=>e.className.includes('item-row')),'grouped rows expand on demand');
assert.ok(all().some(e=>e.textContent==='Edit note'));
assert.ok(all().some(e=>e.textContent==='Re-categorise'));
if(input.protocol==='file:'){
  assert.match(elements.reportEditNotice.textContent,/Standalone HTML editing is unsupported/);
  fire(all().find(e=>e.textContent==='Edit note'));
  assert.equal(elements.reportEditDialog.open,false,'standalone note editing stays disabled');
  fire(all().find(e=>e.textContent==='Re-categorise'));
  assert.equal(elements.reportEditDialog.open,false,'standalone category editing stays disabled');
}
if(input.scenario==='sorting'){
  const sub=walk(elements.omittedGroups).find(e=>e.className==='sub-row expandable'&&e.children[0].textContent.includes('Own Accounts'));
  const order=()=>sub.parent.children.filter(e=>['1','2'].includes(e.dataset.txnId)).map(e=>e.dataset.txnId);
  assert.deepEqual(order(),['1','2']);
  fire(sub.children[2].children[0].children[1]);assert.deepEqual(order(),['2','1'],'Merchant sort uses original grouped rows');
  fire(sub.children[2].children[0].children[0]);assert.deepEqual(order(),['1','2'],'Date sort is restored');
}
if(input.scenario==='unsafe'){
  if(kind!=='income_by_category'&&kind!=='expenditure_heatmap')assert.ok(all().some(e=>e.dataset.txnId==='9007199254740993'));
  assert.equal(vm.runInContext("ALL_TXNS.find(r=>r.txn_id==='9007199254740993').amount_pence",context),'-1');
  assert.ok(all().some(e=>e.textContent.includes('__proto__')));
  assert.ok(all().some(e=>e.textContent.includes('constructor')));
  if(kind!=='income_by_category')assert.ok(all().some(e=>e.textContent.includes('90,071,992,547,410.01')),'DOM total is exact');
  assert.equal(context.breakout,undefined);
}else{
  elements.showExcluded.checked=true;elements.showExcluded.listeners.change();
  s=state();assert.equal(s.display.debits_pence,'1423');assert.equal(s.display.credits_pence,'1200');
  assert.equal(all().filter(e=>e.className.includes('item-row')).length,0,'rerender starts collapsed');
  // The original statement-period selector remains authoritative within the served scope.
  elements.fromMonth.value='9999-01-01';elements.toMonth.value='9999-01-01';elements.fromMonth.listeners.change();
  assert.equal(state().row_count,0);assert.equal(state().display.debits_pence,'0');
}
console.log('classic charts, closed grouped drilldown, exact policy and literal notes verified');
