"""Known legacy correction adapter, shared by generation and in-memory serving."""
import re

FUTURE_RULE_LINK = '<p class="remember-row">Save changes to this transaction only. <a href="/rules" target="_top">Review future rules separately</a></p>'
SAVE_SCRIPT = r'''async function saveRecat() {
  const btn = document.getElementById('rcSave');
  btn.disabled = true;
  const statusEl = document.getElementById('rcStatus');
  statusEl.textContent = '';
  const sub = document.getElementById('rcSubSel').value;
  const body = {
    txn_id: _rcTxnId,
    category: document.getElementById('rcCatSel').value,
    subcategory: sub === '__new__' ? '' : sub,
    new_subcategory: sub === '__new__' ? document.getElementById('rcNewSub').value.trim() : '',
    scope: 'single'
  };
  try {
    const r = await ledgerFetch('/api/recategorize', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
    });
    const data = await r.json();
    if (!data.ok) {
      statusEl.textContent = data.error || 'Save failed.';
      btn.disabled = false;
      return;
    }
    statusEl.textContent = 'Category saved for this transaction only.' + (data.warning ? ' ' + data.warning : ' Reloading…');
    if (!data.warning) setTimeout(() => { closeRecatModal(); window.location.reload(); }, 800);
  } catch(e) {
    statusEl.textContent = 'Save outcome unknown. Reload before retrying. ' + e.message;
  }
}

'''


def adapt_controls(html):
    html = re.sub(r'<label class="remember-row"><input type="checkbox" id="rcRemember"(?: checked)?> Remember rule for future imports</label>',
                  lambda _: FUTURE_RULE_LINK, html)
    html = re.sub(r'\s*<label class="scope-opt"><input type="radio" name="rcScope" value="merchant"> All <strong id="rcMerchantCount"></strong> matching</label>', '', html)
    return html


def adapt_script(code):
    # Limit replacement to the known correction function and its existing wiring.
    # Caller protects complete transaction JSON before interpreting script content.
    if "const remember = document.getElementById('rcRemember').checked;" in code:
        code = re.sub(r'async function saveRecat\(\) \{.*?(?=// Wire up modal buttons)',
                      lambda _: SAVE_SCRIPT, code, count=1, flags=re.S)
    # The count label was removed along with merchant-wide scope.
    code = code.replace("document.getElementById('rcMerchantCount').textContent = matchCount + ' ' + esc(merchant);", '')
    return code
