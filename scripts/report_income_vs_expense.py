"""Generate an Income vs Expenditure HTML report with category breakdown.

Usage:
  python -m scripts.report_income_vs_expense

Outputs a fully interactive HTML file to finance/reports/ and opens it
in the browser.  The report contains from/to month dropdowns so all
date filtering happens client-side — no need for CLI date arguments.
"""

import json
import re
import sqlite3
import webbrowser
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "finance" / "ledger.db"
REPORTS_DIR = Path(__file__).resolve().parent.parent / "finance" / "reports"


def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Ledger database not found: {DB_PATH}")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _statement_date(source_file: str) -> str:
    """Extract YYYY-MM-DD from a source_file path like 'StatementsPDF\\2025-01-10_Statement.pdf'."""
    m = re.search(r'(\d{4}-\d{2}-\d{2})', source_file or '')
    return m.group(1) if m else 'unknown'


def _query_all() -> tuple[list[dict], list[str]]:
    """Return (transactions, sorted_statement_dates).

    Each transaction includes a statement_date (YYYY-MM-DD) derived from source_file.
    """
    conn = _connect()
    rows = conn.execute("""
        SELECT t.txn_id,
               t.txn_date        AS date,
               substr(t.txn_date, 1, 7) AS month,
               t.amount,
               t.merchant_core   AS merchant,
               c.name            AS category,
               s.name            AS subcategory,
               t.excluded,
               t.exclude_reason,
               t.user_note,
               t.source_file
        FROM transactions t
        JOIN subcategories s ON t.subcategory_id = s.subcategory_id
        JOIN categories c    ON s.category_id    = c.category_id
        ORDER BY t.txn_date
    """).fetchall()
    conn.close()

    txns = []
    for r in rows:
        d = dict(r)
        d['statement_date'] = _statement_date(d.pop('source_file'))
        txns.append(d)
    months = sorted({t['statement_date'] for t in txns})
    return txns, months


