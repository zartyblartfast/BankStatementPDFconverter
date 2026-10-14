"""Explicit, signed own-account decisions; never auto-pair or rewrite transactions.

Keep frozen previews server-side. Commit accepts only that object and literal
approval, not reconstructed browser payloads. Explicit existing DB/idle conn
only. No migration, default path, import, category or report side effects.

``root`` is an optional trusted Python caller's project directory, resolved and
frozen at prepare time, never a browser-supplied commit override. Omit it only
for ledger-only callers: no config root is inferred from a DB or current dir.
An explicit root must already be a directory. Each of the three fixed config
files is read once per preparation (missing is a signed version too), and once
again under BEGIN IMMEDIATE before any uncommitted decision writes. This is a
stale check, not a filesystem lock against unrelated editors. Already-applied
tokens return their original result without reapplying or checking new config.

Pairing two independently confirmed unmatched groups is conservatively rejected;
only an unmatched group plus an unconfirmed counterpart can reuse the group.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from decimal import Decimal
import hashlib
import hmac
import json
import math
from pathlib import Path
import secrets
import sqlite3

from scripts.services.accounts import get_account
from scripts.services.ingestion import _ledger_version

_KEY = secrets.token_bytes(32)


class TransferError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def validate_txn_id(value):
    if type(value) is not int or not 1 <= value <= 9223372036854775807:
        raise TransferError('invalid_id')
    return value


def exact_pence(value):
    try:
        # Decimal construction and ratios are exact regardless of caller context.
        # Do the scale/divisibility check with integers: Decimal arithmetic could
        # round away a penny mismatch or fractional pence before validation.
        number = Decimal(str(value))
        numerator, denominator = number.as_integer_ratio()
        pence, remainder = divmod(numerator * 100, denominator)
        if remainder:
            raise ValueError()
        return pence
    except (ValueError, TypeError, ArithmeticError):
        raise TransferError('invalid_amount') from None


# Absolute maximum permitted REAL residue, measured in pence (1e-8 pounds).
# Never relative to magnitude: larger amounts must not gain fractional-penny slack.
_LEDGER_RESIDUE_DENOMINATOR = 1_000_000


def ledger_pence(value):
    """Read stored SQLite REAL money without changing its source value.

    Legacy balance subtraction can leave a tiny binary tail. Only a finite,
    builtin float may be mapped to its nearest cent, and only within an absolute
    1e-6 pence cap. Integer ratios make this independent of Decimal context.
    Text, Decimal and all other inputs retain exact_pence's strict validation.
    This is a ledger-read boundary, NOT an arbitrary transfer-input validator.
    Material fractional pence and nonfinite REALs still fail closed.
    """
    if type(value) is not float:
        return exact_pence(value)
    try:
        numerator, denominator = value.as_integer_ratio()
        lower, remainder = divmod(numerator * 100, denominator)
        nearest = lower + (2 * remainder > denominator)
        residue = abs(numerator * 100 - nearest * denominator)
        if residue * _LEDGER_RESIDUE_DENOMINATOR > denominator:
            raise ValueError()
        return nearest
    except (ValueError, TypeError, ArithmeticError):
        raise TransferError('invalid_amount') from None


@contextmanager
def _connection(*, db_path=None, conn=None, write=False):
    if (db_path is None) == (conn is None):
        raise ValueError('Supply exactly one explicit db_path or conn')
    if conn is not None:
        if conn.in_transaction:
            raise TransferError('connection_busy')
        yield conn
        return
    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError('Transfer database does not exist')
    connection = sqlite3.connect(path.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True)
    try:
        if write:
            connection.execute('PRAGMA foreign_keys=ON')
        else:
            connection.execute('PRAGMA query_only=ON')
        yield connection
    finally:
        connection.close()


def _ready(conn):
    migration = Path(__file__).resolve().parents[1] / 'migrations/004_internal_transfers.sql'
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {'transfer_groups','transfer_members','transfer_audit','schema_migrations','accounts'} <= tables:
        raise TransferError('schema_not_ready')
    row = conn.execute('SELECT name,checksum FROM schema_migrations WHERE version=4').fetchone()
    if not row or tuple(row) != (migration.name, hashlib.sha256(migration.read_bytes()).hexdigest()):
        raise TransferError('schema_not_ready')


def _owned(conn, account_id):
    account = get_account(account_id, conn=conn)
    if not account or not account['owned'] or account['currency'] != 'GBP':
        raise TransferError('account_unavailable')
    return account


def _row(conn, txn_id):
    validate_txn_id(txn_id)
    cursor = conn.execute('SELECT * FROM transactions WHERE txn_id=?', (txn_id,))
    row = cursor.fetchone()
    if row is None:
        raise TransferError('transaction_missing')
    result = dict(zip([c[0] for c in cursor.description], row))
    _owned(conn, result['account_id'])
    if result['currency'] != 'GBP' or not ledger_pence(result['amount']):
        raise TransferError('invalid_amount_or_currency')
    return result


@dataclass(frozen=True)
class TransferPreview:
    token: str
    db_path: str | None = field(repr=False)
    action: str
    txn_ids: tuple[int, ...]
    counterparts: tuple[str, ...]
    group_id: int | None
    ledger_version: str = field(repr=False)
    connection_id: int | None = field(default=None, repr=False)
    root: str | None = field(default=None, repr=False)
    file_versions: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    signature: str = field(default='', repr=False)


@dataclass(frozen=True)
class TransferResult:
    group_id: int
    action: str
    replayed: bool = False


def _signature(preview):
    data = asdict(preview)
    data.pop('signature')
    payload = json.dumps(data, sort_keys=True, separators=(',', ':')).encode()
    return hmac.new(_KEY, payload, hashlib.sha256).hexdigest()


def _config_snapshot(root):
    """Read each fixed classification file once; never discover/create config."""
    if root is None:
        return None, ()
    try:
        if root == '':
            raise ValueError()
        path = Path(root).resolve(strict=True)
        if not path.is_dir():
            raise ValueError()
    except (OSError, TypeError, ValueError, RuntimeError):
        raise TransferError('invalid_root') from None
    versions = []
    for name in ('categories.json', 'category_rules.csv', 'merchant_rules.json'):
        config = path / 'config' / name
        try:
            digest = hashlib.sha256(config.read_bytes()).hexdigest()
        except FileNotFoundError:
            digest = 'missing'
        versions.append((name, digest))
    return str(path), tuple(versions)


def prepare_pair(first_txn_id, second_txn_id, *, db_path=None, conn=None, root=None):
    validate_txn_id(first_txn_id)
    validate_txn_id(second_txn_id)
    with _connection(db_path=db_path, conn=conn) as connection:
        try:
            connection.execute('BEGIN')
            _ready(connection)
            first, second = _row(connection, first_txn_id), _row(connection, second_txn_id)
            if first['account_id'] == second['account_id']:
                raise TransferError('same_account')
            if ledger_pence(first['amount']) != -ledger_pence(second['amount']):
                raise TransferError('amount_mismatch')
            members = connection.execute('SELECT m.group_id,m.txn_id,m.counterpart_account_id,g.status '
                'FROM transfer_members m JOIN transfer_groups g ON g.group_id=m.group_id '
                'WHERE m.active=1 AND m.txn_id IN (?,?)', (first_txn_id, second_txn_id)).fetchall()
            if any(r[3] != 'unmatched' for r in members) or len(members) > 1:
                raise TransferError('already_confirmed')
            if members:
                member = members[0]
                counterpart = second['account_id'] if member[1] == first_txn_id else first['account_id']
                if member[2] != counterpart:
                    raise TransferError('counterpart_mismatch')
            group_id = members[0][0] if members else None
            preview = TransferPreview(secrets.token_urlsafe(24), str(Path(db_path).resolve()) if db_path else None,
                'pair', (first_txn_id, second_txn_id), (second['account_id'], first['account_id']), group_id,
                _ledger_version(connection))
            config_root, file_versions = _config_snapshot(root)
            preview = replace(preview, connection_id=id(conn) if conn is not None else None,
                              root=config_root, file_versions=file_versions)
            return replace(preview, signature=_signature(preview))
        finally:
            connection.rollback()


def prepare_unmatched(txn_id, counterpart_account_id, *, db_path=None, conn=None, root=None):
    validate_txn_id(txn_id)
    with _connection(db_path=db_path, conn=conn) as connection:
        try:
            connection.execute('BEGIN')
            _ready(connection)
            row = _row(connection, txn_id)
            _owned(connection, counterpart_account_id)
            if row['account_id'] == counterpart_account_id:
                raise TransferError('same_account')
            if connection.execute('SELECT 1 FROM transfer_members WHERE txn_id=? AND active=1', (txn_id,)).fetchone():
                raise TransferError('already_confirmed')
            preview = TransferPreview(secrets.token_urlsafe(24), str(Path(db_path).resolve()) if db_path else None,
                'unmatched', (txn_id,), (counterpart_account_id,), None, _ledger_version(connection))
            config_root, file_versions = _config_snapshot(root)
            preview = replace(preview, connection_id=id(conn) if conn is not None else None,
                              root=config_root, file_versions=file_versions)
            return replace(preview, signature=_signature(preview))
        finally:
            connection.rollback()


def prepare_revoke(group_id, *, db_path=None, conn=None, root=None):
    validate_txn_id(group_id)
    with _connection(db_path=db_path, conn=conn) as connection:
        try:
            connection.execute('BEGIN')
            _ready(connection)
            group = connection.execute('SELECT status FROM transfer_groups WHERE group_id=?', (group_id,)).fetchone()
            if not group or group[0] == 'revoked':
                raise TransferError('group_not_active')
            rows = connection.execute('SELECT txn_id,counterpart_account_id FROM transfer_members '
                                      'WHERE group_id=? AND active=1 ORDER BY txn_id', (group_id,)).fetchall()
            preview = TransferPreview(secrets.token_urlsafe(24), str(Path(db_path).resolve()) if db_path else None,
                'revoke', tuple(r[0] for r in rows), tuple(r[1] for r in rows), group_id, _ledger_version(connection))
            config_root, file_versions = _config_snapshot(root)
            preview = replace(preview, connection_id=id(conn) if conn is not None else None,
                              root=config_root, file_versions=file_versions)
            return replace(preview, signature=_signature(preview))
        finally:
            connection.rollback()


def suggest_pairs(txn_id, *, start_date, end_date, limit=50, offset=0,
                  include_category_hints=False, db_path=None, conn=None):
    """Equal/opposite GBP candidates only. Dates bound discovery, not confirmation.

    Stable ID pagination retains duplicates; counts are candidates, not proof.
    Category hints never select, classify, approve or persist any decision.
    """
    validate_txn_id(txn_id)
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 100000:
        raise TransferError('invalid_page')
    try:
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        if start.isoformat() != start_date or end.isoformat() != end_date or not 0 <= (end-start).days <= 90:
            raise ValueError()
    except (ValueError, TypeError):
        raise TransferError('invalid_date_window') from None
    with _connection(db_path=db_path, conn=conn) as connection:
        try:
            connection.execute('BEGIN')
            _ready(connection)
            row = _row(connection, txn_id)
            # Two independently stored REALs can each carry the bounded residue.
            # This SQL window is discovery only: exact cent validation below owns
            # eligibility and pagination/counts, never SQLite ROUND or REAL equality.
            target = -row['amount']
            lower = math.nextafter(target - 2e-8, -math.inf)
            upper = math.nextafter(target + 2e-8, math.inf)
            expected = -ledger_pence(row['amount'])
            where = (' FROM transactions t JOIN accounts a ON a.account_id=t.account_id '
                     "WHERE t.account_id!=? AND a.owned=1 AND a.currency='GBP' AND t.currency='GBP' "
                     'AND t.amount BETWEEN ? AND ? AND t.txn_date BETWEEN ? AND ? '
                     'AND NOT EXISTS(SELECT 1 FROM transfer_members m WHERE m.txn_id=t.txn_id AND m.active=1)')
            params = (row['account_id'], lower, upper, start_date, end_date)
            cursor = connection.execute('SELECT t.txn_id,t.txn_date,t.account_id,t.amount,t.subcategory_id'+where+
                                        ' ORDER BY t.txn_id', params)
            candidates = []
            total = 0
            for candidate_id, txn_date, account_id, amount, subcategory_id in cursor:
                cents = ledger_pence(amount)
                if cents != expected:
                    continue
                total += 1
                if not offset < total <= offset + limit:
                    continue
                candidate = {'txn_id': candidate_id, 'txn_date': txn_date, 'account_id': account_id,
                             'cents': cents}
                if include_category_hints:
                    category = connection.execute('SELECT c.name,s.name FROM subcategories s JOIN categories c '
                        'ON c.category_id=s.category_id WHERE s.subcategory_id=?', (subcategory_id,)).fetchone()
                    candidate['category_hint'] = tuple(category) if category else None
                candidates.append(candidate)
            return {'candidates': candidates, 'total': total, 'limit': limit, 'offset': offset,
                    'has_more': offset+len(candidates) < total,
                    'warnings': ['candidates_are_not_confirmation']}
        finally:
            connection.rollback()


def commit_transfer(preview, *, approved=False, conn=None):
    if approved is not True:
        raise TransferError('approval_required')
    try:
        valid = type(preview) is TransferPreview and hmac.compare_digest(preview.signature, _signature(preview))
    except (TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise TransferError('invalid_preview')
    if (conn is not None and preview.db_path is not None) or (preview.connection_id is not None and (conn is None or id(conn) != preview.connection_id)):
        raise TransferError('invalid_preview')
    with _connection(db_path=preview.db_path, conn=conn, write=True) as connection:
        # Foreign keys are required on caller-owned connections too.
        connection.execute('PRAGMA foreign_keys=ON')
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            _ready(connection)
            prior = connection.execute('SELECT group_id,action,payload_signature FROM transfer_audit WHERE preview_token=?',
                                       (preview.token,)).fetchone()
            if prior:
                if prior[2] != preview.signature:
                    raise TransferError('invalid_preview')
                return TransferResult(prior[0], prior[1], True)
            if _ledger_version(connection) != preview.ledger_version:
                raise TransferError('stale_preview')
            try:
                config_snapshot = _config_snapshot(preview.root)
            except (OSError, TransferError):
                raise TransferError('stale_preview') from None
            if config_snapshot != (preview.root, preview.file_versions):
                raise TransferError('stale_preview')
            group_id = preview.group_id
            if preview.action == 'revoke':
                connection.execute("UPDATE transfer_members SET active=0,ended_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE group_id=? AND active=1", (group_id,))
                connection.execute("UPDATE transfer_groups SET status='revoked',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE group_id=?", (group_id,))
            else:
                status = 'paired' if preview.action == 'pair' else 'unmatched'
                if group_id is None:
                    group_id = connection.execute('INSERT INTO transfer_groups(status) VALUES (?)', (status,)).lastrowid
                else:
                    connection.execute("UPDATE transfer_groups SET status=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE group_id=?", (status, group_id))
                for txn_id, counterpart in zip(preview.txn_ids, preview.counterparts):
                    if not connection.execute('SELECT 1 FROM transfer_members WHERE group_id=? AND txn_id=? AND active=1', (group_id, txn_id)).fetchone():
                        connection.execute('INSERT INTO transfer_members(group_id,txn_id,counterpart_account_id,active) VALUES (?,?,?,1)',
                                           (group_id, txn_id, counterpart))
            members = []
            for txn_id in preview.txn_ids:
                row = connection.execute('SELECT account_id,amount,currency,txn_date FROM transactions WHERE txn_id=?', (txn_id,)).fetchone()
                member = {'txn_id':txn_id,'account_id':row[0],'cents':ledger_pence(row[1]),'currency':row[2],'txn_date':row[3]}
                try:
                    exact_pence(row[1])
                except TransferError:
                    # New decisions retain the original REAL when cent semantics
                    # required residue normalisation; historical audits are untouched.
                    member['stored_amount'] = row[1]
                    member['amount_read_method'] = 'stored_real_near_cent_1e-6_pence'
                members.append(member)
            connection.execute('INSERT INTO transfer_audit(group_id,action,preview_token,payload_signature,decision_json) VALUES (?,?,?,?,?)',
                (group_id, preview.action, preview.token, preview.signature,
                 json.dumps({'txn_ids': preview.txn_ids, 'counterparts': preview.counterparts, 'members': members}, sort_keys=True)))
            assert group_id is not None
            return TransferResult(group_id, preview.action)
