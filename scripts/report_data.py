"""Exact, read-only report records and shared movement evidence.

No default database or report writes. Callers supply an explicit connection/path.
"""
import re
from typing import Any

from scripts.services.movement_policy import read_snapshot, summarise_movement
from scripts.services.transfers import ledger_pence


class ReportRows(list[dict]):
    def __init__(self, rows, policy: dict):
        super().__init__(rows)
        self.policy = policy


def browser_exact(value: Any, key: str = '') -> Any:
    """Stringify pence and identifiers without rounding through JavaScript Number."""
    if isinstance(value, dict):
        return {k: browser_exact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [browser_exact(v, key) for v in value]
    if isinstance(value, int) and (key.endswith('_pence') or key.endswith('_id')):
        return str(value)
    return value


def query_report(*, db_path=None, conn=None, account_ids=None, start='', end='') -> tuple[ReportRows, list[str]]:
    """Read raw rows, active memberships and totals from one borrowed snapshot."""
    if account_ids is not None and (not isinstance(account_ids, (list, tuple)) or
                                   any(not isinstance(a, str) or not a for a in account_ids)):
        raise ValueError('Supply explicit account IDs or None')
    with read_snapshot(db_path=db_path, conn=conn) as connection:
        clauses, parameters = [], []
        if account_ids is not None:
            clauses.append('t.account_id IN (' + ','.join('?' for _ in account_ids) + ')')
            parameters.extend(account_ids)
        if start:
            clauses.append('t.txn_date >= ?')
            parameters.append(start)
        if end:
            clauses.append('t.txn_date <= ?')
            parameters.append(end)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        cursor = connection.execute('SELECT t.*, t.txn_date AS date, t.merchant_core AS merchant, '
            'c.name AS category, s.name AS subcategory FROM transactions t '
            'LEFT JOIN subcategories s ON t.subcategory_id=s.subcategory_id '
            'LEFT JOIN categories c ON s.category_id=c.category_id' + where + ' ORDER BY t.txn_date,t.txn_id', parameters)
        names = [column[0] for column in cursor.description]
        records = [dict(zip(names, row)) for row in cursor]
        ids = [r['txn_id'] for r in records]
        policy = summarise_movement(ids, conn=connection, include_excluded=False)
        memberships, histories = {}, {}
        if policy['transfer_schema_ready']:
            audits = {}
            cursor = connection.execute('SELECT * FROM transfer_audit ORDER BY audit_id')
            names = [c[0] for c in cursor.description]
            for row in cursor:
                evidence = dict(zip(names,row))
                audits.setdefault(evidence['group_id'], []).append(browser_exact(evidence))
            for txn_id, group_id, status, active, account, created, ended in connection.execute(
                'SELECT m.txn_id,m.group_id,g.status,m.active,m.counterpart_account_id,m.created_at,m.ended_at '
                'FROM transfer_members m JOIN transfer_groups g ON g.group_id=m.group_id ORDER BY m.member_id'):
                histories.setdefault(txn_id, []).append({'group_id':str(group_id),'status':status,'active':active,
                    'counterpart_account_id':account,'created_at':created,'ended_at':ended,'audit':audits.get(group_id,[])})
                if active and status in ('paired','unmatched'):
                    # One membership read supplies history and active classification.
                    memberships[txn_id] = {'group_id':str(group_id),'status':status,
                        'counterpart_account_id':account, 'counterpart_txn_ids':[], 'counterpart_dates':[]}
            groups = {}
            for txn_id, membership in memberships.items():
                groups.setdefault(membership['group_id'], []).append(txn_id)
            dates = {r[0]:r[1] for r in connection.execute('SELECT txn_id,txn_date FROM transactions')}
            for txn_id, membership in memberships.items():
                others = [i for i in groups[membership['group_id']] if i != txn_id]
                membership['counterpart_txn_ids'] = [str(i) for i in others]
                membership['counterpart_dates'] = [dates[i] for i in others]
        sources, documents = {}, {}
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'source_documents' in tables:
            cursor = connection.execute('SELECT * FROM source_documents')
            names = [c[0] for c in cursor.description]
            for row in cursor:
                evidence = dict(zip(names,row))
                documents[evidence['document_id']] = browser_exact(evidence)
        if 'transaction_sources' in tables:
            cursor = connection.execute('SELECT * FROM transaction_sources')
            names = [c[0] for c in cursor.description]
            for row in cursor:
                evidence = dict(zip(names,row))
                evidence['document'] = documents.get(evidence['document_id'])
                sources.setdefault(evidence['txn_id'], []).append(browser_exact(evidence))
        for record in records:
            record['amount_pence'] = str(ledger_pence(record['amount']))
            record['month'] = record['date'][:7]
            match = re.search(r'\d{4}-\d{2}-\d{2}', record.get('source_file') or '')
            record['statement_date'] = match.group() if match else 'unknown'
            record['category'] = record['category'] or 'Uncategorised'
            record['subcategory'] = record['subcategory'] or 'Uncategorised'
            record['merchant'] = record['merchant'] or record.get('description_raw') or ''
            record['transfer'] = memberships.get(record['txn_id'])
            record['transfer_history'] = histories.get(record['txn_id'], [])
            record['source_evidence'] = sources.get(record['txn_id'], [])
            record.update(browser_exact(record))
        policy['scope'] = {'account_ids':list(account_ids) if account_ids is not None else None,
                           'start':start,'end':end}
        rows = ReportRows(records, browser_exact(policy))
        return rows, sorted({r['statement_date'] for r in rows})
