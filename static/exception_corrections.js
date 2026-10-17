/* Per-row transaction save, then an independent optional future-rule approval. */
(() => {
  document.querySelectorAll('.apply-btn').forEach(button=>button.addEventListener('click',async()=>{
    if(button.disabled)return;
    const id=button.dataset.exc,row=document.getElementById('exc-'+id);
    const category=row.querySelector('.cat-select').value;
    const subcategory=row.querySelector('.sub-select').value;
    const future=row.querySelector('.remember-cb').checked;
    const status=row.querySelector('.correction-status');
    if(!category||!subcategory)return;
    button.disabled=true;button.textContent='Saving…';
    let saved=false;
    try {
      const response=await fetch('/api/resolve-exception',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':document.getElementById('manageCategories').dataset.csrf},body:window.ruleActions.encode({category,subcategory},{exception_id:id})});
      const data=await response.json();
      if(!response.ok||!data.ok)throw new Error(data.error||'Category save failed or outcome unknown.');
      saved=true;row.classList.add('resolved');
      row.querySelectorAll('input,select,button').forEach(control=>control.disabled=true);
      row.querySelector('.row-select').checked=false;
      updateSelection();button.textContent='✓ Category saved';
      status.textContent=data.warning||'Category saved for this transaction only.';
      const badge=document.querySelector('nav .badge');
      if(badge){const count=document.querySelectorAll('.exc-row:not(.resolved)').length;badge.textContent=count||'✓';if(!count)badge.className='badge badge-ok';}
      if(future) {
        try {
          const rule=await window.ruleActions.preview({category,subcategory},{exception_id:id});
          const message=rule.saved?'Future rule saved.':rule.noop?'Future rule already agrees; no change needed.':rule.unknown?'Future rule outcome unknown: '+rule.error:'Future rule not changed (cancelled).';
          status.textContent='Category saved. '+message+(data.warning?' '+data.warning:'');
        } catch(error) {status.textContent='Category saved. Future rule not saved: '+error.message+(data.warning?' '+data.warning:'');}
      }
    } catch(error) {
      status.textContent=(saved?'Category saved. ':'')+error.message+' Reload to check the saved state before retrying.';
      button.textContent=saved?'✓ Category saved':'Check saved state';
    }
  }));
})();
