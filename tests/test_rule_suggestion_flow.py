"""All-account suggestion defaults and stale proposal safeguards, synthetic only."""
from contextlib import closing
import sqlite3
from tests.test_dashboard_security import DashboardFixture


class RuleSuggestionFlowTests(DashboardFixture):
    def setUp(self):
        super().setUp()
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute('''CREATE TABLE accounts(account_id TEXT PRIMARY KEY, display_name TEXT,
                currency TEXT, institution TEXT, owned INTEGER, opened_on TEXT, closed_on TEXT,
                parser_key TEXT, parser_verified INTEGER, import_enabled INTEGER)''')
            conn.executemany('INSERT INTO accounts VALUES (?, ?, "GBP", NULL, 1, NULL, NULL, NULL, 0, 0)',
                             [('ALPHA', 'Synthetic first'), ('BETA', 'Synthetic second')])
            conn.execute("UPDATE transactions SET account_id='ALPHA'")
            conn.execute('''INSERT INTO transactions(txn_id,txn_date,description_raw,amount,source_file,row_ref,import_hash,account_id)
                VALUES (9223372036854775807,'2026-01-02','synthetic',-2,'other.pdf','2','second','BETA')''')

    def get(self, url):
        return self.client.get(url, base_url=self.base)

    def categorise_fixture(self):
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute("INSERT INTO categories VALUES (1,'Fixture')")
            conn.execute("INSERT INTO subcategories VALUES (1,1,'Test')")
            conn.execute('UPDATE transactions SET subcategory_id=1')

    def test_all_account_evidence_csv_and_existing_rule_changes_stale_proposal(self):
        self.categorise_fixture()
        suggestion = self.get('/api/rules/suggestion?key=SYNTHETIC').json
        payload = dict(key='SYNTHETIC', category='Fixture', subcategory='Test', enabled=True,
                       suggestion_revision=suggestion['suggestion_revision'])
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute("UPDATE transactions SET subcategory_id=NULL WHERE account_id='BETA'")
        self.assertIn('Reopen', self.post('/api/rules/preview', payload).json['error'])
        payload['suggestion_revision'] = self.get('/api/rules/suggestion?key=SYNTHETIC').json['suggestion_revision']
        self.paths.category_rules_path.write_text('merchant_core,pattern,category,subcategory,notes\nOTHER,,Fixture,Test,\n')
        self.assertIn('Reopen', self.post('/api/rules/preview', payload).json['error'])
        self.paths.category_rules_path.write_text('merchant_core,pattern,category,subcategory,notes\nSYNTHETIC,,Fixture,Test,\n')
        self.assertEqual(self.get('/api/rules/suggestion?key=SYNTHETIC').status_code, 409)
        self.assertIn('Reopen', self.post('/api/rules/preview', payload).json['error'])
    def test_proposal_revision_stales_before_obsolete_destination_validation(self):
        self.categorise_fixture()
        self.paths.category_rules_path.write_text('merchant_core,pattern,category,subcategory,notes\n')
        suggestion = self.get('/api/rules/suggestion?key=SYNTHETIC').json
        payload = dict(key='SYNTHETIC', category='Fixture', subcategory='Test', enabled=True,
                       suggestion_revision=suggestion['suggestion_revision'])
        preview = self.post('/api/rules/preview', payload)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(len(preview.json['matches']), 2)
        before = self.paths.category_rules_path.read_bytes()
        self.paths.categories_path.write_text('{"Other": ["New"]}')
        stale = self.post('/api/rules/preview', payload)
        self.assertEqual(stale.status_code, 409)
        self.assertIn('Reopen', stale.json['error'])
        self.assertEqual(before, self.paths.category_rules_path.read_bytes())

    def test_conflict_missing_obsolete_and_absent_evidence_require_choice(self):
        self.categorise_fixture()
        self.paths.category_rules_path.write_text('merchant_core,pattern,category,subcategory,notes\n')
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute("INSERT INTO categories VALUES (2,'Other')")
            conn.execute("INSERT INTO subcategories VALUES (2,2,'Old')")
            conn.execute("UPDATE transactions SET subcategory_id=2 WHERE account_id='BETA'")
        for destination in (2, None):
            with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
                conn.execute("UPDATE transactions SET subcategory_id=? WHERE account_id='BETA'", (destination,))
            data = self.get('/api/rules/suggestion?key=SYNTHETIC').json
            self.assertIsNone(data['proposal'])
            self.assertEqual(sum(r['count'] for r in data['assignments']), 2)
            self.assertIn('explicit choice', data['explanation'])
        self.paths.categories_path.write_text('{"Other": ["New"]}')
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute('UPDATE transactions SET subcategory_id=1')
        self.assertIsNone(self.get('/api/rules/suggestion?key=SYNTHETIC').json['proposal'])
        self.assertIsNone(self.get('/api/rules/suggestion?key=NO%20HISTORY').json['proposal'])
        for query in ('key=synthetic', 'key=SYNTHETIC&key=SYNTHETIC', 'key=SYNTHETIC&account_id=ALPHA'):
            self.assertEqual(self.get('/api/rules/suggestion?'+query).status_code, 409)

    def test_real_controllers_direct_edit_preview_cancel(self):
        import json
        import subprocess
        from pathlib import Path
        self.categorise_fixture()
        self.paths.category_rules_path.write_text('merchant_core,pattern,category,subcategory,notes\n')
        suggestion = self.get('/api/rules/suggestion?key=SYNTHETIC').json
        payload = dict(key='SYNTHETIC', category='Fixture', subcategory='Test', enabled=True,
                       suggestion_revision=suggestion['suggestion_revision'])
        preview = self.post('/api/rules/preview', payload).json
        before = self.paths.db_path.read_bytes(), self.paths.category_rules_path.read_bytes()
        result = subprocess.run(['node', 'tests/rule_suggestion_probe.js'],
                                input=json.dumps(dict(suggestion=suggestion, preview=preview)),
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(before, (self.paths.db_path.read_bytes(), self.paths.category_rules_path.read_bytes()))
        selected = json.loads(result.stdout)['payload']
        confirmation = self.post('/api/rules/preview', selected)
        self.assertEqual(confirmation.status_code, 200)
        saved = self.post('/api/rules/confirm', dict(preview_token=confirmation.json['preview_token'], approved=True))
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(before[0], self.paths.db_path.read_bytes())
        self.assertEqual(self.get('/api/rules?key=SYNTHETIC').json['rule'], dict(category='Fixture', subcategory='Test', enabled=True))

    def test_exceptions_inline_proposal_but_saved_rule_keeps_maintenance_link(self):
        import re
        self.categorise_fixture()
        self.paths.category_rules_path.write_text('merchant_core,pattern,category,subcategory,notes\n')
        page = self.get('/exceptions').get_data(as_text=True)
        self.assertRegex(page, r'class="rule-edit[^"]*"[^>]*>View suggestion</button>')
        self.assertNotIn('intent=add_rule', page)
        self.paths.category_rules_path.write_text('merchant_core,pattern,category,subcategory,notes\nSYNTHETIC,,Other,Old,\n')
        page = self.get('/exceptions').get_data(as_text=True)
        self.assertRegex(page, r'href="/rules\?key=SYNTHETIC">View saved rule')
        self.assertNotIn('intent=add_rule', page)

    def test_unanimous_current_destination_is_an_editable_proposal(self):
        self.categorise_fixture()
        self.paths.category_rules_path.write_text('merchant_core,pattern,category,subcategory,notes\n')
        before = self.paths.db_path.read_bytes(), self.paths.category_rules_path.read_bytes()
        response = self.get('/api/rules/suggestion?key=SYNTHETIC')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['proposal'], dict(category='Fixture', subcategory='Test'))
        self.assertEqual(response.json['assignments'], [dict(category='Fixture', subcategory='Test', count=2, valid=True)])
        self.assertEqual(response.json['categories'], {'Fixture': ['Test']})
        self.assertEqual(before, (self.paths.db_path.read_bytes(), self.paths.category_rules_path.read_bytes()))
