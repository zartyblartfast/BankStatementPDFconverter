/* Rule approval is independent of the already-saved transaction category. */
(() => {
  const dialog=document.getElementById('rulePreview');
  const summary=document.getElementById('rulePreviewSummary');
  const matches=document.getElementById('rulePreviewMatches');
  const approve=document.getElementById('rulePreviewApprove');
  const cancel=document.getElementById('rulePreviewCancel');
  const status=document.getElementById('rulePreviewStatus');
  let active=null,busy=false,preparing=false;
  function integer(value) {
    if(typeof value!=='string'||!/^[1-9][0-9]*$/.test(value)||BigInt(value)>9223372036854775807n)throw new Error('Invalid exact transaction ID. Reload.');
    return value;
  }
  function encode(payload,ids={}) {
    const normal=JSON.stringify(payload);
    const extra=Object.entries(ids).map(([name,value])=>JSON.stringify(name)+':'+(value===null?'null':Array.isArray(value)?'['+value.map(integer).join(',')+']':integer(value)));
    return extra.length?normal.slice(0,-1)+(normal==='{}'?'':',')+extra.join(',')+'}':normal;
  }
  async function post(url,payload,ids={}) {
    const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':dialog.dataset.csrf},body:encode(payload,ids)});
    const data=await response.json();
    if(!response.ok||!data.ok)throw new Error(data.error||'Rule request failed or outcome unknown. Reload Rules before retrying.');
    return data;
  }
  const label=rule=>rule?`${rule.category} / ${rule.subcategory} (${rule.enabled?'enabled':'disabled'})`:'No rule';
  function finish(result){if(active){active.resolve(result);active=null;}}
  cancel.addEventListener('click',()=>{if(!busy)dialog.close();});
  dialog.addEventListener('cancel',event=>{if(busy)event.preventDefault();});
  dialog.addEventListener('close',()=>{if(active)finish({cancelled:true,noop:active.preview.noop});});
  approve.addEventListener('click',async()=>{
    if(!active||busy||active.preview.noop)return;
    busy=true;approve.disabled=true;cancel.disabled=true;status.textContent='Saving future rule…';
    try {
      const data=await post('/api/rules/confirm',{preview_token:active.preview.preview_token,approved:true});
      finish({saved:true,changed:data.changed});dialog.close();
    } catch(error) {
      status.textContent=error.message;
      // Never retry an uncertain/failed confirmation from this preview.
      finish({unknown:true,error:error.message});
    } finally {busy=false;cancel.disabled=false;}
  });
  async function preview(payload,ids={}) {
    if(active||dialog.open||preparing)throw new Error('Close the existing rule preview first.');
    preparing=true;
    let data;
    try {data=await post('/api/rules/preview',payload,ids);} finally {preparing=false;}
    const effect=payload.undo_id?`Undo this rule change: ${data.proposed?'restore '+label(data.proposed):'remove the rule so it no longer matches future imports'}. Other keys and historical transactions stay unchanged.\n`:'';
    summary.textContent=effect+`Matching key: ${data.key}\nCurrent: ${label(data.old)}\nProposed: ${label(data.proposed)}\nAll-account evidence: ${data.matches.length} historical exact matches. Edits affect future matches across all accounts, regardless of the review filter. No historical categories will change.`;
    matches.replaceChildren();
    data.matches.forEach(row=>{const item=document.createElement('li');item.textContent=`#${row.txn_id} · ${row.date} · ${row.description} · Saved: ${row.category||'Uncategorised'}${row.subcategory?' / '+row.subcategory:''} · Account: ${row.account_name||row.account_id||'Account unavailable'}`;matches.appendChild(item);});
    status.textContent=data.noop?'Already the same destination and enabled state. No rule change is needed.':'';
    approve.disabled=data.noop;cancel.disabled=false;
    const result=new Promise(resolve=>{active={preview:data,resolve};});
    dialog.showModal();return result;
  }
  window.ruleActions={preview,encode};
})();
