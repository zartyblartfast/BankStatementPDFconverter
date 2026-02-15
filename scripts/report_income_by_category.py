"""Generate an Income by Category (Source) HTML report.

Usage:
  python -m scripts.report_income_by_category

Interactive HTML report with from/to statement-period filtering,
a summary table, horizontal bar chart, and donut chart.
Income is broken down by subcategory (source).
"""

import json
import webbrowser
from pathlib import Path

from scripts.report_income_vs_expense import _query_all

REPORTS_DIR = Path(__file__).resolve().parent.parent / "finance" / "reports"


def _build_html(txns: list[dict], all_months: list[str]) -> str:
    """Build the Income by Category HTML report."""
    txns_json = json.dumps(txns)
    months_json = json.dumps(all_months)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Income by Source</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
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

  /* ── summary card ── */
  .summary-card {{ background:#1e293b; border-radius:12px; padding:20px; max-width:1600px;
                   margin-bottom:24px; }}
  .summary-card h2 {{ font-size:1.1rem; margin-bottom:4px; }}
  .summary-card .total {{ font-size:1.8rem; font-weight:700; margin-bottom:12px; color:#22c55e; }}
  table {{ width:100%; border-collapse:collapse; margin-top:8px; table-layout:fixed; }}
  table col.col-cat {{ width:50%; }}
  table col.col-amt {{ width:25%; }}
  table col.col-pct {{ width:25%; }}
  thead th {{ text-align:left; padding:6px 12px; color:#94a3b8; font-weight:500;
              font-size:0.85rem; border-bottom:1px solid #334155; }}
  thead th:nth-child(2), thead th:nth-child(3) {{ text-align:right; }}
  tbody td {{ padding:8px 12px; font-size:0.9rem; border-bottom:1px solid #1e293b; }}
  tbody td:nth-child(2), tbody td:nth-child(3) {{ text-align:right; }}
  tbody tr:hover {{ background:#334155; }}
  .cat-bar {{ display:inline-block; height:6px; border-radius:3px; margin-right:8px;
              vertical-align:middle; }}

  /* ── chart row ── */
  .chart-row {{ display:grid; grid-template-columns:1fr 1fr; gap:24px; max-width:1600px;
                margin-bottom:24px; }}
  .chart-card {{ background:#1e293b; border-radius:12px; padding:20px; }}
  .chart-card h3 {{ font-size:0.95rem; color:#94a3b8; margin-bottom:12px; }}
  .chart-wrap {{ position:relative; }}
  .bar-wrap {{ height:400px; }}
  .donut-wrap {{ max-width:400px; margin:0 auto; }}
</style>
</head>
<body>
<h1>Income by Source</h1>
<p class="subtitle" id="subtitle">All statement periods</p>

<div class="date-bar">
  <label for="fromMonth">From</label>
  <select id="fromMonth"></select>
  <label for="toMonth">To</label>
  <select id="toMonth"></select>
</div>

<div class="summary-card">
  <h2>Income Breakdown</h2>
  <div class="total" id="totalEl"></div>
  <table>
    <colgroup><col class="col-cat"><col class="col-amt"><col class="col-pct"></colgroup>
    <thead><tr><th>Source</th><th>Amount</th><th>Share</th></tr></thead>
    <tbody id="tableBody"></tbody>
  </table>
</div>

<div class="chart-row">
  <div class="chart-card">
    <h3>Income by Source</h3>
    <div class="chart-wrap bar-wrap">
      <canvas id="barChart"></canvas>
    </div>
  </div>
  <div class="chart-card">
    <h3>Income Distribution</h3>
    <div class="chart-wrap donut-wrap">
      <canvas id="donutChart"></canvas>
    </div>
  </div>
</div>

<script>
// ── DATA ──
const ALL_TXNS   = {txns_json};
const ALL_MONTHS = {months_json};

const COLOURS = [
  '#4ecdc4','#45b7d1','#96ceb4','#ffeaa7','#dfe6e9',
  '#fd79a8','#a29bfe','#fab1a0','#74b9ff','#00b894',
  '#e17055','#6c5ce7','#fdcb6e','#55efc4','#81ecec',
  '#ff7675','#636e72',
];

const fmt = n => n.toLocaleString('en-GB', {{minimumFractionDigits:2, maximumFractionDigits:2}});
const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');

// ── Chart instances ──
let barChart = null;
let donutChart = null;

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

  // Filter to income only
  const filtered = ALL_TXNS.filter(t =>
    !t.excluded && t.category === 'Income' &&
    t.statement_date >= fromM && t.statement_date <= toM
  );

  // Aggregate by subcategory (income source)
  const srcTotals = {{}};
  filtered.forEach(t => {{
    const s = t.subcategory;
    const a = Math.abs(t.amount);
    srcTotals[s] = (srcTotals[s] || 0) + a;
  }});

  // Sort descending
  const sorted = Object.entries(srcTotals).sort((a, b) => b[1] - a[1]);
  const total = sorted.reduce((s, e) => s + e[1], 0);

  // Update subtitle
  document.getElementById('subtitle').textContent =
    fromM === ALL_MONTHS[0] && toM === ALL_MONTHS[ALL_MONTHS.length - 1]
      ? 'All statement periods'
      : `${{fromM}} to ${{toM}}`;

  // Update total
  document.getElementById('totalEl').textContent = `\\u00a3${{fmt(total)}}`;

  // Build table
  const tbody = document.getElementById('tableBody');
  tbody.innerHTML = '';
  sorted.forEach(([src, amt], i) => {{
    const pct = total > 0 ? (amt / total * 100) : 0;
    const colour = COLOURS[i % COLOURS.length];
    const barWidth = total > 0 ? (amt / sorted[0][1] * 100) : 0;
    const tr = document.createElement('tr');
    tr.innerHTML =
      `<td><span class="cat-bar" style="width:${{barWidth}}%;max-width:120px;background:${{colour}}"></span>${{esc(src)}}</td>` +
      `<td>\\u00a3${{fmt(amt)}}</td>` +
      `<td>${{pct.toFixed(1)}}%</td>`;
    tbody.appendChild(tr);
  }});

  // Add total row
  const totalRow = document.createElement('tr');
  totalRow.innerHTML =
    `<td style="font-weight:700;border-top:2px solid #475569;padding-top:10px">TOTAL</td>` +
    `<td style="font-weight:700;border-top:2px solid #475569;padding-top:10px">\\u00a3${{fmt(total)}}</td>` +
    `<td style="font-weight:700;border-top:2px solid #475569;padding-top:10px">100.0%</td>`;
  tbody.appendChild(totalRow);

  // Chart data
  const labels = sorted.map(e => e[0]);
  const values = sorted.map(e => e[1]);
  const colours = sorted.map((_, i) => COLOURS[i % COLOURS.length]);

  // Bar chart
  if (barChart) barChart.destroy();
  barChart = new Chart(document.getElementById('barChart'), {{
    type: 'bar',
    data: {{
      labels: labels,
      datasets: [{{
        data: values,
        backgroundColor: colours,
        borderColor: colours.map(c => c + '99'),
        borderWidth: 1,
        borderRadius: 4,
      }}]
    }},
    options: {{
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          callbacks: {{
            label: ctx => `\\u00a3${{fmt(ctx.parsed.x)}}`,
          }}
        }}
      }},
      scales: {{
        x: {{
          ticks: {{
            color: '#94a3b8',
            callback: v => `\\u00a3${{(v/1000).toFixed(0)}}k`,
          }},
          grid: {{ color: '#334155', lineWidth: 0.5 }},
        }},
        y: {{
          ticks: {{ color: '#e2e8f0', font: {{ size: 12 }} }},
          grid: {{ display: false }},
        }}
      }}
    }}
  }});

  // Donut chart
  if (donutChart) donutChart.destroy();
  donutChart = new Chart(document.getElementById('donutChart'), {{
    type: 'doughnut',
    data: {{
      labels: labels,
      datasets: [{{
        data: values,
        backgroundColor: colours,
        borderColor: '#1e293b',
        borderWidth: 2,
      }}]
    }},
    options: {{
      responsive: true,
      plugins: {{
        legend: {{
          position: 'right',
          labels: {{
            color: '#e2e8f0',
            font: {{ size: 11 }},
            padding: 12,
            usePointStyle: true,
            pointStyleWidth: 12,
          }}
        }},
        tooltip: {{
          callbacks: {{
            label: ctx => {{
              const pct = total > 0 ? (ctx.parsed / total * 100).toFixed(1) : '0.0';
              return ` ${{ctx.label}}: \\u00a3${{fmt(ctx.parsed)}} (${{pct}}%)`;
            }}
          }}
        }}
      }}
    }}
  }});
}}

// ── INIT ──
populateDropdowns(ALL_MONTHS[0], ALL_MONTHS[ALL_MONTHS.length - 1]);

fromSel.addEventListener('change', () => {{
  if (fromSel.value > toSel.value) {{
    const curFrom = fromSel.value;
    populateDropdowns(curFrom, ALL_MONTHS[ALL_MONTHS.length - 1]);
    fromSel.value = curFrom;
    if (!toSel.value) toSel.value = toSel.options[0]?.value;
  }}
  render();
}});

toSel.addEventListener('change', () => {{
  if (toSel.value < fromSel.value) {{
    const curTo = toSel.value;
    populateDropdowns(ALL_MONTHS[0], curTo);
    toSel.value = curTo;
    if (!fromSel.value) fromSel.value = fromSel.options[0]?.value;
  }}
  render();
}});

render();
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
    filepath = REPORTS_DIR / "income_by_category.html"
    filepath.write_text(html, encoding="utf-8")
    print(f"Report written to: {filepath}")
    webbrowser.open(filepath.as_uri())


if __name__ == "__main__":
    main()
