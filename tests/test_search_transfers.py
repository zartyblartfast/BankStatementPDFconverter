"""Transaction search transfer controls, always on explicit scratch fixtures."""
from contextlib import closing
import sqlite3
import subprocess
from pathlib import Path
import unittest
from flask.testing import FlaskClient
from unittest.mock import patch
from tests import test_transfer_ui as ui_fixture
from scripts import dashboard
from scripts.services import transfers


class SearchTransferTests(unittest.TestCase):
    db: Path
    root: Path

    client: FlaskClient

    def setUp(self):
        ui_fixture.TransferUITests.setUp(self)  # type: ignore[arg-type]
        config = self.root / 'config'
        config.mkdir()
        categories = config / 'categories.json'
        rules = config / 'category_rules.csv'
        categories.write_text('{"Own accounts":["in","out"]}', encoding='utf-8')
        rules.write_text('merchant_core,category,subcategory\n', encoding='utf-8')
        for key, value in [('CATEGORIES_JSON', categories), ('RULES_CSV', rules), ('REPORTS_DIR', self.root / 'reports')]:
            p = patch.object(dashboard, key, value)
            p.start()
            self.addCleanup(p.stop)

    sql = ui_fixture.TransferUITests.sql
    post = ui_fixture.TransferUITests.post

    def test_search_names_raw_totals_and_reversible_status_without_category_changes(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE accounts SET display_name='Demo current' WHERE account_id='HSBC_UK'")
            conn.execute("UPDATE accounts SET display_name='Demo savings' WHERE account_id='IOM'")
            conn.execute("INSERT INTO categories VALUES (900,'Own accounts')")
            conn.execute("INSERT INTO subcategories VALUES (900,900,'out')")
            conn.execute('UPDATE transactions SET subcategory_id=900 WHERE txn_id=1')
        before = self.sql('SELECT * FROM transactions ORDER BY txn_id')
        page = self.client.get('/transactions').get_data(as_text=True)
        self.assertIn('Demo current', page)
        self.assertIn('Demo savings', page)
        self.assertIn('Credits (include transfers)', page)
        self.assertIn('Mark as transfer', page)
        self.assertTrue('Select exactly two rows on this page' in page, 'Explain the explicit page-only pair selection.')
        state = self.post('/api/transfers/preview', {'action':'pair','txn_ids':[1,2]}).json
        self.assertEqual(self.post('/api/transfers/confirm', {'preview_token':state['preview_token'],'approved':True}).status_code, 200)
        page = self.client.get('/transactions').get_data(as_text=True)
        self.assertEqual(page.count('Transfer — excluded from income/expenditure'), 2)
        self.assertEqual(page.count('Undo transfer</button>'), 2)
        self.assertIn('Own accounts / out', page)
        self.assertIn('£10.00', page)
        group = self.sql('SELECT group_id FROM transfer_groups')[0][0]
        state = self.post('/api/transfers/preview', {'action':'revoke','group_id':group}).json
        self.post('/api/transfers/confirm', {'preview_token':state['preview_token'],'approved':True})
        page = self.client.get('/transactions').get_data(as_text=True)
        self.assertNotIn('Transfer — excluded from income/expenditure', page)
        self.assertNotIn('Undo transfer</button>', page)
        self.assertEqual(self.sql('SELECT * FROM transactions ORDER BY txn_id'), before)
    def test_actual_javascript_controller(self):
        for scenario in ('pair', 'undo', 'errors', 'network', 'escape', 'bulk'):
            with self.subTest(scenario=scenario):
                result = subprocess.run(['node', str(Path(__file__).with_name('search_transfers_probe.js')), scenario],
                                        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding='utf-8')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('passed', result.stdout)

    def test_api_pair_and_undo_agree_with_movement_planning_and_report_evidence(self):
        from scripts.services.movement_policy import summarise_movement
        from scripts.report_data import query_report
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO transactions(txn_id,txn_date,description_raw,amount,account_id,import_hash) VALUES (3,'2026-01-31','Fee',-1.23,'HSBC_UK','fee')")
        original = self.sql('SELECT * FROM transactions ORDER BY txn_id')
        configs = {p:p.read_bytes() for p in (self.root / 'config').iterdir()}
        def snapshot():
            movement = summarise_movement([1,2,3], db_path=self.db, include_excluded=False)
            report, _ = query_report(db_path=self.db, account_ids=['HSBC_UK','IOM'])
            planning = self.client.get('/api/planning-data').json
            for section in ('raw','adjusted'):
                self.assertEqual(planning['movement'][section], movement[section])
                self.assertEqual(report.policy[section], {k:str(v) for k,v in movement[section].items()})
            self.assertEqual(self.sql('SELECT * FROM transactions ORDER BY txn_id'), original)
            self.assertEqual({p:p.read_bytes() for p in configs}, configs)
            return movement
        initial = snapshot()
        preview = self.post('/api/transfers/preview', {'action':'pair','txn_ids':[1,2]}).json
        self.assertEqual([r['txn_date'] for r in preview['members']], ['2026-01-31','2026-02-02'])
        result = self.post('/api/transfers/confirm', {'preview_token':preview['preview_token'],'approved':True}).json
        paired = snapshot()
        self.assertEqual(paired['raw'], initial['raw'])
        self.assertEqual(paired['adjusted']['debits_pence'], 123)
        self.assertEqual(paired['adjusted']['credits_pence'], 0)
        self.assertEqual(paired['transfers']['in_pence'], 1000)
        self.assertEqual(paired['transfers']['out_pence'], 1000)
        self.assertEqual(self.post('/api/transfers/preview', {'action':'pair','txn_ids':[1,2]}).status_code, 400)
        undo = self.post('/api/transfers/preview', {'action':'revoke','group_id':result['group_id']}).json
        self.assertEqual(self.post('/api/transfers/confirm', {'preview_token':undo['preview_token'],'approved':True}).status_code, 200)
        restored = snapshot()
        self.assertEqual(restored['adjusted'], initial['adjusted'])
        self.assertEqual(restored['transfers']['in_count'], 0)
        self.assertEqual(self.sql('SELECT action FROM transfer_audit ORDER BY audit_id'), [('pair',),('revoke',)])

    def test_search_status_uses_verified_support_not_category_hints(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DELETE FROM schema_migrations WHERE version=4')
        before = self.db.read_bytes()
        html = self.client.get('/transactions').get_data(as_text=True)
        self.assertIn('Transfer status unavailable', html)
        self.assertNotIn('Transfer — excluded from income/expenditure', html)
        self.assertNotIn('Undo transfer</button>', html)
        self.assertIn('data-transfer-ready="0"', html)
        self.assertEqual(self.db.read_bytes(), before)

    def test_api_rejects_invalid_pair_account_amount_and_stale_approvals(self):
        original = self.sql('SELECT * FROM transactions ORDER BY txn_id')
        for sql in ("UPDATE transactions SET account_id='HSBC_UK' WHERE txn_id=2",
                    'UPDATE transactions SET amount=10.01 WHERE txn_id=2',
                    "UPDATE accounts SET owned=0 WHERE account_id='IOM'",
                    "UPDATE accounts SET currency='USD' WHERE account_id='IOM'"):
            with self.subTest(sql=sql):
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute(sql)
                self.assertEqual(self.post('/api/transfers/preview', {'action':'pair','txn_ids':[1,2]}).status_code, 400)
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute("UPDATE transactions SET account_id='IOM',amount=10 WHERE txn_id=2")
                    conn.execute("UPDATE accounts SET owned=1,currency='GBP' WHERE account_id='IOM'")
        preview = self.post('/api/transfers/preview', {'action':'pair','txn_ids':[1,2]}).json
        self.post('/api/transfers/cancel', {'preview_token':preview['preview_token']})
        self.assertEqual(self.post('/api/transfers/confirm', {'preview_token':preview['preview_token'],'approved':True}).status_code, 409)
        preview = self.post('/api/transfers/preview', {'action':'pair','txn_ids':[1,2]}).json
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE transactions SET user_note='synthetic edit' WHERE txn_id=1")
        self.assertEqual(self.post('/api/transfers/confirm', {'preview_token':preview['preview_token'],'approved':True}).json['error'], 'stale_preview')
        self.assertEqual(self.sql('SELECT * FROM transfer_audit'), [])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE transactions SET user_note='keep' WHERE txn_id=1")
        self.assertEqual(self.sql('SELECT * FROM transactions ORDER BY txn_id'), original)


if __name__ == '__main__':
    unittest.main()
