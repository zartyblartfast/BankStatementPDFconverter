'use strict';
// One row only. Keep SQLite int64 IDs as validated JSON number tokens, never Number.
const LedgerReportActions = (() => {
  const el=id=>document.getElementById(id);
  let row=null,mode='',tree=new Map(),generation=0,busy=false;
  const key=()=> 'report-edit-notice:'+window.location.pathname+window.location.search;
  function idToken(value){
    if(typeof value!=='string'||! /^[1-9][0-9]{0,18}$/.test(value)||BigInt(value)>9223372036854775807n)throw Error('Invalid transaction or subcategory ID. Reload this report.');
    return value;
  }
  const option=value=>{const e=document.createElement('option');e.value=value;e.textContent=value;return e;};
  function subs(){el('reportSubcategory').replaceChildren(...(tree.get(el('reportCategory').value)||[]).map(option));}
  function cancel(){if(busy)return;generation++;el('reportEditDialog').close();row=null;}
  async function open(record,kind){
    if(window.location.protocol==='file:'){
      el('reportEditNotice').textContent='Standalone HTML editing is unsupported. Open this report through the local dashboard.';return;
    }
    const version=++generation;row=record;mode=kind;
    el('reportEditTitle').textContent=(kind==='note'?'Edit note':'Re-categorise')+' — transaction '+record.txn_id+' only';
    el('reportEditStatus').textContent='';el('reportNoteFields').hidden=kind!=='note';el('reportCategoryFields').hidden=kind!=='category';
    el('reportNote').value=record.user_note||'';el('reportEditSave').disabled=kind==='category';el('reportEditDialog').showModal();
    if(kind==='category'){
      el('reportEditStatus').textContent='Loading current category choices…';
      try{
        const response=await ledgerFetch('/api/categories');if(!response.ok)throw Error('Could not load current categories.');
        const data=await response.json();if(version!==generation)return;
        if(!data.tree||typeof data.tree!=='object'||Array.isArray(data.tree))throw Error('Invalid category choices.');
        tree=new Map(Object.entries(data.tree).filter(([k,v])=>Array.isArray(v)&&v.every(s=>typeof s==='string')));
        el('reportCategory').replaceChildren(...[...tree.keys()].map(option));
        if(tree.has(record.category))el('reportCategory').value=record.category;subs();
        if((tree.get(el('reportCategory').value)||[]).includes(record.subcategory))el('reportSubcategory').value=record.subcategory;
        el('reportEditStatus').textContent='Single transaction only. No merchant expansion or remembered rule.';
        el('reportEditSave').disabled=!tree.size;
      }catch(error){if(version===generation)el('reportEditStatus').textContent=error.message+' Cancel and retry; no change was made.';}
    }
  }
  async function save(){
    if(busy||!row||el('reportEditSave').disabled)return;
    let body,path;
    try{
      const txn=idToken(row.txn_id);
      if(mode==='category'){
        const category=el('reportCategory').value,subcategory=el('reportSubcategory').value;
        if(!(tree.get(category)||[]).includes(subcategory))throw Error('Choose an existing category / subcategory.');
        const old=row.subcategory_id==null?'null':idToken(row.subcategory_id);
        body='{"txn_id":'+txn+',"old_subcategory_id":'+old+',"category":'+JSON.stringify(category)+',"subcategory":'+JSON.stringify(subcategory)+'}';path='/api/search-recategorize';
      }else{
        body='{"txn_id":'+txn+',"note":'+JSON.stringify(el('reportNote').value)+',"expected_note":'+JSON.stringify(row.user_note??null)+'}';path='/api/update-note';
      }
    }catch(error){el('reportEditStatus').textContent=error.message;return;}
    busy=true;el('reportEditSave').disabled=true;el('reportEditCancel').disabled=true;
    el('reportEditStatus').textContent='Saving this transaction…';
    try{
      const response=await ledgerFetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body});
      const data=await response.json();
      if(!response.ok||!data.ok){el('reportEditStatus').textContent=data.error||'Save rejected; reload and check this transaction.';return;}
      const notice=data.warning||'Saved. The current served policy view has been reloaded.';
      el('reportEditNotice').textContent=notice;el('reportEditStatus').textContent=notice;
      // A storage failure must not misrepresent the committed save as unsaved.
      try{sessionStorage.setItem(key(),notice);}catch(_){return;}
      window.location.reload();
    }catch(_){el('reportEditStatus').textContent='Save outcome unknown. Reload and check this transaction before retrying.';}
    finally{busy=false;el('reportEditSave').disabled=false;el('reportEditCancel').disabled=false;}
  }
  function controls(record){
    const cell=document.createElement('td');
    for(const [label,kind] of [['Re-categorise','category'],['Edit note','note']]){
      const button=document.createElement('button');button.type='button';button.textContent=label;button.addEventListener('click',()=>open(record,kind));cell.appendChild(button);
    }
    return cell;
  }
  function mount(){
    try{el('reportEditNotice').textContent=sessionStorage.getItem(key())||'';sessionStorage.removeItem(key());}catch(_){}
    if(window.location.protocol==='file:')el('reportEditNotice').textContent='Standalone HTML editing is unsupported. Open this report through the local dashboard.';
    el('reportEditCancel').addEventListener('click',cancel);el('reportEditSave').addEventListener('click',save);
    el('reportCategory').addEventListener('change',subs);
    el('reportEditDialog').addEventListener('cancel',event=>{event.preventDefault();cancel();});
  }
  return {controls,mount};
})();
