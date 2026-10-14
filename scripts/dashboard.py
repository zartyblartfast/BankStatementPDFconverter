"""Flask dashboard for the bank statement ledger.

Usage:  python -m scripts.dashboard

Runs a local web server at http://127.0.0.1:5000
"""

import csv
import json
import os
import re
import secrets
import sqlite3
import stat
import threading
from pathlib import Path
from urllib.parse import urlencode

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, g, session

from scripts.init_db import DB_PATH
from scripts.services.ingestion import (ImportServiceError, commit_import,
                                       prepare_import, preview_summary, available_import_accounts)
from scripts.services.analytics import cashflow_summary, pence_decimal
from scripts.services.movement_policy import read_snapshot, summarise_movement
from scripts.services.transfers import exact_pence, ledger_pence
from scripts.services.coverage import coverage_windows_from_db, month_coverage
from scripts.services.periods import PeriodFilter, PeriodFilterError, resolve_period_filter
from scripts.services.source_matching import document_identifier
from scripts.services.statement_folders import (account_pdfs, folder_choices, read_mappings,
                                               save_mapping, FolderError)

from scripts.settings import get_settings

SETTINGS = get_settings()
PROJECT_ROOT = SETTINGS.root
TEMPLATES_DIR = SETTINGS.templates_dir
STATEMENTS_DIR = SETTINGS.statements_dir
REPORTS_DIR = SETTINGS.reports_dir
RULES_CSV = SETTINGS.category_rules_path
CATEGORIES_JSON = SETTINGS.categories_path

app = Flask(__name__, template_folder=str(TEMPLATES_DIR), static_folder=str(SETTINGS.static_dir))

app.secret_key = os.environ.get('BANKSTATEMENT_SESSION_SECRET') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict')
# Coordinates dashboard edits/config writes and snapshots in this process only.
MUTATION_LOCK = threading.RLock()
_IMPORT_PREVIEWS = {}


_IMPORT_REASON_HELP = {
    'source_account_mismatch': 'This source document is already known under another account. Select its registered account; do not import a duplicate into this account.',
    'schema_not_ready': 'The M1 database metadata migration has not been applied yet. Create/verify a backup, then apply the migration before live imports.',
    'reconciliation_failed': 'The statement totals do not match the extracted transactions.',
    'reconciliation_needs_review': 'The parser found warnings or uncertainty that needs review before import.',
    'reconciliation_unavailable': 'The statement did not provide enough total evidence to approve this import automatically.',
    'database_missing': 'The ledger database was not found.',
    'ambiguous_occurrences': 'Repeated transactions cannot be matched safely without review.',
    'potential_removals': 'The preview would leave previously imported transactions unmatched; review before replacing a statement.',
    'changed_source_candidates': 'Some rows look like changed financial data rather than safe metadata updates.',
    'other_source_overlap': 'This statement overlaps another source document already known to the ledger.',
}


def _annotate_import_preview(summary, source_path=None):
    if summary:
        summary = dict(summary)
        if source_path:
            summary['document_path'] = source_path
            summary['document_name'] = Path(source_path).name
        counts = summary.get('counts', {})
        already_imported = (summary.get('status') == 'eligible'
                            and counts.get('extracted', 0) > 0
                            and counts.get('additions') == 0
                            and counts.get('matches') == counts.get('extracted')
                            and counts.get('changed_source_fields') == 0
                            and counts.get('potential_removals') == 0
                            and counts.get('changed_source_candidates') == 0
                            and counts.get('ambiguous') == 0)
        summary['already_imported'] = already_imported
        summary['reason_details'] = [(reason, _IMPORT_REASON_HELP.get(reason, reason))
                                     for reason in summary.get('reasons', [])]
    return summary


def csrf_token():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_urlsafe(32)
    return session['csrf_token']


app.jinja_env.globals['csrf_token'] = csrf_token


@app.before_request
def protect_local_requests():
    # Never trust forwarded hosts, DNS aliases or browser Origin:null (file:).
    host = request.host
    if not re.fullmatch(r'(127\.0\.0\.1|localhost)(:[0-9]{1,5})?', host):
        return jsonify(ok=False, error='Only the local dashboard host is allowed.'), 403
    origin = request.headers.get('Origin')
    if (origin is not None and origin != request.host_url.rstrip('/')) or request.headers.get('Sec-Fetch-Site') == 'cross-site':
        return jsonify(ok=False, error='Cross-site requests are not allowed. Open the local dashboard.'), 403
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        supplied = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token')
        expected = session.get('csrf_token')
        if not isinstance(supplied, str) or not supplied.isascii() or not expected or not secrets.compare_digest(supplied, expected):
            return jsonify(ok=False, error='Missing or invalid CSRF token. Reload the local dashboard.'), 403
        MUTATION_LOCK.acquire()
        g.mutation_locked = True
        if request.path in ('/api/update-note', '/api/recategorize', '/api/resolve-exception'):
            return validate_mutation_payload()


