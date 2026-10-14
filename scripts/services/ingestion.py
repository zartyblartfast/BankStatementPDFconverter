"""Read-only, signed, server-held import previews (no implicit migrations).

Public API:
 prepare_import(pdf_path, *, db_path, root, account_id=None, extraction=None)
   -> ImportPreview. Explicit paths only; extraction is a TRUSTED Python-only
   ExtractionResult test seam, NEVER a browser/JSON input. The frozen preview
   snapshots all payloads and is authenticated with a process-local HMAC.
 preview_summary(preview) -> safe counts/status/evidence; no financial rows.
 commit_import(preview, *, approved=False) -> CommitResult (implemented below).
   No path/account/payload override. UI must keep preview objects server-side;
   browser submits only an opaque token + approval, not reconstructed objects.
 ImportServiceError.code: approval_required, invalid_preview, blocked,
   schema_not_ready, stale_preview. No audited override exists in M1.

Preparation opens an existing ledger mode=ro in a read transaction. Full logical
ledger/schema fingerprint includes user edits, resolved exceptions and future
columns/tables. Commit checks that fingerprint under BEGIN IMMEDIATE before any
writes. File/config versions are checked there too. No exports/config writes.
"""
from contextlib import closing
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import sqlite3

from scripts.apply_categories import _load_rules
from scripts.settings import get_settings
from scripts.triage_descriptions import decompose_description
from scripts.services.accounts import list_accounts
from scripts.services.extraction import ExtractionResult, extract_statement
from scripts.services.reconciliation import reconcile_transactions
from scripts.services.source_matching import (SourceRow, ExistingRow, MatchPlan,
    document_identifier, source_identity, match_source_rows)


def available_import_accounts(*, db_path=None, conn=None):
    """Read registry/observed identities; never create accounts or migrate.

    Historical UK review is the only pre-registry compatibility path, and
    requires actual, exclusively GBP transaction evidence for that exact ID.
    """
    try:
        accounts = list_accounts(db_path=db_path, conn=conn, allow_legacy=True)
    except (FileNotFoundError, ValueError, sqlite3.OperationalError):
        return []
    for account in accounts:
        if account.get('setup_pending') and account['account_id'] == 'HSBC_UK' and account['currency'] == 'GBP' and account['parser_key'] is None:
            # Only inferred records lack parser policy; never override a registry.
            if conn is not None:
                registered = conn.execute("SELECT 1 FROM sqlite_master WHERE name='accounts' AND type='table'").fetchone()
            else:
                with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True)) as connection:
                    registered = connection.execute("SELECT 1 FROM sqlite_master WHERE name='accounts' AND type='table'").fetchone()
            if not registered:
                account.update(display_name='HSBC UK (inferred; setup pending)', import_enabled=True)
    return accounts

_KEY = secrets.token_bytes(32)
_METADATA_TABLES = {'source_documents', 'source_document_aliases', 'source_document_contents',
                    'transaction_sources', 'import_batches'}
_SOURCE_FIELDS = {'txn_id', 'txn_date', 'description_raw', 'amount', 'currency', 'channel',
                  'merchant_core', 'account_id', 'source_file', 'page', 'row_ref', 'import_hash'}


class ImportServiceError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def to_dict(self):
        return {'error': {'code': self.code}}


@dataclass(frozen=True)
class ImportPreview:
    token: str
    db_path: str = field(repr=False)
    root: str = field(repr=False)
    pdf_path: str = field(repr=False)
    account_id: str
    document_key: str = field(repr=False)
    document_id: int | None
    content_hash: str = field(repr=False)
    file_versions: tuple[tuple[str, str], ...] = field(repr=False)
    ledger_version: str = field(repr=False)
    rows: tuple[SourceRow, ...] = field(repr=False)
    plan: MatchPlan
    schema_ready: bool
    status: str
    reasons: tuple[str, ...]
    reconciliation_status: str
    reconciliation_reasons: tuple[str, ...]
    coverage: tuple[tuple[str, str], ...]
    categories: tuple[tuple[str, tuple[str, ...]], ...] = field(repr=False)
    changed_source_fields: int = 0
    overlaps: int = 0
    categorised: int = 0
    uncategorised: int = 0
    account_display_name: str = ''
    signature: str = field(default='', repr=False)


