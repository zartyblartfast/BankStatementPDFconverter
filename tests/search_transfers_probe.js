/* Execute the real bulk + transfer assets, not a rewritten controller. */
const fs = require('fs'), vm = require('vm'), assert = require('assert');
class Element {
  constructor() { this.listeners = {}; this.textContent = ''; this.children = []; this.dataset = {}; this.value = ''; this.disabled = false; this.checked = false; }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  async emit(name) { for (const fn of this.listeners[name] || []) await fn({preventDefault(){}}); }
  replaceChildren(...nodes) { this.children = nodes; }
  append(...nodes) { this.children.push(...nodes); }
  add(node) { this.children.push(node); }
  showModal() { this.open = true; }
  close() { this.open = false; }
  querySelectorAll() { return controls; }
}
const ids = ['searchBulkToolbar','searchBulkCategory','searchBulkSubcategory','searchBulkApply','searchBulkStatus','searchSelectionCount','searchSelectPage','searchSelectAll','searchClearSelection','manageCategories','searchMarkTransfer','searchTransferDialog','searchTransferTitle','searchTransferMembers','searchTransferCancel','searchTransferConfirm','searchTransferStatus'];
const elements = Object.fromEntries(ids.map(id => [id,new Element()]));
const controls = ['searchBulkCategory','searchBulkSubcategory','searchBulkApply','searchSelectPage','searchSelectAll','searchClearSelection','searchMarkTransfer'].map(id => elements[id]);
const boxes = [new Element(),new Element(),new Element()];
boxes.forEach((b,i) => {b.value = ['9223372036854775807','9007199254740993','3'][i]; b.dataset.transferReady='1'; b.dataset.transferGroup='';});
const undo = new Element(); undo.dataset.group = '9223372036854775806';
elements.manageCategories.dataset.csrf='fixture-csrf';
elements.searchBulkToolbar.dataset.count='2';
elements.searchBulkToolbar.dataset.searchToken='snapshot-not-a-pair';
const documentListeners = {};
global.document = {
  getElementById: id => elements[id] || null,
  addEventListener: (name, fn) => (documentListeners[name] ||= []).push(fn),
  dispatchEvent: event => { for(const fn of documentListeners[event.type] || []) fn(event); },
  querySelectorAll: selector => selector === '.search-row-select' ? boxes : selector === '.search-undo-transfer' ? [undo] : [],
  createElement: () => new Element()
};
global.CustomEvent = class {constructor(type, options={}) {this.type=type;this.detail=options.detail;}};
global.CAT_TREE = {Demo:['One']}; global.confirm=()=>true; global.alert=()=>{};
global.Option = class {constructor(label,value){this.textContent=label;this.value=value;}};
let reloaded=0;
global.window = global; global.location={reload:()=>reloaded++};
const requests=[];
let fail = null, warning = null;
global.fetch = async (url, request) => {
  requests.push({url,request});
  if(fail === 'network') throw new Error('network disconnected');
  if(fail) return {ok:false,json:async()=>({ok:false,error:fail})};
  let body = {ok:true};
  if(url.endsWith('/preview')) body = {ok:true,preview_token:'held-exact',action:request.body.includes('revoke')?'revoke':'pair',members:[
    {txn_id_text:boxes[0].value,txn_date:'2026-01-31',account_name:'Demo current',cents_text:'-1000',description:'<script>literal</script>'},
    {txn_id_text:boxes[1].value,txn_date:'2026-02-02',account_name:'Demo savings',cents_text:'1000',description:'Counterpart'}]};
  if(url.endsWith('/confirm')) body={ok:true,saved:true,warning};
  return {ok:true,json:async()=>body};
};
vm.runInThisContext(fs.readFileSync('static/search_bulk.js','utf8'));
assert(fs.existsSync('static/search_transfers.js'), 'search transfer controller is missing');
vm.runInThisContext(fs.readFileSync('static/search_transfers.js','utf8'));
const mark = elements.searchMarkTransfer, dialog=elements.searchTransferDialog;
const click = async id => {await elements[id].emit('click'); await new Promise(resolve=>setImmediate(resolve));};
const choose = async (...indexes) => {boxes.forEach((b,i)=>b.checked=indexes.includes(i)); await boxes[0].emit('change');};
(async()=>{
  if(process.argv[2] === 'bulk') {
    await choose(0,1);
    elements.searchBulkCategory.value='Demo'; await elements.searchBulkCategory.emit('change');
    elements.searchBulkSubcategory.value='One'; await elements.searchBulkSubcategory.emit('change');
    assert(!elements.searchBulkApply.disabled);
    await click('searchBulkApply');
    assert(requests[0].url.endsWith('/search-bulk-preview'));
    assert(requests[0].request.body.includes('"txn_ids":[9223372036854775807,9007199254740993]'),'bulk must retain exact selected IDs too');
    assert(requests[1].url.endsWith('/search-bulk-apply'));
    assert.equal(reloaded,1);
    assert(!requests.some(r=>r.url.includes('/transfers/')));
    console.log('Existing bulk edit selection retains exact IDs passed'); return;
  }
  if(process.argv[2] === 'undo') {
    await undo.emit('click'); await new Promise(resolve=>setImmediate(resolve));
    assert.equal(requests.length,1,'Undo must prepare a review, not save directly');
    assert.equal(requests[0].request.body,'{"action":"revoke","group_id":9223372036854775806}');
    assert(dialog.open);
    assert.equal(elements.searchTransferTitle.textContent,'Undo transfer?');
    await click('searchTransferConfirm');
    assert(requests.at(-1).url.endsWith('/confirm'));
    assert(elements.searchTransferStatus.textContent.includes('saved'));
    assert.equal(reloaded,1,'successful undo should display current row status');
    console.log('Search undo exact group/confirmation passed'); return;
  }
  if(process.argv[2] === 'errors') {
    await choose(0,1);
    boxes[0].dataset.transferGroup='99'; await boxes[0].emit('change'); assert(mark.disabled);
    await click('searchMarkTransfer'); assert.equal(requests.length,0);
    boxes[0].dataset.transferGroup=''; boxes[0].dataset.transferReady='0'; await boxes[0].emit('change'); assert(mark.disabled);
    boxes[0].dataset.transferReady='1'; await boxes[0].emit('change');
    fail='amount_mismatch'; await click('searchMarkTransfer');
    assert(!dialog.open); assert(elements.searchTransferStatus.textContent.includes('equal and opposite'));
    assert(!boxes[0].disabled);
    fail=null; await click('searchMarkTransfer');
    fail='stale_preview'; await click('searchTransferConfirm');
    assert(!dialog.open); assert(mark.disabled); assert(undo.disabled);
    assert(elements.searchTransferStatus.textContent.includes('changed since review'));
    const count=requests.length;
    await click('searchTransferConfirm'); await click('searchMarkTransfer'); await undo.emit('click');
    assert.equal(requests.length,count,'stale token must never be retried');
    assert(!requests.some(r=>r.url.includes('search-bulk')));
    console.log('Search existing-member/schema/error/stale/replay safety passed'); return;
  }
  if(process.argv[2] === 'network') {
    await choose(0,1); await click('searchMarkTransfer');
    fail='network'; await click('searchTransferConfirm');
    assert(elements.searchTransferStatus.textContent.includes('outcome may be unknown'));
    assert(mark.disabled); assert(!dialog.open);
    const count=requests.length; await click('searchTransferConfirm'); assert.equal(requests.length,count);
    console.log('Search unknown save outcome stays frozen passed'); return;
  }
  if(process.argv[2] === 'escape') {
    await choose(0,1); await click('searchMarkTransfer');
    await dialog.emit('cancel'); await new Promise(resolve=>setImmediate(resolve));
    assert(!dialog.open); assert(requests.at(-1).url.endsWith('/cancel'));
    await click('searchTransferConfirm'); assert(!requests.some(r=>r.url.endsWith('/confirm')));
    console.log('Search Escape cancels held review passed'); return;
  }
  assert(mark.disabled, 'empty selection is not a pair');
  await choose(0); assert(mark.disabled);
  await choose(0,1,2); assert(mark.disabled);
  await click('searchSelectAll'); assert(mark.disabled, 'all-results snapshots are never explicit pairs');
  await choose(0,1); assert(!mark.disabled);
  await click('searchMarkTransfer');
  assert.equal(requests.length,1,'preview alone must not save');
  assert.equal(requests[0].request.body,'{"action":"pair","txn_ids":[9223372036854775807,9007199254740993]}');
  assert.equal(requests[0].request.headers['X-CSRF-Token'],'fixture-csrf');
  assert(dialog.open);
  const text=elements.searchTransferMembers.children.map(p=>p.textContent).join('\n');
  for(const value of ['Demo current','Demo savings','2026-01-31','2026-02-02','-£10.00','+£10.00','<script>literal</script>']) assert(text.includes(value),value);
  assert(boxes.every(b=>b.disabled),'selection frozen while reviewing');
  await click('searchTransferCancel');
  assert(requests.at(-1).url.endsWith('/cancel'));
  assert.equal(JSON.parse(requests.at(-1).request.body).preview_token,'held-exact');
  assert(!dialog.open);
  assert(!boxes[0].disabled);
  await click('searchMarkTransfer');
  warning='Transfer saved, but reports could not be refreshed.';
  await click('searchTransferConfirm');
  assert(requests.at(-1).url.endsWith('/confirm'));
  assert.deepEqual(JSON.parse(requests.at(-1).request.body),{preview_token:'held-exact',approved:true});
  assert(elements.searchTransferStatus.textContent.includes(warning));
  assert.equal(reloaded,0,'saved warning must remain visible');
  assert(mark.disabled,'saved page stays frozen until explicit reload');
  const reload=elements.searchTransferStatus.children.find(n=>n.textContent==='Reload updated results');
  assert(reload); await reload.emit('click'); assert.equal(reloaded,1);
  console.log('Search selection/exact pair/confirmation/cancel/saved warning passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
