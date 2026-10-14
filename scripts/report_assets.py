"""Adapt served legacy reports only; never rewrite stored financial reports."""
import json
import re
import uuid

CHART_TAG = '<script src="/static/vendor/chart.umd-4.4.8.js"></script>'
CLIENT_TAG = '<script src="/static/report-client.js"></script>'
LEGACY_CHART_TAG = '<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>'
LEGACY_API_BASE = "const API_BASE = (window.location.protocol === 'file:') ? 'http://127.0.0.1:5000' : '';"
_DATA_ASSIGNMENT = re.compile(r'^[ \t]*const\s+(?:ALL_TXNS|ALL_MONTHS)\s*=\s*', re.MULTILINE)
_SCRIPT = re.compile(r'(<script\b[^>]*>)(.*?)(</script\s*>)', re.IGNORECASE | re.DOTALL)
# These literal/comment forms occur in the known report generators.
_JS_LITERAL = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|//[^\n]*|/\*.*?\*/''', re.DOTALL)


def _escape_json_text(text: str) -> str:
    """Keep decoded JSON identical while removing HTML script delimiters."""
    return text.replace('<', r'\u003c').replace('>', r'\u003e').replace('&', r'\u0026')


def script_json(value) -> str:
    """Serialize report data for an inline script, not an HTML text node."""
    return _escape_json_text(json.dumps(value))


def _protect_report_data(html: str) -> tuple[str, dict[str, str]]:
    """Decode complete generator assignments, including nested arrays/objects.

    Protect data before interpreting HTML: old JSON strings can contain </script>.
    Opaque markers cannot collide with source text and are restored after adaptation.
    """
    decoder = json.JSONDecoder()
    prefix = '__REPORT_DATA_' + uuid.uuid4().hex + '_'
    while prefix in html:
        prefix = '__REPORT_DATA_' + uuid.uuid4().hex + '_'
    parts, protected, cursor = [], {}, 0
    for match in _DATA_ASSIGNMENT.finditer(html):
        if match.start() < cursor:
            continue
        try:
            value, end = decoder.raw_decode(html, match.end())
        except ValueError:
            continue
        if not isinstance(value, list) or not html[end:].lstrip().startswith(';'):
            continue
        marker = prefix + str(len(protected)) + '__'
        protected[marker] = _escape_json_text(html[match.end():end])
        parts.extend((html[cursor:match.end()], marker))
        cursor = end
    parts.append(html[cursor:])
    return ''.join(parts), protected


def _adapt_code(code: str) -> str:
    replacements = {LEGACY_API_BASE: "const API_BASE = '';"}
    replacements.update({f"fetch(API_BASE + '/api/{route}'": f"ledgerFetch('/api/{route}'"
                         for route in ('categories', 'recategorize', 'update-note')})
    # Match approved expressions at code positions, before skipping their literals.
    parts, cursor = [], 0
    while cursor < len(code):
        for old, new in replacements.items():
            if code.startswith(old, cursor) and (cursor == 0 or not (code[cursor - 1].isalnum() or code[cursor - 1] in '_$')):
                parts.append(new)
                cursor += len(old)
                break
        else:
            literal = _JS_LITERAL.match(code, cursor)
            if literal:
                parts.append(literal.group())
                cursor = literal.end()
            else:
                parts.append(code[cursor])
                cursor += 1
    return ''.join(parts)


def build_policy_report(txns, all_months, kind):
    """Keep the familiar report layout; bridge only exact data and saved policy.

    The old inline code used float pounds, category heuristics and unsafe edit
    IDs. Reuse its presentation, not those superseded calculation/mutation paths.
    """
    from importlib import import_module
    module = import_module('scripts.report_' + kind)
    layout = module._build_legacy_html([], []).split('<script>', 1)[0]
    layout = layout.replace('<body>', '<body data-movement-policy="confirmed-v1" data-report-kind="' + kind + '">', 1)
    layout = layout.replace('</head>', '<script src="/static/report-actions.js"></script>\n'
        '<script src="/static/report-classic.js"></script>\n</head>', 1)
    if 'id="showExcluded"' not in layout:
        layout = layout.replace('</select>\n</div>', '</select>\n'
            '<label class="toggle-label"><input type="checkbox" id="showExcluded"> Show excluded</label>\n</div>', 1)
    return layout + f'''
<p class="subtitle" id="policySummary"></p><p class="subtitle" id="policyWarnings" role="status"></p>
<details id="omittedRows"><summary>Excluded rows and confirmed transfers</summary><div id="omittedGroups"></div></details>
<p id="reportEditNotice" role="status">Standalone HTML editing is unsupported. Open this report through the local dashboard for single-row editing.</p>
<dialog id="reportEditDialog" aria-labelledby="reportEditTitle">
<h2 id="reportEditTitle"></h2>
<div id="reportNoteFields"><label>Note <textarea id="reportNote" maxlength="10000" rows="5"></textarea></label></div>
<div id="reportCategoryFields"><label>Category <select id="reportCategory"></select></label><label>Subcategory <select id="reportSubcategory"></select></label></div>
<p id="reportEditStatus" role="status"></p>
<button id="reportEditSave" type="button">Save this transaction</button><button id="reportEditCancel" type="button">Cancel</button>
</dialog>
<script>
const ALL_TXNS = {script_json(txns)};
const ALL_MONTHS = {script_json(all_months)};
const REPORT_POLICY = {script_json(txns.policy)};
const REPORT_KIND = {script_json(kind)};
LedgerClassicReport.mount(ALL_TXNS, ALL_MONTHS, REPORT_POLICY, REPORT_KIND);
</script></body></html>'''


