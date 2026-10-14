/* Explicit search-row pairing only; never turn all-results scope into a pair. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const selection = window.searchBulkSelection, mark = $('searchMarkTransfer');
  if (!selection || !mark) return;
  const dialog = $('searchTransferDialog'), status = $('searchTransferStatus');
  const confirm = $('searchTransferConfirm'), cancel = $('searchTransferCancel');
  const undo = [...document.querySelectorAll('.search-undo-transfer')];
  let token = null, pending = false, frozen = false;
  function eligible() {
    const state = selection.read();
    return !state.all && !state.busy && state.rows.length === 2 &&
      state.rows.every(row => row.dataset.transferReady === '1' && !row.dataset.transferGroup);
  }
  function update() {
    mark.disabled = pending || frozen || !eligible();
    undo.forEach(button => button.disabled = pending || frozen || selection.read().busy);
  }
  document.addEventListener('search-selection-updated', update);
  function exactId(value) {
    if (!/^[1-9][0-9]{0,18}$/.test(value) || BigInt(value) > 9223372036854775807n)
      throw new Error('Invalid transaction reference. Reload the results.');
    return value;
  }
  function money(value) {
    const pence = BigInt(value), absolute = pence < 0n ? -pence : pence;
    return (pence < 0n ? '-' : '+') + '£' + absolute / 100n + '.' + String(absolute % 100n).padStart(2, '0');
  }
  async function post(action, body) {
    const response = await fetch('/api/transfers/' + action, {
      method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json', 'X-CSRF-Token':$('manageCategories').dataset.csrf},
      body: typeof body === 'string' ? body : JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok || !data.ok) {
      const explanations = new Map([
        ['amount_mismatch', 'Choose equal and opposite amounts.'],
        ['same_account', 'Choose transactions from two different owned GBP accounts.'],
        ['account_unavailable', 'Both accounts must be registered as owned GBP accounts.'],
        ['already_confirmed', 'A selected transaction is already a transfer. Undo it before pairing again.'],
        ['invalid_amount_or_currency', 'Choose non-zero GBP transactions.'],
        ['schema_not_ready', 'Transfer support is unavailable. No setup or migration was applied.'],
        ['stale_preview', 'The ledger or settings changed since review. Prepare a new review after reloading.'],
        ['group_not_active', 'This transfer has already been undone.']
      ]);
      throw new Error(explanations.get(data.error) || data.error || 'Request failed.');
    }
    return data;
  }
  function lock(value) {
    selection.setBusy(value);
    update();
  }
  async function preview(body) {
    if (pending || frozen || selection.read().busy) return;
    pending = true; lock(true); status.textContent = 'Checking the selected transactions…';
    try {
      const data = await post('preview', body);
      token = data.preview_token;
      $('searchTransferTitle').textContent = data.action === 'revoke' ? 'Undo transfer?' : 'Mark these two transactions as a transfer?';
      $('searchTransferMembers').replaceChildren();
      for (const row of data.members) {
        const line = document.createElement('p');
        line.textContent = row.account_name + ' · ' + row.txn_date + ' · ' + money(row.cents_text) + ' · ' + row.description;
        $('searchTransferMembers').append(line);
      }
      status.textContent = '';
      dialog.showModal();
    } catch (error) {
      token = null; lock(false); status.textContent = error.message + ' No transfer was saved. Reload to review current results.';
    } finally { pending = false; update(); }
  }
  mark.addEventListener('click', () => {
    if (!eligible() || pending || frozen) return;
    try {
      const rows = selection.read().rows;
      preview('{"action":"pair","txn_ids":[' + rows.map(row => exactId(row.value)).join(',') + ']}');
    } catch (error) { status.textContent = error.message; }
  });
  undo.forEach(button => button.addEventListener('click', () => {
    try { preview('{"action":"revoke","group_id":' + exactId(button.dataset.group) + '}'); }
    catch (error) { status.textContent = error.message; }
  }));
  async function cancelReview() {
    if (pending || !token) return;
    pending = true; confirm.disabled = true; cancel.disabled = true;
    const current = token; token = null;
    try {
      await post('cancel', {preview_token:current});
      status.textContent = 'Cancelled — no transfer decision saved.';
    } catch (error) {
      status.textContent = error.message + ' No confirmation was sent. Reload before reviewing again.';
    } finally {
      dialog.close(); pending = false; confirm.disabled = false; cancel.disabled = false; lock(false);
    }
  }
  cancel.addEventListener('click', cancelReview);
  dialog.addEventListener('cancel', event => { event.preventDefault(); cancelReview(); });
  confirm.addEventListener('click', async () => {
    if (pending || !token) return;
    pending = true; confirm.disabled = true; cancel.disabled = true;
    const current = token; token = null;
    try {
      const data = await post('confirm', {preview_token:current,approved:true});
      status.textContent = data.warning || 'Transfer decision saved. Reload to see updated results.';
      if (!data.warning) window.location.reload();
    } catch (error) {
      status.textContent = error.message + ' Save outcome may be unknown. Reload to check current decisions before trying again.';
    } finally {
      dialog.close(); pending = false; frozen = true; lock(true);
      const reload = document.createElement('button');
      reload.type = 'button'; reload.className = 'btn btn-sm'; reload.textContent = 'Reload updated results';
      reload.addEventListener('click', () => window.location.reload());
      status.append(' ', reload);
    }
  });
  update();
})();