def validate_mutation_payload():
    """Reject malformed JSON before any category/config/database write."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(ok=False, error='A JSON object is required.'), 400
    resolving = request.path == '/api/resolve-exception'
    key = 'exception_id' if resolving else 'txn_id'
    if type(data.get(key)) is not int or not 1 <= data[key] <= 9223372036854775807:
        return jsonify(ok=False, error=f'A positive SQLite integer {key} is required.'), 400
    for field in ('note', 'category', 'subcategory', 'new_subcategory', 'merchant_core', 'scope'):
        if field in data and not isinstance(data[field], str):
            return jsonify(ok=False, error=f'{field} must be a string.'), 400
    for field in ('remember_rule',):
        if field in data and type(data[field]) is not bool:
            return jsonify(ok=False, error=f'{field} must be a boolean.'), 400
    if request.path == '/api/update-note':
        if 'note' not in data:
            return jsonify(ok=False, error='note is required (use an empty string to clear it).'), 400
    else:
        if not data.get('category', '').strip() or not (data.get('subcategory', '').strip() or (not resolving and data.get('new_subcategory', '').strip())):
            return jsonify(ok=False, error='Category and subcategory are required.'), 400
        if data.get('scope', 'single') not in ('single', 'merchant'):
            return jsonify(ok=False, error='Invalid scope.'), 400
    if not DB_PATH.is_file():
        return jsonify(ok=False, error='Record not found.'), 404
    conn = _get_db()
    try:
        table = 'exceptions' if resolving else 'transactions'
        if conn.execute(f'SELECT 1 FROM {table} WHERE {key} = ?', (data[key],)).fetchone() is None:
            return jsonify(ok=False, error='Record not found.'), 404
    finally:
        conn.close()
    return None


@app.teardown_request
def release_mutation_lock(error=None):
    if getattr(g, 'mutation_locked', False):
        g.mutation_locked = False
        MUTATION_LOCK.release()


@app.after_request
def local_response_headers(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; form-action 'self'"
    return response


def health_identity():
    import hashlib
    return {'app': 'bankstatement-local-dashboard', 'mode': 'loopback',
            'instance': hashlib.sha256(str(PROJECT_ROOT).encode('utf-8')).hexdigest()}


@app.route('/health')
def health():
    return jsonify(health_identity())


@app.route('/api/csrf-token')
def api_csrf_token():
    return jsonify(csrf_token=csrf_token())


# ── Helpers ──

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _open_exception_count() -> int:
    """Count open exceptions for the nav badge."""
    if not DB_PATH.exists():
        return 0
    conn = _get_db()
    count = conn.execute("SELECT COUNT(*) FROM exceptions WHERE status = 'open'").fetchone()[0]
    conn.close()
    return count


def _missing_dashboard_movement():
    zero = {'credits_pence': 0, 'debits_pence': 0, 'net_pence': 0}
    return {'currency': 'GBP', 'row_count': 0, 'raw': dict(zero), 'adjusted': dict(zero),
            'adjusted_row_count': 0, 'transfers': {'in_count': 0, 'out_count': 0, 'in_pence': 0,
                                               'out_pence': 0, 'unmatched_count': 0},
            'excluded_count': 0, 'unclassified_count': 0, 'warnings': ['database_missing'],
            'transfer_schema_ready': False, 'counterpart_outside_scope_count': 0,
            'different_posting_date_group_count': 0, 'transfer_evidence_ref': None}


def _dashboard_account_scope(conn):
    """Exact account_id selector; legacy GBP evidence is read-only, never seeded."""
    if len(request.args.getlist('account_id')) > 1:
        raise ValueError('Supply account_id once')
    selected = request.args.get('account_id', '')
    if conn is None:
        accounts = []
    elif _table_exists(conn, 'accounts'):
        accounts = [dict(r) for r in conn.execute(
            "SELECT account_id,display_name FROM accounts WHERE owned=1 AND currency='GBP' "
            "ORDER BY display_name,account_id")]
    else:
        accounts = [dict(r) for r in conn.execute(
            "SELECT account_id,account_id AS display_name FROM transactions WHERE account_id='HSBC_UK' GROUP BY account_id "
            "HAVING MIN(currency)='GBP' AND MAX(currency)='GBP' AND COUNT(currency)=COUNT(*)")]
    if selected and selected not in {a['account_id'] for a in accounts}:
        raise ValueError('Unknown or unsupported account_id')
    return selected or None, accounts


def _dashboard_period():
    for key in ('period', 'year', 'month', 'start', 'end'):
        if len(request.args.getlist(key)) > 1:
            raise ValueError(f'Supply {key} once')
    args = request.args.to_dict()
    if 'period' not in args and (args.get('start') or args.get('end')):
        args['period'] = 'custom'
    period = _report_period_context(args)
    if period['error']:
        raise ValueError('Invalid period filter')
    return period


def _dashboard_movement_rows(conn, account_id, accounts, period=None):
    ids = [account_id] if account_id else [a['account_id'] for a in accounts]
    if not ids:
        return []
    date_clause = ''
    params = list(ids)
    if period and period['kind'] != 'all':
        date_clause = ' AND t.txn_date BETWEEN ? AND ?'
        params.extend((period['start'], period['end']))
    return conn.execute(
        "SELECT t.*,COALESCE(c.name,'Uncategorised') AS category,SUBSTR(t.txn_date,1,7) AS month "
        "FROM transactions t LEFT JOIN subcategories s ON t.subcategory_id=s.subcategory_id "
        "LEFT JOIN categories c ON s.category_id=c.category_id "
        "WHERE t.account_id IN (" + ','.join('?' for _ in ids) + ") AND t.currency='GBP' "
        + date_clause + " ORDER BY t.txn_date,t.txn_id", params).fetchall()


def _home_coverage_stats(conn, account_id=None) -> dict[str, object]:
    # Retained helper entry point; coverage and averages share the same scope.
    return _home_average_stats(conn, account_id)


def _fmt_money(value) -> str:
    if value is None:
        return '—'
    return f'{value:,.2f}'


def _home_average_stats(conn, account_id=None, accounts=None, period=None) -> dict[str, object]:
    """Intersect independently verified account months; never union coverage."""
    if accounts is None:
        _, accounts = _dashboard_account_scope(conn)
    ids = [account_id] if account_id else [a['account_id'] for a in accounts]
    try:
        windows = tuple(w for w in coverage_windows_from_db(conn, account_id=account_id)
                        if w.account_id in ids)
    except sqlite3.OperationalError:
        windows = ()
    result = {
        'average_available': False, 'average_income': '—', 'average_expense': '—',
        'average_net': '—', 'average_net_raw': 0,
        'average_summary': 'No complete covered months available for averages',
        'average_warnings': ('No complete covered months available for averages',),
        'coverage_status': 'unavailable', 'coverage_range': '—',
        'coverage_complete_months': 0, 'coverage_partial_months': 0,
        'coverage_missing_months': 0, 'coverage_summary': 'No verified coverage',
        'complete_months': [], 'coverage_by_account': [], 'months': [],
    }
    if not windows or not ids:
        result['coverage_by_account'] = [{'account_id': a, 'complete_months': [],
                                         'warnings': ['No verified statement coverage for account ' + a]} for a in ids]
        result['average_warnings'] = [item['warnings'][0] for item in result['coverage_by_account']] + list(result['average_warnings'])
        return result
    start, end = ((period['start'], period['end']) if period and period['kind'] != 'all' else
                  (min(w.start for w in windows), max(w.end for w in windows)))
    summaries = {a: cashflow_summary(conn, windows, start=start, end=end, account_id=a) for a in ids}
    monthly = {a: {m.month: m for m in summary.months} for a, summary in summaries.items()}
    complete = sorted(set.intersection(*(set(values) for values in monthly.values())))
    month_names = [m.month for m in month_coverage((), start=start, end=end)]
    missing = sum(all(m in s.missing_months for s in summaries.values()) for m in month_names)
    partial = len(month_names) - len(complete) - missing
    warnings = sorted({w for s in summaries.values() for w in s.warnings})
    if not complete:
        warnings.append('No complete covered months common to every selected account; averages unavailable')
    result.update(complete_months=complete, months=month_names, average_available=bool(complete),
                  average_warnings=warnings, coverage_range=f'{start} to {end}',
                  coverage_complete_months=len(complete), coverage_partial_months=partial,
                  coverage_missing_months=missing,
                  coverage_status='complete' if complete and not partial and not missing else 'partial',
                  coverage_summary=(f"{len(complete)} complete month{'s' if len(complete) != 1 else ''} common to every selected account; "
                                    f'{partial} partial months; {missing} missing months'),
                  coverage_by_account=[{'account_id': a, 'complete_months': sorted(monthly[a]),
                                        'warnings': list(summaries[a].warnings)} for a in ids])
    if complete:
        def average(attribute):
            cents = sum(exact_pence(getattr(monthly[a][m], attribute)) for a in ids for m in complete)
            quotient, remainder = divmod(abs(cents), len(complete))
            rounded = quotient + (2 * remainder >= len(complete))
            return -rounded if cents < 0 else rounded
        income, expense, net = average('income'), average('expenditure'), average('net')
        result.update(average_income=_fmt_money(pence_decimal(income)),
                      average_expense=_fmt_money(pence_decimal(expense)),
                      average_net=('+' if net >= 0 else '-') + _fmt_money(pence_decimal(abs(net))),
                      average_net_raw=net,
                      average_summary=f'Based on {len(complete)} complete covered months common to every selected account')
    return result


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _home_import_history(conn) -> list[dict[str, object]]:
    """Return import-log rows with human-readable source names.

    M1 import_log rows are keyed as ``m1:<source_identity>`` for stable
    dedupe/replay. That internal identity is useful to the import service, but
    the dashboard should show the statement filename/document key instead.
    """
    if not _table_exists(conn, 'source_documents'):
        rows = conn.execute(
            "SELECT source_file, row_count, imported_at FROM import_log ORDER BY source_file"
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT
                COALESCE(
                    CASE WHEN il.source_file LIKE 'm1:%' THEN sd.document_key END,
                    il.source_file
                ) AS source_file,
                il.source_file AS source_identity_key,
                il.row_count,
                il.imported_at
            FROM import_log il
            LEFT JOIN source_documents sd
                ON il.source_file = 'm1:' || sd.source_identity
            ORDER BY il.source_file
            """
        ).fetchall()
    display_rows: dict[str, dict[str, object]] = {}
    for row in rows:
        item = dict(row)
        source_name = str(item.get('source_file') or '')
        match = re.search(r'(\d{4}-\d{2}-\d{2})', source_name)
        key = match.group(1) if match else source_name
        existing = display_rows.get(key)
        is_m1 = str(item.get('source_identity_key') or '').startswith('m1:')
        existing_is_m1 = bool(existing and str(existing.get('source_identity_key') or '').startswith('m1:'))
        if existing is None or (is_m1 and not existing_is_m1):
            display_rows[key] = item
    return sorted(display_rows.values(), key=lambda r: str(r.get('source_file') or ''))


def _load_cat_tree() -> dict[str, list[str]]:
    """Load categories.json as {category: [subcategories]}."""
    with open(CATEGORIES_JSON, encoding="utf-8") as f:
        return json.load(f)


@app.context_processor
def inject_globals():
    """Make exception_count available to all templates (for nav badge)."""
    return {"exception_count": _open_exception_count()}


# ── Routes ──

def _home_statement_range(conn) -> str:
    """Sort actual statement dates, never path prefixes or internal source IDs."""
    from datetime import date
    sources = [row[0] for row in conn.execute('SELECT DISTINCT source_file FROM transactions')]
    if _table_exists(conn, 'source_documents'):
        sources.extend(row[0] for row in conn.execute('SELECT document_key FROM source_documents'))
    dates = []
    for source in sources:
        filename = str(source or '').replace('\\', '/').rsplit('/', 1)[-1]
        match = re.search(r'(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)', filename)
        if match:
            try:
                dates.append(date.fromisoformat(match.group(1)))
            except ValueError:
                continue
    if not dates:
        return '—'
    return f'{min(dates):%Y-%m} to {max(dates):%Y-%m}'


