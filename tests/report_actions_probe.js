const fs=require('fs'),vm=require('vm'),assert=require('assert');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
class Element {
  constructor(){this.children=[];this.listeners={};this.value='';this.textContent='';this.disabled=false;this.open=false;}
  appendChild(e){this.children.push(e);return e;}
  replaceChildren(...e){this.children=e;this.value=e[0]?.value||'';}
  addEventListener(k,f){this.listeners[k]=f;}
  showModal(){this.open=true;}
  close(){this.open=false;}
}
const elements=new Map(), requests=[],storage=new Map();let reloads=0,failChoices=false,failWrite=false;
const document={getElementById:id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);},createElement:()=>new Element()};
const context=vm.createContext({document,URL,Headers,window:{location:{protocol:'http:',origin:'http://127.0.0.1:5000',href:'http://127.0.0.1:5000/report/raw/income_vs_expense?account_id=HSBC_UK',pathname:'/report/raw/income_vs_expense',search:'?account_id=HSBC_UK',reload:()=>reloads++}},
  sessionStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},console,
  fetch:async(url,options)=>{
    const path=new URL(url,'http://127.0.0.1:5000').pathname;
    if(path==='/api/csrf-token')return {ok:true,json:async()=>({csrf_token:'fixture-session-token'})};
    requests.push({path,options:options.body?options:undefined});
    assert.equal(options.credentials,'same-origin');
    if(options.body){assert.equal(options.headers.get('X-CSRF-Token'),'fixture-session-token');if(failWrite)throw Error('offline');return {ok:true,json:async()=>({ok:true,warning:'Saved, report file refresh failed.'})};}
    if(failChoices)throw Error('choices unavailable');return {ok:true,json:async()=>({tree:JSON.parse('{"__proto__":["constructor"],"Other":["Normal"]}')})};}});
vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(input.helper),'report-client.js'),'utf8'),context);
vm.runInContext(fs.readFileSync(input.helper,'utf8'),context);
(async()=>{
  vm.runInContext('LedgerReportActions.mount()',context);
  const row={txn_id:'9007199254740993',subcategory_id:'9223372036854775807',category:'__proto__',subcategory:'constructor',user_note:'<img onerror=boom> &'};
  context.row=row;
  const cell=vm.runInContext('LedgerReportActions.controls(row)',context);
  assert.equal(cell.children.length,2);
  await cell.children[0].listeners.click();
  assert.equal(elements.get('reportEditDialog').open,true);
  assert.equal(elements.get('reportCategory').value,'__proto__');
  elements.get('reportEditCancel').listeners.click();
  assert.equal(requests.filter(r=>r.options).length,0,'cancel must not mutate');
  failChoices=true;await cell.children[0].listeners.click();
  assert.equal(elements.get('reportEditDialog').open,true);assert.match(elements.get('reportEditStatus').textContent,/choices unavailable/);
  assert.equal(elements.get('reportEditSave').disabled,true);
  elements.get('reportEditCancel').listeners.click();failChoices=false;
  await cell.children[0].listeners.click();await elements.get('reportEditSave').listeners.click();
  let req=requests.find(r=>r.options);
  assert.equal(req.path,'/api/search-recategorize');
  assert.equal(req.options.body,'{"txn_id":9007199254740993,"old_subcategory_id":9223372036854775807,"category":"__proto__","subcategory":"constructor"}');
  assert.equal(reloads,1);assert.ok([...storage.values()][0].includes('Saved'));
  vm.runInContext('LedgerReportActions.mount()',context);assert.match(elements.get('reportEditNotice').textContent,/Saved/);
  await cell.children[1].listeners.click();assert.equal(elements.get('reportNote').value,row.user_note);
  elements.get('reportNote').value='</script><img> &';await elements.get('reportEditSave').listeners.click();
  req=requests.filter(r=>r.options).at(-1);assert.equal(req.path,'/api/update-note');
  assert.ok(req.options.body.startsWith('{"txn_id":9007199254740993,'));
  const decoded=JSON.parse(req.options.body);assert.equal(decoded.note,'</script><img> &');assert.equal(decoded.expected_note,row.user_note);
  await cell.children[1].listeners.click();failWrite=true;await elements.get('reportEditSave').listeners.click();
  assert.match(elements.get('reportEditStatus').textContent,/unknown.*reload/i);assert.equal(reloads,2);
  context.row={...row,txn_id:'9223372036854775808'};
  const bad=vm.runInContext('LedgerReportActions.controls(row)',context);await bad.children[1].listeners.click();await elements.get('reportEditSave').listeners.click();
  assert.match(elements.get('reportEditStatus').textContent,/Invalid/);
  await assert.rejects(vm.runInContext("ledgerFetch('https://foreign.example/api/update-note',{method:'POST'})",context),/same-origin/);
  context.window.location.protocol='file:';await cell.children[1].listeners.click();assert.match(elements.get('reportEditNotice').textContent,/Standalone.*unsupported/);
  await assert.rejects(vm.runInContext("ledgerFetch('/api/update-note',{method:'POST'})",context),/Standalone/);
  console.log('Actual dialogs: exact int64 payloads, prototype choices, cancel/load failure, notes, saved warnings, unknown network outcome and standalone refusal verified');
})().catch(e=>{console.error(e);process.exitCode=1;});