def _build_html(txns: list[dict], all_months: list[str]) -> str:
    """Build the fully-interactive HTML report."""
    txns_json = json.dumps(txns)
    months_json = json.dumps(all_months)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Income vs Expenditure</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  html, body {{ margin:0; padding:0; overflow-x:hidden; overflow-y:auto; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
         background:#0f172a; color:#e2e8f0; padding:24px; }}
  h1 {{ font-size:1.6rem; margin-bottom:4px; color:#f8fafc; }}
  .subtitle {{ color:#94a3b8; font-size:0.9rem; margin-bottom:16px; }}
  /* ── date picker ── */
  .date-bar {{ display:flex; align-items:center; gap:12px; margin-bottom:20px;
               max-width:1200px; background:#1e293b; border-radius:10px; padding:12px 20px; }}
  .date-bar label {{ color:#94a3b8; font-size:0.85rem; }}
  .date-bar select {{ background:#334155; color:#e2e8f0; border:1px solid #475569;
                      border-radius:6px; padding:5px 10px; font-size:0.85rem;
                      cursor:pointer; }}
  .date-bar select:focus {{ outline:none; border-color:#60a5fa; }}
  .date-bar .separator {{ width:1px; height:24px; background:#475569; margin:0 4px; }}
  .toggle-label {{ display:flex; align-items:center; gap:6px; color:#94a3b8; font-size:0.85rem;
                   cursor:pointer; user-select:none; }}
  .toggle-label input {{ accent-color:#f59e0b; cursor:pointer; }}
  .excluded-tag {{ display:inline-block; background:#78350f; color:#fbbf24; font-size:0.65rem;
                   padding:1px 6px; border-radius:3px; margin-left:6px; vertical-align:middle; }}
  .item-row.excluded-item td {{ opacity:0.45; text-decoration:line-through; }}
  .item-row.excluded-item .excluded-tag {{ text-decoration:none; opacity:1; }}
  /* ── donut row ── */
  .donut-row {{ display:flex; align-items:center; justify-content:center; gap:0;
                max-width:1200px; margin-bottom:24px; }}
  .donut-cell {{ background:#1e293b; border-radius:12px; padding:20px; flex:1;
                 position:relative; min-height:280px; display:flex; flex-direction:column;
                 align-items:center; }}
  .donut-cell h3 {{ font-size:0.95rem; color:#94a3b8; margin-bottom:8px; }}
  .donut-wrap {{ position:relative; width:220px; height:220px; }}
  .donut-center {{ position:absolute; top:50%; left:50%; transform:translate(-50%,-50%);
                   text-align:center; pointer-events:none; }}
  .donut-center .amt {{ font-size:1.3rem; font-weight:700; }}
  .donut-center .lbl {{ font-size:0.75rem; color:#94a3b8; }}
  .net-pill {{ background:#1e293b; border-radius:12px; padding:16px 20px; text-align:center;
               min-width:130px; display:flex; flex-direction:column; align-items:center;
               justify-content:center; margin:0 12px; }}
  .net-pill .lbl {{ font-size:0.75rem; color:#94a3b8; margin-bottom:4px; }}
  .net-pill .amt {{ font-size:1.4rem; font-weight:700; }}
  /* ── summary cards ── */
  .summary-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:24px; max-width:1200px; }}
  .summary-card {{ background:#1e293b; border-radius:12px; padding:20px; }}
  .summary-card h2 {{ font-size:1.1rem; margin-bottom:4px; }}
  .summary-card .total {{ font-size:1.8rem; font-weight:700; margin-bottom:12px; }}
  .income-total {{ color:#22c55e; }}
  .expense-total {{ color:#ef4444; }}
  table {{ width:100%; border-collapse:collapse; margin-top:8px; }}
  thead th {{ text-align:left; padding:6px 12px; color:#94a3b8; font-weight:500;
              font-size:0.85rem; border-bottom:1px solid #334155; }}
  thead th:nth-child(2), thead th:nth-child(3) {{ text-align:right; }}
  tbody tr:hover {{ background:#334155; }}
  /* ── expandable rows ── */
  .cat-row {{ cursor:default; }}
  .cat-row.expandable {{ cursor:pointer; }}
  .cat-row.expandable td:first-child {{ user-select:none; }}
  .arrow {{ display:inline-block; font-size:0.7rem; transition:transform 0.2s; }}
  .open > td:first-child .arrow {{ transform:rotate(90deg); }}
  .sub-row td {{ border-left:2px solid #334155; }}
  .sub-row.expandable td:first-child {{ user-select:none; cursor:pointer; }}
  .item-row td {{ border-left:2px solid #1e293b; }}
  .item-row:hover {{ background:#283548 !important; }}
  /* ── sort buttons ── */
  .sort-btns {{ margin-left:8px; }}
  .sort-btn {{ background:#334155; border:none; color:#94a3b8; font-size:0.7rem;
               padding:2px 8px; border-radius:4px; cursor:pointer; margin-left:4px; }}
  .sort-btn.active {{ background:#475569; color:#e2e8f0; }}
  .sort-btn:hover {{ background:#475569; }}
  /* ── net bar ── */
  .net-bar {{ max-width:1200px; background:#1e293b; border-radius:12px; padding:20px;
              margin-top:24px; text-align:center; }}
  .net-bar .label {{ color:#94a3b8; font-size:0.9rem; }}
  .net-bar .value {{ font-size:2rem; font-weight:700; }}
  .net-positive {{ color:#22c55e; }}
  .net-negative {{ color:#ef4444; }}
  /* ── edit button ── */
  .edit-btn {{ background:none; border:none; color:#94a3b8; cursor:pointer; font-size:0.75rem;
               padding:2px 6px; border-radius:4px; opacity:0.7; transition:opacity 0.15s; }}
  .edit-btn:hover {{ opacity:1; color:#60a5fa; }}
  /* ── note ── */
  .note-tag {{ color:#f59e0b; font-style:italic; font-size:0.75rem; margin-left:6px; }}
  .note-btn {{ background:none; border:none; color:#94a3b8; cursor:pointer; font-size:0.7rem;
               padding:2px 4px; border-radius:4px; opacity:0.5; transition:opacity 0.15s; margin-left:2px; }}
  .note-btn:hover {{ opacity:1; color:#f59e0b; }}
  .note-inline {{ display:inline-flex; align-items:center; gap:4px; margin-left:6px; }}
  .note-inline input {{ background:#334155; color:#f59e0b; border:1px solid #475569; font-style:italic;
                        border-radius:4px; padding:2px 6px; font-size:0.75rem; width:180px; }}
  .note-inline input:focus {{ outline:none; border-color:#f59e0b; }}
  .note-inline button {{ background:#334155; border:none; color:#94a3b8; cursor:pointer;
                         font-size:0.7rem; padding:2px 6px; border-radius:4px; }}
  .note-inline button:hover {{ color:#e2e8f0; }}
  /* ── modal ── */
  .modal-overlay {{ display:none; position:fixed; inset:0; background:rgba(0,0,0,0.6);
                    z-index:1000; align-items:center; justify-content:center; }}
  .modal-overlay.active {{ display:flex; }}
  .modal {{ background:#1e293b; border-radius:12px; padding:24px; width:440px; max-width:90vw;
            box-shadow:0 20px 60px rgba(0,0,0,0.5); }}
  .modal h3 {{ font-size:1rem; margin-bottom:16px; color:#f8fafc; }}
  .modal-info {{ color:#94a3b8; font-size:0.82rem; margin-bottom:16px;
                 background:#0f172a; padding:10px 14px; border-radius:8px; }}
  .modal-info .mi-label {{ color:#64748b; font-size:0.75rem; }}
  .modal-info .mi-val {{ color:#e2e8f0; }}
  .modal label {{ display:block; color:#94a3b8; font-size:0.82rem; margin-bottom:4px; margin-top:12px; }}
  .modal select, .modal input[type=text] {{
    width:100%; background:#334155; color:#e2e8f0; border:1px solid #475569;
    border-radius:6px; padding:7px 10px; font-size:0.85rem; }}
  .modal select:focus, .modal input:focus {{ outline:none; border-color:#60a5fa; }}
  .modal .scope-row {{ display:flex; gap:12px; margin-top:12px; }}
  .modal .scope-opt {{ display:flex; align-items:center; gap:6px; color:#94a3b8;
                       font-size:0.82rem; cursor:pointer; }}
  .modal .scope-opt input {{ accent-color:#60a5fa; }}
  .modal .remember-row {{ margin-top:12px; display:flex; align-items:center; gap:6px;
                          color:#94a3b8; font-size:0.82rem; cursor:pointer; }}
  .modal .remember-row input {{ accent-color:#f59e0b; }}
  .modal-actions {{ margin-top:20px; display:flex; gap:10px; justify-content:flex-end; }}
  .modal-actions button {{ padding:8px 18px; border-radius:6px; border:none;
                           font-size:0.85rem; cursor:pointer; }}
  .btn-cancel {{ background:#334155; color:#94a3b8; }}
  .btn-cancel:hover {{ background:#475569; }}
  .btn-save {{ background:#2563eb; color:#fff; }}
  .btn-save:hover {{ background:#1d4ed8; }}
  .btn-save:disabled {{ opacity:0.4; cursor:not-allowed; }}
  .modal .status-msg {{ margin-top:10px; font-size:0.82rem; padding:6px 10px; border-radius:6px; }}
  .status-ok {{ background:#14532d; color:#4ade80; }}
  .status-err {{ background:#7f1d1d; color:#fca5a5; }}
</style>
</head>
<body>

<h1>Income vs Expenditure</h1>
<p class="subtitle" id="subtitle"></p>

<div class="date-bar">
  <label for="fromMonth">From</label>
  <select id="fromMonth"></select>
  <label for="toMonth">To</label>
  <select id="toMonth"></select>
  <div class="separator"></div>
  <label class="toggle-label"><input type="checkbox" id="showExcluded"> Show excluded</label>
</div>

<div class="donut-row">
  <div class="donut-cell">
    <h3>Income</h3>
    <div class="donut-wrap">
      <canvas id="incomeDonut"></canvas>
      <div class="donut-center"><div class="amt income-total" id="donutIncomeAmt"></div><div class="lbl">Total Income</div></div>
    </div>
  </div>
  <div class="net-pill">
    <div class="lbl">Net</div>
    <div class="amt" id="donutNet"></div>
  </div>
  <div class="donut-cell">
    <h3>Expenditure</h3>
    <div class="donut-wrap">
      <canvas id="expenseDonut"></canvas>
      <div class="donut-center"><div class="amt expense-total" id="donutExpenseAmt"></div><div class="lbl">Total Expenditure</div></div>
    </div>
  </div>
</div>

<div class="summary-grid">
  <div class="summary-card">
    <h2>Income</h2>
    <div class="total income-total" id="incomeTotalEl"></div>
    <table>
      <thead><tr><th>Category</th><th>Amount</th><th>Share</th></tr></thead>
      <tbody id="incomeBody"></tbody>
    </table>
  </div>
  <div class="summary-card">
    <h2>Expenditure</h2>
    <div class="total expense-total" id="expenseTotalEl"></div>
    <table>
      <thead><tr><th>Category</th><th>Amount</th><th>Share</th></tr></thead>
      <tbody id="expenseBody"></tbody>
    </table>
  </div>
</div>

<div class="net-bar">
  <div class="label">Net (Income &minus; Expenditure)</div>
  <div class="value" id="netValue"></div>
</div>

<script>
// ── DATA ──
const ALL_TXNS   = {txns_json};
const ALL_MONTHS = {months_json};

const INCOME_COLOURS  = ["#22c55e","#16a34a","#15803d","#166534","#4ade80","#86efac",
                         "#a7f3d0","#34d399","#059669","#047857","#6ee7b7","#10b981"];
const EXPENSE_COLOURS = ["#ef4444","#f97316","#eab308","#8b5cf6","#ec4899","#06b6d4",
                         "#64748b","#a855f7","#14b8a6","#f43f5e","#6366f1","#84cc16",
                         "#d946ef","#fb923c","#facc15","#c084fc","#f472b6","#22d3ee",
                         "#94a3b8","#7c3aed","#2dd4bf","#e11d48","#818cf8","#a3e635",
                         "#e879f9","#fdba74","#fde047","#d8b4fe","#f9a8d4","#67e8f9"];

// ── HELPERS ──
const fmt = v => v.toLocaleString('en-GB', {{minimumFractionDigits:2, maximumFractionDigits:2}});
const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');

// ── DROPDOWN SETUP ──
const fromSel = document.getElementById('fromMonth');
const toSel   = document.getElementById('toMonth');

function populateDropdowns(fromVal, toVal) {{
  // Populate From: only months <= toVal
  const prevFrom = fromSel.value || fromVal;
  fromSel.innerHTML = '';
  ALL_MONTHS.filter(m => m <= toVal).forEach(m => {{
    const o = document.createElement('option');
    o.value = m; o.textContent = m;
    if (m === prevFrom) o.selected = true;
    fromSel.appendChild(o);
  }});
  if (!fromSel.value) fromSel.value = fromSel.options[0]?.value;

  // Populate To: only months >= fromSel.value
  const prevTo = toSel.value || toVal;
  toSel.innerHTML = '';
  ALL_MONTHS.filter(m => m >= fromSel.value).forEach(m => {{
    const o = document.createElement('option');
    o.value = m; o.textContent = m;
    if (m === prevTo) o.selected = true;
    toSel.appendChild(o);
  }});
  if (!toSel.value) toSel.value = toSel.options[toSel.options.length-1]?.value;
}}

// ── CHART INSTANCES ──
let incomeDonutInstance = null;
let expenseDonutInstance = null;

// ── MAIN RENDER ──
function render() {{
  const fromM = fromSel.value;
  const toM   = toSel.value;

  // Filter transactions
  const showExcl = document.getElementById('showExcluded').checked;
  const txns = ALL_TXNS.filter(t => t.statement_date >= fromM && t.statement_date <= toM && (showExcl || !t.excluded));

  // Subtitle
  document.getElementById('subtitle').innerHTML =
    fromM + ' to ' + toM + ' &middot; ' + txns.length + ' transactions';

  // ── Aggregate ──
  const catTotals = {{}};          // cat -> {{credits, debits}}
  const subTotals = {{}};          // cat -> sub -> {{credits, debits}}
  const subItems  = {{}};          // "cat||sub" -> [items]

  txns.forEach(t => {{
    const m = t.month, c = t.category, s = t.subcategory;
    const key = c + '||' + s;

    // cat totals
    if (!catTotals[c]) catTotals[c] = {{credits:0, debits:0}};
    if (t.amount > 0) catTotals[c].credits += t.amount;
    else              catTotals[c].debits  += t.amount;

    // sub totals
    if (!subTotals[c]) subTotals[c] = {{}};
    if (!subTotals[c][s]) subTotals[c][s] = {{credits:0, debits:0}};
    if (t.amount > 0) subTotals[c][s].credits += t.amount;
    else              subTotals[c][s].debits  += t.amount;

    // items
    if (!subItems[key]) subItems[key] = [];
    subItems[key].push({{txn_id: t.txn_id, date: t.date, amount: t.amount, merchant: t.merchant,
                         category: c, subcategory: s,
                         excluded: t.excluded, exclude_reason: t.exclude_reason,
                         user_note: t.user_note || ''}});
  }});

  // ── Category lists ──
  const allCats = Object.keys(catTotals).sort();
  const incomeCats  = allCats.filter(c => catTotals[c].credits > 0);
  const expenseCats = allCats.filter(c => catTotals[c].debits < 0);

  // ── Summary totals ──
  let totalIncome  = 0, totalExpense = 0;
  Object.values(catTotals).forEach(v => {{ totalIncome += v.credits; totalExpense += Math.abs(v.debits); }});

  document.getElementById('incomeTotalEl').textContent  = '\\u00a3' + fmt(totalIncome);
  document.getElementById('expenseTotalEl').textContent = '\\u00a3' + fmt(totalExpense);
  const net = totalIncome - totalExpense;
  const netEl = document.getElementById('netValue');
  netEl.textContent = '\\u00a3' + (net < 0 ? '-' : '') + fmt(Math.abs(net));
  netEl.className = 'value ' + (net >= 0 ? 'net-positive' : 'net-negative');

  // ── Donut totals ──
  document.getElementById('donutIncomeAmt').textContent  = '\\u00a3' + fmt(totalIncome);
  document.getElementById('donutExpenseAmt').textContent = '\\u00a3' + fmt(totalExpense);
  const donutNetEl = document.getElementById('donutNet');
  donutNetEl.textContent = '\\u00a3' + (net < 0 ? '-' : '') + fmt(Math.abs(net));
  donutNetEl.className = 'amt ' + (net >= 0 ? 'net-positive' : 'net-negative');

  // ── Donut charts (subcategory level) ──
  function buildDonutData(cats, catTotals, subTotals, side) {{
    const labels = [];
    const data = [];
    // Gather all subcategories, grouped by category (sorted by cat total desc)
    const sortedCats = [...cats].sort((a,b) => {{
      const va = side==='credits' ? catTotals[a].credits : Math.abs(catTotals[a].debits);
      const vb = side==='credits' ? catTotals[b].credits : Math.abs(catTotals[b].debits);
      return vb - va;
    }});
    sortedCats.forEach(cat => {{
      const subs = subTotals[cat] || {{}};
      const subNames = Object.keys(subs).filter(s =>
        side==='credits' ? subs[s].credits > 0 : subs[s].debits < 0
      ).sort((a,b) => {{
        const va = side==='credits' ? subs[a].credits : Math.abs(subs[a].debits);
        const vb = side==='credits' ? subs[b].credits : Math.abs(subs[b].debits);
        return vb - va;
      }});
      subNames.forEach(sub => {{
        const amt = side==='credits' ? subs[sub].credits : Math.abs(subs[sub].debits);
        labels.push(cat + ' / ' + sub);
        data.push(Math.round(amt * 100) / 100);
      }});
    }});
    return {{ labels, data }};
  }}

  // Income donut
  const incDonut = buildDonutData(incomeCats, catTotals, subTotals, 'credits');
  const incColours = incDonut.labels.map((_, i) => INCOME_COLOURS[i % INCOME_COLOURS.length]);
  if (incomeDonutInstance) incomeDonutInstance.destroy();
  incomeDonutInstance = new Chart(document.getElementById('incomeDonut').getContext('2d'), {{
    type: 'doughnut',
    data: {{ labels: incDonut.labels, datasets: [{{ data: incDonut.data, backgroundColor: incColours, borderWidth: 0 }}] }},
    options: {{
      cutout: '62%',
      responsive: true, maintainAspectRatio: true,
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{ callbacks: {{
          label: ctx => ctx.label + ': \\u00a3' + fmt(ctx.raw)
        }} }}
      }}
    }}
  }});

  // Expense donut
  const expDonut = buildDonutData(expenseCats, catTotals, subTotals, 'debits');
  const expColours = expDonut.labels.map((_, i) => EXPENSE_COLOURS[i % EXPENSE_COLOURS.length]);
  if (expenseDonutInstance) expenseDonutInstance.destroy();
  expenseDonutInstance = new Chart(document.getElementById('expenseDonut').getContext('2d'), {{
    type: 'doughnut',
    data: {{ labels: expDonut.labels, datasets: [{{ data: expDonut.data, backgroundColor: expColours, borderWidth: 0 }}] }},
    options: {{
      cutout: '62%',
      responsive: true, maintainAspectRatio: true,
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{ callbacks: {{
          label: ctx => ctx.label + ': \\u00a3' + fmt(ctx.raw)
        }} }}
      }}
    }}
  }});

  // ── Build summary tables ──
  buildTable('incomeBody',  incomeCats,  catTotals, subTotals, subItems, totalIncome,  'credits');
  buildTable('expenseBody', expenseCats, catTotals, subTotals, subItems, totalExpense, 'debits');
}}

function buildTable(tbodyId, cats, catTotals, subTotals, subItems, grandTotal, side) {{
  const tbody = document.getElementById(tbodyId);
  tbody.innerHTML = '';

  // Sort categories by amount descending
  const sorted = [...cats].sort((a,b) => {{
    const va = side==='credits' ? catTotals[a].credits : Math.abs(catTotals[a].debits);
    const vb = side==='credits' ? catTotals[b].credits : Math.abs(catTotals[b].debits);
    return vb - va;
  }});

  sorted.forEach(cat => {{
    const catAmt = side==='credits' ? catTotals[cat].credits : Math.abs(catTotals[cat].debits);
    const catPct = grandTotal ? (catAmt / grandTotal * 100).toFixed(1) : '0.0';
    const subs = subTotals[cat] || {{}};
    const subNames = Object.keys(subs).filter(s => {{
      return side==='credits' ? subs[s].credits > 0 : subs[s].debits < 0;
    }});
    // Sort subcategories by amount desc
    subNames.sort((a,b) => {{
      const va = side==='credits' ? subs[a].credits : Math.abs(subs[a].debits);
      const vb = side==='credits' ? subs[b].credits : Math.abs(subs[b].debits);
      return vb - va;
    }});
    const hasSubs = subNames.length > 0;

    // Category row
    const catRow = document.createElement('tr');
    catRow.className = hasSubs ? 'cat-row expandable' : 'cat-row';
    catRow.dataset.cat = cat;
    const arrow = hasSubs ? '<span class="arrow">&#9654;</span> ' : '&nbsp;&nbsp;&nbsp;&nbsp;';
    catRow.innerHTML =
      `<td style="padding:6px 12px">${{arrow}}${{esc(cat)}}</td>`+
      `<td style="padding:6px 12px;text-align:right">\\u00a3${{fmt(catAmt)}}</td>`+
      `<td style="padding:6px 12px;text-align:right">${{catPct}}%</td>`;
    tbody.appendChild(catRow);

    // Subcategory rows (hidden)
    subNames.forEach(sub => {{
      const subAmt = side==='credits' ? subs[sub].credits : Math.abs(subs[sub].debits);
      const subPct = catAmt ? (subAmt / catAmt * 100).toFixed(1) : '0.0';
      const subKey = cat + '||' + sub;
      const items = (subItems[subKey]||[]).filter(it => side==='credits' ? it.amount>0 : it.amount<0);
      const hasItems = items.length > 0;
      const subArrow = hasItems ? '<span class="arrow">&#9654;</span> ' : '&nbsp;&nbsp;&nbsp;&nbsp;';
      const sortBtns = hasItems ?
        '<span class="sort-btns" style="display:none">' +
        '<button class="sort-btn active" data-sort="date">Date</button>' +
        '<button class="sort-btn" data-sort="merchant">Merchant</button></span>' : '';

      const subRow = document.createElement('tr');
      subRow.className = hasItems ? 'sub-row expandable' : 'sub-row';
      subRow.dataset.parent = cat;
      subRow.dataset.subkey = subKey;
      subRow.dataset.sortMode = 'date';
      if (hasItems) subRow.dataset.items = JSON.stringify(items);
      subRow.style.display = 'none';
      subRow.innerHTML =
        `<td style="padding:4px 12px 4px 36px;color:#94a3b8;font-size:0.85rem">${{subArrow}}${{esc(sub)}}</td>`+
        `<td style="padding:4px 12px;text-align:right;color:#94a3b8;font-size:0.85rem">\\u00a3${{fmt(subAmt)}}</td>`+
        `<td style="padding:4px 12px;text-align:right;color:#94a3b8;font-size:0.85rem">${{subPct}}% ${{sortBtns}}</td>`;
      tbody.appendChild(subRow);
    }});

    // Category click handler
    if (hasSubs) {{
      catRow.addEventListener('click', () => {{
        const isOpen = catRow.classList.toggle('open');
        const subs = tbody.querySelectorAll(`.sub-row[data-parent="${{cat}}"]`);
        subs.forEach(s => {{
          if (isOpen) {{
            s.style.display = '';
          }} else {{
            s.style.display = 'none';
            s.classList.remove('open');
            const sb = s.querySelector('.sort-btns');
            if (sb) sb.style.display = 'none';
            const sk = s.dataset.subkey;
            if (sk) tbody.querySelectorAll(`.item-row[data-subparent="${{sk}}"]`).forEach(i => i.remove());
          }}
        }});
      }});
    }}

    // Subcategory click + sort handlers
    subNames.forEach(sub => {{
      const subKey = cat + '||' + sub;
      const subRow = tbody.querySelector(`.sub-row[data-subkey="${{CSS.escape(subKey)}}"]`);
      if (!subRow || !subRow.classList.contains('expandable')) return;

      subRow.querySelector('td:first-child').addEventListener('click', e => {{
        e.stopPropagation();
        const isOpen = subRow.classList.toggle('open');
        const sortBtns = subRow.querySelector('.sort-btns');
        if (isOpen) {{
          if (sortBtns) sortBtns.style.display = 'inline';
          renderItemRows(tbody, subRow);
        }} else {{
          if (sortBtns) sortBtns.style.display = 'none';
          tbody.querySelectorAll(`.item-row[data-subparent="${{CSS.escape(subKey)}}"]`).forEach(r => r.remove());
        }}
      }});

      subRow.querySelectorAll('.sort-btn').forEach(btn => {{
        btn.addEventListener('click', e => {{
          e.stopPropagation();
          subRow.dataset.sortMode = btn.dataset.sort;
          subRow.querySelectorAll('.sort-btn').forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          renderItemRows(tbody, subRow);
        }});
      }});
    }});
  }});
}}

function renderItemRows(tbody, subRow) {{
  const sk = subRow.dataset.subkey;
  tbody.querySelectorAll(`.item-row[data-subparent="${{CSS.escape(sk)}}"]`).forEach(r => r.remove());
  const items = JSON.parse(subRow.dataset.items || '[]');
  if (!items.length) return;
  const sorted = [...items];
  const mode = subRow.dataset.sortMode || 'date';
  if (mode === 'merchant')
    sorted.sort((a,b) => a.merchant.localeCompare(b.merchant) || a.date.localeCompare(b.date));
  else
    sorted.sort((a,b) => a.date.localeCompare(b.date) || a.merchant.localeCompare(b.merchant));

  let ref = subRow;
  sorted.forEach(item => {{
    const tr = document.createElement('tr');
    tr.className = 'item-row' + (item.excluded ? ' excluded-item' : '');
    tr.dataset.subparent = sk;
    const sign = item.amount < 0 ? '-' : '';
    const exTag = item.excluded ? `<span class="excluded-tag" title="${{esc(item.exclude_reason||'')}}">EXCLUDED</span>` : '';
    const editBtn = `<button class="edit-btn" title="Re-categorise" data-txnid="${{item.txn_id}}" data-merchant="${{esc(item.merchant)}}" data-cat="${{esc(item.category)}}" data-sub="${{esc(item.subcategory)}}">&#9998;</button>`;
    const noteDisplay = item.user_note
      ? `<span class="note-tag">${{esc(item.user_note)}}</span>`
      : '';
    const noteBtn = `<button class="note-btn" title="${{item.user_note ? 'Edit note' : 'Add note'}}" data-txnid="${{item.txn_id}}" data-note="${{esc(item.user_note)}}">&#128221;</button>`;
    tr.innerHTML =
      `<td style="padding:2px 12px 2px 60px;color:#64748b;font-size:0.8rem">${{item.date}} &mdash; ${{esc(item.merchant.substring(0,45))}}${{exTag}}${{noteDisplay}} ${{editBtn}}${{noteBtn}}</td>`+
      `<td style="padding:2px 12px;text-align:right;color:#64748b;font-size:0.8rem">\u00a3${{sign}}${{fmt(Math.abs(item.amount))}}</td>`+
      `<td style="padding:2px 12px"></td>`;
    ref.after(tr);
    ref = tr;
  }});
}}

// ── INIT ──
populateDropdowns(ALL_MONTHS[0], ALL_MONTHS[ALL_MONTHS.length-1]);

fromSel.addEventListener('change', () => {{
  // Re-filter To options: must be >= fromSel.value
  const curTo = toSel.value;
  toSel.innerHTML = '';
  ALL_MONTHS.filter(m => m >= fromSel.value).forEach(m => {{
    const o = document.createElement('option');
    o.value = m; o.textContent = m;
    if (m === curTo) o.selected = true;
    toSel.appendChild(o);
  }});
  if (!toSel.value) toSel.value = toSel.options[toSel.options.length-1]?.value;
  render();
}});

toSel.addEventListener('change', () => {{
  // Re-filter From options: must be <= toSel.value
  const curFrom = fromSel.value;
  fromSel.innerHTML = '';
  ALL_MONTHS.filter(m => m <= toSel.value).forEach(m => {{
    const o = document.createElement('option');
    o.value = m; o.textContent = m;
    if (m === curFrom) o.selected = true;
    fromSel.appendChild(o);
  }});
  if (!fromSel.value) fromSel.value = fromSel.options[0]?.value;
  render();
}});

document.getElementById('showExcluded').addEventListener('change', render);

render();
</script>

<!-- ── Recategorise Modal ── -->
<div class="modal-overlay" id="recatOverlay">
  <div class="modal">
    <h3>Re-categorise Transaction</h3>
    <div class="modal-info">
      <div><span class="mi-label">Merchant:</span> <span class="mi-val" id="rcMerchant"></span></div>
      <div><span class="mi-label">Current:</span> <span class="mi-val" id="rcCurrent"></span></div>
    </div>
    <label for="rcCatSel">Category</label>
    <select id="rcCatSel"><option value="">Loading…</option></select>
    <label for="rcSubSel">Subcategory</label>
    <select id="rcSubSel"><option value="">—</option></select>
    <label for="rcNewSub" id="rcNewSubLabel" style="display:none">New subcategory name</label>
    <input type="text" id="rcNewSub" style="display:none" placeholder="e.g. Car Insurance">
    <div class="scope-row">
      <label class="scope-opt"><input type="radio" name="rcScope" value="single" checked> This transaction only</label>
      <label class="scope-opt"><input type="radio" name="rcScope" value="merchant"> All <strong id="rcMerchantCount"></strong> matching</label>
    </div>
    <label class="remember-row"><input type="checkbox" id="rcRemember" checked> Remember rule for future imports</label>
    <div id="rcStatus"></div>
    <div class="modal-actions">
      <button class="btn-cancel" id="rcCancel">Cancel</button>
      <button class="btn-save" id="rcSave">Save</button>
    </div>
  </div>
</div>

<script>
// ── Recategorise logic ──
let _catTree = null;
let _rcTxnId = null;

async function loadCatTree() {{
  if (_catTree) return _catTree;
  try {{
    const r = await fetch('/api/categories');
    _catTree = await r.json();
  }} catch(e) {{ _catTree = {{tree:{{}}, id_map:{{}}}}; }}
  return _catTree;
}}

function populateCatSelect(tree, currentCat) {{
  const sel = document.getElementById('rcCatSel');
  sel.innerHTML = '';
  Object.keys(tree).sort().forEach(c => {{
    const o = document.createElement('option');
    o.value = c; o.textContent = c;
    if (c === currentCat) o.selected = true;
    sel.appendChild(o);
  }});
}}

function populateSubSelect(tree, cat, currentSub) {{
  const sel = document.getElementById('rcSubSel');
  sel.innerHTML = '';
  const subs = tree[cat] || [];
  subs.forEach(s => {{
    const o = document.createElement('option');
    o.value = s; o.textContent = s;
    if (s === currentSub) o.selected = true;
    sel.appendChild(o);
  }});
  // "+ New subcategory" option
  const oNew = document.createElement('option');
  oNew.value = '__new__'; oNew.textContent = '+ New subcategory…';
  sel.appendChild(oNew);
  toggleNewSub();
}}

function toggleNewSub() {{
  const isNew = document.getElementById('rcSubSel').value === '__new__';
  document.getElementById('rcNewSub').style.display = isNew ? '' : 'none';
  document.getElementById('rcNewSubLabel').style.display = isNew ? '' : 'none';
  if (isNew) document.getElementById('rcNewSub').focus();
}}

async function openRecatModal(txnId, merchant, curCat, curSub) {{
  _rcTxnId = txnId;
  document.getElementById('rcMerchant').textContent = merchant;
  document.getElementById('rcCurrent').textContent = curCat + ' / ' + curSub;
  document.getElementById('rcStatus').innerHTML = '';
  document.getElementById('rcSave').disabled = false;
  document.getElementById('rcNewSub').value = '';

  // Count matching merchant transactions
  const matchCount = ALL_TXNS.filter(t => t.merchant === merchant).length;
  document.getElementById('rcMerchantCount').textContent = matchCount + ' ' + esc(merchant);

  const data = await loadCatTree();
  populateCatSelect(data.tree, curCat);
  populateSubSelect(data.tree, curCat, curSub);

  document.getElementById('rcCatSel').onchange = () => {{
    populateSubSelect(data.tree, document.getElementById('rcCatSel').value, '');
  }};
  document.getElementById('rcSubSel').onchange = toggleNewSub;

  document.getElementById('recatOverlay').classList.add('active');
}}

function closeRecatModal() {{
  document.getElementById('recatOverlay').classList.remove('active');
}}

async function saveRecat() {{
  const btn = document.getElementById('rcSave');
  btn.disabled = true;
  const statusEl = document.getElementById('rcStatus');
  statusEl.innerHTML = '';

  const cat = document.getElementById('rcCatSel').value;
  const subSel = document.getElementById('rcSubSel').value;
  const newSub = document.getElementById('rcNewSub').value.trim();
  const scope = document.querySelector('input[name="rcScope"]:checked').value;
  const remember = document.getElementById('rcRemember').checked;

  const body = {{
    txn_id: _rcTxnId,
    category: cat,
    subcategory: subSel === '__new__' ? '' : subSel,
    new_subcategory: subSel === '__new__' ? newSub : '',
    scope: scope,
    remember_rule: remember
  }};

  try {{
    const r = await fetch('/api/recategorize', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(body)
    }});
    const data = await r.json();
    if (data.ok) {{
      statusEl.innerHTML = `<div class="status-msg status-ok">Updated ${{data.updated}} transaction(s). Reloading…</div>`;
      // Reload the report after a short delay
      setTimeout(() => {{
        closeRecatModal();
        window.location.reload();
      }}, 800);
    }} else {{
      statusEl.innerHTML = `<div class="status-msg status-err">${{esc(data.error)}}</div>`;
      btn.disabled = false;
    }}
  }} catch(e) {{
    statusEl.innerHTML = `<div class="status-msg status-err">Network error: ${{esc(e.message)}}</div>`;
    btn.disabled = false;
  }}
}}

// Wire up modal buttons
document.getElementById('rcCancel').addEventListener('click', closeRecatModal);
document.getElementById('rcSave').addEventListener('click', saveRecat);
document.getElementById('recatOverlay').addEventListener('click', e => {{
  if (e.target === e.currentTarget) closeRecatModal();
}});

// Delegate edit button clicks
document.addEventListener('click', e => {{
  const btn = e.target.closest('.edit-btn');
  if (!btn) return;
  e.stopPropagation();
  openRecatModal(
    parseInt(btn.dataset.txnid),
    btn.dataset.merchant,
    btn.dataset.cat,
    btn.dataset.sub
  );
}});

// ── Inline note editing ──
function openNoteEditor(noteBtn) {{
  // Don't open twice
  if (noteBtn.parentElement.querySelector('.note-inline')) return;
  const txnId = noteBtn.dataset.txnid;
  const current = noteBtn.dataset.note || '';

  const wrap = document.createElement('span');
  wrap.className = 'note-inline';
  const inp = document.createElement('input');
  inp.type = 'text'; inp.value = current; inp.placeholder = 'Add note\u2026';
  inp.maxLength = 120;
  const saveBtn = document.createElement('button');
  saveBtn.textContent = '\u2713';
  saveBtn.title = 'Save';
  const cancelBtn = document.createElement('button');
  cancelBtn.textContent = '\u2717';
  cancelBtn.title = 'Cancel';
  wrap.appendChild(inp);
  wrap.appendChild(saveBtn);
  wrap.appendChild(cancelBtn);
  noteBtn.after(wrap);
  inp.focus();

  async function save() {{
    const note = inp.value.trim();
    try {{
      const r = await fetch('/api/update-note', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{txn_id: parseInt(txnId), note: note}})
      }});
      const data = await r.json();
      if (data.ok) {{
        // Update local data and re-render
        noteBtn.dataset.note = note;
        noteBtn.title = note ? 'Edit note' : 'Add note';
        // Update the note tag next to merchant
        const td = noteBtn.closest('td');
        const oldTag = td.querySelector('.note-tag');
        if (oldTag) oldTag.remove();
        if (note) {{
          const tag = document.createElement('span');
          tag.className = 'note-tag';
          tag.textContent = note;
          // Insert before the edit button
          const editB = td.querySelector('.edit-btn');
          if (editB) editB.before(tag);
        }}
        wrap.remove();
      }}
    }} catch(err) {{
      inp.style.borderColor = '#ef4444';
    }}
  }}

  saveBtn.addEventListener('click', e => {{ e.stopPropagation(); save(); }});
  cancelBtn.addEventListener('click', e => {{ e.stopPropagation(); wrap.remove(); }});
  inp.addEventListener('keydown', e => {{
    if (e.key === 'Enter') {{ e.stopPropagation(); save(); }}
    if (e.key === 'Escape') {{ e.stopPropagation(); wrap.remove(); }}
  }});
  inp.addEventListener('click', e => e.stopPropagation());
}}

document.addEventListener('click', e => {{
  const btn = e.target.closest('.note-btn');
  if (!btn) return;
  e.stopPropagation();
  openNoteEditor(btn);
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
    filepath = REPORTS_DIR / "income_vs_expense.html"
    filepath.write_text(html, encoding="utf-8")
    print(f"Report written to: {filepath}")
    webbrowser.open(filepath.as_uri())


if __name__ == "__main__":
    main()
