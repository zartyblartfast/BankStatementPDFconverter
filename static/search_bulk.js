(() => {
  const toolbar=document.getElementById('searchBulkToolbar');
  if(!toolbar)return;
  const boxes=[...document.querySelectorAll('.search-row-select')];
  const category=document.getElementById('searchBulkCategory');
  const subcategory=document.getElementById('searchBulkSubcategory');
  const apply=document.getElementById('searchBulkApply');
  const message=document.getElementById('searchBulkStatus');
  let all=false,busy=false;
  const selected=()=>boxes.filter(b=>b.checked);
  function update(){
    const n=all?Number(toolbar.dataset.count):selected().length;
    document.getElementById('searchSelectionCount').textContent=all?`All ${n} matching results selected across all pages`:`${n} selected on this page`;
    apply.textContent=`Apply to ${n} selected`;
    apply.disabled=busy || !n || !subcategory.value;
    document.dispatchEvent(new CustomEvent('search-selection-updated'));
  }
  function subs(value=''){
    subcategory.replaceChildren(new Option('— Select —',''));
    (CAT_TREE[category.value] || []).forEach(s=>subcategory.add(new Option(s,s)));
    subcategory.value=value;subcategory.disabled=!category.value || busy;update();
  }
  category.addEventListener('change',()=>subs());
  subcategory.addEventListener('change',update);
  boxes.forEach(b=>b.addEventListener('change',()=>{all=false;update();}));
  document.getElementById('searchSelectPage').addEventListener('click',()=>{all=false;boxes.forEach(b=>b.checked=true);update();});
  document.getElementById('searchSelectAll').addEventListener('click',()=>{all=true;boxes.forEach(b=>b.checked=true);update();});
  document.getElementById('searchClearSelection').addEventListener('click',()=>{all=false;boxes.forEach(b=>b.checked=false);update();});
  document.addEventListener('category-definitions-updated',()=>{
    const cat=category.value,sub=subcategory.value;
    category.replaceChildren(new Option('— Select —',''));
    Object.keys(CAT_TREE).sort().forEach(c=>category.add(new Option(c,c)));
    category.value=cat;subs(sub);
  });
  function setBusy(value){
    busy=value;
    toolbar.querySelectorAll('button,input,select').forEach(el=>el.disabled=value);
    boxes.forEach(b=>b.disabled=value);
    document.querySelectorAll('.recat-search').forEach(b=>b.disabled=value);
    if(!value){subcategory.disabled=!category.value;}
    update();
  }
  // Shared page-only selection. IDs remain strings; all-results is a distinct scope.
  window.searchBulkSelection = {read:()=>({all,busy,rows:selected()}), setBusy};
  async function post(url,payload){
    // Emit validated int64 IDs as JSON integer tokens, never rounded Numbers.
    let body=JSON.stringify(payload);
    if(payload.txn_ids){
      const ids=payload.txn_ids;
      if(ids.some(id=>!/^[1-9][0-9]{0,18}$/.test(id) || BigInt(id)>9223372036854775807n))throw new Error('Invalid transaction reference. Reload the results.');
      const {txn_ids,...rest}=payload;
      body=JSON.stringify(rest).slice(0,-1)+',"txn_ids":['+ids.join(',')+']}';
    }
    const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':document.getElementById('manageCategories').dataset.csrf},body});
    const data=await response.json();
    if(!response.ok || !data.ok)throw new Error(data.error || 'Request failed.');
    return data;
  }
  apply.addEventListener('click',async()=>{
    if(busy || !subcategory.value)return;
    const payload={search_token:toolbar.dataset.searchToken,scope:all?'all':'selected',category:category.value,subcategory:subcategory.value};
    if(!all)payload.txn_ids=selected().map(b=>b.value);
    setBusy(true);message.textContent='Checking the selection…';
    try{
      const preview=await post('/api/search-bulk-preview',payload);
      const scope=preview.scope==='all'?'ALL matching results across EVERY page':'only the explicitly checked rows';
      if(!confirm(`Apply ${preview.category} / ${preview.subcategory} to ${preview.count} transactions: ${scope}?\n\nTransactions already assigned this category will stay unchanged. No other rows or future rules will change.`)){
        message.textContent='Cancelled — no transactions changed.';setBusy(false);return;
      }
      message.textContent='Saving the approved selection…';
      const result=await post('/api/search-bulk-apply',{preview_token:preview.preview_token,approved:true});
      if(result.warning)alert(result.warning);
      window.location.reload();
    }catch(error){
      message.textContent=error.message+' Reload to review current results before retrying if the connection was lost.';
      setBusy(false);
    }
  });
  update();
})();
