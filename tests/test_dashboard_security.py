from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import dashboard as d
from scripts.init_db import init_db
from scripts.settings import get_settings


class DashboardFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'project'
        self.paths = get_settings(self.root)
        self.paths.config_dir.mkdir(parents=True)
        self.paths.categories_path.write_text('{"Fixture": ["Test"]}', encoding='utf-8')
        self.paths.category_rules_path.write_text('merchant_core,pattern,category,subcategory,notes\n', encoding='utf-8')
        init_db(db_path=self.paths.db_path)
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.execute("INSERT INTO transactions (txn_date,description_raw,amount,source_file,row_ref,import_hash,merchant_core) VALUES ('2026-01-01','synthetic',-1,'fixture.pdf','1','fixture','SYNTHETIC')")
            c.commit()
        for name, value in {'PROJECT_ROOT': self.root, 'DB_PATH': self.paths.db_path,
                            'CATEGORIES_JSON': self.paths.categories_path, 'RULES_CSV': self.paths.category_rules_path,
                            'REPORTS_DIR': self.paths.reports_dir, 'STATEMENTS_DIR': self.paths.statements_dir}.items():
            p = patch.object(d, name, value); p.start(); self.addCleanup(p.stop)
        p = patch.object(d, '_regenerate_report'); p.start(); self.addCleanup(p.stop)
        self.client = d.app.test_client()
        self.base = 'http://127.0.0.1:5000'

    def token(self):
        response = self.client.get('/api/csrf-token', base_url=self.base)
        self.assertEqual(response.status_code, 200)
        return response.json['csrf_token']

    def post(self, route, payload, **headers):
        return self.client.post(route, json=payload, base_url=self.base,
                                headers={'Origin': self.base, 'X-CSRF-Token': self.token(), **headers})


