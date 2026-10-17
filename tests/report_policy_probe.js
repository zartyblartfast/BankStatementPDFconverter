const fs=require('fs'),vm=require('vm'),assert=require('assert');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
class Element {
  constructor(){this.style={};this.value='';this.checked=false;this.children=[];this.listeners={};this.textContent='';this.innerHTML='';}
  appendChild(child){this.children.push(child);return child;}
  replaceChildren(...children){this.children=children;}
  addEventListener(event,fn){this.listeners[event]=fn;}
  getContext(){return {};}
}
const elements={};
const document={getElementById:id=>elements[id]||(elements[id]=new Element()),createElement:()=>new Element()};
const charts=[];
const context=vm.createContext({document, window:{location:{protocol:'http:',pathname:'/report/raw/fixture',search:''}}, Chart:class{constructor(_,config){charts.push(config);} destroy(){}},console});
vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(input.helper),'report-actions.js'),'utf8'),context);
vm.runInContext(fs.readFileSync(input.helper,'utf8'),context);
for(const script of input.scripts) vm.runInContext(script,context);
const state=()=>JSON.parse(vm.runInContext('JSON.stringify(LedgerReport.state())',context));
if(input.scenario==='unsafe') {
  const s=state();
  assert.equal(s.adjusted.debits_pence,'9007199254741001');
  assert.equal(s.adjusted.credits_pence,'200');
  assert.equal(s.adjusted.net_pence,'-9007199254740801');
  assert.equal(elements.drilldown.children.length,6);
  assert.ok(elements.drilldown.children.some(r=>r.children[0].textContent.startsWith('9007199254740993 /')));
  assert.ok(elements.drilldown.children.some(r=>r.children.some(c=>c.textContent.includes('keep </ScRiPt> &'))));
  const groupDebits=s.groups.reduce((n,g)=>n+BigInt(g.debits_pence),0n);
  if(vm.runInContext('REPORT_KIND',context)!=='income_by_category') assert.equal(groupDebits.toString(),'9007199254741001');
  assert.equal(context.breakout,undefined);
  console.log('unsafe integer IDs/pence, prototype keys and exact DOM verified');
  process.exit(0);
}
let s=state();
assert.equal(s.adjusted.debits_pence,'123');assert.equal(s.adjusted.credits_pence,'200');
assert.equal(s.raw.debits_pence,'1423');assert.equal(s.raw.credits_pence,'1200');
assert.ok(s.transfers,'separate transfer movement must be exposed');
assert.equal(s.transfers.in_pence,'1000');assert.equal(s.transfers.out_pence,'1000');
assert.equal(s.excluded_count,1);assert.equal(s.unclassified_count,2);
assert.match(elements.transferTotals.textContent,/£10.00/);
assert.match(elements.policyEvidence.textContent,/transfers:/);
assert.equal(elements.drilldown.children.length,5,'all original legs, excluded and uncategorised visible');
assert.match(elements.legend.textContent,/confirmed active principal/i);
assert.match(elements.legend.textContent,/Cash credits/);
assert.ok(elements.drilldown.children.some(r=>r.children.some(c=>c.textContent.includes('paired'))));
elements.viewMode.value='raw';elements.viewMode.listeners.change();
s=state();assert.equal(s.display.debits_pence,'1423');assert.equal(s.display.credits_pence,'1200');
elements.viewMode.value='adjusted';elements.accountFilter.value='HSBC_UK';elements.startDate.value='2026-01-01';elements.endDate.value='2026-01-31';elements.accountFilter.listeners.change();
s=state();assert.equal(s.raw.credits_pence,'200');assert.equal(s.adjusted.debits_pence,'123');
assert.equal(elements.drilldown.children.length,4);
assert.ok(s.warnings.includes('confirmed_counterpart_outside_scope'));
assert.ok(s.warnings.includes('confirmed_transfer_different_posting_dates'));
assert.match(elements.policyWarnings.textContent,/different posting dates/i);
assert.match(elements.policyWarnings.textContent,/outside the selected scope/i);
assert.ok(charts.length>=3,'chart rendering executed');
console.log('generated report DOM, exact conservation and scope warnings verified');