@dataclass(frozen=True)
class CommitResult:
    batch_id: int
    document_id: int
    added: int
    matched: int
    total_current: int
    exceptions_created: int
    replayed: bool = False

    def to_dict(self):
        return asdict(self)


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     default=repr, separators=(',', ':')).encode()).hexdigest()


def _signature(preview: ImportPreview) -> str:
    payload = asdict(preview)
    payload.pop('signature')
    return hmac.new(_KEY, _digest(payload).encode(), hashlib.sha256).hexdigest()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else 'missing'


def _ledger_version(conn) -> str:
    schema = conn.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    tables = [r[1] for r in schema if r[0] == 'table' and not r[1].startswith('sqlite_')]
    payload = [tuple(tuple(r) for r in schema)]
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        rows = [tuple(r) for r in conn.execute('SELECT * FROM ' + quoted)]
        payload.append((table, sorted(rows, key=repr)))
    return _digest(payload)


def _schema_ready(conn) -> bool:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not _METADATA_TABLES.issubset(tables) or 'schema_migrations' not in tables:
        return False
    migration = Path(__file__).resolve().parents[1] / 'migrations/001_import_metadata.sql'
    row = conn.execute('SELECT name,checksum FROM schema_migrations WHERE version=1').fetchone()
    return bool(row and tuple(row) == (migration.name, _file_hash(migration)))


def _cents(value) -> int:
    number = Decimal(str(value))
    if not number.is_finite() or abs(number) >= Decimal('10000000000'):
        raise ValueError('Invalid monetary amount')
    return int(number.quantize(Decimal('.01'), rounding=ROUND_HALF_UP) * 100)


def _canonical_date(value: str) -> str:
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('Invalid ISO transaction date')
    return value


def _row_from_ledger(row) -> SourceRow:
    return SourceRow(row['txn_date'], row['description_raw'], _cents(row['amount']), row['currency'],
                     row['page'], row['row_ref'], row['channel'], row['merchant_core'])


def _related_metadata(conn):
    """Meaningful decision state, without per-occurrence IDs or timestamps.

    Exception detail JSON is canonicalised so formatting/key order do not turn
    otherwise equal duplicates into edited duplicates. Audit event order is kept,
    but generated IDs/times are not themselves a user decision.
    """
    related = {}
    for table, ignored, order in (
        ('exceptions', {'exception_id', 'txn_id', 'created_at', 'resolved_at'}, 'exception_id'),
        ('edit_log', {'edit_id', 'txn_id', 'changed_at'}, 'edit_id'),
    ):
        for row in conn.execute(f'SELECT * FROM {table} ORDER BY {order}'):
            state = {k: row[k] for k in row.keys() if k not in ignored}
            if table == 'exceptions' and state.get('detail') is not None:
                try:
                    state['detail'] = json.loads(state['detail'])
                except (ValueError, TypeError):
                    pass  # Preserve malformed/non-JSON detail as meaningful text.
            related.setdefault(row['txn_id'], {}).setdefault(table, []).append(state)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if {'transfer_groups', 'transfer_members', 'transfer_audit'} <= tables:
        audits = {}
        for audit in conn.execute('SELECT group_id,action,decision_json FROM transfer_audit ORDER BY audit_id'):
            try:
                decision = json.loads(audit['decision_json'])
                # Stable choices/financial evidence matter; generated membership,
                # approval tokens/signatures and self-occurrence IDs do not.
                decision.pop('txn_ids', None)
                if isinstance(decision.get('members'), list):
                    decision['members'] = sorted(
                        [{k:v for k,v in member.items() if k != 'txn_id'} for member in decision['members']],
                        key=_digest)
            except (ValueError, TypeError, AttributeError):
                decision = audit['decision_json']
            audits.setdefault(audit['group_id'], []).append({'action': audit['action'], 'decision': decision})
        for member in conn.execute('SELECT m.txn_id,m.group_id,m.active,m.counterpart_account_id,g.status '
                                   'FROM transfer_members m JOIN transfer_groups g ON g.group_id=m.group_id '
                                   'ORDER BY m.member_id'):
            state = {'active': member['active'], 'counterpart_account_id': member['counterpart_account_id'],
                     'status': member['status'], 'audit': audits.get(member['group_id'], [])}
            related.setdefault(member['txn_id'], {}).setdefault('transfers', []).append(state)
    for state in related.values():
        if 'exceptions' in state:
            state['exceptions'].sort(key=_digest)
    return related


