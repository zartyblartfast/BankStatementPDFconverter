"""Shared GBP cash-movement policy on an explicit row scope, not income inference.

Raw retains every selected movement, regardless of category/excluded flag.
Adjusted removes explicitly confirmed active paired/unmatched legs and optionally
report-excluded rows. Fees, uncategorised rows and category-only hints otherwise
remain movement. Pass account/date/literal-scoped IDs without pre-dropping excluded
rows to retain raw evidence; include_excluded controls adjusted totals only.
No default DB, creation, migration, writes, SQL or tool adapter.
"""
from contextlib import contextmanager, nullcontext
import hashlib
import json

from scripts.services.transfers import _connection, _ready, exact_pence, ledger_pence, validate_txn_id, TransferError


@contextmanager
def read_snapshot(*, db_path=None, conn=None):
    """Borrow an active snapshot unchanged; roll back only our own read transaction.

    Caller connections retain their authorizer, query_only setting and ownership.
    This helper issues SELECTs and transaction control only, never ledger writes.
    """
    if (db_path is None) == (conn is None):
        raise ValueError('Supply exactly one explicit db_path or conn')
    active = conn is not None and conn.in_transaction
    manager = nullcontext(conn) if active else _connection(db_path=db_path, conn=conn)
    with manager as connection:
        assert connection is not None
        if active:
            yield connection
        else:
            try:
                connection.execute('BEGIN')
                yield connection
            finally:
                connection.rollback()


def summarise_movement(txn_ids, *, db_path=None, conn=None, include_excluded=True):
    if type(include_excluded) is not bool:
        raise ValueError('include_excluded must be boolean')
    if not isinstance(txn_ids, (list, tuple)) or len(txn_ids) > 50000:
        raise ValueError('Supply a bounded explicit transaction ID scope')
    for txn_id in txn_ids:
        validate_txn_id(txn_id)
    if len(set(txn_ids)) != len(txn_ids):
        raise ValueError('Duplicate transaction IDs in scope')
    raw = {'credits_pence': 0, 'debits_pence': 0, 'net_pence': 0}
    adjusted = dict(raw)
    transfers = {'in_count': 0, 'out_count': 0, 'in_pence': 0, 'out_pence': 0, 'unmatched_count': 0}
    excluded_count = unclassified_count = adjusted_count = normalised_count = 0
    warnings = []
    with read_snapshot(db_path=db_path, conn=conn) as connection:
        try:
            _ready(connection)
            schema_ready = True
        except TransferError as error:
            if error.code != 'schema_not_ready':
                raise
            schema_ready = False
            warnings.append('transfer_schema_missing_or_unverified')
        selected = []
        statuses = {}
        group_ids = set()
        for index in range(0, len(txn_ids), 900):
            chunk = txn_ids[index:index+900]
            marks = ','.join('?' for _ in chunk)
            selected.extend(connection.execute('SELECT txn_id,amount,currency,excluded,subcategory_id FROM transactions '
                                               'WHERE txn_id IN ('+marks+')', chunk).fetchall())
            if schema_ready:
                for txn_id, status, group_id in connection.execute('SELECT m.txn_id,g.status,g.group_id FROM transfer_members m '
                    'JOIN transfer_groups g ON g.group_id=m.group_id WHERE m.active=1 '
                    "AND g.status IN ('paired','unmatched') AND m.txn_id IN ("+marks+')', chunk):
                    statuses[txn_id] = status
                    group_ids.add(group_id)
        if len(selected) != len(txn_ids):
            raise TransferError('transaction_missing')
        for txn_id, amount, currency, excluded, subcategory_id in selected:
            if currency != 'GBP':
                raise TransferError('unsupported_currency')
            cents = ledger_pence(amount)
            # Count only legacy decimal-spelling tails, not ordinary cent REALs.
            try:
                exact_pence(amount)
            except TransferError:
                normalised_count += 1
            key = 'credits_pence' if cents > 0 else 'debits_pence'
            raw[key] += abs(cents)
            excluded_count += bool(excluded)
            unclassified_count += subcategory_id is None
            if txn_id in statuses:
                direction = 'in' if cents > 0 else 'out'
                transfers[direction+'_count'] += 1
                transfers[direction+'_pence'] += abs(cents)
                transfers['unmatched_count'] += statuses[txn_id] == 'unmatched'
            elif include_excluded or not excluded:
                adjusted[key] += abs(cents)
                adjusted_count += 1
        for totals in (raw, adjusted):
            totals['net_pence'] = totals['credits_pence'] - totals['debits_pence']
        outside = in_transit = 0
        selected_ids = set(txn_ids)
        for group_id in sorted(group_ids):
            legs = connection.execute(
                'SELECT m.txn_id,t.txn_date FROM transfer_members m JOIN transactions t ON t.txn_id=m.txn_id '
                'WHERE m.active=1 AND m.group_id=?', (group_id,)).fetchall()
            outside += any(leg[0] not in selected_ids for leg in legs)
            in_transit += len({leg[1] for leg in legs}) > 1
        if outside:
            warnings.append('confirmed_counterpart_outside_scope')
        if in_transit:
            warnings.append('confirmed_transfer_different_posting_dates')
        digest = hashlib.sha256()
        if schema_ready:
            for table in ('transfer_groups', 'transfer_members', 'transfer_audit'):
                digest.update(table.encode())
                for row in connection.execute(f'SELECT * FROM {table} ORDER BY rowid'):
                    digest.update(json.dumps(tuple(row), default=str).encode())
        if transfers['unmatched_count']:
            warnings.append('explicit_unmatched_transfers')
        if excluded_count:
            warnings.append('selected_excluded_rows_retained')
        if normalised_count:
            warnings.append('stored_real_amount_residue_normalised')
        return {'currency': 'GBP', 'row_count': len(selected), 'raw': raw, 'adjusted': adjusted,
                'amount_read_method': 'stored_real_near_cent_1e-6_pence',
                'normalised_amount_count': normalised_count,
                'adjusted_row_count': adjusted_count,
                'transfers': transfers, 'excluded_count': excluded_count,
                'unclassified_count': unclassified_count, 'warnings': warnings,
                'transfer_schema_ready': schema_ready,
                'counterpart_outside_scope_count': outside,
                'different_posting_date_group_count': in_transit,
                'transfer_evidence_ref': 'transfers:' + digest.hexdigest()}