@app.route("/")
def home():
    stats = {
        "total_txns": 0,
        "statements": 0,
        "statement_range": "—",
        "open_exceptions": 0,
        "total_income": "0.00",
        "total_expense": "0.00",
        "net": "0.00",
        "net_raw": 0,
        "last_import": "—",
        "coverage_status": "unavailable",
        "coverage_range": "—",
        "coverage_complete_months": 0,
        "coverage_partial_months": 0,
        "coverage_missing_months": 0,
        "coverage_summary": "No verified coverage",
        "uncategorised": 0,
        "categorisation_summary": "0 uncategorised",
        "average_available": False,
        "average_income": "—",
        "average_expense": "—",
        "average_net": "—",
        "average_net_raw": 0,
        "average_summary": "No complete covered months available for averages",
        "average_warnings": (),
    }
    recent_imports = []
    conn = _get_db() if DB_PATH.exists() else None
    try:
        period = _dashboard_period()
        if conn is None:
            account_id, accounts = _dashboard_account_scope(None)
        else:
            with read_snapshot(conn=conn):
                account_id, accounts = _dashboard_account_scope(conn)
                rows = _dashboard_movement_rows(conn, account_id, accounts, period)
                stats['total_txns'] = len(rows)
                stats['statements'] = len({r['source_file'] for r in rows if r['source_file']})
                stats['statement_range'] = _home_statement_range(conn) if account_id is None else '—'
                stats['open_exceptions'] = conn.execute("SELECT COUNT(*) FROM exceptions WHERE status='open'").fetchone()[0]
                stats['uncategorised'] = sum(r['subcategory_id'] is None for r in rows)
                stats['categorisation_summary'] = f"{stats['uncategorised']} uncategorised"
                stats.update(_home_average_stats(conn, account_id, accounts, period))
                movement = summarise_movement([r['txn_id'] for r in rows], conn=conn, include_excluded=False)
                stats['movement'] = movement
                totals = movement['adjusted']
                stats['total_income'] = _fmt_money(pence_decimal(totals['credits_pence']))
                stats['total_expense'] = _fmt_money(pence_decimal(totals['debits_pence']))
                net = totals['net_pence']
                stats['net'] = ('+' if net >= 0 else '-') + _fmt_money(pence_decimal(abs(net)))
                stats['net_raw'] = net
                row = conn.execute('SELECT imported_at FROM import_log ORDER BY imported_at DESC LIMIT 1').fetchone()
                if row:
                    stats['last_import'] = row[0][:16].replace('T', ' ')
                recent_imports = _home_import_history(conn)
    except ValueError as error:
        return str(error), 400
    finally:
        if conn is not None:
            conn.close()
    return render_template('home.html', active_page='home', stats=stats,
                           recent_imports=recent_imports, accounts=accounts, account_id=account_id or '', period=period)


def _available_statement_pdfs():
    """Exact lexical identities; never descend through symlinks or junctions."""
    available = {}

    def regular(path):
        info = path.lstat()
        return not (stat.S_ISLNK(info.st_mode) or
                    getattr(info, 'st_file_attributes', 0) &
                    getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400))

    try:
        root = PROJECT_ROOT.resolve()
        statements = STATEMENTS_DIR.resolve()
        # Check lexical ancestors before resolution can erase link identity.
        relative = STATEMENTS_DIR.relative_to(PROJECT_ROOT)
        ancestor = PROJECT_ROOT
        for part in relative.parts:
            ancestor = ancestor / part
            if not regular(ancestor):
                return available
        if not statements.is_relative_to(root) or not statements.is_dir():
            return available
        for directory, dirs, files in os.walk(STATEMENTS_DIR, followlinks=False):
            parent = Path(directory)
            safe_dirs = []
            for name in dirs:
                child = parent / name
                try:
                    if regular(child) and child.resolve().is_relative_to(statements):
                        safe_dirs.append(name)
                except (OSError, ValueError, RuntimeError):
                    continue
            dirs[:] = sorted(safe_dirs)
            for name in sorted(files):
                candidate = parent / name
                if candidate.suffix.lower() != '.pdf':
                    continue
                try:
                    actual = candidate.resolve()
                    if (regular(candidate) and actual.is_file() and
                            actual.is_relative_to(statements) and actual.is_relative_to(root)):
                        available[str(candidate.relative_to(PROJECT_ROOT))] = candidate
                except (OSError, ValueError, RuntimeError):
                    continue
    except (OSError, ValueError, RuntimeError):
        return available
    return dict(sorted(available.items()))


def _statement_folder_state(selected_account, accounts):
    state = dict(choices=folder_choices(root=STATEMENTS_DIR), selected=None,
                 message='', ready=False, can_save=False)
    try:
        mappings = read_mappings(db_path=DB_PATH)
        state['selected'] = mappings.get(selected_account)
        account = next((a for a in accounts if a['account_id'] == selected_account), None)
        if account is None or not account['import_enabled']:
            state['message'] = 'Select an available, verified account to choose its statement folder.'
            return {}, state
        state['can_save'] = True
        scoped = account_pdfs(selected_account, db_path=DB_PATH, root=STATEMENTS_DIR)
        allowed = set(scoped.values())
        available = {key: path for key, path in _available_statement_pdfs().items() if path in allowed}
        state['ready'] = True
        return available, state
    except (FolderError, FileNotFoundError) as exc:
        state['message'] = str(exc)
        return {}, state


@app.route("/ingest", methods=["GET", "POST"])
def ingest():
    result = None
    import_preview = None
    commit_result = None
    commit_account = None
    selected_pdf = None
    accounts = available_import_accounts(db_path=DB_PATH)
    selected_account = request.values.get("account_id", "")
    # Accept only the exact project-relative values offered by this page. Resolve
    # containment before listing/processing so linked files cannot escape either root.
    available, folder_state = _statement_folder_state(selected_account, accounts)

    if request.method == "POST":
        action = request.form.get("action", "preview")
        if action == 'save_folder':
            try:
                save_mapping(selected_account, request.form.get('folder_path', ''),
                             db_path=DB_PATH, root=STATEMENTS_DIR)
            except (ValueError, FileNotFoundError) as exc:
                return str(exc), 400
            flash('Statement folder saved. No statements imported or moved.', 'success')
            return redirect(url_for('ingest', account_id=selected_account))
        if action == "commit":
            token = request.form.get("import_token", "")
            held_entry = _IMPORT_PREVIEWS.get(token)
            if held_entry is None:
                return 'Import preview expired or was not prepared by this server.', 400
            held_preview, held_summary = held_entry
            selected_account = held_summary.get('account_id', '')
            selected_pdf = held_summary.get('document_path')
            available, folder_state = _statement_folder_state(selected_account, accounts)
            if selected_pdf not in available:
                return 'Statement folder scope changed or became unavailable; review again.', 400
            if request.form.get("approve") != "yes":
                flash("Import approval is required.", "error")
                import_preview = held_summary
            elif held_summary.get('already_imported'):
                _IMPORT_PREVIEWS.pop(token, None)
                import_preview = held_summary
                flash(f"{held_summary.get('document_name', 'Statement')} is already fully imported; no import is needed.", "success")
            else:
                try:
                    commit_result = commit_import(held_preview, approved=True)
                except ImportServiceError as exc:
                    flash(f"Import blocked: {exc.code}", "error")
                    import_preview = held_summary
                else:
                    commit_account = held_summary
                    _IMPORT_PREVIEWS.pop(token, None)
                    try:
                        _regenerate_report()
                    except Exception:
                        app.logger.exception('Report refresh failed after import was saved')
                        flash('Import saved, but reports could not be refreshed.', 'warning')
                    flash(f"Imported {held_summary.get('document_name', 'statement')} for {held_summary.get('account_display_name', '')} ({selected_account}): {commit_result.added} new and matched {commit_result.matched} existing transactions.", "success")
        else:
            if not any(a["account_id"] == selected_account and a["import_enabled"] for a in accounts):
                return "Select an available, verified account before reviewing a statement.", 400
            pdf_path = request.form.get("pdf", "")
            if pdf_path:
                if pdf_path not in available:
                    return 'Select an available statement PDF from the configured project.', 400
                selected_pdf = pdf_path
                try:
                    held_preview = prepare_import(pdf_path, db_path=DB_PATH, root=PROJECT_ROOT, account_id=selected_account)
                    import_preview = _annotate_import_preview(preview_summary(held_preview), pdf_path)
                    _IMPORT_PREVIEWS[held_preview.token] = (held_preview, import_preview)
                    if import_preview.get("already_imported"):
                        flash(f"{import_preview['document_name']} checked. It is already fully imported; no import is needed.", "success")
                    elif import_preview["status"] == "eligible":
                        flash(f"{import_preview['document_name']} checked. Review the preview before importing.", "success")
                    else:
                        flash(f"{import_preview['document_name']} import is blocked until the preview issues are resolved.", "error")
                except Exception as exc:
                    flash(f"Import preview failed: {getattr(exc, 'code', str(exc))}", "error")
            else:
                flash("No PDF selected.", "error")

    # Recompute using the frozen account after a commit; never the posted override.
    available, folder_state = _statement_folder_state(selected_account, accounts)
    # List available PDFs
    pdfs = []
    if STATEMENTS_DIR.exists():
        imported_counts = {}
        if DB_PATH.exists() and any(a['account_id'] == selected_account for a in accounts):
            conn = _get_db()
            # Union transaction IDs by account-local canonical/alias identity.
            # Never use a global filename count or stale linked source_file.
            source_ids = {}
            linked_ids = set()
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if {'source_documents', 'source_document_aliases', 'transaction_sources'} <= tables:
                rows = conn.execute(
                    'SELECT d.document_key,s.txn_id FROM source_documents d '
                    'JOIN transaction_sources s ON s.document_id=d.document_id '
                    'WHERE d.account_id=? '
                    'UNION SELECT a.alias_key,s.txn_id FROM source_document_aliases a '
                    'JOIN source_documents d ON d.document_id=a.document_id '
                    'JOIN transaction_sources s ON s.document_id=d.document_id '
                    'WHERE a.account_id=? AND d.account_id=?',
                    (selected_account, selected_account, selected_account)).fetchall()
                for key, txn_id in rows:
                    # Registry keys are already canonical StatementsPDF-relative
                    # identities; re-parsing them would discard nested directories.
                    source_ids.setdefault(key, set()).add(txn_id)
                linked_ids = {r[0] for r in conn.execute('SELECT txn_id FROM transaction_sources')}
            for txn_id, source in conn.execute(
                    'SELECT txn_id,source_file FROM transactions WHERE account_id=? AND source_file IS NOT NULL',
                    (selected_account,)):
                if txn_id in linked_ids:
                    continue
                try:
                    source_ids.setdefault(document_identifier(source, statements_root=STATEMENTS_DIR), set()).add(txn_id)
                except (TypeError, ValueError):
                    pass
            imported_counts = {key: len(ids) for key, ids in source_ids.items()}
            conn.close()

        for rel_path, pdf_file in available.items():
            try:
                size_bytes = pdf_file.stat().st_size
            except OSError:
                # A filtered file or its parent may disappear after inventory.
                # Skip only that entry; never fall back to an unfiltered listing.
                continue
            document_key = document_identifier(str(pdf_file), statements_root=STATEMENTS_DIR)
            txn_count = imported_counts.get(document_key, 0)
            pdfs.append({
                "name": pdf_file.name,
                "display_path": pdf_file.relative_to(STATEMENTS_DIR).as_posix(),
                "path": rel_path,
                "size_kb": f"{size_bytes / 1024:.0f}",
                "imported": txn_count > 0,
                "txn_count": txn_count,
                "selected": rel_path == selected_pdf,
            })

    return render_template("ingest.html", active_page="ingest", pdfs=pdfs, result=result,
                           import_preview=import_preview, commit_result=commit_result,
                           commit_account=commit_account,
                           accounts=accounts, selected_account=selected_account, folder_state=folder_state)