def _source_accounts(conn, key, content_hash, ready, statements_root=None):
    """Keep independent identity evidence; selected ownership is not an override.

    Any other-account owner requires review, even if this path or the same bytes
    are also recorded for the selected account. Financial-row equality is not
    identity evidence and is deliberately absent from these checks.
    """
    evidence = {'path': set(), 'content': set(), 'legacy': set()}
    if ready:
        evidence['path'].update(r[0] for r in conn.execute(
            'SELECT account_id FROM source_documents WHERE document_key=? '
            'UNION SELECT account_id FROM source_document_aliases WHERE alias_key=?', (key, key)))
        evidence['content'].update(r[0] for r in conn.execute(
            'SELECT d.account_id FROM source_documents d JOIN source_document_contents c '
            'ON c.document_id=d.document_id WHERE c.content_hash=?', (content_hash,)))
        linked = {r[0] for r in conn.execute('SELECT txn_id FROM transaction_sources')}
    else:
        linked = set()
    for txn_id, account_id, source_file in conn.execute(
            'SELECT txn_id,account_id,source_file FROM transactions WHERE source_file IS NOT NULL'):
        if txn_id in linked:
            continue
        legacy_key = document_identifier(source_file, statements_root=statements_root)
        if legacy_key == key or ('/' in key and legacy_key == key.rsplit('/', 1)[-1]):
            evidence['legacy'].add(account_id)
    return evidence


def _select_source(conn, account, key, content_hash, ready, statements_root=None):
    all_rows = [dict(r) for r in conn.execute('SELECT * FROM transactions WHERE account_id=? ORDER BY txn_id', (account,))]
    doc_ids = set()
    if ready:
        doc_ids.update(r[0] for r in conn.execute(
            'SELECT document_id FROM source_document_aliases WHERE account_id=? AND alias_key=?', (account, key)))
        doc_ids.update(r[0] for r in conn.execute(
            'SELECT document_id FROM source_documents WHERE account_id=? AND document_key=?', (account, key)))
        doc_ids.update(r[0] for r in conn.execute(
            'SELECT d.document_id FROM source_documents d JOIN source_document_contents c ON c.document_id=d.document_id '
            'WHERE d.account_id=? AND c.content_hash=?', (account, content_hash)))
    linked_ids = {r[0] for r in conn.execute('SELECT txn_id FROM transaction_sources')} if ready else set()
    legacy = [r for r in all_rows if r['txn_id'] not in linked_ids and
              r['source_file'] and document_identifier(r['source_file'], statements_root=statements_root) == key]
    # Basename compatibility is only for genuinely unlinked legacy rows. Once
    # linked, identity comes from canonical keys/aliases or verified account-scoped
    # content, never the old transaction filename (even for another document).
    if not legacy and '/' in key:
        legacy = [r for r in all_rows if r['txn_id'] not in linked_ids and r['source_file'] and
                  document_identifier(r['source_file'], statements_root=statements_root) == key.rsplit('/', 1)[-1]]
    if len(doc_ids) > 1:
        return None, (), all_rows, True
    doc_id = next(iter(doc_ids), None)
    linked = set()
    if doc_id is not None:
        linked = {r[0] for r in conn.execute('SELECT txn_id FROM transaction_sources WHERE document_id=?', (doc_id,))}
    selected_ids = linked | {r['txn_id'] for r in legacy}
    related = _related_metadata(conn)
    selected = tuple(ExistingRow(r['txn_id'], _row_from_ledger(r),
        _digest({'transaction': {k: v for k, v in r.items() if k not in _SOURCE_FIELDS},
                 'related': related.get(r['txn_id'], {})}))
        for r in all_rows if r['txn_id'] in selected_ids)
    return doc_id, selected, [r for r in all_rows if r['txn_id'] not in selected_ids], False


