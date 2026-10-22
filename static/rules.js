(() => {
  const status=document.getElementById('rulesStatus');
  const inspect=document.getElementById('ruleInspect');
  let busy=false;
  const label=rule=>rule?`${rule.category} / ${rule.subcategory} (${rule.enabled?'enabled':'disabled'})`:'No rule';
  const edit=document.getElementById('ruleEdit');
  const category=document.getElementById('ruleEditCategory'),sub=document.getElementById('ruleEditSubcategory');
  const mode=document.getElementById('ruleEditMode'),state=document.getElementById('ruleEditState');
  const editStatus=document.getElementById('ruleEditStatus'),save=document.getElementById('ruleEditSave');
  let editing=null,opener=null,suggestionRevision=null,definitions=CAT_TREE,saved=false;
  const validPair=(cat,child)=>Object.hasOwn(definitions,cat)&&definitions[cat].includes(child);
  function populate(value='') {
    sub.replaceChildren(new Option('— Select —',''));
    (Object.hasOwn(definitions,category.value)?definitions[category.value]:[]).forEach(name=>sub.add(new Option(name,name)));
    sub.value=value;sub.disabled=!category.value;
  }
  function showMode() {
    const stateOnly=mode.value==='state';
    document.getElementById('ruleEditCategories').hidden=stateOnly;
    document.getElementById('ruleEditStateField').hidden=!stateOnly;
    document.getElementById('ruleEditHint').textContent=stateOnly?
      'Only the enabled status changes. The saved category and subcategory stay exactly the same. To enable an obsolete category, first save a valid category while disabled.':
      (editing&&editing.dataset.category?(editing.dataset.enabled==='true'?'The rule stays enabled. Disable it as a separate change.':'The rule stays disabled. Enable it as a separate change after saving a valid category.'):'New rules start enabled.');
    editStatus.textContent='';
  }
  category.addEventListener('change',()=>populate());mode.addEventListener('change',showMode);
  document.getElementById('ruleEditCancel').addEventListener('click',()=>{if(!busy)edit.close();});
  edit.addEventListener('cancel',event=>{if(busy)event.preventDefault();});
  edit.addEventListener('close',()=>{if(!saved)status.textContent='Changes not saved.';editing=null;if(opener&&opener.isConnected!==false)opener.focus();else status.focus();});
  save.addEventListener('click',()=>{
    if(!editing||busy)return;
    const row=editing,stateOnly=mode.value==='state';
    const cat=stateOnly?row.dataset.category:category.value,child=stateOnly?row.dataset.subcategory:sub.value;
    const enabled=stateOnly?state.value==='true':(row.dataset.category?row.dataset.enabled==='true':true);
    if((!stateOnly||enabled)&&!validPair(cat,child)){
      editStatus.textContent=stateOnly?'Choose and save a valid category / subcategory before enabling this rule.':'Choose an existing category / subcategory.';return;
    }
    const payload={key:row.dataset.key,category:cat,subcategory:child,enabled};
    if(suggestionRevision)payload.suggestion_revision=suggestionRevision;
    return action(payload,true);
  });
  async function action(payload,fromEdit=false) {
    if(busy)return;
    busy=true;save.disabled=true;status.textContent='';editStatus.textContent='';
    try {
      const result=await window.ruleActions.preview(payload);
      if(result.saved){
        saved=true;
        if(fromEdit)edit.close();
        status.textContent=(payload.key?'Rule saved for '+payload.key+'.':'Rule change saved.')+' Only future matching changes; saved transactions are unchanged.';
        if(inspect){
          try{
            const response=await fetch(window.location.href);
            if(!response.ok)throw new Error('Refresh failed');
            const fresh=new DOMParser().parseFromString(await response.text(),'text/html');
            for(const id of ['savedRules','ruleSuggestions'])document.getElementById(id)?.remove();
            for(const id of ['savedRules','ruleSuggestions']){const section=fresh.getElementById(id);if(section)status.before(section);}
            bindRows();
            sorting('#rulesTable [data-sort]',document.getElementById('rulesBody'));
            sorting('#suggestionsTable [data-sort]',document.getElementById('suggestionsBody'));
          }catch(error){status.textContent+=' Refresh this page to see the updated list.';}
        }else{
          const row=Array.from(document.querySelectorAll('.rule-row')).find(item=>item.dataset.key===payload.key);
          row?.remove();
          const remaining=document.querySelectorAll('.rule-row').length;
          document.getElementById('rulesReviewTitle').textContent='Optional rule review ('+remaining+')';
          if(!remaining){document.getElementById('exceptionRulesTable').hidden=true;document.getElementById('rulesAllDone').hidden=false;}
        }
        status.focus();
      }
      else if(result.unknown)status.textContent='Rule outcome unknown. Reload before another action. '+result.error;
      else {status.textContent=result.noop?'No rule change needed.':'Changes not saved.';if(fromEdit)editStatus.textContent=status.textContent;}
      if(!result.unknown)busy=false;
    } catch(error) {status.textContent=error.message;if(fromEdit)editStatus.textContent=error.message;busy=false;}
    finally {save.disabled=busy;if(fromEdit&&!busy&&edit.open)save.focus();}
  }
  // Reorder existing nodes: pending dropdown values and attached actions survive.
  function sorting(selector, body) {
    const buttons=Array.from(document.querySelectorAll(selector));
    const labels=new Map(buttons.map(button=>[button,button.textContent]));
    let original=[],field=null,ascending=true;
    function reset() {
      original=body?Array.from(body.children):[];field=null;
      buttons.forEach(button=>{button.parentElement.setAttribute('aria-sort','none');button.textContent=labels.get(button);});
    }
    buttons.forEach(button=>button.addEventListener('click',()=>{
      const next=button.dataset.sort;
      ascending=field===next?!ascending:true;field=next;
      const ordered=original.map((node,index)=>({node,index,value:(node.dataset['sort'+field[0].toUpperCase()+field.slice(1)]||node.dataset[field]||'').toLowerCase()}));
      ordered.sort((a,b)=>((a.value<b.value?-1:a.value>b.value?1:0)*(ascending?1:-1))||a.index-b.index);
      body.replaceChildren(...ordered.map(item=>item.node));
      buttons.forEach(other=>{
        other.parentElement.setAttribute('aria-sort',other===button?(ascending?'ascending':'descending'):'none');
        other.textContent=labels.get(other)+(other===button?(ascending?' ↑':' ↓'):'');
      });
    }));
    reset();return reset;
  }
  sorting('#rulesTable [data-sort]',document.getElementById('rulesBody'));
  sorting('#suggestionsTable [data-sort]',document.getElementById('suggestionsBody'));
  const resetMatches=sorting('#ruleMatchesTable [data-sort]',document.getElementById('ruleInspectMatches'));
  document.getElementById('ruleInspectClose')?.addEventListener('click',()=>inspect.close());
  async function openEdit(row) {
    if(busy)return;
    let suggestion=null;
    suggestionRevision=null;definitions=CAT_TREE;
    if(!row.dataset.category){
      busy=true;status.textContent='Loading all-account suggestion…';
      try {
        const response=await fetch('/api/rules/suggestion?key='+encodeURIComponent(row.dataset.key));
        suggestion=await response.json();
        if(!response.ok||!suggestion.ok)throw new Error(suggestion.error||'Could not load suggestion.');
        definitions=suggestion.categories;suggestionRevision=suggestion.suggestion_revision;
      } catch(error) {status.textContent=error.message;return;}
      finally {busy=false;}
    }
    saved=false;status.textContent='';editing=row;opener=row.querySelector('.rule-edit');
    document.getElementById('ruleEditTitle').textContent=row.dataset.category?'Edit rule':'Add rule';
    document.getElementById('ruleEditKey').textContent=row.dataset.key+' · '+(row.dataset.category?`${row.dataset.category} / ${row.dataset.subcategory} (${row.dataset.enabled==='true'?'enabled':'disabled'})`:'No rule');
    category.replaceChildren(new Option('— Select —',''));
    Object.keys(definitions).sort().forEach(name=>category.add(new Option(name,name)));
    const proposed=suggestion?suggestion.proposal:row.dataset;
    category.value=proposed&&Object.hasOwn(definitions,proposed.category)?proposed.category:'';
    populate(proposed?proposed.subcategory:'');state.value=row.dataset.enabled;
    mode.value='category';mode.disabled=!row.dataset.category;showMode();
    if(suggestion){
      const evidence=suggestion.assignments.map(item=>`${item.category||'Uncategorised'}${item.subcategory?' / '+item.subcategory:''} (${item.count})${item.valid?'':' — missing or obsolete'}`);
      document.getElementById('ruleEditHint').textContent='New rules start enabled. '+suggestion.explanation+' '+(evidence.length?evidence.join('; '):'No historical matches remain.');
    }
    edit.showModal();
  }
  function bindRows(){document.querySelectorAll('.rule-row').forEach(row=>{
    row.querySelector('.rule-edit')?.addEventListener('click',()=>openEdit(row));
    row.querySelector('.rule-inspect')?.addEventListener('click',async()=>{
      if(busy)return;
      busy=true;status.textContent='';
      try {
        const account=document.getElementById('rulesAccount');
        const response=await fetch('/api/rules?key='+encodeURIComponent(row.dataset.key)+(account&&account.dataset.applied?'&account_id='+encodeURIComponent(account.dataset.applied):''));
        const data=await response.json();
        if(!response.ok||!data.ok)throw new Error(data.error||'Could not load rule evidence.');
        document.getElementById('ruleInspectKey').textContent=data.key+' · '+label(data.rule);
        const matches=document.getElementById('ruleInspectMatches'),history=document.getElementById('ruleInspectHistory');
        matches.replaceChildren();history.replaceChildren();
        data.matches.forEach(txn=>{
          const tr=document.createElement('tr');
          tr.dataset.date=txn.date||'';tr.dataset.description=txn.description||'';
          tr.dataset.category=txn.category||'Uncategorised';tr.dataset.subcategory=txn.subcategory||'';
          tr.dataset.account=txn.account_name||txn.account_id||'Account unavailable';
          [txn.txn_id,tr.dataset.date,tr.dataset.description,tr.dataset.category,tr.dataset.subcategory,tr.dataset.account].forEach(value=>{
            const td=document.createElement('td');td.textContent=value;tr.appendChild(td);
          });matches.appendChild(tr);
        });
        if(!data.matches.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=6;td.textContent='No historical matches in this account view.';tr.appendChild(td);matches.appendChild(tr);}
        resetMatches();
        data.history.forEach(event=>{const li=document.createElement('li');li.textContent=`${event.at} · ${label(event.before)} → ${label(event.after)}${event.undo_of?' (targeted undo)':''} `;
          if(event.can_undo){const undo=document.createElement('button');undo.type='button';undo.className='btn btn-sm';undo.textContent='Undo this change';undo.addEventListener('click',()=>{inspect.close();action({undo_id:event.id});});li.appendChild(undo);}history.appendChild(li);});
        if(!data.history.length){const li=document.createElement('li');li.textContent='No maintenance history yet. Legacy CSV rules remain supported.';history.appendChild(li);}
        inspect.showModal();
      } catch(error) {status.textContent=error.message;}
      finally {busy=false;}
    });
  });
  }
  bindRows();
  // A filter is not an action. Consume an explicit intent before any async work.
  if(window.location.search){
    const params=new URLSearchParams(window.location.search);
    if(params.has('intent')){
      const valid=params.getAll('intent').length===1&&params.get('intent')==='add_rule'&&params.getAll('key').length===1;
      const key=params.get('key');
      const url=new URL(window.location.href);url.searchParams.delete('intent');
      window.history.replaceState(window.history.state,'',url);
      const row=valid?Array.from(document.querySelectorAll('.rule-row')).find(item=>item.dataset.key===key&&!item.dataset.category):null;
      if(row)openEdit(row);
      else status.textContent='This exact suggestion is no longer available. Find the merchant and review its current rule.';
    }
  }
})();
