"""Bounded session-owned search snapshots and explicit atomic bulk corrections."""
import secrets
import threading
import time
from scripts.services.transaction_search import matching_snapshot

_CACHE = {}
_LOCK = threading.Lock()
TTL_SECONDS = 1200


def hold(owner, kind, data):
    now=time.monotonic()
    with _LOCK:
        for key in list(_CACHE):
            if _CACHE[key]['expires']<now: del _CACHE[key]
        while len(_CACHE)>=64: del _CACHE[next(iter(_CACHE))]
        token=secrets.token_urlsafe(32)
        _CACHE[token]=dict(owner=owner,kind=kind,expires=now+TTL_SECONDS,data=data)
    return token


def load(token, owner, kind):
    if not isinstance(token,str): raise ValueError('Reload the search and preview the selection again.')
    with _LOCK:
        item=_CACHE.get(token)
        if not item or item['expires']<time.monotonic() or item['owner']!=owner or item['kind']!=kind:
            raise ValueError('Search or preview expired. Reload and review again.')
        return item['data']


def discard(token):
    with _LOCK: _CACHE.pop(token,None)


def check_snapshot(conn, state):
    current=matching_snapshot(conn,state['filters'],state['cents'])
    expected=state['snapshot']
    if state['scope']=='all': valid=current==expected
    else: valid=all(current.get(i)==digest for i,digest in expected.items())
    if not valid: raise ValueError('Matching transactions changed since review. Reload the search before applying.')


def apply_bulk(conn, state):
    conn.execute('BEGIN IMMEDIATE')
    check_snapshot(conn,state)
    category,subcategory=state['category'],state['subcategory']
    conn.execute('INSERT OR IGNORE INTO categories(name) VALUES (?)',(category,))
    cid=conn.execute('SELECT category_id FROM categories WHERE name=?',(category,)).fetchone()[0]
    conn.execute('INSERT OR IGNORE INTO subcategories(category_id,name) VALUES (?,?)',(cid,subcategory))
    sid=conn.execute('SELECT subcategory_id FROM subcategories WHERE category_id=? AND name=?',(cid,subcategory)).fetchone()[0]
    updated=0
    for txn_id in state['snapshot']:
        row=conn.execute('''SELECT t.subcategory_id,t.merchant_core,c.name AS category,s.name AS subcategory
            FROM transactions t LEFT JOIN subcategories s ON t.subcategory_id=s.subcategory_id
            LEFT JOIN categories c ON c.category_id=s.category_id WHERE t.txn_id=?''',(txn_id,)).fetchone()
        if row['subcategory_id']!=sid:
            old_label=f"{row['category']} / {row['subcategory']}" if row['category'] else 'Uncategorised'
            conn.execute('UPDATE transactions SET subcategory_id=? WHERE txn_id=?',(sid,txn_id))
            conn.execute('''INSERT INTO edit_log(txn_id,field_changed,old_value,new_value,old_category,new_category,merchant_core,scope)
                VALUES (?,'subcategory_id',?,?,?,?,?,?)''',
                (txn_id,str(row['subcategory_id']),str(sid),old_label,f'{category} / {subcategory}',row['merchant_core'],
                 'search_all' if state['scope']=='all' else 'selected'))
            updated+=1
        conn.execute("""UPDATE exceptions SET status='resolved',resolved_at=strftime('%Y-%m-%dT%H:%M:%S','now')
            WHERE txn_id=? AND status='open' AND type IN ('uncategorised','unknown_subcategory')""",(txn_id,))
    conn.commit()
    return updated