# Frozen transfer objects stay on this server, never in browser/session cookies.
_TRANSFER_PREVIEWS = {}
_TRANSFER_TTL = 600
_TRANSFER_MAX_BODY = 4096


def _transfer_json(keys=None):
    if request.content_length is None or request.content_length > _TRANSFER_MAX_BODY:
        raise ValueError('Transfer request is too large or has no bounded size.')
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or (keys is not None and set(data) != set(keys)):
        raise ValueError('Unexpected transfer fields.')
    return data


def _transfer_entry(token):
    import time
    if not isinstance(token, str) or len(token) > 100:
        raise ValueError('Invalid preview token.')
    entry = _TRANSFER_PREVIEWS.get(token)
    if (not entry or entry['owner'] != csrf_token() or entry['expires'] <= time.monotonic()
            or entry['db'] != str(DB_PATH.resolve()) or entry['root'] != str(PROJECT_ROOT.resolve())):
        raise ValueError('Preview expired, unavailable or belongs to another session. Review again.')
    return entry


def _transfer_members(preview):
    from scripts.services import transfers
    with transfers._connection(db_path=DB_PATH) as conn:
        conn.execute('BEGIN')
        transfers._ready(conn)
        if transfers._ledger_version(conn) != preview.ledger_version:
            raise transfers.TransferError('stale_preview')
        members = []
        for txn_id, counterpart in zip(preview.txn_ids, preview.counterparts):
            row = conn.execute('SELECT t.txn_id,t.txn_date,a.display_name,t.amount,t.description_raw '
                'FROM transactions t JOIN accounts a ON a.account_id=t.account_id WHERE t.txn_id=?', (txn_id,)).fetchone()
            name = conn.execute('SELECT display_name FROM accounts WHERE account_id=?', (counterpart,)).fetchone()
            cents = transfers.ledger_pence(row[3])
            members.append(dict(txn_id=row[0], txn_id_text=str(row[0]), cents_text=str(cents), txn_date=row[1], account_name=row[2], cents=cents,
                description=row[4], counterpart_name=name[0], direction='out' if cents < 0 else 'in'))
        return members


@app.route('/api/transfers/preview', methods=['POST'])
def transfer_preview():
    import time
    from scripts.services import transfers
    try:
        data = _transfer_json()
        action = data.get('action')
        if action == 'pair' and set(data) == {'action', 'txn_ids'}:
            ids = data['txn_ids']
            if not isinstance(ids, list) or len(ids) != 2:
                raise ValueError('Choose two transaction IDs.')
            preview = transfers.prepare_pair(*ids, db_path=DB_PATH, root=PROJECT_ROOT)
        elif action == 'unmatched' and set(data) == {'action', 'txn_id', 'counterpart_account_id'}:
            counterpart = data['counterpart_account_id']
            if not isinstance(counterpart, str) or not 1 <= len(counterpart) <= 100:
                raise ValueError('Choose a known owned counter-account.')
            preview = transfers.prepare_unmatched(data['txn_id'], counterpart, db_path=DB_PATH, root=PROJECT_ROOT)
        elif action == 'revoke' and set(data) == {'action', 'group_id'}:
            preview = transfers.prepare_revoke(data['group_id'], db_path=DB_PATH, root=PROJECT_ROOT)
        else:
            raise ValueError('Choose an exact pair, known unmatched transfer or revoke.')
        members = _transfer_members(preview)
        for token, entry in list(_TRANSFER_PREVIEWS.items()):
            if entry['expires'] <= time.monotonic():
                _TRANSFER_PREVIEWS.pop(token, None)
        if len(_TRANSFER_PREVIEWS) >= 1000:
            raise ValueError('Too many pending previews; cancel or wait for expiry.')
        token = secrets.token_urlsafe(32)
        _TRANSFER_PREVIEWS[token] = dict(preview=preview, owner=csrf_token(),
            expires=time.monotonic() + _TRANSFER_TTL, db=str(DB_PATH.resolve()), root=str(PROJECT_ROOT.resolve()))
        return jsonify(ok=True, preview_token=token, action=preview.action, group_id=preview.group_id, members=members)
    except (ValueError, FileNotFoundError) as exc:
        return jsonify(ok=False, error=str(exc)), 400


@app.route('/api/transfers/cancel', methods=['POST'])
def transfer_cancel():
    try:
        data = _transfer_json({'preview_token'})
        _transfer_entry(data['preview_token'])
        _TRANSFER_PREVIEWS.pop(data['preview_token'])
        return jsonify(ok=True, cancelled=True)
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 409


@app.route('/api/transfers/confirm', methods=['POST'])
def transfer_confirm():
    try:
        data = _transfer_json({'preview_token', 'approved'})
        if data['approved'] is not True:
            raise ValueError('Literal approval is required.')
        entry = _transfer_entry(data['preview_token'])
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 409
    from scripts.services import transfers
    # Consume before commit: replay/cancel/stale failures all need a new review.
    _TRANSFER_PREVIEWS.pop(data['preview_token'])
    try:
        result = transfers.commit_transfer(entry['preview'], approved=True)
    except (ValueError, FileNotFoundError) as exc:
        return jsonify(ok=False, error=str(exc)), 409
    warning = None
    try:
        _regenerate_report()
    except Exception:
        app.logger.exception('Report refresh failed after transfer decision was saved')
        warning = 'Transfer decision saved, but reports could not be refreshed.'
    return jsonify(ok=True, saved=True, group_id=result.group_id, action=result.action, warning=warning)


@app.route('/transfers')
def transfers_page():
    # Retired review UI: old bookmarks lead to the existing read-only search.
    filters = {key: request.args[key] for key in ('q', 'start', 'end', 'amount', 'direction')
               if key in request.args}
    return redirect(url_for('transactions_page', **filters))


@app.route('/transactions')
def transactions_page():
    from scripts.services.transaction_search import parse_filters, search_transactions, empty_result
    filters = {key: request.args.get(key, '') for key in ('q', 'start', 'end', 'amount', 'direction')}
    result: dict = empty_result()
    cents = None
    error = None
    try:
        filters, cents, page = parse_filters(request.args)
        if DB_PATH.is_file():
            conn = sqlite3.connect(DB_PATH.resolve().as_uri() + '?mode=ro', uri=True)
            conn.row_factory = sqlite3.Row
            try:
                result = search_transactions(conn, filters, cents, page)
            finally:
                conn.close()
    except ValueError as exc:
        error = str(exc)
    def page_url(page):
        return url_for('transactions_page') + '?' + urlencode({**filters, 'page': page})
    search_token = ''
    if not error and result['count']:
        from scripts.services.search_bulk import hold
        search_token = hold(csrf_token(), 'search', dict(filters=filters, cents=cents,
            snapshot=result['snapshot'], visible_ids=[row['txn_id'] for row in result['rows']],
            db=str(DB_PATH.resolve())))
    cat_tree = _load_cat_tree()
    return render_template('transactions.html', active_page='transactions', filters=filters,
                           result=result, error=error, page_url=page_url, search_token=search_token,
                           cat_tree=cat_tree, categories=sorted(cat_tree)), 400 if error else 200