def _source_overlaps(conn, account_id, document_id, rows, other_rows, coverage, ready, statements_root=None):
    """Count distinct other sources; inferred row ranges are suspicion, not evidence."""
    documents, links = {}, {}
    if ready:
        documents = {r['document_id']: dict(r) for r in conn.execute(
            'SELECT * FROM source_documents WHERE account_id=? AND document_id!=?',
            (account_id, document_id or -1))}
        links = dict(conn.execute('SELECT txn_id,document_id FROM transaction_sources'))
    groups = {}
    for row in other_rows:
        source = ('document', links[row['txn_id']]) if row['txn_id'] in links else (
            'legacy', document_identifier(row['source_file'], statements_root=statements_root) if row['source_file'] else str(row['txn_id']))
        groups.setdefault(source, []).append(row)
    for doc_id in documents:
        groups.setdefault(('document', doc_id), [])
    cov = dict(coverage)
    keys = {row.key for row in rows}
    dates = [row.txn_date for row in rows]
    inferred_start, inferred_end = (min(dates), max(dates)) if dates else (None, None)
    overlapping, reasons = 0, []
    for source, source_rows in groups.items():
        document = documents.get(source[1], {}) if source[0] == 'document' else {}
        old_start, old_end = document.get('coverage_start'), document.get('coverage_end')
        duplicate = any(_row_from_ledger(row).key in keys for row in source_rows)
        evidenced = bool(cov and old_start and old_end)
        verified_overlap = evidenced and cov['start'] <= old_end and cov['end'] >= old_start
        suspicious_date_overlap = False
        if not evidenced and dates:
            start, end = (cov['start'], cov['end']) if cov else (inferred_start, inferred_end)
            suspicious_date_overlap = bool(
                (old_start and old_end and start <= old_end and end >= old_start)
                or any(start <= row['txn_date'] <= end for row in source_rows))
        if duplicate or verified_overlap or suspicious_date_overlap:
            overlapping += 1
            if verified_overlap or (duplicate and cov):
                reasons.append('other_source_overlap')
            elif duplicate:
                reasons.append('missing_coverage_row_overlap')
            else:
                reasons.append('missing_coverage_date_overlap')
    return overlapping, tuple(dict.fromkeys(reasons))


def _require_import_account(account_id, *, db_path=None, conn=None):
    # Exact identity and policy must be resolved before extraction, including
    # trusted Python callers. There is no default account or filename inference.
    if not isinstance(account_id, str):
        raise ImportServiceError('account_unavailable')
    account = next((a for a in available_import_accounts(db_path=db_path, conn=conn)
                    if a['account_id'] == account_id and a['import_enabled']), None)
    if account is None:
        raise ImportServiceError('account_unavailable')
    return account


