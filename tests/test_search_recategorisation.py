from contextlib import closing
from html.parser import HTMLParser
import json
from pathlib import Path
import sqlite3
import subprocess
from unittest.mock import patch
from tests.test_dashboard_security import DashboardFixture


class SearchRecategorisationTests(DashboardFixture):
    def setUp(self):
        super().setUp()
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.execute("INSERT INTO categories VALUES (1,'Fixture')")
            c.execute("INSERT INTO subcategories VALUES (1,1,'Old')")
            c.execute('UPDATE transactions SET subcategory_id=1 WHERE txn_id=1')
            c.execute("INSERT INTO transactions(txn_id,txn_date,description_raw,amount,source_file,row_ref,import_hash,merchant_core) VALUES (2,'2026-01-01','Synthetic',-2,'fixture','2','second','SYNTHETIC')")
            c.execute("INSERT INTO exceptions(txn_id,type,status) VALUES (2,'uncategorised','open')")
            c.execute("INSERT INTO exceptions(txn_id,type,status) VALUES (2,'recon_warning','open')")
            c.commit()

    def payload(self, **changes):
        return dict(txn_id=1,old_subcategory_id=1,category='Fixture',subcategory='Test',**changes)

    def test_search_page_has_manage_and_single_row_dialog(self):
        html=self.client.get('/transactions?q=Synthetic',base_url=self.base).get_data(as_text=True)
        self.assertIn('Manage categories',html)
        self.assertIn('Re-categorise',html)
        self.assertIn('Only this transaction',html)
        self.assertIn('id="searchRecategoryFuture"',html)
        self.assertIn('Also use this category for future matches',html)
        self.assertIn('data-old-subcategory="1"',html)

    def test_actual_controller_context_matches_rendered_account_column_rows(self):
        # Parse the real Flask response, retaining cell order, classes and button data.
        class SearchRows(HTMLParser):
            def __init__(self):
                super().__init__()
                self.rows, self.ids = [], []
                self.row = self.cell = None

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if 'id' in attrs:
                    self.ids.append(attrs['id'])
                if tag == 'tr':
                    self.row = {'cells': [], 'buttons': []}
                elif tag == 'td' and self.row is not None:
                    self.cell = {'attrs': attrs, 'textContent': ''}
                    self.row['cells'].append(self.cell)
                elif tag == 'button' and self.row is not None and 'recat-search' in (attrs.get('class') or '').split():
                    self.row['buttons'].append(attrs)

            def handle_data(self, data):
                if self.cell is not None:
                    self.cell['textContent'] += data

            def handle_endtag(self, tag):
                if tag == 'td':
                    self.cell = None
                elif tag == 'tr' and self.row is not None:
                    if self.row['buttons']:
                        self.rows.append(self.row)
                    self.row = None

        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute("UPDATE transactions SET description_raw='Synthetic <shop> & goods' WHERE txn_id=1")
        response = self.client.get('/transactions', base_url=self.base)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('<th>Account</th>', html)
        parsed = SearchRows()
        parsed.feed(html)
        self.assertEqual(len(parsed.rows), 2)
        result = subprocess.run(
            ['node', str(Path(__file__).with_name('search_recategorisation_probe.js'))],
            input=json.dumps({'rows': parsed.rows, 'ids': parsed.ids}),
            cwd=Path(__file__).resolve().parents[1], capture_output=True,
            text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('2 rendered row contexts passed', result.stdout)

    def test_only_target_changes_with_audit_and_no_config_changes(self):
        rules=self.paths.category_rules_path.read_bytes()
        tree=self.paths.categories_path.read_bytes()
        r=self.post('/api/search-recategorize',self.payload())
        self.assertEqual(r.status_code,200)
        self.assertTrue(r.json['ok'])
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            self.assertIsNone(c.execute('SELECT subcategory_id FROM transactions WHERE txn_id=2').fetchone()[0])
            log=c.execute('SELECT txn_id,old_category,new_category,scope FROM edit_log').fetchone()
            self.assertEqual(log,(1,'Fixture / Old','Fixture / Test','single'))
        self.assertEqual(rules,self.paths.category_rules_path.read_bytes())
        self.assertEqual(tree,self.paths.categories_path.read_bytes())

    def test_uncategorised_target_resolves_only_category_exceptions(self):
        r=self.post('/api/search-recategorize',dict(txn_id=2,old_subcategory_id=None,category='Fixture',subcategory='Test'))
        self.assertEqual(r.status_code,200)
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            statuses=dict(c.execute('SELECT type,status FROM exceptions WHERE txn_id=2'))
        self.assertEqual(statuses,{'uncategorised':'resolved','recon_warning':'open'})

    def test_stale_or_invalid_payload_does_not_write(self):
        for payload in (None,{},dict(txn_id=True,old_subcategory_id=1,category='Fixture',subcategory='Test'),
                        dict(txn_id=1,category='Fixture',subcategory='Test'),
                        dict(txn_id=1,old_subcategory_id=None,category='Fixture',subcategory='Test'),
                        dict(txn_id=1,old_subcategory_id=1,category='Fixture',subcategory='Unknown'),
                        dict(txn_id=1,old_subcategory_id=1,category='Fixture',subcategory='Test',scope='merchant'),
                        dict(txn_id=1,old_subcategory_id=1,category='Fixture',subcategory='Test',remember_rule=True)):
            r=self.post('/api/search-recategorize',payload)
            self.assertIn(r.status_code,(400,409))
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            self.assertEqual(c.execute('SELECT subcategory_id FROM transactions WHERE txn_id=1').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM edit_log').fetchone()[0],0)

    def test_failed_audit_write_rolls_back_category_and_exceptions(self):
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON edit_log BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
            c.commit()
        r=self.post('/api/search-recategorize',dict(txn_id=2,old_subcategory_id=None,category='Fixture',subcategory='Test'))
        self.assertEqual(r.status_code,500)
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            self.assertIsNone(c.execute('SELECT subcategory_id FROM transactions WHERE txn_id=2').fetchone()[0])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM exceptions WHERE status='open'").fetchone()[0],2)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM edit_log').fetchone()[0],0)

    def test_csrf_required_and_report_failure_not_reported_as_unsaved(self):
        r=self.client.post('/api/search-recategorize',json=self.payload(),base_url=self.base)
        self.assertEqual(r.status_code,403)
        with patch('scripts.dashboard._regenerate_report',side_effect=RuntimeError('fixture')):
            r=self.post('/api/search-recategorize',self.payload())
        self.assertTrue(r.json['ok'])
        self.assertIn('reports',r.json['warning'])