@app.route('/api/search-bulk-preview', methods=['POST'])
def search_bulk_preview():
    from scripts.services.search_bulk import load, hold, check_snapshot
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or data.get('scope') not in ('all', 'selected'):
        return jsonify(ok=False, error='Choose explicit rows or all matching results.'), 400
    try:
        search = load(data.get('search_token'), csrf_token(), 'search')
        if not DB_PATH.is_file() or search['db'] != str(DB_PATH.resolve()):
            raise ValueError('Reload the search.')
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 409
    category, subcategory = data.get('category'), data.get('subcategory')
    if not isinstance(category, str) or not isinstance(subcategory, str) or subcategory not in _load_cat_tree().get(category, []):
        return jsonify(ok=False, error='Choose an existing Category / Subcategory.'), 400
    snapshot = search['snapshot']
    if data['scope'] == 'selected':
        ids = data.get('txn_ids')
        if (not isinstance(ids, list) or not ids or
            any(type(i) is not int or i not in search['visible_ids'] for i in ids) or len(set(ids)) != len(ids)):
            return jsonify(ok=False, error='Select distinct rows from the displayed page.'), 400
        snapshot = {i: snapshot[i] for i in ids}
    if not snapshot:
        return jsonify(ok=False, error='No transactions selected.'), 400
    state = dict(filters=search['filters'], cents=search['cents'], snapshot=snapshot,
                 scope=data['scope'], category=category, subcategory=subcategory, db=search['db'])
    conn = _get_db()
    try:
        conn.execute('BEGIN')
        check_snapshot(conn, state)
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 409
    finally:
        conn.rollback()
        conn.close()
    token = hold(csrf_token(), 'bulk_preview', state)
    return jsonify(ok=True, preview_token=token, count=len(snapshot), category=category,
                   subcategory=subcategory, scope=data['scope'])


@app.route('/api/search-bulk-apply', methods=['POST'])
def search_bulk_apply():
    from scripts.services.search_bulk import load, discard, apply_bulk
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) != {'preview_token', 'approved'} or data.get('approved') is not True:
        return jsonify(ok=False, error='An explicit approved preview is required.'), 400
    try:
        state = load(data['preview_token'], csrf_token(), 'bulk_preview')
        if not DB_PATH.is_file() or state['db'] != str(DB_PATH.resolve()) or state['subcategory'] not in _load_cat_tree().get(state['category'], []):
            raise ValueError('Definitions changed. Reload and preview again.')
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 409
    conn = _get_db()
    try:
        updated = apply_bulk(conn, state)
    except ValueError as exc:
        conn.rollback()
        return jsonify(ok=False, error=str(exc)), 409
    except Exception:
        conn.rollback()
        app.logger.exception('Search bulk update failed')
        return jsonify(ok=False, error='Save failed; no selected transactions changed.'), 500
    finally:
        conn.close()
    discard(data['preview_token'])
    warning = None
    try:
        _regenerate_report()
    except Exception:
        app.logger.exception('Report refresh failed after search bulk update')
        warning = 'Categories saved, but reports could not be refreshed.'
    return jsonify(ok=True, updated=updated, selected=len(state['snapshot']), warning=warning)


@app.route('/api/search-recategorize', methods=['POST'])
def search_recategorize():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) != {'txn_id', 'old_subcategory_id', 'category', 'subcategory'}:
        return jsonify(ok=False, error='Only a single transaction and existing category choice are accepted.'), 400
    txn_id, old_sid = data['txn_id'], data['old_subcategory_id']
    if type(txn_id) is not int or not 1 <= txn_id <= 9223372036854775807:
        return jsonify(ok=False, error='Invalid transaction ID.'), 400
    if old_sid is not None and (type(old_sid) is not int or not 1 <= old_sid <= 9223372036854775807):
        return jsonify(ok=False, error='Invalid original subcategory.'), 400
    category, subcategory = data['category'], data['subcategory']
    if not isinstance(category, str) or not isinstance(subcategory, str) or subcategory not in _load_cat_tree().get(category, []):
        return jsonify(ok=False, error='Choose an existing Category / Subcategory. Use Manage categories to add definitions.'), 400
    if not DB_PATH.is_file():
        return jsonify(ok=False, error='Ledger is not available.'), 409
    from scripts.services.search_recategorisation import recategorise_one
    conn = _get_db()
    try:
        sid = recategorise_one(conn, txn_id, old_sid, category, subcategory)
    except ValueError as exc:
        conn.rollback()
        return jsonify(ok=False, error=str(exc)), 409
    except Exception:
        conn.rollback()
        app.logger.exception('Search recategorisation failed')
        return jsonify(ok=False, error='Save failed; transaction was not changed.'), 500
    finally:
        conn.close()
    warning = None
    try:
        _regenerate_report()
    except Exception:
        app.logger.exception('Report refresh failed after search recategorisation')
        warning = 'Category saved, but reports could not be refreshed.'
    return jsonify(ok=True, txn_id=txn_id, subcategory_id=sid, warning=warning)


@app.route("/exceptions")
def exceptions_page():
    exceptions = []
    if DB_PATH.exists():
        conn = _get_db()
        rows = conn.execute(
            "SELECT * FROM exceptions WHERE status = 'open' ORDER BY txn_date, merchant_core"
        ).fetchall()
        exceptions = [dict(r) for r in rows]
        from scripts.services.exception_review import review_groups
        groups = review_groups(conn, exceptions)
        conn.close()
    else:
        groups = []

    cat_tree = _load_cat_tree()
    categories = sorted(cat_tree.keys())

    return render_template("exceptions.html", active_page="exceptions",
                           exceptions=exceptions, groups=groups, categories=categories, cat_tree=cat_tree)


@app.route('/api/resolve-selected-exceptions', methods=['POST'])
def resolve_selected_exceptions():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(ok=False, error='A JSON object is required.'), 400
    ids = data.get('exception_ids')
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 500 or
        any(type(i) is not int or not 1 <= i <= 9223372036854775807 for i in ids) or
        len(set(ids)) != len(ids)):
        return jsonify(ok=False, error='Select distinct valid exception rows.'), 400
    category, subcategory = data.get('category'), data.get('subcategory')
    tree = _load_cat_tree()
    if not isinstance(category, str) or not isinstance(subcategory, str) or subcategory not in tree.get(category, []):
        return jsonify(ok=False, error='Choose an existing Category / Subcategory.'), 400
    if not DB_PATH.is_file():
        return jsonify(ok=False, error='Ledger not available.'), 409
    from scripts.services.exception_review import resolve_selected
    conn = _get_db()
    try:
        resolved = resolve_selected(conn, ids, category, subcategory)
    except ValueError as exc:
        conn.rollback()
        return jsonify(ok=False, error=str(exc)), 409
    except Exception:
        conn.rollback()
        app.logger.exception('Selected exception update failed')
        return jsonify(ok=False, error='Update failed; no selected rows were applied.'), 500
    finally:
        conn.close()
    try:
        _regenerate_report()
        warning = None
    except Exception:
        app.logger.exception('Reports could not be refreshed after categorisation')
        warning = 'Categories saved, but reports could not be refreshed.'
    return jsonify(ok=True, resolved_ids=resolved, warning=warning)