def prepare_import(pdf_path, *, db_path, root, account_id=None, extraction=None) -> ImportPreview:
    account = _require_import_account(account_id, db_path=db_path)
    account_id = account['account_id']
    settings = get_settings(root=root)
    pdf = Path(pdf_path)
    pdf = (pdf if pdf.is_absolute() else settings.root / pdf).resolve()
    db = Path(db_path).resolve()
    key = document_identifier(str(pdf), statements_root=settings.statements_dir)
    paths = (pdf, settings.categories_path, settings.category_rules_path)
    versions = tuple((str(p), _file_hash(p)) for p in paths)
    if versions[0][1] == 'missing':
        raise FileNotFoundError('PDF does not exist')
    reasons = []
    tree = {}
    rules = {}
    if any(version == 'missing' for _, version in versions[1:]):
        reasons.append('configuration_missing')
    else:
        tree = json.loads(settings.categories_path.read_text(encoding='utf-8'))
        if (not isinstance(tree, dict) or any(not isinstance(cat, str) or not cat or
            not isinstance(subs, list) or any(not isinstance(sub, str) or not sub for sub in subs)
            for cat, subs in tree.items())):
            raise ValueError('Invalid category tree')
        rules = _load_rules(settings.category_rules_path)
    if extraction is None:
        extraction = extract_statement(pdf, account_id=account_id, source_file=key)
    if not isinstance(extraction, ExtractionResult):
        raise TypeError('extraction is a trusted Python ExtractionResult, not JSON')
    rec = reconcile_transactions(extraction.transactions, extraction.statement_totals,
        warnings=extraction.warnings, recon_gaps=extraction.recon_gaps,
        suspects=extraction.suspects + (extraction.extractor_diag.suspect_merged_lines
                                      if extraction.extractor_diag else []))
    if rec.status != 'passed':
        reasons.append('reconciliation_' + rec.status)
    rows = []
    for txn in extraction.transactions:
        if txn.account_id != account_id:
            raise ValueError('Extraction account differs from requested account')
        if not isinstance(txn.description_raw, str) or not txn.description_raw.strip():
            raise ValueError('Empty transaction description')
        if txn.currency != 'GBP' or type(txn.page) is not int or txn.page < 1 or not isinstance(txn.row_ref, str) or not txn.row_ref:
            raise ValueError('Invalid transaction provenance or currency')
        decomposed = decompose_description(txn.description_raw)
        rule = rules.get(decomposed.merchant_core.upper(), {})
        cat, sub = rule.get('category', ''), rule.get('subcategory', '')
        exception = None if cat in tree and sub in tree[cat] else ('unknown_subcategory' if cat or sub else 'uncategorised')
        rows.append(SourceRow(_canonical_date(txn.txn_date), txn.description_raw, _cents(txn.amount), txn.currency,
            txn.page, txn.row_ref, decomposed.channel, decomposed.merchant_core,
            cat if exception is None else '', sub if exception is None else '', exception))
    coverage = ()
    if extraction.coverage:
        cov = dict(extraction.coverage)
        start, end = _canonical_date(cov['start']), _canonical_date(cov['end'])
        if start > end or not cov.get('source_row_ref') or any(not start <= r.txn_date <= end for r in rows):
            reasons.append('invalid_coverage')
        else:
            coverage = tuple(sorted(cov.items()))
    ready, ledger, document_id, existing, overlaps, changed = False, 'missing', None, (), 0, 0
    if not db.is_file():
        reasons.append('database_missing')
    else:
        with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('BEGIN')
            account = _require_import_account(account_id, conn=conn)
            ledger = _ledger_version(conn)
            ready = _schema_ready(conn)
            account_evidence = _source_accounts(conn, key, versions[0][1], ready, settings.statements_dir)
            if any(owners - {account_id} for owners in account_evidence.values()):
                reasons.append('source_account_mismatch')
            if not ready:
                reasons.append('schema_not_ready')
            document_id, existing, other_rows, ambiguous_document = _select_source(
                conn, account_id, key, versions[0][1], ready, settings.statements_dir)
            if ambiguous_document:
                reasons.append('ambiguous_document_identity')
            overlaps, overlap_reasons = _source_overlaps(conn, account_id, document_id,
                rows, other_rows, coverage, ready, settings.statements_dir)
            reasons.extend(overlap_reasons)
            old_map = {r.txn_id: r.source for r in existing}
            plan = match_source_rows(tuple(rows), existing)
            changed = sum(rows[i] != replace(old_map[txn_id], category=rows[i].category,
                          subcategory=rows[i].subcategory, exception_type=rows[i].exception_type)
                          for i, txn_id in plan.matches)
    if not db.is_file():
        plan = match_source_rows(tuple(rows), ())
    if plan.ambiguous:
        reasons.append('ambiguous_occurrences')
    if plan.potential_removals:
        reasons.append('potential_removals')
    if plan.changed_candidates:
        reasons.append('changed_source_candidates')
    if versions != tuple((str(p), _file_hash(p)) for p in paths):
        raise ImportServiceError('stale_preview')
    result = ImportPreview(secrets.token_urlsafe(24), str(db), str(settings.root), str(pdf),
        account_id, key, document_id, versions[0][1], versions, ledger, tuple(rows), plan, ready,
        'eligible' if not reasons else 'blocked', tuple(reasons), rec.status, rec.reasons, coverage,
        tuple((cat, tuple(subs)) for cat, subs in tree.items()), changed, overlaps,
        sum(r.exception_type is None for r in rows), sum(r.exception_type is not None for r in rows),
        account_display_name=account['display_name'])
    return replace(result, signature=_signature(result))


def _verify_files(preview):
    if any(_file_hash(Path(path)) != version for path, version in preview.file_versions):
        raise ImportServiceError('stale_preview')


def _seed_categories(conn, categories):
    lookup = {}
    for category, subcategories in categories:
        conn.execute('INSERT OR IGNORE INTO categories(name) VALUES (?)', (category,))
        category_id = conn.execute('SELECT category_id FROM categories WHERE name=?', (category,)).fetchone()[0]
        for subcategory in subcategories:
            conn.execute('INSERT OR IGNORE INTO subcategories(category_id,name) VALUES (?,?)', (category_id, subcategory))
            lookup[(category, subcategory)] = conn.execute(
                'SELECT subcategory_id FROM subcategories WHERE category_id=? AND name=?',
                (category_id, subcategory)).fetchone()[0]
    return lookup


