(() => {
  const dialog=document.getElementById('searchRecategory');
  const form=document.getElementById('searchRecategoryForm');
  const category=document.getElementById('searchRecategoryCategory');
  const subcategory=document.getElementById('searchRecategorySubcategory');
  const status=document.getElementById('searchRecategoryStatus');
  const save=document.getElementById('searchRecategorySave');
  let target=null, saving=false;
  function populateSubs(value='') {
    subcategory.replaceChildren(new Option('— Select —',''));
    (CAT_TREE[category.value] || []).forEach(sub=>subcategory.add(new Option(sub,sub)));
    subcategory.value=value;
    subcategory.disabled=!category.value;
  }
  function populateCategories(value='') {
    category.replaceChildren(new Option('— Select —',''));
    Object.keys(CAT_TREE).sort().forEach(cat=>category.add(new Option(cat,cat)));
    category.value=value;
  }
  document.querySelectorAll('.recat-search').forEach(button=>button.addEventListener('click',()=>{
    target=button; status.textContent='';
    const row=button.closest('tr');
    document.getElementById('searchRecategoryTransaction').textContent=
      `${row.querySelector('.search-date').textContent} · ${row.querySelector('.search-description').textContent} · ${row.querySelector('.search-amount').textContent} · Current: ${row.querySelector('.search-category').textContent}`;
    document.getElementById('searchRecategoryFuture').checked=false;
    populateCategories(button.dataset.category);populateSubs(button.dataset.subcategory);
    dialog.showModal();
  }));
  category.addEventListener('change',()=>populateSubs());
  document.getElementById('searchRecategoryClose').addEventListener('click',()=>{if(!saving)dialog.close();});
  dialog.addEventListener('cancel',event=>{if(saving)event.preventDefault();});
  dialog.addEventListener('close',()=>{if(target)target.focus();});
  document.addEventListener('category-definitions-updated',()=>{
    const cat=category.value,sub=subcategory.value;populateCategories(cat);populateSubs(sub);
  });
  form.addEventListener('submit',async event=>{
    event.preventDefault();
    if(saving || !target || !category.value || !subcategory.value)return;
    const destination={category:category.value,subcategory:subcategory.value};
    const future=document.getElementById('searchRecategoryFuture').checked;
    saving=true;save.disabled=true;status.textContent='Saving this transaction…';
    try {
      const response=await fetch('/api/search-recategorize',{
        method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':document.getElementById('manageCategories').dataset.csrf},
        body:window.ruleActions.encode(destination,{txn_id:target.dataset.txn,old_subcategory_id:target.dataset.oldSubcategory||null})
      });
      const data=await response.json();
      if(!response.ok || !data.ok)throw new Error(data.error || 'Save failed.');
      if(data.warning)alert(data.warning);
      if(future) {
        status.textContent='Category saved. Review the separate future rule proposal.';
        try {
          const rule=await window.ruleActions.preview(destination,{txn_id:target.dataset.txn});
          if(rule.unknown)alert('Category saved. Future rule outcome unknown: '+rule.error);
          else if(rule.cancelled&&!rule.noop)alert('Category saved. Future rule was not changed.');
        } catch(error) {alert('Category saved. Future rule was not saved: '+error.message);}
      }
      // Reload this exact URL: all submitted search filters and page remain intact.
      window.location.reload();
    } catch(error) {
      status.textContent=error.message+' If the connection was lost, reload to check the saved category before retrying.';
      saving=false;save.disabled=false;
    }
  });
})();
