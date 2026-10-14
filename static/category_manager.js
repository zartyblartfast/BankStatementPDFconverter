(() => {
  const open = document.getElementById('manageCategories');
  const dialog = document.getElementById('categoryManager');
  const form = document.getElementById('categoryManagerForm');
  const mode = document.getElementById('categoryManagerNewMode');
  const existing = document.getElementById('categoryManagerExisting');
  const name = document.getElementById('categoryManagerName');
  const subs = document.getElementById('categoryManagerSubs');
  const status = document.getElementById('categoryManagerStatus');
  const save = document.getElementById('categoryManagerSave');
  let saving = false;
  function toggleMode() {
    document.getElementById('categoryManagerExistingLabel').hidden = mode.checked;
    document.getElementById('categoryManagerNewLabel').hidden = !mode.checked;
    existing.required = !mode.checked;
    existing.disabled = mode.checked;
    name.required = mode.checked;
    name.disabled = !mode.checked;
  }
  mode.addEventListener('change',toggleMode);
  toggleMode();
  open.addEventListener('click',() => { status.textContent=''; dialog.showModal(); });
  document.getElementById('categoryManagerClose').addEventListener('click',() => { if (!saving) dialog.close(); });
  dialog.addEventListener('close',() => open.focus());
  dialog.addEventListener('cancel',event => { if (saving) event.preventDefault(); });
  form.addEventListener('submit',async event => {
    event.preventDefault();
    if (saving) return;
    const category = mode.checked ? name.value.trim() : existing.value;
    const subcategories = subs.value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean);
    if (!category || !subcategories.length) {status.textContent='Choose a category and enter at least one subcategory.';return;}
    saving = true; save.disabled = true; status.textContent='Saving…';
    try {
      const response = await fetch('/api/manage-categories',{
        method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':open.dataset.csrf},
        body:JSON.stringify({category,subcategories})
      });
      const data = await response.json();
      if (!response.ok || !data.ok) throw new Error(data.error || 'Save failed.');
      // Refresh options in place; preserve checked rows and existing category choices.
      Object.keys(CAT_TREE).forEach(key => delete CAT_TREE[key]);
      Object.assign(CAT_TREE,data.tree);
      document.querySelectorAll('.cat-select,#batchCategory,#categoryManagerExisting').forEach(select => {
        const value=select.value;
        select.replaceChildren(new Option('— Select —',''));
        Object.keys(CAT_TREE).sort().forEach(cat=>select.add(new Option(cat,cat)));
        select.value=value;
      });
      document.querySelectorAll('.cat-select').forEach(cat => {
        const sub=document.querySelector(`.sub-select[data-exc="${cat.dataset.exc}"]`);
        const value=sub.value;
        sub.replaceChildren(new Option('— Select —',''));
        (CAT_TREE[cat.value] || []).forEach(s=>sub.add(new Option(s,s)));
        sub.value=value;
      });
      const batch=document.getElementById('batchCategory');
      if (batch) {
        const sub=document.getElementById('batchSubcategory'),value=sub.value;
        sub.replaceChildren(new Option('— Select —',''));
        (CAT_TREE[batch.value] || []).forEach(s=>sub.add(new Option(s,s)));
        sub.value=value;
      }
      document.dispatchEvent(new Event('category-definitions-updated'));
      mode.checked=false; toggleMode(); existing.value=data.category;
      name.value=''; subs.value='';
      status.textContent=`Added ${data.added.length} subcategory definition(s) to ${data.category}. Available in the pickers now; no transactions changed.`;
    } catch(error) {
      status.textContent=error.message+' If the connection was lost, reload to check whether the definitions were saved.';
    } finally {saving=false;save.disabled=false;}
  });
})();
