"""Explicit selection and read-only merchant history for exception review."""
import sqlite3


def review_groups(conn, exceptions):
    history = {}
    for row in conn.execute("""
        SELECT UPPER(TRIM(t.merchant_core)) AS merchant, c.name AS category,
               s.name AS subcategory, COUNT(*) AS count
        FROM transactions t
        JOIN subcategories s ON t.subcategory_id=s.subcategory_id
        JOIN categories c ON s.category_id=c.category_id
        WHERE COALESCE(t.excluded,0)=0 AND TRIM(COALESCE(t.merchant_core,'')) != ''
        GROUP BY UPPER(TRIM(t.merchant_core)),c.name,s.name
        ORDER BY count DESC,c.name,s.name
    """):
        history.setdefault(row['merchant'], []).append(dict(row))
    groups = {}
    for exc in exceptions:
        merchant = (exc.get('merchant_core') or '').strip().upper()
        # Never group unknown merchants together for bulk selection.
        key = merchant or f"unknown-{exc['exception_id']}"
        group = groups.setdefault(key, {'key':key, 'merchant':merchant or 'Unknown merchant',
                                        'history':history.get(merchant,[]), 'rows':[]})
        group['rows'].append(exc)
    return [groups[key] for key in sorted(groups)]


def resolve_selected(conn, ids, category, subcategory):
    """Atomically resolve only approved open uncategorised rows; reject stale lists."""
    conn.execute('BEGIN IMMEDIATE')
    slots=','.join('?' for _ in ids)
    rows=conn.execute(f"""
        SELECT e.exception_id,e.txn_id,e.status,e.type,t.subcategory_id,t.merchant_core
        FROM exceptions e JOIN transactions t ON e.txn_id=t.txn_id
        WHERE e.exception_id IN ({slots})
    """,ids).fetchall()
    if len(rows)!=len(ids) or any(r['status']!='open' or r['type']!='uncategorised' or r['subcategory_id'] is not None for r in rows):
        raise ValueError('Selection changed or is no longer uncategorised. Reload before applying.')
    conn.execute('INSERT OR IGNORE INTO categories(name) VALUES (?)',(category,))
    cat_id=conn.execute('SELECT category_id FROM categories WHERE name=?',(category,)).fetchone()[0]
    conn.execute('INSERT OR IGNORE INTO subcategories(category_id,name) VALUES (?,?)',(cat_id,subcategory))
    sid=conn.execute('SELECT subcategory_id FROM subcategories WHERE category_id=? AND name=?',(cat_id,subcategory)).fetchone()[0]
    for row in rows:
        conn.execute('UPDATE transactions SET subcategory_id=? WHERE txn_id=?',(sid,row['txn_id']))
        conn.execute("UPDATE exceptions SET status='resolved',resolved_at=strftime('%Y-%m-%dT%H:%M:%S','now') WHERE exception_id=?",(row['exception_id'],))
        conn.execute("""INSERT INTO edit_log(txn_id,field_changed,old_value,new_value,old_category,new_category,merchant_core,scope)
                        VALUES (?,'subcategory_id',?,?, 'Uncategorised',?,?, 'selected')""",
                     (row['txn_id'],str(row['subcategory_id']),str(sid),f'{category} / {subcategory}',row['merchant_core']))
    conn.commit()
    return ids
