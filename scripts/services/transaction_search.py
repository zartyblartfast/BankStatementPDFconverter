"""Read-only, parameterised search across all ledger transactions."""
from datetime import date
from decimal import Decimal, InvalidOperation
import re

PAGE_SIZE = 50


def parse_filters(args):
    filters={key:(args.get(key,'') or '').strip() for key in ('q','start','end','amount','direction')}
    if len(filters['q'])>200:
        raise ValueError('Search text must be at most 200 characters.')
    for key in ('start','end'):
        value=filters[key]
        if value:
            try:
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
                    raise ValueError()
                date.fromisoformat(value)
            except ValueError:
                raise ValueError('Use valid dates in YYYY-MM-DD format.')
    if filters['start'] and filters['end'] and filters['start']>filters['end']:
        raise ValueError('Start date must not be after end date.')
    filters['direction']=filters['direction'] or 'all'
    if filters['direction'] not in ('all','credit','debit'):
        raise ValueError('Choose All, Credits or Debits.')
    cents=None
    if filters['amount']:
        try:
            amount=Decimal(filters['amount'])
            if not amount.is_finite() or amount<0 or amount>Decimal('1000000000000') or amount!=amount.quantize(Decimal('0.01')):
                raise InvalidOperation()
            cents=int(amount*100)
        except (InvalidOperation,ValueError,OverflowError):
            raise ValueError('Enter a positive amount with at most two decimal places, without £ or commas.')
    raw_page=args.get('page','1')
    if not re.fullmatch(r'[1-9]\d{0,5}',str(raw_page)):
        raise ValueError('Invalid page number.')
    return filters,cents,int(raw_page)


def empty_result():
    return dict(rows=[],count=0,credits='0.00',debits='0.00',net='0.00',page=1,pages=1,first=0,last=0)


def search_where(filters, cents):
    clauses=[]; params=[]
    if filters['q']:
        clauses.append("(INSTR(UPPER(t.description_raw),UPPER(?))>0 OR INSTR(UPPER(COALESCE(t.merchant_core,'')),UPPER(?))>0)")
        params.extend([filters['q'],filters['q']])
    for key,op in (('start','>='),('end','<=')):
        if filters[key]:
            clauses.append(f't.txn_date {op} ?');params.append(filters[key])
    if cents is not None:
        clauses.append('CAST(ROUND(ABS(t.amount)*100) AS INTEGER)=?');params.append(cents)
    if filters['direction']=='credit': clauses.append('t.amount>0')
    if filters['direction']=='debit': clauses.append('t.amount<0')
    where=' WHERE '+' AND '.join(clauses) if clauses else ''
    return where, params


def matching_snapshot(conn, filters, cents):
    import hashlib
    import json
    where, params = search_where(filters, cents)
    rows = conn.execute('SELECT t.* FROM transactions t' + where + ' ORDER BY txn_id', params).fetchall()
    return {row['txn_id']: hashlib.sha256(json.dumps(dict(row), sort_keys=True, default=str).encode()).hexdigest() for row in rows}


def search_transactions(conn, filters, cents, page):
    where, params = search_where(filters, cents)
    # One consistent read snapshot for aggregate counts and the displayed page.
    conn.execute('BEGIN')
    totals=conn.execute('''SELECT COUNT(*) AS count,
        COALESCE(SUM(CASE WHEN amount>0 THEN CAST(ROUND(amount*100) AS INTEGER) ELSE 0 END),0) AS credits,
        COALESCE(SUM(CASE WHEN amount<0 THEN -CAST(ROUND(amount*100) AS INTEGER) ELSE 0 END),0) AS debits
        FROM transactions t'''+where,params).fetchone()
    count=totals['count'];pages=max(1,(count+PAGE_SIZE-1)//PAGE_SIZE);page=min(page,pages)
    rows=conn.execute('''SELECT t.txn_id,t.txn_date,t.description_raw,t.amount,t.excluded,t.subcategory_id,t.account_id,
        c.name AS category,s.name AS subcategory
        FROM transactions t LEFT JOIN subcategories s ON t.subcategory_id=s.subcategory_id
        LEFT JOIN categories c ON s.category_id=c.category_id'''+where+
        ' ORDER BY t.txn_date DESC,t.txn_id DESC LIMIT ? OFFSET ?',params+[PAGE_SIZE,(page-1)*PAGE_SIZE]).fetchall()
    # Read names/status in the same snapshot; unverified support is never called Included.
    from scripts.services.transfers import _ready, TransferError
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    names = dict(conn.execute('SELECT account_id,display_name FROM accounts')) if 'accounts' in tables else {}
    try:
        _ready(conn)
        ready = True
    except TransferError as error:
        if error.code != 'schema_not_ready':
            raise
        ready = False
    statuses = {}
    if ready and rows:
        marks = ','.join('?' for _ in rows)
        statuses = {r[0]: (str(r[1]), r[2]) for r in conn.execute(
            'SELECT m.txn_id,g.group_id,g.status FROM transfer_members m '
            'JOIN transfer_groups g ON g.group_id=m.group_id '
            "WHERE m.active=1 AND g.status IN ('paired','unmatched') AND m.txn_id IN (" + marks + ')',
            [r['txn_id'] for r in rows])}
    snapshot = matching_snapshot(conn, filters, cents)
    conn.rollback()
    money=lambda value:f'{Decimal(value)/100:,.2f}'
    display=[]
    for row in rows:
        item=dict(row)
        item['account_name'] = names.get(row['account_id'], row['account_id'])
        item['transfer_group'], item['transfer_status'] = statuses.get(row['txn_id'], ('', ''))
        item['transfer_ready'] = ready
        item['amount_display']=money(int(Decimal(str(row['amount'])).quantize(Decimal('0.01'))*100))
        display.append(item)
    return dict(rows=display,count=count,snapshot=snapshot,credits=money(totals['credits']),debits=money(totals['debits']),
                net=money(totals['credits']-totals['debits']),page=page,pages=pages,
                first=(page-1)*PAGE_SIZE+1 if count else 0,last=min(page*PAGE_SIZE,count))