@app.route("/api/resolve-exception", methods=["POST"])
def resolve_exception():
    """AJAX endpoint to resolve an exception — assign category/subcategory."""
    data = request.get_json()
    exc_id = data.get("exception_id")
    category = data.get("category", "")
    subcategory = data.get("subcategory", "")
    remember = data.get("remember_rule", False)
    merchant_core = data.get("merchant_core", "")

    if not exc_id or not category or not subcategory:
        return jsonify({"ok": False, "error": "Missing required fields."})

    conn = _get_db()

    try:
        # Ensure category + subcategory exist in DB (auto-seed from categories.json edits)
        conn.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (category,))
        cat_row = conn.execute(
            "SELECT category_id FROM categories WHERE name = ?", (category,)
        ).fetchone()
        cat_id = cat_row["category_id"]
        conn.execute(
            "INSERT OR IGNORE INTO subcategories (category_id, name) VALUES (?, ?)",
            (cat_id, subcategory),
        )
        sub_row = conn.execute(
            "SELECT subcategory_id FROM subcategories WHERE category_id = ? AND name = ?",
            (cat_id, subcategory),
        ).fetchone()
        subcat_id = sub_row["subcategory_id"]

        # Get the exception
        exc = conn.execute(
            "SELECT * FROM exceptions WHERE exception_id = ?", (exc_id,)
        ).fetchone()
        if not exc:
            return jsonify({"ok": False, "error": "Exception not found."})

        # Update the transaction's subcategory
        if exc["txn_id"]:
            conn.execute(
                "UPDATE transactions SET subcategory_id = ?, merchant_core = ? WHERE txn_id = ?",
                (subcat_id, merchant_core, exc["txn_id"]),
            )

        # Mark exception resolved
        conn.execute(
            "UPDATE exceptions SET status = 'resolved', resolved_at = strftime('%Y-%m-%dT%H:%M:%S','now') "
            "WHERE exception_id = ?",
            (exc_id,),
        )

        # Resolve only the clicked exception. Similar merchants may need different categories,
        # so matching rows must remain open until the user applies them explicitly.
        bulk_resolved = []

        # Remember rule — add to category_rules.csv
        if remember and merchant_core:
            _add_category_rule(merchant_core, category, subcategory)

        conn.commit()
        warning = None
        try:
            _regenerate_report()
        except Exception:
            app.logger.exception('Report refresh failed after exception Apply')
            warning = 'Category saved, but reports could not be refreshed.'
        return jsonify({"ok": True, "bulk_resolved": bulk_resolved, "warning": warning})

    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)})
    finally:
        conn.close()


def _add_category_rule(merchant_core: str, category: str, subcategory: str):
    """Append a new rule to category_rules.csv (if not already present)."""
    existing = set()
    if RULES_CSV.exists():
        with open(RULES_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing.add(row.get("merchant_core", "").strip().upper())

    mc_upper = merchant_core.strip().upper()
    if mc_upper in existing:
        return  # already has a rule

    with open(RULES_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([merchant_core, "", category, subcategory, "auto-added by dashboard"])


@app.route("/api/update-note", methods=["POST"])
def api_update_note():
    """Update the user_note on a single transaction."""
    data = request.get_json()
    txn_id = data.get("txn_id")
    if len(data['note']) > 10000:
        return jsonify(ok=False, error='Note must be at most 10000 characters.'), 400
    if 'expected_note' in data and data['expected_note'] is not None and not isinstance(data['expected_note'], str):
        return jsonify(ok=False, error='Original note must be a string or null.'), 400
    note = data['note'].strip() or None  # preserve legacy empty-note semantics
    if not DB_PATH.is_file():
        return jsonify(ok=False, error='Ledger is not available.'), 409
    conn = _get_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        current = conn.execute('SELECT user_note FROM transactions WHERE txn_id=?', (txn_id,)).fetchone()
        if current is None:
            conn.rollback()
            return jsonify(ok=False, error='Transaction is no longer available.'), 409
        if 'expected_note' in data and current['user_note'] != data['expected_note']:
            conn.rollback()
            return jsonify(ok=False, error='Note changed since this report loaded. Reload before editing.'), 409
        conn.execute("UPDATE transactions SET user_note = ? WHERE txn_id = ?", (note, txn_id))
        conn.commit()
    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)})
    finally:
        conn.close()
    warning = None
    try:
        _regenerate_report()
    except Exception:
        app.logger.exception('Report refresh failed after note save')
        warning = 'Note saved, but stored reports could not be refreshed. Reload the dashboard report for the current policy view.'
    return jsonify(ok=True, warning=warning)


@app.route('/api/manage-categories', methods=['POST'])
def manage_categories():
    from scripts.services.category_manager import add_definitions
    try:
        tree, category, subs = add_definitions(CATEGORIES_JSON, request.get_json(silent=True))
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    except Exception:
        app.logger.exception('Category definition save failed')
        return jsonify(ok=False, error='Could not save category definitions. Please retry.'), 500
    return jsonify(ok=True, tree=tree, category=category, added=subs)


@app.route("/api/categories")
def api_categories():
    """Return the full category tree plus a flat lookup of subcategory_ids."""
    cat_tree = _load_cat_tree()
    # Build subcategory_id lookup from DB
    id_map = {}  # {"Category|Subcategory": subcategory_id}
    if DB_PATH.exists():
        conn = _get_db()
        rows = conn.execute(
            "SELECT c.name AS cat, s.name AS sub, s.subcategory_id "
            "FROM subcategories s JOIN categories c ON s.category_id = c.category_id"
        ).fetchall()
        for r in rows:
            id_map[f"{r['cat']}|{r['sub']}"] = r["subcategory_id"]
        conn.close()
    return jsonify({"tree": cat_tree, "id_map": id_map})


@app.route("/api/recategorize", methods=["POST"])
def api_recategorize():
    """Re-assign category/subcategory for one transaction or all with same merchant_core."""
    data = request.get_json()
    txn_id = data.get("txn_id")
    category = data.get("category", "")
    subcategory = data.get("subcategory", "")
    new_subcat_name = data.get("new_subcategory", "").strip()
    scope = data.get("scope", "single")        # 'single' | 'merchant'
    remember_rule = data.get("remember_rule", False)

    if not txn_id or not category:
        return jsonify({"ok": False, "error": "Missing required fields."})

    # If creating a new subcategory, use that name
    if new_subcat_name:
        subcategory = new_subcat_name

    if not subcategory:
        return jsonify({"ok": False, "error": "Subcategory is required."})

    conn = _get_db()
    try:
        # ── Ensure category + subcategory exist in DB and categories.json ──
        # Category
        conn.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (category,))
        cat_row = conn.execute(
            "SELECT category_id FROM categories WHERE name = ?", (category,)
        ).fetchone()
        cat_id = cat_row["category_id"]

        # Subcategory
        conn.execute(
            "INSERT OR IGNORE INTO subcategories (category_id, name) VALUES (?, ?)",
            (cat_id, subcategory),
        )
        sub_row = conn.execute(
            "SELECT subcategory_id FROM subcategories WHERE category_id = ? AND name = ?",
            (cat_id, subcategory),
        ).fetchone()
        new_subcat_id = sub_row["subcategory_id"]

        # Update categories.json if new subcategory was added
        cat_tree = _load_cat_tree()
        if category not in cat_tree:
            cat_tree[category] = []
        if subcategory not in cat_tree[category]:
            cat_tree[category].append(subcategory)
            with open(CATEGORIES_JSON, "w", encoding="utf-8") as f:
                json.dump(cat_tree, f, indent=2, ensure_ascii=False)
                f.write("\n")

        # ── Get current transaction info for audit log ──
        txn = conn.execute(
            "SELECT txn_id, subcategory_id, merchant_core FROM transactions WHERE txn_id = ?",
            (txn_id,),
        ).fetchone()
        if not txn:
            return jsonify({"ok": False, "error": "Transaction not found."})

        old_subcat_id = txn["subcategory_id"]
        merchant = txn["merchant_core"] or ""

        # Resolve old category name for audit
        old_cat_label = "Uncategorised"
        if old_subcat_id:
            old_info = conn.execute(
                "SELECT c.name AS cat, s.name AS sub FROM subcategories s "
                "JOIN categories c ON s.category_id = c.category_id "
                "WHERE s.subcategory_id = ?", (old_subcat_id,)
            ).fetchone()
            if old_info:
                old_cat_label = f"{old_info['cat']} / {old_info['sub']}"
        new_cat_label = f"{category} / {subcategory}"

        # ── Determine which transactions to update ──
        if scope == "merchant" and merchant:
            affected = conn.execute(
                "SELECT txn_id, subcategory_id FROM transactions WHERE merchant_core = ?",
                (merchant,),
            ).fetchall()
        else:
            affected = [txn]

        # ── Update transactions + write audit log ──
        updated_count = 0
        for t in affected:
            tid = t["txn_id"]
            old_sid = t["subcategory_id"]
            if old_sid == new_subcat_id:
                continue  # already correct
            conn.execute(
                "UPDATE transactions SET subcategory_id = ? WHERE txn_id = ?",
                (new_subcat_id, tid),
            )
            # Audit log
            old_label = old_cat_label
            if tid != txn_id and old_sid:
                oi = conn.execute(
                    "SELECT c.name AS cat, s.name AS sub FROM subcategories s "
                    "JOIN categories c ON s.category_id = c.category_id "
                    "WHERE s.subcategory_id = ?", (old_sid,)
                ).fetchone()
                if oi:
                    old_label = f"{oi['cat']} / {oi['sub']}"
            conn.execute(
                "INSERT INTO edit_log (txn_id, field_changed, old_value, new_value, "
                "old_category, new_category, merchant_core, scope) "
                "VALUES (?, 'subcategory_id', ?, ?, ?, ?, ?, ?)",
                (tid, str(old_sid), str(new_subcat_id), old_label, new_cat_label, merchant, scope),
            )
            updated_count += 1

        # ── Remember rule ──
        if remember_rule and merchant:
            _update_category_rule(merchant, category, subcategory)

        conn.commit()

        # ── Regenerate report ──
        _regenerate_report()

        return jsonify({"ok": True, "updated": updated_count, "merchant_core": merchant})

    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)})
    finally:
        conn.close()


