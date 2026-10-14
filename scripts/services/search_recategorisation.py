"""Audited single-transaction category correction with stale-state protection."""

def recategorise_one(conn, txn_id, expected_sid, category, subcategory):
    conn.execute('BEGIN IMMEDIATE')
    row=conn.execute('''SELECT t.subcategory_id,t.merchant_core,c.name AS category,s.name AS subcategory
        FROM transactions t LEFT JOIN subcategories s ON t.subcategory_id=s.subcategory_id
        LEFT JOIN categories c ON c.category_id=s.category_id WHERE t.txn_id=?''',(txn_id,)).fetchone()
    if row is None:
        raise ValueError('Transaction no longer exists. Reload the search.')
    if row['subcategory_id']!=expected_sid:
        raise ValueError('This transaction was changed since the search loaded. Reload before editing.')
    old_label=f"{row['category']} / {row['subcategory']}" if row['category'] else 'Uncategorised'
    conn.execute('INSERT OR IGNORE INTO categories(name) VALUES (?)',(category,))
    cid=conn.execute('SELECT category_id FROM categories WHERE name=?',(category,)).fetchone()[0]
    conn.execute('INSERT OR IGNORE INTO subcategories(category_id,name) VALUES (?,?)',(cid,subcategory))
    sid=conn.execute('SELECT subcategory_id FROM subcategories WHERE category_id=? AND name=?',(cid,subcategory)).fetchone()[0]
    if sid!=expected_sid:
        conn.execute('UPDATE transactions SET subcategory_id=? WHERE txn_id=?',(sid,txn_id))
        conn.execute('''INSERT INTO edit_log(txn_id,field_changed,old_value,new_value,old_category,new_category,merchant_core,scope)
            VALUES (?,'subcategory_id',?,?,?,?,?,'single')''',
            (txn_id,str(expected_sid),str(sid),old_label,f'{category} / {subcategory}',row['merchant_core']))
    conn.execute("""UPDATE exceptions SET status='resolved',resolved_at=strftime('%Y-%m-%dT%H:%M:%S','now')
        WHERE txn_id=? AND status='open' AND type IN ('uncategorised','unknown_subcategory')""",(txn_id,))
    conn.commit()
    return sid
