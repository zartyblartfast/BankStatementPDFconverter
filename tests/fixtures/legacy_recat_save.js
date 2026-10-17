// Pre-maintenance save function from the supported legacy report generator.
async function saveRecat() {
  const btn = document.getElementById('rcSave');
  btn.disabled = true;
  const statusEl = document.getElementById('rcStatus');
  statusEl.innerHTML = '';

  const cat = document.getElementById('rcCatSel').value;
  const subSel = document.getElementById('rcSubSel').value;
  const newSub = document.getElementById('rcNewSub').value.trim();
  const scope = document.querySelector('input[name="rcScope"]:checked').value;
  const remember = document.getElementById('rcRemember').checked;

  const body = {
    txn_id: _rcTxnId,
    category: cat,
    subcategory: subSel === '__new__' ? '' : subSel,
    new_subcategory: subSel === '__new__' ? newSub : '',
    scope: scope,
    remember_rule: remember
  };

  try {
    const r = await ledgerFetch('/api/recategorize', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    const data = await r.json();
    if (data.ok) {
      statusEl.innerHTML = `<div class="status-msg status-ok">Updated ${data.updated} transaction(s). Reloading…</div>`;
      // Reload the report after a short delay
      setTimeout(() => {
        closeRecatModal();
        window.location.reload();
      }, 800);
    } else {
      statusEl.innerHTML = `<div class="status-msg status-err">${esc(data.error)}</div>`;
      btn.disabled = false;
    }
  } catch(e) {
    statusEl.innerHTML = `<div class="status-msg status-err">Network error: ${esc(e.message)}</div>`;
    btn.disabled = false;
  }
}
