"""Generate an Expenditure Heatmap HTML report (Category × Statement Period).

Usage:
  python -m scripts.report_expenditure_heatmap

Interactive HTML heatmap with from/to statement-period filtering.
Rows = expense categories (sorted by total spend), columns = statement periods.
"""

import json
import webbrowser
from pathlib import Path

from scripts.report_income_vs_expense import _query_all

REPORTS_DIR = Path(__file__).resolve().parent.parent / "finance" / "reports"


def _build_html(txns: list[dict], all_months: list[str]) -> str:
    """Build the Expenditure Heatmap HTML report."""
    txns_json = json.dumps(txns)
    months_json = json.dumps(all_months)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Expenditure Heatmap</title>
<style>
  html, body {{ margin:0; padding:0; overflow-x:hidden; overflow-y:auto; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
         background:#0f172a; color:#e2e8f0; padding:24px; }}
  h1 {{ font-size:1.6rem; margin-bottom:4px; color:#f8fafc; }}
  .subtitle {{ color:#94a3b8; font-size:0.9rem; margin-bottom:16px; }}

  /* ── date picker ── */
  .date-bar {{ display:flex; align-items:center; gap:12px; margin-bottom:20px;
               max-width:1600px; background:#1e293b; border-radius:10px; padding:12px 20px; }}
  .date-bar label {{ color:#94a3b8; font-size:0.85rem; }}
  .date-bar select {{ background:#334155; color:#e2e8f0; border:1px solid #475569;
                      border-radius:6px; padding:5px 10px; font-size:0.85rem; cursor:pointer; }}
  .date-bar select:focus {{ outline:none; border-color:#60a5fa; }}

  /* ── heatmap container ── */
  .heatmap-card {{ background:#1e293b; border-radius:12px; padding:20px; max-width:1600px;
                   overflow-x:auto; margin-bottom:24px; }}
  .heatmap-card h2 {{ font-size:1.1rem; margin-bottom:12px; }}

  /* ── heatmap table ── */
  .hm-table {{ border-collapse:collapse; }}
  .hm-table th {{ padding:6px 10px; font-size:0.75rem; color:#94a3b8; font-weight:500;
                  white-space:nowrap; }}
  .hm-table th.col-hdr {{ writing-mode:vertical-lr; text-orientation:mixed;
                          transform:rotate(180deg); text-align:left; min-width:36px;
                          max-width:36px; height:100px; }}
  .hm-table th.row-hdr {{ text-align:right; padding-right:12px; font-size:0.8rem;
                          color:#e2e8f0; font-weight:400; white-space:nowrap; }}
  .hm-table td {{ padding:0; }}

  .hm-cell {{ min-width:60px; height:32px; display:flex; align-items:center;
              justify-content:center; font-size:0.7rem; font-weight:600;
              border:1px solid #0f172a; cursor:default; position:relative;
              transition:transform 0.1s; }}
  .hm-cell:hover {{ transform:scale(1.08); z-index:2; box-shadow:0 0 8px rgba(255,255,255,0.2); }}
  .hm-cell .tip {{ display:none; position:absolute; bottom:110%; left:50%;
                   transform:translateX(-50%); background:#0f172a; color:#e2e8f0;
                   padding:4px 8px; border-radius:4px; font-size:0.75rem; white-space:nowrap;
                   box-shadow:0 2px 8px rgba(0,0,0,0.5); pointer-events:none; z-index:10; }}
  .hm-cell:hover .tip {{ display:block; }}

  /* ── legend ── */
  .legend {{ display:flex; align-items:center; gap:8px; margin-top:16px; }}
  .legend-label {{ font-size:0.75rem; color:#94a3b8; }}
  .legend-bar {{ display:flex; height:14px; border-radius:3px; overflow:hidden; flex:1;
                 max-width:300px; }}
  .legend-bar span {{ flex:1; }}

  /* ── totals row ── */
  .hm-table .total-row th {{ border-top:2px solid #475569; padding-top:8px; color:#f59e0b; }}
  .hm-table .total-row .hm-cell {{ border-top:2px solid #475569; font-weight:700;
                                    background:transparent !important; color:#f59e0b;
                                    font-size:0.7rem; }}
  /* ── clickable cells ── */
  .hm-cell.clickable {{ cursor:pointer; }}
  .hm-cell.clickable:active {{ transform:scale(0.95); }}

  /* ── subcategory popup ── */
  .popup-overlay {{ display:none; position:fixed; inset:0; background:rgba(0,0,0,0.6);
                    z-index:100; align-items:center; justify-content:center; }}
  .popup-overlay.open {{ display:flex; }}
  .popup {{ background:#1e293b; border:1px solid #475569; border-radius:12px;
            padding:24px; min-width:340px; max-width:500px; max-height:80vh;
            overflow-y:auto; box-shadow:0 8px 32px rgba(0,0,0,0.5); }}
  .popup h3 {{ font-size:1rem; margin:0 0 4px 0; color:#f8fafc; }}
  .popup .popup-period {{ color:#94a3b8; font-size:0.8rem; margin-bottom:14px; }}
  .popup table {{ width:100%; border-collapse:collapse; }}
  .popup th {{ text-align:left; padding:5px 10px; color:#94a3b8; font-size:0.78rem;
               font-weight:500; border-bottom:1px solid #334155; }}
  .popup th:nth-child(2), .popup th:nth-child(3) {{ text-align:right; }}
  .popup td {{ padding:6px 10px; font-size:0.85rem; border-bottom:1px solid #1e293b; }}
  .popup td:nth-child(2), .popup td:nth-child(3) {{ text-align:right; }}
  .popup tr:hover {{ background:#334155; }}
  .popup .popup-total td {{ font-weight:700; border-top:2px solid #475569; padding-top:8px; }}
  .popup-close {{ float:right; background:none; border:none; color:#94a3b8; font-size:1.2rem;
                  cursor:pointer; padding:0 4px; line-height:1; }}
  .popup-close:hover {{ color:#e2e8f0; }}
  .popup tr.sub-clickable {{ cursor:pointer; }}
  .popup tr.sub-clickable td:first-child {{ text-decoration:underline; text-decoration-color:#475569;
                                             text-underline-offset:2px; }}
  .popup tr.sub-clickable:hover td:first-child {{ text-decoration-color:#60a5fa; color:#60a5fa; }}

  /* ── transaction detail popup (layer 2) ── */
  .popup-overlay2 {{ display:none; position:fixed; inset:0; background:rgba(0,0,0,0.5);
                     z-index:200; align-items:center; justify-content:center; }}
  .popup-overlay2.open {{ display:flex; }}
  .popup2 {{ background:#1e293b; border:1px solid #475569; border-radius:12px;
             padding:24px; min-width:400px; max-width:600px; max-height:80vh;
             overflow-y:auto; box-shadow:0 8px 32px rgba(0,0,0,0.5); }}
  .popup2 h3 {{ font-size:1rem; margin:0 0 2px 0; color:#f8fafc; }}
  .popup2 .popup-sub {{ color:#60a5fa; font-size:0.85rem; margin-bottom:2px; }}
  .popup2 .popup-period {{ color:#94a3b8; font-size:0.8rem; margin-bottom:14px; }}
  .popup2 table {{ width:100%; border-collapse:collapse; }}
  .popup2 th {{ text-align:left; padding:5px 10px; color:#94a3b8; font-size:0.78rem;
                font-weight:500; border-bottom:1px solid #334155; }}
  .popup2 th:nth-child(2) {{ text-align:right; }}
  .popup2 td {{ padding:5px 10px; font-size:0.82rem; border-bottom:1px solid #1e293b; }}
  .popup2 td:nth-child(2) {{ text-align:right; }}
  .popup2 tr:hover {{ background:#334155; }}
  .popup2 .popup-total td {{ font-weight:700; border-top:2px solid #475569; padding-top:8px; }}
  .popup2 .note-tag {{ color:#f59e0b; font-style:italic; font-size:0.75rem; margin-left:6px; }}
</style>
</head>
<body>
<h1>Expenditure Heatmap</h1>
<p class="subtitle" id="subtitle">Category &times; Statement Period &mdash; All periods</p>

<div class="date-bar">
  <label for="fromMonth">From</label>
  <select id="fromMonth"></select>
  <label for="toMonth">To</label>
  <select id="toMonth"></select>
</div>

<div class="heatmap-card">
  <h2>Spend Intensity by Category &amp; Statement Period</h2>
  <div id="heatmapWrap"></div>
  <div class="legend" id="legend"></div>
</div>

<div class="popup-overlay" id="popupOverlay">
  <div class="popup" id="popupContent"></div>
</div>
<div class="popup-overlay2" id="popupOverlay2">
  <div class="popup2" id="popupContent2"></div>
</div>

<script>
const ALL_TXNS   = {txns_json};
const ALL_MONTHS = {months_json};

const fmt = n => n.toLocaleString('en-GB', {{minimumFractionDigits:0, maximumFractionDigits:0}});
const fmt2 = n => n.toLocaleString('en-GB', {{minimumFractionDigits:2, maximumFractionDigits:2}});
const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

// ── Colour scale (YlOrRd-inspired) ──
const STOPS = [
  [0.00, [30, 41, 59]],     // #1e293b (background — zero)
  [0.01, [254, 240, 138]],  // light yellow
  [0.25, [253, 186, 73]],   // amber
  [0.50, [249, 115, 22]],   // orange
  [0.75, [239, 68, 68]],    // red
  [1.00, [153, 27, 27]],    // dark red
];

function interpolateColour(t) {{
  if (t <= 0) return STOPS[0][1];
  if (t >= 1) return STOPS[STOPS.length - 1][1];
  for (let i = 1; i < STOPS.length; i++) {{
    if (t <= STOPS[i][0]) {{
      const [t0, c0] = STOPS[i - 1];
      const [t1, c1] = STOPS[i];
      const f = (t - t0) / (t1 - t0);
      return c0.map((v, j) => Math.round(v + (c1[j] - v) * f));
    }}
  }}
  return STOPS[STOPS.length - 1][1];
}}

function cellBg(val, maxVal) {{
  if (val === 0 || maxVal === 0) return 'rgba(30,41,59,1)';
  const t = Math.sqrt(val / maxVal);  // sqrt scale for better contrast
  const [r, g, b] = interpolateColour(t);
  return `rgb(${{r}},${{g}},${{b}})`;
}}

function textColour(val, maxVal) {{
  if (val === 0) return 'transparent';
  const t = Math.sqrt(val / maxVal);
  return t > 0.45 ? '#fff' : '#1e293b';
}}

// Pretty label for statement period
function periodLabel(dateStr) {{
  const d = new Date(dateStr + 'T00:00:00');
  return d.toLocaleDateString('en-GB', {{ month:'short', year:'numeric' }});
}}

// ── Dropdowns ──
const fromSel = document.getElementById('fromMonth');
const toSel   = document.getElementById('toMonth');

function populateDropdowns(curFrom, curTo) {{
  fromSel.innerHTML = '';
  toSel.innerHTML   = '';
  ALL_MONTHS.forEach(m => {{
    const o1 = new Option(m, m); const o2 = new Option(m, m);
    if (m === curFrom) o1.selected = true;
    if (m === curTo)   o2.selected = true;
    fromSel.appendChild(o1); toSel.appendChild(o2);
  }});
}}

// ── Render ──
function render() {{
  const fromM = fromSel.value;
  const toM   = toSel.value;

  // Filter
  const filtered = ALL_TXNS.filter(t =>
    !t.excluded && t.category !== 'Income' &&
    t.statement_date >= fromM && t.statement_date <= toM
  );

  // Get active periods (only those in range)
  const activePeriods = ALL_MONTHS.filter(m => m >= fromM && m <= toM);

  // Build matrix: category → period → amount
  // Also build subcategory detail: category → period → subcategory → amount
  const catPeriod = {{}};
  const catTotals = {{}};
  const subDetail = {{}};
  filtered.forEach(t => {{
    const c = t.category;
    const s = t.subcategory;
    const p = t.statement_date;
    const a = Math.abs(t.amount);
    if (!catPeriod[c]) catPeriod[c] = {{}};
    catPeriod[c][p] = (catPeriod[c][p] || 0) + a;
    catTotals[c] = (catTotals[c] || 0) + a;
    // Subcategory detail
    const key = c + '|||' + p;
    if (!subDetail[key]) subDetail[key] = {{}};
    subDetail[key][s] = (subDetail[key][s] || 0) + a;
  }});
  // Store globally for click handlers
  window._subDetail = subDetail;
  window._filtered = filtered;

  // Sort categories by total descending
  const cats = Object.keys(catTotals).sort((a, b) => catTotals[b] - catTotals[a]);

  // Find global max for colour scale
  let maxVal = 0;
  cats.forEach(c => {{
    activePeriods.forEach(p => {{
      const v = (catPeriod[c] || {{}})[p] || 0;
      if (v > maxVal) maxVal = v;
    }});
  }});

  // Period totals
  const periodTotals = {{}};
  activePeriods.forEach(p => {{
    let s = 0;
    cats.forEach(c => {{ s += (catPeriod[c] || {{}})[p] || 0; }});
    periodTotals[p] = s;
  }});

  // Subtitle
  document.getElementById('subtitle').textContent =
    fromM === ALL_MONTHS[0] && toM === ALL_MONTHS[ALL_MONTHS.length - 1]
      ? 'Category \\u00d7 Statement Period \\u2014 All periods'
      : `Category \\u00d7 Statement Period \\u2014 ${{fromM}} to ${{toM}}`;

  // Build HTML table
  let html = '<table class="hm-table"><thead><tr><th></th>';
  activePeriods.forEach(p => {{
    html += `<th class="col-hdr">${{periodLabel(p)}}</th>`;
  }});
  html += '<th class="col-hdr" style="color:#f59e0b">Total</th></tr></thead><tbody>';

  cats.forEach(c => {{
    html += `<tr><th class="row-hdr">${{esc(c)}}</th>`;
    let rowTotal = 0;
    activePeriods.forEach(p => {{
      const v = (catPeriod[c] || {{}})[p] || 0;
      rowTotal += v;
      const bg = cellBg(v, maxVal);
      const fg = textColour(v, maxVal);
      const label = v > 0 ? `\\u00a3${{fmt(v)}}` : '';
      const clickAttr = v > 0 ? ` class="hm-cell clickable" data-cat="${{esc(c)}}" data-period="${{p}}"` : ' class="hm-cell"';
      html += `<td><div${{clickAttr}} style="background:${{bg}};color:${{fg}}">${{label}}`;
      if (v > 0) html += `<span class="tip">${{esc(c)}} &middot; ${{periodLabel(p)}}: \u00a3${{fmt2(v)}}</span>`;
      html += '</div></td>';
    }});
    // Row total
    html += `<td><div class="hm-cell" style="background:transparent;color:#f59e0b;font-weight:700">\\u00a3${{fmt(rowTotal)}}</div></td>`;
    html += '</tr>';
  }});

  // Totals row
  html += '<tr class="total-row"><th class="row-hdr" style="color:#f59e0b;font-weight:700">TOTAL</th>';
  let grandTotal = 0;
  activePeriods.forEach(p => {{
    const v = periodTotals[p];
    grandTotal += v;
    html += `<td><div class="hm-cell">\\u00a3${{fmt(v)}}</div></td>`;
  }});
  html += `<td><div class="hm-cell" style="color:#f59e0b;font-weight:700">\\u00a3${{fmt(grandTotal)}}</div></td>`;
  html += '</tr></tbody></table>';

  document.getElementById('heatmapWrap').innerHTML = html;

  // Legend
  let legendHtml = '<span class="legend-label">\\u00a30</span><div class="legend-bar">';
  for (let i = 0; i <= 20; i++) {{
    const [r, g, b] = interpolateColour(i / 20);
    legendHtml += `<span style="background:rgb(${{r}},${{g}},${{b}})"></span>`;
  }}
  legendHtml += `</div><span class="legend-label">\\u00a3${{fmt(maxVal)}}</span>`;
  document.getElementById('legend').innerHTML = legendHtml;
}}

// ── INIT ──
populateDropdowns(ALL_MONTHS[0], ALL_MONTHS[ALL_MONTHS.length - 1]);

fromSel.addEventListener('change', () => {{
  if (fromSel.value > toSel.value) {{
    const curFrom = fromSel.value;
    populateDropdowns(curFrom, ALL_MONTHS[ALL_MONTHS.length - 1]);
    fromSel.value = curFrom;
  }}
  render();
}});

toSel.addEventListener('change', () => {{
  if (toSel.value < fromSel.value) {{
    const curTo = toSel.value;
    populateDropdowns(ALL_MONTHS[0], curTo);
    toSel.value = curTo;
  }}
  render();
}});

render();

// ── Subcategory popup on click ──
const overlay = document.getElementById('popupOverlay');
const popupEl = document.getElementById('popupContent');

function closePopup() {{ overlay.classList.remove('open'); }}
overlay.addEventListener('click', e => {{ if (e.target === overlay) closePopup(); }});

document.getElementById('heatmapWrap').addEventListener('click', e => {{
  const cell = e.target.closest('.hm-cell.clickable');
  if (!cell) return;
  const cat = cell.dataset.cat;
  const period = cell.dataset.period;
  const key = cat + '|||' + period;
  const subs = window._subDetail[key];
  if (!subs) return;

  // Sort subcategories by amount descending
  const sorted = Object.entries(subs).sort((a, b) => b[1] - a[1]);
  const total = sorted.reduce((s, e) => s + e[1], 0);

  let html = `<button class="popup-close" title="Close">&times;</button>`;
  html += `<h3>${{esc(cat)}}</h3>`;
  html += `<div class="popup-period">${{periodLabel(period)}} &mdash; \u00a3${{fmt2(total)}}</div>`;
  html += '<table><thead><tr><th>Subcategory</th><th>Amount</th><th>Share</th></tr></thead><tbody>';
  sorted.forEach(([sub, amt]) => {{
    const pct = total > 0 ? (amt / total * 100).toFixed(1) : '0.0';
    html += `<tr class="sub-clickable" data-cat="${{esc(cat)}}" data-sub="${{esc(sub)}}" data-period="${{period}}"><td>${{esc(sub)}}</td><td>\u00a3${{fmt2(amt)}}</td><td>${{pct}}%</td></tr>`;
  }});
  html += `<tr class="popup-total"><td>Total</td><td>\u00a3${{fmt2(total)}}</td><td>100.0%</td></tr>`;
  html += '</tbody></table>';

  popupEl.innerHTML = html;
  overlay.classList.add('open');

  popupEl.querySelector('.popup-close').addEventListener('click', closePopup);

  // Subcategory row click → transaction detail
  popupEl.querySelectorAll('tr.sub-clickable').forEach(row => {{
    row.addEventListener('click', () => showTxnPopup(row.dataset.cat, row.dataset.sub, row.dataset.period));
  }});
}});

// ── Transaction detail popup (layer 2) ──
const overlay2 = document.getElementById('popupOverlay2');
const popup2El = document.getElementById('popupContent2');

function closePopup2() {{ overlay2.classList.remove('open'); }}
overlay2.addEventListener('click', e => {{ if (e.target === overlay2) closePopup2(); }});

function showTxnPopup(cat, sub, period) {{
  const txns = (window._filtered || []).filter(t =>
    t.category === cat && t.subcategory === sub && t.statement_date === period
  );
  if (!txns.length) return;

  // Sort by date
  txns.sort((a, b) => a.date.localeCompare(b.date));
  const total = txns.reduce((s, t) => s + Math.abs(t.amount), 0);

  let html = `<button class="popup-close" title="Close" onclick="document.getElementById('popupOverlay2').classList.remove('open')">&times;</button>`;
  html += `<h3>${{esc(cat)}}</h3>`;
  html += `<div class="popup-sub">${{esc(sub)}}</div>`;
  html += `<div class="popup-period">${{periodLabel(period)}} &mdash; ${{txns.length}} transaction${{txns.length === 1 ? '' : 's'}} &mdash; \u00a3${{fmt2(total)}}</div>`;
  html += '<table><thead><tr><th>Date / Merchant</th><th>Amount</th></tr></thead><tbody>';
  txns.forEach(t => {{
    const noteTag = t.user_note ? `<span class="note-tag">${{esc(t.user_note)}}</span>` : '';
    html += `<tr><td>${{t.date}} &mdash; ${{esc(t.merchant.substring(0,40))}}${{noteTag}}</td><td>\u00a3${{fmt2(Math.abs(t.amount))}}</td></tr>`;
  }});
  html += `<tr class="popup-total"><td>Total (${{txns.length}})</td><td>\u00a3${{fmt2(total)}}</td></tr>`;
  html += '</tbody></table>';

  popup2El.innerHTML = html;
  overlay2.classList.add('open');
}}

// Update Escape to close top-most popup
document.addEventListener('keydown', e => {{
  if (e.key === 'Escape') {{
    if (overlay2.classList.contains('open')) closePopup2();
    else closePopup();
  }}
}});
</script>
</body>
</html>"""


def main():
    txns, months = _query_all()
    if not months:
        print("No data found in ledger.")
        return

    html = _build_html(txns, months)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    filepath = REPORTS_DIR / "expenditure_heatmap.html"
    filepath.write_text(html, encoding="utf-8")
    print(f"Report written to: {filepath}")
    webbrowser.open(filepath.as_uri())


if __name__ == "__main__":
    main()
