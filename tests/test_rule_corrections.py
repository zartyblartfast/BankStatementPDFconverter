from contextlib import closing
import sqlite3
from tests.test_dashboard_security import DashboardFixture


class CorrectionScopeTests(DashboardFixture):
    def setUp(self):
        super().setUp()
        with closing(sqlite3.connect(self.paths.db_path)) as c, c:
            c.execute("INSERT INTO exceptions(exception_id,txn_id,type,status,merchant_core,amount) VALUES (1,1,'uncategorised','open','WRONG CLIENT HINT',-1)")

    def test_remember_requires_independent_preview_before_any_transaction_write(self):
        before = self.paths.db_path.read_bytes()
        rules = self.paths.category_rules_path.read_bytes()
        r = self.post('/api/resolve-exception',dict(exception_id=1,category='Fixture',subcategory='Test',remember_rule=True))
        self.assertEqual(r.status_code,400)
        self.assertIn('separate',r.json['error'])
        self.assertEqual(self.paths.db_path.read_bytes(),before)
        self.assertEqual(self.paths.category_rules_path.read_bytes(),rules)

    def test_exception_only_is_audited_and_ignores_client_merchant_and_replay(self):
        r = self.post('/api/resolve-exception',dict(exception_id=1,category='Fixture',subcategory='Test',merchant_core='CLIENT FORGERY'))
        self.assertTrue(r.json['ok'])
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            self.assertEqual(c.execute('SELECT merchant_core FROM transactions').fetchone()[0],'SYNTHETIC')
            self.assertEqual(c.execute('SELECT txn_id,scope FROM edit_log').fetchone(),(1,'single'))
        r = self.post('/api/resolve-exception',dict(exception_id=1,category='Fixture',subcategory='Test'))
        self.assertEqual(r.status_code,409)

    def test_selected_exception_response_keeps_int64_ids_exact(self):
        with closing(sqlite3.connect(self.paths.db_path)) as c, c:
            c.execute('UPDATE exceptions SET exception_id=9223372036854775807')
        response=self.post('/api/resolve-selected-exceptions',dict(exception_ids=[9223372036854775807],category='Fixture',subcategory='Test'))
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['resolved_ids'],['9223372036854775807'])

    def test_import_completion_links_to_shared_category_and_rule_review(self):
        from flask import render_template
        from scripts import dashboard as d
        with d.app.test_request_context('/',base_url=self.base):
            html=render_template('ingest.html',commit_result=dict(added=1,matched=0),commit_account=dict(account_id='FIXTURE',account_display_name='Fixture'),accounts=[],pdfs=[],folder_state=dict(can_save=False,choices=[],selected='',message='',ready=False))
        self.assertIn('Review categories and future rules',html)
        self.assertIn('href="/exceptions"',html)

    def test_page_defaults_to_only_transaction_and_separates_rule_review(self):
        html=self.client.get('/exceptions',base_url=self.base).get_data(as_text=True)
        self.assertIn('Transactions needing categorisation',html)
        self.assertIn('Optional rule review',html)
        self.assertNotIn('class="remember-cb" data-exc="1" checked',html)
        self.assertIn('Also use this category for future matches',html)