class SecurityTests(DashboardFixture):
    def test_home_net_sign_precedes_currency_symbol(self):
        for amount, expected in ((5667.43,'+£5,667.43'),(-1,'-£1.00'),(0,'+£0.00')):
            with self.subTest(amount=amount):
                with closing(sqlite3.connect(self.paths.db_path)) as c:
                    c.execute('UPDATE transactions SET amount=?',(amount,))
                    c.commit()
                html=self.client.get('/',base_url=self.base).get_data(as_text=True)
                self.assertIn('>'+expected+'</div>',html)
                self.assertNotIn('£+',html)
                self.assertNotIn('£-',html)

    def test_home_statement_range_uses_dates_not_path_sort_order(self):
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            for i,source in enumerate(('Z/2025-01-10_Statement.pdf','A/2026-04-10_Statement.pdf',
                                       'B/2026-10-10_Statement.pdf','2020-01-01/archive/2026-02-10_Statement.pdf'),2):
                c.execute("INSERT INTO transactions(txn_date,description_raw,amount,source_file,row_ref,import_hash) VALUES('2026-01-01','fixture',-1,?,?,?)",(source,str(i),str(i)))
            c.commit()
        html=self.client.get('/',base_url=self.base).get_data(as_text=True)
        self.assertIn('2025-01 to 2026-10',html)
        self.assertNotIn('2020-01 to',html)

    def test_statement_range_handles_metadata_keys_and_invalid_dates(self):
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.execute('CREATE TABLE source_documents(document_key TEXT)')
            c.executemany('INSERT INTO source_documents VALUES (?)',[
                ('StatementsPDF/2025-01-10_Statement.pdf',),
                ('StatementsPDF\\2026-10-10_Statement.pdf',),
                ('2020-01-01/archive/2026-02-10_Statement.pdf',),
                ('StatementsPDF/2024-99-10_Statement.pdf',),
                ('m1:hash-with-no-statement-date',)])
            c.commit()
            self.assertEqual(d._home_statement_range(c),'2025-01 to 2026-10')

    def test_home_shows_coverage_and_categorisation_indicators(self):
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.execute('''CREATE TABLE source_documents (
                document_id INTEGER PRIMARY KEY,
                account_id TEXT NOT NULL,
                document_key TEXT NOT NULL,
                source_identity TEXT NOT NULL UNIQUE,
                content_hash TEXT NOT NULL,
                coverage_start TEXT,
                coverage_end TEXT,
                coverage_row_ref TEXT,
                reconciliation_status TEXT NOT NULL
            )''')
            c.executemany('''INSERT INTO source_documents
                (account_id, document_key, source_identity, content_hash, coverage_start, coverage_end, coverage_row_ref, reconciliation_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''', [
                ('HSBC_UK', 'StatementsPDF/a.pdf', 'HSBC_UK:a', 'hash-a', '2026-04-11', '2026-05-10', '1:1', 'passed'),
                ('HSBC_UK', 'StatementsPDF/b.pdf', 'HSBC_UK:b', 'hash-b', '2026-05-11', '2026-06-10', '1:1', 'passed'),
            ])
            c.commit()

        page = self.client.get('/', base_url=self.base)

        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('Coverage', html)
        self.assertIn('1 complete month', html)
        self.assertIn('2 partial months', html)
        self.assertIn('Categorisation', html)
        self.assertIn('1 uncategorised', html)

    def test_home_shows_coverage_aware_complete_month_averages(self):
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.execute('''CREATE TABLE source_documents (
                document_id INTEGER PRIMARY KEY,
                account_id TEXT NOT NULL,
                document_key TEXT NOT NULL,
                source_identity TEXT NOT NULL UNIQUE,
                content_hash TEXT NOT NULL,
                coverage_start TEXT,
                coverage_end TEXT,
                coverage_row_ref TEXT,
                reconciliation_status TEXT NOT NULL
            )''')
            c.execute("DELETE FROM transactions")
            c.executemany('''INSERT INTO source_documents
                (account_id, document_key, source_identity, content_hash, coverage_start, coverage_end, coverage_row_ref, reconciliation_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''', [
                ('HSBC_UK', 'StatementsPDF/may.pdf', 'HSBC_UK:may', 'hash-may', '2026-05-01', '2026-05-31', '1:1', 'passed'),
                ('HSBC_UK', 'StatementsPDF/june.pdf', 'HSBC_UK:june', 'hash-june', '2026-06-10', '2026-06-30', '1:1', 'passed'),
            ])
            c.executemany("""
                INSERT INTO transactions (txn_date,description_raw,amount,source_file,row_ref,import_hash,merchant_core)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, [
                ('2026-05-05', 'income', 1000.00, 'may.pdf', '1', 'avg-1', 'INCOME'),
                ('2026-05-10', 'expense', -250.00, 'may.pdf', '2', 'avg-2', 'EXPENSE'),
                ('2026-06-10', 'partial income', 9999.00, 'june.pdf', '3', 'avg-3', 'INCOME'),
            ])
            c.commit()

        page = self.client.get('/', base_url=self.base)

        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('Complete-month average', html)
        self.assertIn('£1,000.00', html)
        self.assertIn('£250.00', html)
        self.assertIn('+£750.00', html)
        self.assertIn('Based on 1 complete covered month', html)
        self.assertIn('1 partial month excluded from averages: 2026-06', html)

    def test_home_import_history_shows_m1_document_names_not_internal_keys(self):
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.execute('''CREATE TABLE source_documents (
                document_id INTEGER PRIMARY KEY,
                account_id TEXT NOT NULL,
                document_key TEXT NOT NULL,
                source_identity TEXT NOT NULL UNIQUE,
                content_hash TEXT NOT NULL,
                coverage_start TEXT,
                coverage_end TEXT,
                coverage_row_ref TEXT,
                reconciliation_status TEXT NOT NULL
            )''')
            c.execute('''INSERT INTO source_documents
                (document_id, account_id, document_key, source_identity, content_hash, reconciliation_status)
                VALUES (7, 'HSBC_UK', '2026-10-10_statement.pdf', 'source-hash', 'content-hash', 'passed')''')
            c.execute("INSERT INTO import_log(source_file, row_count) VALUES ('m1:source-hash', 103)")
            c.commit()

        page = self.client.get('/', base_url=self.base)

        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('2026-10-10_statement.pdf', html)
        self.assertNotIn('m1:source-hash', html)

    def test_home_import_history_deduplicates_legacy_and_m1_rows(self):
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.execute('''CREATE TABLE source_documents (
                document_id INTEGER PRIMARY KEY,
                account_id TEXT NOT NULL,
                document_key TEXT NOT NULL,
                source_identity TEXT NOT NULL UNIQUE,
                content_hash TEXT NOT NULL,
                coverage_start TEXT,
                coverage_end TEXT,
                coverage_row_ref TEXT,
                reconciliation_status TEXT NOT NULL
            )''')
            c.execute('''INSERT INTO source_documents
                (document_id, account_id, document_key, source_identity, content_hash, reconciliation_status)
                VALUES (7, 'HSBC_UK', '2026-10-10_statement.pdf', 'source-hash', 'content-hash', 'passed')''')
            c.execute("INSERT INTO import_log(source_file, row_count) VALUES ('StatementsPDF\\2026-10-10_Statement.pdf', 103)")
            c.execute("INSERT INTO import_log(source_file, row_count) VALUES ('m1:source-hash', 103)")
            c.commit()

        page = self.client.get('/', base_url=self.base)

        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertEqual(html.count('2026-10-10'), 1)
        self.assertNotIn('m1:source-hash', html)

    def test_report_page_resolves_generic_year_period_filter(self):
        self.paths.reports_dir.mkdir(parents=True)
        (self.paths.reports_dir / 'income_vs_expense.html').write_text('<html>fixture</html>', encoding='utf-8')

        page = self.client.get('/report?name=income_vs_expense&period=year&year=2025', base_url=self.base)

        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('Period', html)
        self.assertIn('2025 full year', html)
        self.assertIn('2025-01-01 to 2025-12-31', html)
        self.assertIn('Transaction date', html)
        self.assertIn('period=year', html)
        self.assertIn('year=2025', html)

    def test_report_page_month_picker_uses_fixed_month_options(self):
        self.paths.reports_dir.mkdir(parents=True)
        (self.paths.reports_dir / 'income_vs_expense.html').write_text('<html>fixture</html>', encoding='utf-8')

        page = self.client.get('/report?name=income_vs_expense&period=month&year=2026&month=7', base_url=self.base)

        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('<select id="periodMonth" name="month">', html)
        self.assertIn('data-period-fields="year month"', html)
        self.assertIn('<option value="7" selected>July</option>', html)
        self.assertIn('<option value="12" >December</option>', html)
        self.assertNotIn('placeholder="1-12"', html)

    def test_report_page_year_picker_uses_ledger_year_options_not_browser_suggestions(self):
        self.paths.reports_dir.mkdir(parents=True)
        (self.paths.reports_dir / 'income_vs_expense.html').write_text('<html>fixture</html>', encoding='utf-8')
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.execute("INSERT INTO transactions (txn_date,description_raw,amount,source_file,row_ref,import_hash,merchant_core) VALUES ('2025-06-01','synthetic',-1,'fixture.pdf','2','fixture-2025','SYNTHETIC')")
            c.execute("INSERT INTO transactions (txn_date,description_raw,amount,source_file,row_ref,import_hash,merchant_core) VALUES ('2026-06-01','synthetic',-1,'fixture.pdf','3','fixture-2026','SYNTHETIC')")
            c.commit()

        page = self.client.get('/report?name=income_vs_expense&period=year&year=2025', base_url=self.base)

        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('<select id="periodYear" name="year">', html)
        self.assertIn('<option value="2025" selected>2025</option>', html)
        self.assertIn('<option value="2026" >2026</option>', html)
        self.assertNotIn('placeholder="2025"', html)
        self.assertNotIn('1958', html)

    def test_report_page_all_dates_hides_unused_period_inputs(self):
        self.paths.reports_dir.mkdir(parents=True)
        (self.paths.reports_dir / 'income_vs_expense.html').write_text('<html>fixture</html>', encoding='utf-8')

        page = self.client.get('/report?name=income_vs_expense&period=all', base_url=self.base)

        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('data-period-fields="year month" hidden', html)
        self.assertIn('data-period-fields="month" hidden', html)
        self.assertIn('data-period-fields="custom" hidden', html)

    def test_report_raw_period_filter_hides_embedded_date_picker_and_filters_data(self):
        self.paths.reports_dir.mkdir(parents=True)
        fixture_html = (
            '<html><head></head><body>'
            '<div class="date-bar"><select id="fromMonth"></select><select id="toMonth"></select></div>'
            '<p class="subtitle" id="subtitle">internal date range</p>'
            '<script>'
            'const ALL_TXNS = ['
            '{"date":"2026-09-02","statement_date":"2026-09-10","amount":-1},'
            '{"date":"2026-10-02","statement_date":"2026-10-10","amount":-2}'
            '];'
            'const ALL_MONTHS = ["2026-09-10","2026-10-10"];'
            '</script></body></html>'
        )
        for report_name in ('income_vs_expense', 'income_by_category', 'expenditure_by_category', 'expenditure_heatmap'):
            (self.paths.reports_dir / f'{report_name}.html').write_text(fixture_html, encoding='utf-8')

        for report_name in ('income_vs_expense', 'income_by_category', 'expenditure_by_category', 'expenditure_heatmap'):
            with self.subTest(report=report_name):
                response = self.client.get(f'/report/raw/{report_name}?period=month&year=2026&month=9', base_url=self.base)

                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertIn('data-dashboard-period-filtered="true"', html)
                self.assertIn('#fromMonth,#toMonth,.date-bar label[for="fromMonth"]', html)
                self.assertIn('.date-bar label[for="toMonth"],.date-bar .separator,#subtitle{display:none!important;}', html)
                self.assertIn('.date-bar:not(:has(#showExcluded)){display:none!important;}', html)
                self.assertNotIn('.date-bar,#subtitle{display:none!important;}', html)
                self.assertIn('"date": "2026-09-02"', html)
                self.assertNotIn('"date": "2026-10-02"', html)
                self.assertIn('const ALL_MONTHS = ["2026-09-10"]', html)

    def test_report_iframe_has_accessible_title_and_period_query(self):
        self.paths.reports_dir.mkdir(parents=True)
        (self.paths.reports_dir / 'income_vs_expense.html').write_text('<html>fixture</html>', encoding='utf-8')

        page = self.client.get('/report?name=income_vs_expense&period=month&year=2026&month=9', base_url=self.base)

        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('title="Income vs Expenditure report"', html)
        self.assertIn('src="/report/raw/income_vs_expense?period=month&amp;year=2026&amp;month=9&amp;account_id="', html)

    def test_report_page_rejects_invalid_custom_period_without_crashing(self):
        self.paths.reports_dir.mkdir(parents=True)
        (self.paths.reports_dir / 'income_vs_expense.html').write_text('<html>fixture</html>', encoding='utf-8')

        page = self.client.get(
            '/report?name=income_vs_expense&period=custom&start=2025-10-01&end=2025-09-30',
            base_url=self.base,
        )

        self.assertEqual(page.status_code, 400)
        html = page.get_data(as_text=True)
        self.assertIn('Invalid period filter', html)
        self.assertNotIn('<iframe', html)

    def test_note_requires_session_token_and_same_origin_then_succeeds(self):
        self.assertEqual(self.client.post('/api/update-note', json={'txn_id': 1, 'note': 'new'}, base_url=self.base).status_code, 403)
        response = self.post('/api/update-note', {'txn_id': 1, 'note': 'new'})
        self.assertEqual(response.status_code, 200)
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            self.assertEqual(c.execute('SELECT user_note FROM transactions WHERE txn_id=1').fetchone()[0], 'new')
        self.assertNotIn('Access-Control-Allow-Origin', response.headers)
        self.assertGreaterEqual(len(self.token()), 32)
        other = d.app.test_client().get('/api/csrf-token', base_url=self.base)
        self.assertNotEqual(self.token(), other.json['csrf_token'])

    def test_invalid_non_ascii_csrf_is_rejected_not_server_error(self):
        self.token()
        self.assertEqual(self.client.post('/api/update-note', base_url=self.base,
            json={'txn_id': 1, 'note': 'not written'}, headers={'X-CSRF-Token': 'é'}).status_code, 403)

    def test_rebinding_and_cross_site_reads_and_writes_blocked(self):
        for host in ['http://evil.example:5000', 'http://127.0.0.1.evil.example:5000', 'http://localhost.evil:5000']:
            self.assertEqual(self.client.get('/api/categories', base_url=host).status_code, 403)
        for origin in ['https://evil.example', 'null', 'http://localhost:5000', 'http://127.0.0.1:5001']:
            self.assertEqual(self.post('/api/update-note', {'txn_id': 1, 'note': 'bad'}, Origin=origin).status_code, 403)
            self.assertEqual(self.client.get('/api/csrf-token', base_url=self.base, headers={'Origin': origin}).status_code, 403)
        self.assertEqual(self.client.get('/api/categories', base_url=self.base, headers={'Sec-Fetch-Site': 'cross-site'}).status_code, 403)

    def test_ingest_form_token_rendered_and_missing_rejected(self):
        self.paths.statements_dir.mkdir()
        (self.paths.statements_dir / 'fixture.pdf').write_bytes(b'synthetic fixture placeholder')
        page = self.client.get('/ingest', base_url=self.base)
        self.assertEqual(page.status_code, 200)
        self.assertIn('name="csrf_token"', page.get_data(as_text=True))
        self.assertEqual(self.client.post('/ingest', data={'pdf': 'fixture.pdf'}, base_url=self.base).status_code, 403)
        # Exercise form token acceptance without invoking statement import (out of M0).
        result = self.client.post('/ingest', data={'csrf_token': self.token(), 'pdf': '', 'account_id': 'HSBC_UK'}, base_url=self.base)
        self.assertEqual(result.status_code, 200)
        self.assertIn('No PDF selected.', result.get_data(as_text=True))

    def test_api_payload_validation_and_missing_ids_never_change_config(self):
        original = self.paths.categories_path.read_bytes()
        for route, valid in [('/api/update-note', {'txn_id': 999, 'note': 'test'}),
                             ('/api/recategorize', {'txn_id': 999, 'category': 'Fixture', 'new_subcategory': 'Brand new'}),
                             ('/api/resolve-exception', {'exception_id': 999, 'category': 'Fixture', 'subcategory': 'Test'})]:
            with self.subTest(route=route):
                self.assertEqual(self.post(route, valid).status_code, 404)
                for payload in [None, [], {}, {'txn_id': [], 'exception_id': [], 'category': []}]:
                    response = self.client.post(route, data=json.dumps(payload), content_type='application/json', base_url=self.base,
                        headers={'Origin': self.base, 'X-CSRF-Token': self.token()})
                    self.assertEqual(response.status_code, 400)
                self.assertEqual(self.client.post(route, data='{', content_type='application/json', base_url=self.base,
                    headers={'X-CSRF-Token': self.token()}).status_code, 400)
        self.assertEqual(self.paths.categories_path.read_bytes(), original)
        for payload in [{'txn_id': 1}, {'txn_id': True, 'note': 'test'}, {'txn_id': 1, 'note': []}]:
            self.assertEqual(self.post('/api/update-note', payload).status_code, 400)
        self.assertEqual(self.post('/api/recategorize', {'txn_id': 1, 'category': 'Fixture', 'subcategory': 'Test', 'scope': 'invalid'}).status_code, 400)

    def test_mutation_ids_are_positive_sqlite_integers(self):
        original = self.paths.categories_path.read_bytes()
        for route, key, fields in [
            ('/api/update-note', 'txn_id', {'note': 'test'}),
            ('/api/recategorize', 'txn_id', {'category': 'Fixture', 'subcategory': 'Test'}),
            ('/api/resolve-exception', 'exception_id', {'category': 'Fixture', 'subcategory': 'Test'}),
        ]:
            for value in [9223372036854775808, 10**100, True, False, 1.0, '1', 0, -1]:
                with self.subTest(route=route, value=value):
                    response = self.post(route, {key: value, **fields})
                    self.assertEqual(response.status_code, 400)
                    self.assertFalse(response.json['ok'])
            with self.subTest(route=route, boundary='largest valid ID'):
                self.assertEqual(self.post(route, {key: 9223372036854775807, **fields}).status_code, 404)
        self.assertEqual(self.paths.categories_path.read_bytes(), original)
        # A rejected request must release the mutation lock and leave the record intact.
        self.assertEqual(self.post('/api/update-note', {'txn_id': 1, 'note': 'after rejection'}).status_code, 200)

    def test_exception_apply_refreshes_reports_after_commit(self):
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.execute("INSERT INTO exceptions (txn_id,type,merchant_core) VALUES (1,'uncategorised','SYNTHETIC')")
            c.commit()
        def check_committed():
            with closing(sqlite3.connect(self.paths.db_path)) as c:
                self.assertEqual(c.execute('SELECT status FROM exceptions WHERE exception_id=1').fetchone()[0],'resolved')
        with patch.object(d,'_regenerate_report',side_effect=check_committed) as refresh:
            result=self.post('/api/resolve-exception',{'exception_id':1,'category':'Fixture','subcategory':'Test','merchant_core':'SYNTHETIC'})
            self.assertTrue(result.json['ok'])
            refresh.assert_called_once()

    def test_exception_apply_report_failure_returns_saved_warning(self):
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.execute("INSERT INTO exceptions (txn_id,type,merchant_core) VALUES (1,'uncategorised','SYNTHETIC')")
            c.commit()
        with patch.object(d,'_regenerate_report',side_effect=RuntimeError('fixture report failure')):
            result=self.post('/api/resolve-exception',{'exception_id':1,'category':'Fixture','subcategory':'Test','merchant_core':'SYNTHETIC'})
        self.assertTrue(result.json['ok'])
        self.assertIn('reports',result.json['warning'])
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            self.assertEqual(c.execute('SELECT status FROM exceptions WHERE exception_id=1').fetchone()[0],'resolved')

    def test_new_subcategory_and_exception_workflows_still_work(self):
        rejected = self.post('/api/recategorize', {'txn_id': 1, 'category': 'Fixture', 'new_subcategory': 'New', 'scope': 'single', 'remember_rule': True})
        self.assertEqual(rejected.status_code, 400)
        self.assertNotIn('New', json.loads(self.paths.categories_path.read_text())['Fixture'])
        result = self.post('/api/recategorize', {'txn_id': 1, 'category': 'Fixture', 'new_subcategory': 'New', 'scope': 'single', 'remember_rule': False})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json['ok'])
        preview = self.post('/api/rules/preview', {'txn_id': 1, 'category': 'Fixture', 'subcategory': 'New'})
        self.assertEqual(preview.status_code, 200)
        remembered = self.post('/api/rules/confirm', {'preview_token': preview.json['preview_token'], 'approved': True})
        self.assertTrue(remembered.json['ok'])
        self.assertIn('New', json.loads(self.paths.categories_path.read_text())['Fixture'])
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.execute("INSERT INTO exceptions (txn_id,type,merchant_core) VALUES (1,'uncategorised','SYNTHETIC')")
            c.commit()
        result = self.post('/api/resolve-exception', {'exception_id': 1, 'category': 'Fixture', 'subcategory': 'Test', 'merchant_core': 'SYNTHETIC'})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json['ok'])

    def test_resolve_exception_only_resolves_clicked_row_not_same_merchant(self):
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.execute("INSERT INTO transactions(txn_id, txn_date, description_raw, amount, source_file, row_ref, import_hash, merchant_core) VALUES (2, '2026-03-25', 'HALF MOON BAY CAFE', -3.50, 'fixture.pdf', '2', 'hm-1', 'HALF MOON BAY CAFE LANCASTER')")
            c.execute("INSERT INTO transactions(txn_id, txn_date, description_raw, amount, source_file, row_ref, import_hash, merchant_core) VALUES (3, '2026-03-25', 'HALF MOON BAY CAFE', -11.15, 'fixture.pdf', '3', 'hm-2', 'HALF MOON BAY CAFE LANCASTER')")
            c.execute("INSERT INTO exceptions(exception_id, txn_id, type, status, merchant_core) VALUES (21, 2, 'uncategorised', 'open', 'HALF MOON BAY CAFE LANCASTER')")
            c.execute("INSERT INTO exceptions(exception_id, txn_id, type, status, merchant_core) VALUES (22, 3, 'uncategorised', 'open', 'HALF MOON BAY CAFE LANCASTER')")
            c.commit()

        result = self.post('/api/resolve-exception', {
            'exception_id': 21,
            'category': 'Fixture',
            'subcategory': 'Test',
            'merchant_core': 'HALF MOON BAY CAFE LANCASTER',
        })

        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json['ok'])
        self.assertEqual(result.json.get('bulk_resolved'), [])
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            statuses = dict(c.execute("SELECT exception_id, status FROM exceptions WHERE exception_id IN (21, 22)").fetchall())
        self.assertEqual(statuses[21], 'resolved')
        self.assertEqual(statuses[22], 'open')