def commit_import(preview: ImportPreview, *, approved=False) -> CommitResult:
    """Approve precisely a server-held preview, atomically, with no implicit setup.

    Replay of the SAME signed token returns its recorded result even if subsequent
    ledger/files changed; it performs no writes. A new token must pass every
    fingerprint check. SQL/IO errors propagate after automatic rollback; expected
    policy errors have ImportServiceError.code. Approval must be literal True.
    """
    if approved is not True:
        raise ImportServiceError('approval_required')
    try:
        valid = (type(preview) is ImportPreview and isinstance(preview.signature, str)
                 and hmac.compare_digest(preview.signature, _signature(preview)))
    except (TypeError, ValueError, AttributeError, RecursionError):
        valid = False
    if not valid:
        raise ImportServiceError('invalid_preview')
    if not Path(preview.db_path).is_file():
        raise ImportServiceError('schema_not_ready')
    with closing(sqlite3.connect(Path(preview.db_path).as_uri() + '?mode=rw', uri=True)) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            if not _schema_ready(conn):
                raise ImportServiceError('schema_not_ready')
            prior = conn.execute(
                'SELECT batch_id,document_id,added_count,matched_count,total_current,exceptions_created,approved_payload_hash '
                'FROM import_batches WHERE preview_token=?', (preview.token,)).fetchone()
            if prior:
                if prior[6] != preview.signature:
                    raise ImportServiceError('invalid_preview')
                return CommitResult(*prior[:6], replayed=True)
            if preview.status != 'eligible':
                raise ImportServiceError('blocked')
            _verify_files(preview)
            if _ledger_version(conn) != preview.ledger_version:
                raise ImportServiceError('stale_preview')
            lookup = _seed_categories(conn, preview.categories)
            coverage = dict(preview.coverage)
            document_id = preview.document_id
            if document_id is None:
                document_id = conn.execute(
                    'INSERT INTO source_documents(account_id,document_key,source_identity,content_hash,'
                    'coverage_start,coverage_end,coverage_row_ref,reconciliation_status) VALUES (?,?,?,?,?,?,?,?)',
                    (preview.account_id, preview.document_key, source_identity(preview.account_id, preview.document_key),
                     preview.content_hash, coverage.get('start'), coverage.get('end'), coverage.get('source_row_ref'),
                     preview.reconciliation_status)).lastrowid
            else:
                conn.execute('UPDATE source_documents SET content_hash=?,coverage_start=?,coverage_end=?,coverage_row_ref=?,'
                             "reconciliation_status=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE document_id=?",
                    (preview.content_hash, coverage.get('start'), coverage.get('end'), coverage.get('source_row_ref'),
                     preview.reconciliation_status, document_id))
            conn.execute('INSERT OR IGNORE INTO source_document_aliases(account_id,alias_key,document_id) VALUES (?,?,?)',
                         (preview.account_id, preview.document_key, document_id))
            conn.execute('INSERT OR IGNORE INTO source_document_contents(document_id,content_hash) VALUES (?,?)',
                         (document_id, preview.content_hash))
            canonical = conn.execute('SELECT document_key,source_identity FROM source_documents WHERE document_id=?',
                                     (document_id,)).fetchone()
            ids = dict(preview.plan.matches)
            # Only source-derived presentation/provenance fields. Never replace
            # rows, old hashes, categories, exclusion/note or future metadata.
            for index, txn_id in preview.plan.matches:
                row = preview.rows[index]
                conn.execute('UPDATE transactions SET channel=?,merchant_core=?,page=?,row_ref=? WHERE txn_id=?',
                             (row.channel, row.merchant_core, row.page, row.row_ref, txn_id))
            exceptions = 0
            for index in preview.plan.additions:
                row = preview.rows[index]
                import_hash = _digest(['m1', preview.account_id, canonical[1], preview.content_hash,
                                       row.row_ref, index, row.key])
                cursor = conn.execute(
                    'INSERT INTO transactions(txn_date,description_raw,amount,currency,channel,merchant_core,'
                    'subcategory_id,account_id,source_file,page,row_ref,import_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (row.txn_date, row.description_raw, float(Decimal(row.cents)/100), row.currency, row.channel,
                     row.merchant_core, lookup.get((row.category, row.subcategory)), preview.account_id,
                     'StatementsPDF/' + canonical[0], row.page, row.row_ref, import_hash))
                txn_id = cursor.lastrowid
                ids[index] = txn_id
                if row.exception_type:
                    conn.execute('INSERT INTO exceptions(txn_id,type,merchant_core,description_raw,amount,txn_date,detail) '
                                 'VALUES (?,?,?,?,?,?,?)',
                        (txn_id, row.exception_type, row.merchant_core, row.description_raw,
                         float(Decimal(row.cents)/100), row.txn_date, json.dumps({'channel': row.channel})))
                    exceptions += 1
            # Rebuild source-only links in the same transaction; this avoids a
            # uniqueness conflict when row references legitimately shift/swap.
            conn.execute('DELETE FROM transaction_sources WHERE document_id=?', (document_id,))
            for index, txn_id in sorted(ids.items()):
                conn.execute('INSERT INTO transaction_sources(txn_id,document_id,row_ref,occurrence) VALUES (?,?,?,?)',
                             (txn_id, document_id, preview.rows[index].row_ref, index))
            total = conn.execute('SELECT COUNT(*) FROM transaction_sources WHERE document_id=?', (document_id,)).fetchone()[0]
            # Namespace the legacy log key by account/document rather than a path;
            # an existing legacy source log, where unambiguous, is updated too.
            log_key = 'm1:' + canonical[1]
            conn.execute('INSERT INTO import_log(source_file,row_count) VALUES (?,?) '
                         "ON CONFLICT(source_file) DO UPDATE SET row_count=excluded.row_count,imported_at=strftime('%Y-%m-%dT%H:%M:%S','now')",
                         (log_key, total))
            legacy_sources = {r[0] for r in conn.execute(
                'SELECT DISTINCT t.source_file FROM transactions t JOIN transaction_sources s ON s.txn_id=t.txn_id '
                'WHERE s.document_id=? AND t.source_file IS NOT NULL', (document_id,))}
            for legacy_source in legacy_sources:
                # The legacy log has no account column. Update it only where its
                # filename is not shared by another account/source document.
                shared = conn.execute(
                    'SELECT 1 FROM transactions t LEFT JOIN transaction_sources s ON s.txn_id=t.txn_id '
                    'WHERE t.source_file=? AND (s.document_id IS NULL OR s.document_id!=?) LIMIT 1',
                    (legacy_source, document_id)).fetchone()
                if not shared:
                    conn.execute("UPDATE import_log SET row_count=?,imported_at=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE source_file=?",
                                 (total, legacy_source))
            versions = json.dumps({'files': preview.file_versions, 'ledger': preview.ledger_version,
                                   'account_id': preview.account_id, 'payload_signature': preview.signature})
            batch_id = conn.execute(
                'INSERT INTO import_batches(preview_token,approved_payload_hash,document_id,input_versions,added_count,'
                'matched_count,total_current,exceptions_created,approved,reconciliation_status) VALUES (?,?,?,?,?,?,?,?,1,?)',
                (preview.token, preview.signature, document_id, versions, len(preview.plan.additions),
                 len(preview.plan.matches), total, exceptions, preview.reconciliation_status)).lastrowid
            _verify_files(preview)
            return CommitResult(batch_id, document_id, len(preview.plan.additions), len(preview.plan.matches), total, exceptions)


def preview_summary(preview: ImportPreview) -> dict:
    return {'token': preview.token, 'status': preview.status, 'reasons': list(preview.reasons),
            'account_id': preview.account_id, 'account_display_name': preview.account_display_name,
            'schema_ready': preview.schema_ready,
            'reconciliation': {'status': preview.reconciliation_status,
                               'reasons': list(preview.reconciliation_reasons)},
            'coverage': dict(preview.coverage) if preview.coverage else None,
            'coverage_status': 'evidenced' if preview.coverage else 'unavailable',
            'counts': {'extracted': len(preview.rows), 'additions': len(preview.plan.additions),
                       'matches': len(preview.plan.matches), 'changed_source_fields': preview.changed_source_fields,
                       'potential_removals': len(preview.plan.potential_removals),
                       'changed_source_candidates': len(preview.plan.changed_candidates),
                       'ambiguous': len(preview.plan.ambiguous), 'overlaps': preview.overlaps,
                       'categorised': preview.categorised, 'uncategorised': preview.uncategorised}}
