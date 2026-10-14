import re
import sqlite3
from contextlib import closing
from tests.test_dashboard_security import DashboardFixture


class SearchBulkTests(DashboardFixture):
    def setUp(self):
        super().setUp()
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.execute('DELETE FROM transactions')
            for i in range(1,57):
                desc='MATCH' if i<=55 else 'OUTSIDE'
                c.execute("INSERT INTO transactions(txn_id,txn_date,description_raw,amount,source_file,row_ref,import_hash,merchant_core) VALUES (?,'2026-01-01',?,600,'fixture',?,?,'SAME')",(i,desc,str(i),str(i)))
            c.commit()

    def token_search(self):
        html=self.client.get('/transactions?q=MATCH&direction=credit',base_url=self.base).get_data(as_text=True)
        m=re.search(r'data-search-token="([^"]+)"',html)
        self.assertIsNotNone(m,'bulk selection requires a server-held search snapshot')
        assert m is not None
        return m.group(1)

    def preview(self, scope='all', ids=None):
        payload={'search_token':self.token_search(),'scope':scope,'category':'Fixture','subcategory':'Test'}
        if ids is not None:payload['txn_ids']=ids
        return self.post('/api/search-bulk-preview',payload)

    def test_all_matches_include_every_page_not_outside_results(self):
        rules=self.paths.category_rules_path.read_bytes()
        r=self.preview()
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json['count'],55)
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM transactions WHERE subcategory_id IS NOT NULL').fetchone()[0],0)
        result=self.post('/api/search-bulk-apply',{'preview_token':r.json['preview_token'],'approved':True})
        self.assertTrue(result.json['ok'])
        self.assertEqual(result.json['updated'],55)
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            self.assertIsNone(c.execute('SELECT subcategory_id FROM transactions WHERE txn_id=56').fetchone()[0])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM edit_log').fetchone()[0],55)
        self.assertEqual(rules,self.paths.category_rules_path.read_bytes())

    def test_selected_rows_only_and_replay_rejected(self):
        r=self.preview('selected',[55,53])
        self.assertEqual(r.json['count'],2)
        payload={'preview_token':r.json['preview_token'],'approved':True}
        self.assertTrue(self.post('/api/search-bulk-apply',payload).json['ok'])
        self.assertEqual(self.post('/api/search-bulk-apply',payload).status_code,409)
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM transactions WHERE subcategory_id IS NOT NULL').fetchone()[0],2)

    def test_stale_search_rejected_before_preview(self):
        token=self.token_search()
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.execute("UPDATE transactions SET description_raw='changed' WHERE txn_id=1");c.commit()
        r=self.post('/api/search-bulk-preview',{'search_token':token,'scope':'all','category':'Fixture','subcategory':'Test'})
        self.assertEqual(r.status_code,409)

    def test_stale_preview_rejected_and_no_other_rows_changed(self):
        r=self.preview()
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.execute('UPDATE transactions SET amount=601 WHERE txn_id=1');c.commit()
        r=self.post('/api/search-bulk-apply',{'preview_token':r.json['preview_token'],'approved':True})
        self.assertEqual(r.status_code,409)
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM edit_log').fetchone()[0],0)

    def test_invalid_selection_and_approval_rejected(self):
        token=self.token_search()
        for ids in ([56],[1],[True],[],[55,55]):
            r=self.post('/api/search-bulk-preview',{'search_token':token,'scope':'selected','txn_ids':ids,'category':'Fixture','subcategory':'Test'})
            self.assertEqual(r.status_code,400)
        r=self.preview()
        bad=self.post('/api/search-bulk-apply',{'preview_token':r.json['preview_token'],'approved':False})
        self.assertEqual(bad.status_code,400)

    def test_new_match_after_preview_blocks_all_results_apply(self):
        r=self.preview()
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.execute("UPDATE transactions SET description_raw='MATCH' WHERE txn_id=56");c.commit()
        response=self.post('/api/search-bulk-apply',{'preview_token':r.json['preview_token'],'approved':True})
        self.assertEqual(response.status_code,409)

    def test_csrf_and_session_ownership_enforced(self):
        r=self.preview()
        payload={'preview_token':r.json['preview_token'],'approved':True}
        self.assertEqual(self.client.post('/api/search-bulk-apply',json=payload,base_url=self.base).status_code,403)
        from scripts import dashboard as d
        other=d.app.test_client()
        csrf=other.get('/api/csrf-token',base_url=self.base).json['csrf_token']
        response=other.post('/api/search-bulk-apply',json=payload,base_url=self.base,
                            headers={'Origin':self.base,'X-CSRF-Token':csrf})
        self.assertEqual(response.status_code,409)

    def test_already_correct_rows_skip_audit_and_category_exceptions_resolve_only(self):
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.execute("INSERT INTO categories VALUES(1,'Fixture')")
            c.execute("INSERT INTO subcategories VALUES(1,1,'Test')")
            c.execute('UPDATE transactions SET subcategory_id=1 WHERE txn_id=55')
            c.execute("INSERT INTO exceptions(txn_id,type,status) VALUES(54,'uncategorised','open')")
            c.execute("INSERT INTO exceptions(txn_id,type,status) VALUES(54,'recon_warning','open')")
            c.commit()
        r=self.preview('selected',[55,54])
        response=self.post('/api/search-bulk-apply',{'preview_token':r.json['preview_token'],'approved':True})
        self.assertEqual(response.json['updated'],1)
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM edit_log').fetchone()[0],1)
            self.assertEqual(dict(c.execute('SELECT type,status FROM exceptions')),
                             {'uncategorised':'resolved','recon_warning':'open'})

    def test_bulk_failure_rolls_back_every_row(self):
        r=self.preview()
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.execute("CREATE TRIGGER fail_change BEFORE UPDATE ON transactions WHEN OLD.txn_id=25 BEGIN SELECT RAISE(ABORT,'fixture failure'); END");c.commit()
        r=self.post('/api/search-bulk-apply',{'preview_token':r.json['preview_token'],'approved':True})
        self.assertEqual(r.status_code,500)
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM transactions WHERE subcategory_id IS NOT NULL').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM edit_log').fetchone()[0],0)
