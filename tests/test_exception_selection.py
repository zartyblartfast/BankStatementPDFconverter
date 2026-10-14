from contextlib import closing
import sqlite3
from tests.test_dashboard_security import DashboardFixture


class ExceptionSelectionTests(DashboardFixture):
    def setUp(self):
        super().setUp()
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.execute("INSERT INTO categories VALUES (1,'Fixture')")
            c.execute("INSERT INTO subcategories VALUES (1,1,'Test')")
            c.execute("UPDATE transactions SET subcategory_id=1 WHERE txn_id=1")
            for i in (2,3,4):
                c.execute("INSERT INTO transactions(txn_id,txn_date,description_raw,amount,source_file,row_ref,import_hash,merchant_core) VALUES (?,'2026-02-01','sample',-10,'fixture',?,?, 'SYNTHETIC')", (i,str(i),str(i)))
                c.execute("INSERT INTO exceptions(exception_id,txn_id,type,merchant_core,txn_date,amount) VALUES (?,?,'uncategorised','SYNTHETIC','2026-02-01',-10)",(i,i))
            c.commit()

    def test_group_history_visible_without_prefilling(self):
        html=self.client.get('/exceptions',base_url=self.base).get_data(as_text=True)
        self.assertIn('Previous assignments',html)
        self.assertIn('Fixture / Test',html)
        self.assertIn('Select this merchant',html)
        self.assertIn('No transactions are applied automatically',html)

    def test_only_explicit_selected_rows_change_and_rules_unchanged(self):
        before=self.paths.category_rules_path.read_bytes()
        response=self.post('/api/resolve-selected-exceptions',{'exception_ids':[2,3], 'category':'Fixture','subcategory':'Test'})
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json['ok'])
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            self.assertEqual(c.execute('SELECT status FROM exceptions WHERE exception_id=4').fetchone()[0],'open')
            self.assertIsNone(c.execute('SELECT subcategory_id FROM transactions WHERE txn_id=4').fetchone()[0])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM edit_log').fetchone()[0],2)
        self.assertEqual(before,self.paths.category_rules_path.read_bytes())

    def test_invalid_or_stale_selection_changes_nothing(self):
        for ids in ([2,999],[2,2],[True],[],[9223372036854775808]):
            r=self.post('/api/resolve-selected-exceptions',{'exception_ids':ids,'category':'Fixture','subcategory':'Test'})
            self.assertIn(r.status_code,(400,409))
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM exceptions WHERE status='open'").fetchone()[0],3)

    def test_failed_second_update_rolls_back_first_and_audit(self):
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.execute("CREATE TRIGGER refuse_second BEFORE UPDATE ON transactions WHEN OLD.txn_id=3 BEGIN SELECT RAISE(ABORT,'test failure'); END")
            c.commit()
        r=self.post('/api/resolve-selected-exceptions',{'exception_ids':[2,3],'category':'Fixture','subcategory':'Test'})
        self.assertEqual(r.status_code,500)
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM exceptions WHERE status='open'").fetchone()[0],3)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM edit_log').fetchone()[0],0)
            self.assertIsNone(c.execute('SELECT subcategory_id FROM transactions WHERE txn_id=2').fetchone()[0])

    def test_conflicting_history_shows_all_assignments(self):
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.execute("INSERT INTO subcategories VALUES (2,1,'Other')")
            c.execute("UPDATE transactions SET subcategory_id=2 WHERE txn_id=4")
            c.execute("UPDATE exceptions SET status='resolved' WHERE exception_id=4")
            c.commit()
        html=self.client.get('/exceptions',base_url=self.base).get_data(as_text=True)
        self.assertIn('Fixture / Test',html)
        self.assertIn('Fixture / Other',html)

    def test_unknown_category_rejected_and_csrf_required(self):
        r=self.post('/api/resolve-selected-exceptions',{'exception_ids':[2],'category':'Unknown','subcategory':'Test'})
        self.assertEqual(r.status_code,400)
        r=self.client.post('/api/resolve-selected-exceptions',base_url=self.base,json={'exception_ids':[2],'category':'Fixture','subcategory':'Test'})
        self.assertEqual(r.status_code,403)