def _update_category_rule(merchant_core: str, category: str, subcategory: str):
    """Add or update a rule in category_rules.csv for the given merchant."""
    rows = []
    updated = False
    mc_upper = merchant_core.strip().upper()

    if RULES_CSV.exists():
        with open(RULES_CSV, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            for row in reader:
                if row.get("merchant_core", "").strip().upper() == mc_upper:
                    row["category"] = category
                    row["subcategory"] = subcategory
                    row["notes"] = row.get("notes", "") or "updated by dashboard"
                    updated = True
                rows.append(row)

    if updated:
        with open(RULES_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)
    else:
        _add_category_rule(merchant_core, category, subcategory)


# ── Expenditure Planning ─────────────────────────────────────

@app.route("/planning")
def planning():
    """Expenditure Planning page — monthly spend grid with category toggles."""
    conn = _get_db() if DB_PATH.exists() else None
    try:
        account_id, accounts = _dashboard_account_scope(conn)
        period = _dashboard_period()
    except ValueError as error:
        return str(error), 400
    finally:
        if conn is not None:
            conn.close()
    return render_template('planning.html', active_page='planning', accounts=accounts,
                           account_id=account_id or '', period=period)


@app.route("/api/planning-data")
def api_planning_data():
    """Return expenditure data grouped by category × month for the planning grid.

    Response JSON:
      categories: [str]          — sorted category names
      months:     [str]          — sorted YYYY-MM strings
      grid:       {cat: {month: total}}  — absolute spend (positive numbers)
    """
    if not DB_PATH.exists():
        try:
            _dashboard_account_scope(None)
            period = _dashboard_period()
        except ValueError as error:
            return jsonify(error=str(error)), 400
        movement = _missing_dashboard_movement()
        return jsonify(categories=[], months=[], grid={}, grid_pence={}, grid_pence_text={},
                       movement=movement, movement_pence_text={key: {name: str(value) for name, value in movement[key].items()
                                                                    if name.endswith('_pence')}
                                                              for key in ('raw', 'adjusted', 'transfers')},
                       filters={'account_id': '', 'start': period['start'], 'end': period['end'],
                                'period': period['kind'], 'method': 'transaction_date'},
                       complete_months=[], coverage=[], warnings=['database_missing', 'No complete covered months available for averages'],
                       method={'calculation_basis': 'transfer_adjusted', 'include_excluded': False,
                               'meaning': 'Cash movement, not inferred income or consumption'})

    conn = _get_db()
    try:
        with read_snapshot(conn=conn):
            account_id, accounts = _dashboard_account_scope(conn)
            period = _dashboard_period()
            rows = _dashboard_movement_rows(conn, account_id, accounts, period)
            baseline = _home_average_stats(conn, account_id, accounts, period)
            movement = summarise_movement([r['txn_id'] for r in rows], conn=conn, include_excluded=False)
            debit_groups = {}
            for row in rows:
                if ledger_pence(row['amount']) < 0 and not row['excluded']:
                    debit_groups.setdefault((row['category'], row['month']), []).append(row['txn_id'])
            grid_pence = {}
            for (category, month), txn_ids in debit_groups.items():
                # The shared policy, not category names or a separate principal rule,
                # determines each cell. Negative-only groups preserve fee movement.
                cents = summarise_movement(txn_ids, conn=conn, include_excluded=False)['adjusted']['debits_pence']
                if cents:
                    grid_pence.setdefault(category, {})[month] = cents
    except ValueError as error:
        return jsonify(error=str(error)), 400
    finally:
        conn.close()
    grid = {cat: {month: float(pence_decimal(value)) for month, value in values.items()}
            for cat, values in grid_pence.items()}
    return jsonify(categories=sorted(grid), months=sorted({r['month'] for r in rows} | set(baseline['months'])),
                   grid=grid, grid_pence=grid_pence, movement=movement,
                   movement_pence_text={key: {name: str(value) for name, value in movement[key].items()
                                              if name.endswith('_pence')}
                                        for key in ('raw', 'adjusted', 'transfers')},
                   grid_pence_text={cat: {month: str(value) for month, value in values.items()}
                                    for cat, values in grid_pence.items()},
                   filters={'account_id': account_id or '', 'start': period['start'], 'end': period['end'],
                            'period': period['kind'], 'method': 'transaction_date'},
                   complete_months=baseline['complete_months'], coverage=baseline['coverage_by_account'],
                   warnings=sorted(set(baseline['average_warnings']) | set(movement['warnings'])),
                   method={'calculation_basis': 'transfer_adjusted', 'include_excluded': False,
                           'meaning': 'Cash movement, not inferred income or consumption'})


# ── Local safety: no startup migration, import or automatic backup ──


def _snapshot_counts(snapshot):
    from contextlib import closing
    uri = (snapshot / 'finance' / 'ledger.db').resolve().as_uri() + '?mode=ro&immutable=1'
    counts = {}
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        available = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ('transactions', 'exceptions', 'edit_log', 'import_log'):
            if table in available:
                counts[table] = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    return counts


def _snapshot_records():
    from scripts.backup_ledger import verify_backup
    parent = get_settings(PROJECT_ROOT).backup_dir
    records = []
    if parent.is_dir():
        for snapshot in sorted(parent.iterdir(), reverse=True):
            if not snapshot.is_dir() or snapshot.name.startswith('.'):
                continue
            try:
                manifest = verify_backup(snapshot)
                records.append({'path': snapshot, 'created_at': manifest['created_at'],
                                'status': 'Validated', 'counts': _snapshot_counts(snapshot)})
            except (OSError, ValueError, sqlite3.Error):
                records.append({'path': snapshot, 'created_at': 'Unknown', 'status': 'Validation failed', 'counts': {}})
    from datetime import datetime, timezone
    records.sort(key=lambda record: datetime.fromisoformat(record['created_at']) if record['status'] == 'Validated' else datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return records


@app.route('/safety')
def safety():
    parent = get_settings(PROJECT_ROOT).backup_dir
    restore_status = 'Restore check not run yet.'
    try:
        state = json.loads((parent / 'state.json').read_text(encoding='utf-8'))
        check = state['restore_check']
        restore_status = ('Restore check passed' if check['status'] == 'passed' else 'Restore check failed') + ' · ' + check['checked_at']
    except (OSError, ValueError, KeyError, TypeError):
        pass
    # Read-only verification; never creates a database, snapshot or state file.
    with MUTATION_LOCK:
        records = _snapshot_records()
    return render_template('safety.html', active_page='safety', records=records, restore_status=restore_status)


@app.route('/safety/backup', methods=['POST'])
def safety_backup():
    from scripts.backup_ledger import create_backup, verify_backup
    try:
        snapshot = create_backup(root=PROJECT_ROOT, db_path=DB_PATH)
        verify_backup(snapshot)
        flash('Local snapshot created and validated (unencrypted).', 'success')
    except (OSError, ValueError, sqlite3.Error):
        flash('Snapshot failed. Check the local ledger/config and available disk space; no backup was accepted.', 'error')
    return redirect(url_for('safety'))


@app.route('/safety/restore-check', methods=['POST'])
def safety_restore_check():
    import tempfile
    from datetime import datetime, timezone
    from scripts.backup_ledger import restore_backup, verify_backup
    if request.form.get('approve_restore') != 'yes':
        return 'Explicit approval is required to create and delete a temporary verification copy.', 400
    paths = get_settings(PROJECT_ROOT)
    parent = paths.restore_check_dir
    # Resolve the whole backup root, not just a selected snapshot: a Windows
    # junction can place backups outside the lexical live-project directory.
    protected = (paths.root.resolve(), paths.backup_dir.resolve())
    if any(parent.is_relative_to(root) or root.is_relative_to(parent) for root in protected):
        return 'Restore-check directory must be outside the live project and backup root, and not an ancestor of either. Set BANKSTATEMENT_RESTORE_CHECK_DIR to a dedicated isolated directory.', 400
    valid = [record for record in _snapshot_records() if record['status'] == 'Validated']
    if not valid:
        return 'Create a validated local snapshot first.', 400
    snapshot = valid[0]['path']
    state = {'restore_check': {'status': 'failed', 'checked_at': datetime.now(timezone.utc).isoformat(),
                               'snapshot': snapshot.name, 'counts': {}}}
    try:
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='restore-check-', dir=parent) as owned:
            restored = restore_backup(snapshot, Path(owned) / 'verified', root=paths.root)
            verify_backup(restored)
            state['restore_check']['counts'] = _snapshot_counts(restored)
        state['restore_check']['status'] = 'passed'
        flash('Restore check passed; the isolated copy was deleted. Live data unchanged.', 'success')
    except (OSError, ValueError, sqlite3.Error):
        flash('Restore check failed; live data was not overwritten.', 'error')
    # State is outside snapshot inventories. Atomic replacement avoids partial JSON.
    state_path = paths.backup_dir / 'state.json'
    pending = state_path.with_suffix('.json.tmp')
    try:
        pending.write_text(json.dumps(state, indent=2) + '\n', encoding='utf-8')
        pending.replace(state_path)
    except OSError:
        flash('Unable to save restore-check status.', 'error')
    return redirect(url_for('safety'))


# ── Report registry ──────────────────────────────────────────
REPORT_REGISTRY = [
    ("income_vs_expense",        "Income vs Expenditure"),
    ("income_by_category",       "Income by Source"),
    ("expenditure_by_category",  "Expenditure by Category"),
    ("expenditure_heatmap",      "Expenditure Heatmap"),
]
MONTH_OPTIONS = (
    (1, 'January'), (2, 'February'), (3, 'March'), (4, 'April'),
    (5, 'May'), (6, 'June'), (7, 'July'), (8, 'August'),
    (9, 'September'), (10, 'October'), (11, 'November'), (12, 'December'),
)


def _report_year_options(conn=None, selected_year: str | None = None) -> list[int]:
    years: set[int] = set()
    close_conn = False
    if conn is None and DB_PATH.exists():
        conn = _get_db()
        close_conn = True
    try:
        if conn is not None:
            for row in conn.execute(
                "SELECT DISTINCT substr(txn_date, 1, 4) FROM transactions WHERE txn_date IS NOT NULL"
            ):
                value = row[0]
                if value and value.isdigit():
                    years.add(int(value))
            if _table_exists(conn, 'source_documents'):
                for row in conn.execute(
                    "SELECT coverage_start, coverage_end FROM source_documents "
                    "WHERE reconciliation_status='passed' AND coverage_start IS NOT NULL AND coverage_end IS NOT NULL"
                ):
                    for value in row:
                        if value and len(value) >= 4 and value[:4].isdigit():
                            years.add(int(value[:4]))
    finally:
        if close_conn:
            conn.close()
    if selected_year and selected_year.isdigit():
        years.add(int(selected_year))
    return sorted(years)


def _report_period_context(args):
    period = (args.get('period') or 'all').strip().lower()
    context = {
        'kind': period,
        'year': args.get('year', ''),
        'month': args.get('month', ''),
        'start': args.get('start', ''),
        'end': args.get('end', ''),
        'label': 'All dates',
        'range': 'All available transactions',
        'method': 'Transaction date',
        'error': None,
        'query': '',
    }
    if period == 'all':
        return context
    try:
        if period == 'year':
            resolved = resolve_period_filter(PeriodFilter(kind='year', year=int(args.get('year', ''))))
        elif period == 'month':
            resolved = resolve_period_filter(PeriodFilter(kind='month', year=int(args.get('year', '')), month=int(args.get('month', ''))))
        elif period == 'custom':
            resolved = resolve_period_filter(PeriodFilter(kind='custom', start=args.get('start'), end=args.get('end')))
        else:
            raise PeriodFilterError('Unsupported period kind')
    except (PeriodFilterError, TypeError, ValueError):
        context['kind'] = 'all'
        context['error'] = 'Invalid period filter; showing all dates.'
        return context
    context.update({
        'label': resolved.label,
        'range': f'{resolved.start} to {resolved.end}',
        'start': resolved.start,
        'end': resolved.end,
        'method': 'Transaction date' if resolved.method == 'transaction_date' else 'Statement period',
    })
    query = {'period': period}
    for key in ('year', 'month', 'start', 'end'):
        value = args.get(key)
        if value:
            query[key] = value
    context['query'] = urlencode(query)
    return context


def _replace_report_json_assignment(html: str, name: str, value) -> str:
    from scripts.report_assets import script_json

    pattern = re.compile(rf'(const\s+{re.escape(name)}\s*=\s*)')
    decoder = json.JSONDecoder()
    match = pattern.search(html)
    if not match:
        return html
    try:
        _, end = decoder.raw_decode(html, match.end())
    except ValueError:
        return html
    if not html[end:].lstrip().startswith(';'):
        return html
    return html[:match.end()] + script_json(value) + html[end:]


def _apply_raw_report_period(html: str, period_context: dict[str, object]) -> str:
    if period_context.get('kind') == 'all' or period_context.get('error'):
        return html
    start = str(period_context.get('start') or '')
    end = str(period_context.get('end') or '')
    if not start or not end:
        return html

    decoder = json.JSONDecoder()
    txns_match = re.search(r'(const\s+ALL_TXNS\s*=\s*)', html)
    if not txns_match:
        return html
    try:
        txns, _ = decoder.raw_decode(html, txns_match.end())
    except ValueError:
        return html
    if not isinstance(txns, list):
        return html

    filtered = [t for t in txns if isinstance(t, dict) and start <= str(t.get('date', '')) <= end]
    months = sorted({str(t.get('statement_date')) for t in filtered if t.get('statement_date')})
    html = _replace_report_json_assignment(html, 'ALL_TXNS', filtered)
    html = _replace_report_json_assignment(html, 'ALL_MONTHS', months)

    hide_controls = '<style data-dashboard-period-filtered="true">.date-bar,#subtitle{display:none!important;}</style>'
    if '</head>' in html:
        html = html.replace('</head>', hide_controls + '</head>', 1)
    else:
        html = hide_controls + html
    return html


def _regenerate_report():
    """Re-run all report generators to update the static HTML."""
    from scripts.report_income_vs_expense import _query_all
    from scripts.report_income_vs_expense import _build_html as build_ive
    from scripts.report_income_by_category import _build_html as build_ibc
    from scripts.report_expenditure_by_category import _build_html as build_ebc
    from scripts.report_expenditure_heatmap import _build_html as build_ehm
    txns, months = _query_all()
    if months:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        (REPORTS_DIR / "income_vs_expense.html").write_text(
            build_ive(txns, months), encoding="utf-8")
        (REPORTS_DIR / "income_by_category.html").write_text(
            build_ibc(txns, months), encoding="utf-8")
        (REPORTS_DIR / "expenditure_by_category.html").write_text(
            build_ebc(txns, months), encoding="utf-8")
        (REPORTS_DIR / "expenditure_heatmap.html").write_text(
            build_ehm(txns, months), encoding="utf-8")


@app.route("/report")
def report():
    """Show the report wrapped in the dashboard nav with a selector dropdown."""
    name = request.args.get("name", REPORT_REGISTRY[0][0])
    # Validate
    valid_keys = {k for k, _ in REPORT_REGISTRY}
    if name not in valid_keys:
        name = REPORT_REGISTRY[0][0]
    conn = _get_db() if DB_PATH.is_file() else None
    try:
        selected, accounts = _dashboard_account_scope(conn)
        period_context = _dashboard_period()
    except ValueError as exc:
        return str(exc), 400
    finally:
        if conn is not None:
            conn.close()
    report_path = REPORTS_DIR / f"{name}.html"
    if not report_path.exists():
        flash("Report not found. Please generate it first.", "error")
        return redirect(url_for("home"))
    report_labels = dict(REPORT_REGISTRY)
    return render_template("report.html", active_page="report",
                           reports=REPORT_REGISTRY, current_report=name,
                           current_report_label=report_labels[name],
                           period=period_context, month_options=MONTH_OPTIONS,
                           accounts=accounts, account_id=selected or '',
                           year_options=_report_year_options(selected_year=period_context.get('year')))


@app.route("/report/raw/<name>")
def report_raw(name):
    """Serve a raw HTML report by name (used by the iframe)."""
    valid_keys = {k for k, _ in REPORT_REGISTRY}
    if name not in valid_keys:
        return "Unknown report.", 404
    from scripts.report_assets import serve_report_html
    conn = _get_db() if DB_PATH.is_file() else None
    try:
        selected, accounts = _dashboard_account_scope(conn)
        period = _dashboard_period()
        report_path = REPORTS_DIR / f"{name}.html"
        if not report_path.exists():
            return "Report not found.", 404
        html = report_path.read_text(encoding='utf-8')
        if conn is None:
            return "Ledger unavailable; cannot verify current report policy.", 409
        ids = [selected] if selected else [a['account_id'] for a in accounts]
        html = serve_report_html(html, conn=conn, account_ids=ids, period=period, name=name,
                                 legacy_all_accounts=selected is None)
        # Scope filtering is complete in the adapter. Do not run a second legacy
        # JSON rewrite, which can mistake assignment-looking note text for code.
        return app.response_class(html, mimetype='text/html')
    except ValueError as exc:
        return str(exc), 400
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    from scripts.serve_dashboard import main
    raise SystemExit(main())
