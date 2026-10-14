/* Local transfer review. IDs/amounts never round through JavaScript Number. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const dialog = $('transfer-preview');
  if (!dialog) return;
  let token = null, unmatchedId = null, busy = false;
  const message = (text, error = false) => {
    $('transfer-message').textContent = text;
    $('transfer-message').className = error ? 'alert alert-error' : 'alert alert-info';
  };
  function id(value) {
    if (!/^[1-9][0-9]{0,18}$/.test(value) || BigInt(value) > 9223372036854775807n)
      throw new Error('Enter an exact positive SQLite transaction ID.');
    return value;
  }
  function money(text) {
    const cents = BigInt(text), absolute = cents < 0n ? -cents : cents;
    return (cents < 0n ? '-' : '+') + '£' + (absolute / 100n) + '.' + String(absolute % 100n).padStart(2, '0');
  }
  async function send(action, body) {
    const response = await fetch('/api/transfers/' + action, {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': $('transfer-csrf').value},
      body: typeof body === 'string' ? body : JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || 'Request failed. Review again.');
    return data;
  }
  async function guard(fn) {
    if (busy) return;
    busy = true;
    document.querySelectorAll('button').forEach(b => b.disabled = true);
    try { await fn(); } catch (error) { message(error.message, true); }
    finally { busy = false; document.querySelectorAll('button').forEach(b => b.disabled = false); }
  }
  async function preview(body) {
    // Replacing a held review explicitly cancels it first.
    if (token) { await send('cancel', {preview_token: token}); token = null; }
    const data = await send('preview', body);
    token = data.preview_token;
    $('preview-title').textContent = 'Review ' + (data.action === 'revoke' ? 'undo' : data.action) + ' decision';
    $('preview-members').replaceChildren();
    for (const row of data.members) {
      const p = document.createElement('p');
      p.textContent = '#' + row.txn_id_text + ' · ' + row.txn_date + ' · ' + row.account_name +
        ' · ' + money(row.cents_text) + ' · ' + row.direction + ' · Counter-account: ' +
        row.counterpart_name + ' · ' + row.description;
      $('preview-members').append(p);
    }
    dialog.showModal();
  }
  function pair(first, second) {
    return preview('{"action":"pair","txn_ids":[' + id(first) + ',' + id(second) + ']}');
  }
  $('exact-pair').addEventListener('submit', event => {
    event.preventDefault();
    guard(() => pair(event.target.elements.first.value, event.target.elements.second.value));
  });
  if ($('preview-pair')) $('preview-pair').addEventListener('click', () => guard(async () => {
    const selected = document.querySelector('input[name="counterpart"]:checked');
    if (!selected) throw new Error('Choose an exact counterpart; no candidate is preselected.');
    await pair($('candidates').dataset.anchor, selected.value);
  }));
  document.querySelectorAll('[data-revoke]').forEach(button => button.addEventListener('click', () =>
    guard(() => preview('{"action":"revoke","group_id":' + id(button.dataset.revoke) + '}'))));
  document.querySelectorAll('[data-unmatched]').forEach(button => button.addEventListener('click', () => {
    if (busy) return;
    unmatchedId = button.dataset.unmatched;
    $('owned-counterpart').value = '';
    Array.from($('owned-counterpart').options).forEach(option => option.disabled = option.value === button.dataset.account);
    $('unmatched-dialog').showModal();
  }));
  $('close-unmatched').addEventListener('click', () => $('unmatched-dialog').close());
  $('unmatched-form').addEventListener('submit', event => {
    event.preventDefault();
    guard(async () => {
      const counterpart = $('owned-counterpart').value;
      if (!counterpart) throw new Error('Choose the known owned counter-account explicitly.');
      await preview('{"action":"unmatched","txn_id":' + id(unmatchedId) +
        ',"counterpart_account_id":' + JSON.stringify(counterpart) + '}');
      $('unmatched-dialog').close();
    });
  });
  async function cancel() {
    if (token) await send('cancel', {preview_token: token});
    token = null; dialog.close(); message('Cancelled. No transfer decision was saved.');
  }
  $('cancel-transfer').addEventListener('click', () => guard(cancel));
  dialog.addEventListener('cancel', event => { event.preventDefault(); guard(cancel); });
  $('confirm-transfer').addEventListener('click', () => guard(async () => {
    if (!token) throw new Error('Review expired. Prepare a new preview.');
    const current = token;
    // A failed network response is unknown, never represented as an unsaved decision.
    try {
      const data = await send('confirm', {preview_token: current, approved: true});
      token = null; dialog.close();
      message(data.warning || 'Transfer decision saved. Open a policy-stamped dashboard report for the current adjusted view; legacy reports remain warned.');
      // Preserve saved-warning evidence on screen. Reload explicitly after inspecting it.
      const reload = document.createElement('button');
      reload.className = 'btn'; reload.textContent = 'Reload updated review';
      reload.addEventListener('click', () => location.reload());
      $('transfer-message').append(' ', reload);
    } catch (error) {
      token = null; dialog.close();
      message(error.message + ' Reload to check current decisions before trying again.', true);
    }
  }));
})();
