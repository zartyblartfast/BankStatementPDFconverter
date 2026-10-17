"""Read-only evidence using the actual exact import key, not stored merchant hints."""
import hashlib
import json
from scripts.triage_descriptions import decompose_description
from scripts.services.category_rules import is_enabled
from scripts.services import accounts


def merchant_key(description):
    return decompose_description(description or '').merchant_core.upper()


def registered_accounts(conn):
    if conn is None:
        return []
    try:
        return accounts.list_accounts(conn=conn)
    except accounts.AccountSchemaMissing:
        return []


def rows(conn, account_id=None):
    names = {a['account_id']: a['display_name'] for a in registered_accounts(conn)}
    for row in conn.execute('''SELECT t.txn_id,t.description_raw,t.txn_date,t.subcategory_id,t.account_id,
            c.name AS category,s.name AS subcategory FROM transactions t
            LEFT JOIN subcategories s ON t.subcategory_id=s.subcategory_id
            LEFT JOIN categories c ON c.category_id=s.category_id ORDER BY t.txn_id'''):
        if account_id is not None and row['account_id'] != account_id:
            continue
        yield dict(txn_id=str(row['txn_id']), key=merchant_key(row['description_raw']),
                   account_id=row['account_id'], account_name=names.get(row['account_id'], row['account_id'] + ' (not registered)'),
                   description=row['description_raw'], date=row['txn_date'],
                   subcategory_id=str(row['subcategory_id']) if row['subcategory_id'] is not None else None,
                   category=row['category'], subcategory=row['subcategory'])


def matches(conn, key, account_id=None):
    return [row for row in rows(conn, account_id) if row['key'] == key]


def fingerprint(evidence):
    return hashlib.sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def candidates(conn, rules, account_id=None):
    grouped = {}
    for row in rows(conn, account_id):
        rule = rules.get(row['key'])
        if not row['key']:
            continue
        if not rule:
            reason = 'missing'
        elif not is_enabled(rule):
            reason = 'disabled'
        elif (row['category'], row['subcategory']) != (rule['category'], rule['subcategory']):
            reason = 'disagreement'
        else:
            continue
        group = grouped.setdefault((row['key'], reason), dict(key=row['key'], reason=reason, count=0, rule=rule, saved={}, uncategorised_count=0))
        group['count'] += 1
        if not row['category'] or not row['subcategory']:
            group['uncategorised_count'] += 1
        pair = (row['category'] or 'Uncategorised', row['subcategory'] or '')
        group['saved'][pair] = group['saved'].get(pair, 0) + 1
    result = []
    for _, group in sorted(grouped.items()):
        group['saved'] = [dict(category=pair[0], subcategory=pair[1], count=count) for pair, count in sorted(group['saved'].items())]
        result.append(group)
    return result