def serve_report_html(html, *, conn, account_ids, period, name, legacy_all_accounts=False):
    """Serve current policy data in memory; never rewrite a stored report.

    Dashboard must resolve literal selectors with _dashboard_account_scope and
    periods with the shared resolver first. Explicit empty account scope stays
    empty. Invalid periods/scopes are rejected, not broadened to all rows.
    """
    from scripts.report_data import query_report
    from scripts.services.movement_policy import read_snapshot
    if period.get('error'):
        raise ValueError(str(period['error']))
    if not isinstance(account_ids, (list, tuple)):
        raise ValueError('Resolved explicit account IDs required')
    with read_snapshot(conn=conn) as connection:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'accounts' in tables:
            known = {r[0] for r in connection.execute('SELECT account_id FROM accounts')}
        else:
            known = {r[0] for r in connection.execute('SELECT DISTINCT account_id FROM transactions')}
        if any(not isinstance(a,str) or a not in known for a in account_ids):
            raise ValueError('Invalid account scope')
        if 'data-movement-policy="confirmed-v1"' in html:
            rows, months = query_report(conn=connection,account_ids=account_ids,
                start=str(period.get('start') or ''),end=str(period.get('end') or ''))
            return localize_report_html(build_policy_report(rows,months,name))
    # Legacy data has no verifiable transfer snapshot. Filter existing raw data
    # only; no timestamp can make it policy-aware. Missing account IDs never match.
    match = re.search(r'const\s+ALL_TXNS\s*=\s*',html)
    if match:
        try:
            rows, end = json.JSONDecoder().raw_decode(html,match.end())
        except ValueError:
            rows = None
        if isinstance(rows,list):
            start_date,end_date = str(period.get('start') or ''),str(period.get('end') or '')
            original_rows = rows
            rows = [r for r in rows if isinstance(r,dict)
                    and (r.get('account_id') in account_ids or
                         (legacy_all_accounts and bool(account_ids) and 'account_id' not in r))
                    and (not start_date or str(r.get('date','')) >= start_date)
                    and (not end_date or str(r.get('date','')) <= end_date)]
            serialized_rows = script_json(rows)
            html = html[:match.end()] + serialized_rows + html[end:]
            # Search after the complete decoded payload, never inside note text.
            months_match = re.compile(r'const\s+ALL_MONTHS\s*=\s*').search(html, match.end()+len(serialized_rows))
            if months_match and (rows != original_rows or start_date or end_date):
                _,end = json.JSONDecoder().raw_decode(html,months_match.end())
                months = sorted({r.get('statement_date') for r in rows if r.get('statement_date')})
                html = html[:months_match.end()]+script_json(months)+html[end:]
    if (period.get('start') or period.get('end')) and 'data-dashboard-period-filtered=' not in html:
        controls = '<style data-dashboard-period-filtered="true">.date-bar,#subtitle{display:none!important;}</style>'
        html = html.replace('</head>', controls+'</head>', 1) if '</head>' in html else controls+html
    return localize_report_html(html)


def standalone_report_html(html: str) -> str:
    """Resolve known script assets for CLI file viewing; editing stays disabled.

    Keep dashboard builders root-relative. Only script tags are adapted, never
    inline code or financial data, and no server or network fallback is started.
    """
    from pathlib import Path
    static = Path(__file__).resolve().parents[1] / 'static'
    assets = ('vendor/chart.umd-4.4.8.js', 'report-client.js',
              'report-actions.js', 'report-classic.js')
    tags = {f'<script src="/static/{asset}">':
            f'<script src="{(static / asset).as_uri()}">' for asset in assets}
    return _SCRIPT.sub(lambda match: tags.get(match.group(1), match.group(1))
                       + match.group(2) + match.group(3), html)


def localize_report_html(html: str) -> str:
    html, protected = _protect_report_data(html)

    def adapt_script(match):
        if match.group() == LEGACY_CHART_TAG:
            return CHART_TAG
        if re.search(r'\bsrc\s*=', match.group(1), re.IGNORECASE):
            return match.group()
        return match.group(1) + _adapt_code(match.group(2)) + match.group(3)

    html = _SCRIPT.sub(adapt_script, html)
    if 'data-movement-policy="confirmed-v1"' not in html and 'data-legacy-policy-warning' not in html:
        warning = ('<aside data-legacy-policy-warning="true" role="alert" '
                   'style="padding:16px;background:#78350f;color:#fef3c7">'
                   'Legacy report: confirmed-transfer adjustment is not verified. '
                   'Regenerate this report before using adjusted totals. Categories and '
                   'report timestamps do not establish transfer policy freshness. '
                   'Account/date filtering may be incomplete where old records lack account identity.</aside>')
        body = re.search(r'<body\b[^>]*>',html,re.I)
        html = html[:body.end()] + warning + html[body.end():] if body else warning + html
    if CLIENT_TAG not in html:
        html = html.replace('</head>', CLIENT_TAG + '\n</head>', 1)
    for marker, data in protected.items():
        html = html.replace(marker, data)
    return html
