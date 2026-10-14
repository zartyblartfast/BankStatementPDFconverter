"""Home/Planning integration uses isolated ledgers, never generated live reports."""
from contextlib import closing
import sqlite3
import json
import re
import shutil
import subprocess
from pathlib import Path
from decimal import localcontext, Inexact, Rounded
import unittest
from unittest.mock import patch
from scripts import dashboard
from scripts.services.coverage import CoverageWindow
from scripts.services.transfers import prepare_pair, prepare_unmatched, prepare_revoke, commit_transfer
from scripts.init_db import _SCHEMA_SQL
from tests import test_movement_policy as movement_fixture


class TransferDashboardTests(unittest.TestCase):
    db: Path

    def setUp(self):
        movement_fixture.MovementPolicyTests.setUp(self)
        p = patch.object(dashboard, 'DB_PATH', self.db)
        p.start()
        self.addCleanup(p.stop)
        self.client = dashboard.app.test_client()

    def home_stats(self, query=''):
        with patch.object(dashboard, 'render_template', side_effect=lambda template, **values: values):
            return self.client.get('/' + query).json['stats']

    def test_pair_keeps_raw_and_fee_uncategorised_in_home_and_planning(self):
        commit_transfer(prepare_pair(1, 2, db_path=self.db), approved=True)
        stats = self.home_stats()
        self.assertEqual(stats['movement']['raw']['credits_pence'], 3000)
        self.assertEqual(stats['movement']['adjusted']['credits_pence'], 2000)
        self.assertEqual(stats['total_expense'], '1.00')
        data = self.client.get('/api/planning-data').json
        self.assertEqual(data['grid_pence'], {'Uncategorised': {'2026-01': 100}})
        self.assertEqual(data['movement'], stats['movement'])
        self.assertEqual(data['method']['calculation_basis'], 'transfer_adjusted')

    def test_common_complete_coverage_is_required_but_uk_history_remains_available(self):
        windows = (CoverageWindow('HSBC_UK', '2026-01-01', '2026-01-31', 'uk'),
                   CoverageWindow('IOM', '2026-01-15', '2026-01-31', 'iom'))
        commit_transfer(prepare_pair(1, 2, db_path=self.db), approved=True)
        with patch.object(dashboard, 'coverage_windows_from_db', return_value=windows):
            all_stats = self.home_stats()
            self.assertFalse(all_stats['average_available'])
            self.assertEqual(all_stats['average_income'], '—')
            self.assertEqual(all_stats['coverage_complete_months'], 0)
            uk = self.home_stats('?account_id=HSBC_UK')
            self.assertEqual(uk['average_income'], '20.00')
            self.assertEqual(uk['average_expense'], '1.00')
            iom = self.home_stats('?account_id=IOM')
            self.assertFalse(iom['average_available'])
            planning = self.client.get('/api/planning-data').json
            self.assertEqual(planning['complete_months'], [])
            self.assertTrue(planning['warnings'])

    def test_date_scope_keeps_raw_exclusions_and_outside_counterpart_evidence(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE transactions SET txn_date='2026-01-02' WHERE txn_id=2")
        commit_transfer(prepare_pair(1, 2, db_path=self.db), approved=True)
        query = '?account_id=HSBC_UK&start=2026-01-01&end=2026-01-01'
        stats = self.home_stats(query)
        data = self.client.get('/api/planning-data' + query).json
        self.assertEqual(data['filters']['start'], '2026-01-01')
        self.assertEqual(data['movement'], stats['movement'])
        self.assertEqual(data['movement']['excluded_count'], 1)
        self.assertEqual(data['movement']['raw']['debits_pence'], 1100)
        self.assertEqual(data['movement']['adjusted']['debits_pence'], 100)
        self.assertIn('confirmed_transfer_different_posting_dates', data['movement']['warnings'])
        for route in ('/', '/planning', '/api/planning-data'):
            self.assertEqual(self.client.get(route + '?start=2026-01-02&end=2026-01-01').status_code, 400)
        with patch.object(dashboard, 'coverage_windows_from_db', return_value=(
                CoverageWindow('HSBC_UK', '2026-01-01', '2026-01-31', 'uk'),)):
            self.assertFalse(self.home_stats(query)['average_available'])
        january = self.client.get('/api/planning-data?period=month&year=2026&month=1').json
        self.assertEqual(january['filters']['end'], '2026-01-31')

    def test_out_of_calendar_years_are_rejected_before_movement_queries(self):
        for route in ('/', '/planning', '/api/planning-data'):
            for kind in ('year', 'month'):
                for year in (0, -1, 10000, 1000000):
                    with self.subTest(route=route, kind=kind, year=year):
                        with patch.object(dashboard, '_dashboard_movement_rows') as rows:
                            response = self.client.get(f'{route}?period={kind}&year={year}&month=1')
                        self.assertEqual(response.status_code, 400)
                        rows.assert_not_called()

    def test_boundary_year_presets_keep_canonical_scope_on_all_endpoints(self):
        for year in (1, 9999):
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute('UPDATE transactions SET txn_date=?', (f'{year:04d}-01-01',))
            for kind in ('year', 'month'):
                query = f'?period={kind}&year={year}&month=1'
                with self.subTest(year=year, kind=kind):
                    for route in ('/', '/planning', '/api/planning-data'):
                        self.assertEqual(self.client.get(route + query).status_code, 200)
                    data = self.client.get('/api/planning-data' + query).json
                    self.assertEqual(data['filters']['start'], f'{year:04d}-01-01')
                    self.assertEqual(data['filters']['end'], f'{year:04d}-' + ('12-31' if kind == 'year' else '01-31'))
                    self.assertEqual(data['movement']['row_count'], 4)
                    self.assertEqual(self.home_stats(query)['total_txns'], 4)

    def test_final_calendar_presets_use_nonempty_verified_source_coverage(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE transactions SET txn_date='9999-12-31'")
            conn.executemany('''INSERT INTO source_documents
                (account_id, document_key, source_identity, content_hash,
                 coverage_start, coverage_end, reconciliation_status)
                VALUES (?, ?, ?, ?, '9999-01-01', '9999-12-31', 'passed')''',
                [(account, account + '.pdf', account + '-final', account + '-hash')
                 for account in ('HSBC_UK', 'IOM')])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM source_documents WHERE reconciliation_status='passed'").fetchone()[0], 2)
        commit_transfer(prepare_pair(1, 2, db_path=self.db), approved=True)
        for kind, months, income, expense in (
                ('month', ['9999-12'], '20.00', '1.00'),
                ('year', [f'9999-{month:02d}' for month in range(1, 13)], '1.67', '0.08')):
            coverage_start = '9999-12-01' if kind == 'month' else '9999-01-01'
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute('UPDATE source_documents SET coverage_start=?', (coverage_start,))
            before = self.db.read_bytes()
            query = f'?period={kind}&year=9999&month=12'
            with self.subTest(kind=kind):
                for route in ('/', '/planning', '/api/planning-data'):
                    self.assertEqual(self.client.get(route + query).status_code, 200)
                stats = self.home_stats(query)
                data = self.client.get('/api/planning-data' + query).json
                self.assertEqual(stats['total_txns'], 4)
                self.assertEqual(stats['total_income'], '20.00')
                self.assertEqual(stats['total_expense'], '1.00')
                self.assertEqual(stats['movement']['raw'], {'credits_pence': 3000, 'debits_pence': 1100, 'net_pence': 1900})
                self.assertEqual(stats['movement']['adjusted'], {'credits_pence': 2000, 'debits_pence': 100, 'net_pence': 1900})
                self.assertTrue(stats['average_available'])
                self.assertEqual(stats['average_income'], income)
                self.assertEqual(stats['average_expense'], expense)
                self.assertEqual(stats['coverage_status'], 'complete')
                self.assertEqual(stats['coverage_complete_months'], len(months))
                self.assertEqual(stats['coverage_partial_months'], 0)
                self.assertEqual(stats['coverage_missing_months'], 0)
                self.assertEqual(stats['complete_months'], months)
                self.assertEqual(data['months'], months)
                self.assertEqual(data['complete_months'], months)
                self.assertEqual(data['movement'], stats['movement'])
                self.assertEqual(data['grid_pence'], {'Uncategorised': {'9999-12': 100}})
                self.assertEqual(data['grid_pence_text'], {'Uncategorised': {'9999-12': '100'}})
                self.assertEqual(data['filters']['start'], '9999-12-01' if kind == 'month' else '9999-01-01')
                self.assertEqual(data['filters']['end'], '9999-12-31')
                self.assertEqual({item['account_id'] for item in data['coverage']}, {'HSBC_UK', 'IOM'})
                for item in data['coverage']:
                    self.assertEqual(item['complete_months'], months)
                for warning in data['warnings'] + list(stats['average_warnings']):
                    self.assertNotIn('missing month', warning)
                    self.assertNotIn('No verified', warning)
                    self.assertNotIn('unavailable', warning)
                uk_query = query + '&account_id=HSBC_UK'
                uk = self.home_stats(uk_query)
                self.assertEqual(uk['complete_months'], months)
                self.assertEqual(uk['average_income'], income)
                self.assertEqual(uk['average_expense'], expense)
                uk_data = self.client.get('/api/planning-data' + uk_query).json
                self.assertEqual(uk_data['complete_months'], months)
                self.assertEqual([item['account_id'] for item in uk_data['coverage']], ['HSBC_UK'])
                self.assertEqual(uk_data['movement'], uk['movement'])
                self.assertEqual(self.db.read_bytes(), before)
        # A UK-covered day cannot fill an IOM source gap at the calendar boundary.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE source_documents SET coverage_start='9999-12-16' WHERE account_id='IOM'")
        before = self.db.read_bytes()
        query = '?period=month&year=9999&month=12'
        stats = self.home_stats(query)
        data = self.client.get('/api/planning-data' + query).json
        self.assertFalse(stats['average_available'])
        self.assertEqual(stats['average_income'], '—')
        self.assertEqual(stats['coverage_complete_months'], 0)
        self.assertEqual(stats['coverage_partial_months'], 1)
        self.assertEqual(stats['coverage_missing_months'], 0)
        self.assertEqual(data['complete_months'], [])
        self.assertEqual({item['account_id']: item['complete_months'] for item in data['coverage']},
                         {'HSBC_UK': ['9999-12'], 'IOM': []})
        self.assertEqual(self.home_stats(query + '&account_id=HSBC_UK')['average_income'], '20.00')
        self.assertEqual(self.db.read_bytes(), before)

    def test_ui_explains_policy_and_has_exact_scope_and_error_handling(self):
        home = self.client.get('/?account_id=HSBC_UK').get_data(as_text=True)
        self.assertTrue('name="account_id"' in home, 'Home account selector missing')
        self.assertIn('Raw credits', home)
        self.assertIn('Transfer-adjusted credits', home)
        self.assertIn('Net cash movement', home)
        self.assertNotIn('Net Balance', home)
        planning = self.client.get('/planning?account_id=IOM').get_data(as_text=True)
        self.assertIn('name="account_id"', planning)
        self.assertIn('resp.ok', planning)
        self.assertIn('grid_pence_text', planning)
        self.assertIn('complete_months', planning)
        self.assertIn('BigInt', planning)
        self.assertNotIn('href="/transfers', home)
        self.assertNotIn('href="/transfers', planning)
        data = self.client.get('/api/planning-data').json
        self.assertEqual(data['grid_pence_text'], {'Uncategorised': {'2026-01': '100'}})

    def test_empty_database_keeps_api_contract_and_rejects_unknown_scope(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DELETE FROM transactions')
        data = self.client.get('/api/planning-data').json
        self.assertEqual(data['grid_pence'], {})
        self.assertEqual({item['account_id'] for item in data['coverage']}, {'HSBC_UK', 'IOM'})
        self.assertTrue(all(item['warnings'] for item in data['coverage']))
        missing = self.db.parent / 'not-created.db'
        with patch.object(dashboard, 'DB_PATH', missing):
            data = self.client.get('/api/planning-data').json
            self.assertEqual(data['grid_pence_text'], {})
            self.assertEqual(data['movement']['row_count'], 0)
            self.assertEqual(self.client.get('/api/planning-data?account_id=HSBC_UK').status_code, 400)
            self.assertEqual(self.client.get('/planning').status_code, 200)
        self.assertFalse(missing.exists())

    @unittest.skipUnless(shutil.which('node'), 'Node required for real Planning JavaScript probe')
    def test_actual_planning_javascript_exact_amounts_coverage_empty_and_errors(self):
        html = self.client.get('/planning?account_id=HSBC_UK').get_data(as_text=True)
        script = next(s for s in re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)
                      if 'grid_pence_text' in s)
        data = self.client.get('/api/planning-data?account_id=HSBC_UK').json
        def run(payload, **kwargs):
            result = subprocess.run([shutil.which('node'), str(Path(__file__).with_name('planning_ui_probe.js'))],
                                    input=json.dumps({'script': script, 'data': payload, 'query': '?account_id=HSBC_UK', **kwargs}),
                                    capture_output=True, text=True, encoding='utf-8', check=True)
            return json.loads(result.stdout)
        no_coverage = run(data)
        self.assertEqual(no_coverage['url'], '/api/planning-data?account_id=HSBC_UK')
        self.assertEqual(no_coverage['gridTable']['style']['display'], '')
        self.assertIn('—', no_coverage['gridFoot']['innerHTML'])
        data['complete_months'] = ['2026-01', '2026-02']
        data['months'] = ['2026-01', '2026-02']
        data['categories'] = ['<unsafe>']
        data['grid_pence_text'] = {'<unsafe>': {'2026-01': '9007199254740993'}}
        exact = run(data)
        self.assertIn('90,071,992,547,409.93', exact['gridBody']['innerHTML'])
        self.assertIn('45,035,996,273,704.97', exact['gridFoot']['innerHTML'])
        self.assertIn('&lt;unsafe&gt;', exact['gridBody']['innerHTML'])
        self.assertNotIn('<unsafe>', exact['gridBody']['innerHTML'])
        deselected = run(data, toggleNone=True)
        self.assertIn('0.00', deselected['gridFoot']['innerHTML'])
        error = run({'error': 'Invalid scope'}, ok=False)
        self.assertIn('Invalid scope', error['gridLoading']['textContent'])
        self.assertTrue(error['btnDownload']['disabled'])
        self.assertEqual(error['gridTable']['style']['display'], 'none')

    @unittest.skipUnless(shutil.which('node'), 'Node required for real Planning JavaScript probe')
    def test_prototype_named_categories_deselect_exactly_and_prevent_empty_export(self):
        html = self.client.get('/planning').get_data(as_text=True)
        script = next(s for s in re.findall(r'<script[^>]*>(.*?)</script>', html, re.S)
                      if 'grid_pence_text' in s)
        base = self.client.get('/api/planning-data').json
        node = shutil.which('node')
        assert node is not None
        def run(names, **actions):
            payload = dict(base, categories=names, months=['2026-01'], complete_months=['2026-01'],
                           grid_pence_text={name: {'2026-01': '123'} for name in names})
            result = subprocess.run([node, str(Path(__file__).with_name('planning_ui_probe.js'))],
                                    input=json.dumps({'script': script, 'data': payload, 'download': True, **actions}),
                                    capture_output=True, text=True, encoding='utf-8', check=True)
            return json.loads(result.stdout)
        names = ['__proto__', 'constructor', 'toString']
        for selection in ([name] for name in names):
            for actions in ({'toggleNone': True}, {'deselect': selection}):
                with self.subTest(selection=selection, actions=actions):
                    result = run(selection, **actions)
                    self.assertIn('1.23', result['initial']['foot'])
                    self.assertEqual(result['initial']['labels'], selection)
                    amounts = re.findall(r'<td class="num[^\"]*">(.*?)</td>', result['gridFoot']['innerHTML'])
                    self.assertEqual(amounts, ['0.00'] * 4)
                    self.assertIn('0 / 1', result['summaryCard']['innerHTML'])
                    self.assertTrue(all(not checkbox['checked'] for checkbox in result['checked']))
                    self.assertEqual(result['alerts'], ['No categories selected.'])
                    self.assertEqual(result['exports'], [])
        none = run(names, toggleNone=True)
        self.assertIn('3.69', none['initial']['foot'])
        self.assertIn('0 / 3', none['summaryCard']['innerHTML'])
        self.assertEqual(none['exports'], [])
        self.assertEqual(none['gridBody']['innerHTML'].count('class="excluded"'), 3)
        restored = run(names, toggleNone=True, toggleAll=True)
        self.assertIn('3 / 3', restored['summaryCard']['innerHTML'])
        self.assertIn('3.69', restored['gridFoot']['innerHTML'])
        self.assertEqual(restored['alerts'], [])
        self.assertEqual(restored['exports'], ['"Category","2026-01"\r\n' +
                                               '\r\n'.join(f'"{name}",1.23' for name in names)])

    def test_unmatched_revoke_and_category_renames_never_auto_classify(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE transactions SET excluded=0 WHERE txn_id=1')
            conn.execute("INSERT INTO categories(category_id,name) VALUES (999,'Income')")
            conn.execute("INSERT INTO subcategories(subcategory_id,category_id,name) VALUES (999,999,'Own Accounts')")
            conn.execute('UPDATE transactions SET subcategory_id=999 WHERE txn_id=1')
        initial = self.client.get('/api/planning-data').json
        self.assertEqual(initial['grid_pence']['Income']['2026-01'], 1000)
        result = commit_transfer(prepare_unmatched(1, 'IOM', db_path=self.db), approved=True)
        marked = self.client.get('/api/planning-data').json
        self.assertEqual(marked['grid_pence'], {'Uncategorised': {'2026-01': 100}})
        self.assertEqual(marked['movement']['transfers']['unmatched_count'], 1)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE categories SET name='Renamed hint' WHERE category_id=999")
        self.assertEqual(self.client.get('/api/planning-data').json['movement'], marked['movement'])
        commit_transfer(prepare_revoke(result.group_id, db_path=self.db), approved=True)
        reverted = self.client.get('/api/planning-data').json
        self.assertEqual(reverted['grid_pence']['Renamed hint']['2026-01'], 1000)
        self.assertEqual(reverted['movement']['transfers']['out_count'], 0)

    def test_legacy_absent_004_keeps_fees_and_reports_warning_without_writes(self):
        legacy = self.db.parent / 'legacy.db'
        with closing(sqlite3.connect(legacy)) as conn, conn:
            conn.executescript(_SCHEMA_SQL)
            conn.execute("INSERT INTO transactions(txn_date,description_raw,amount,account_id,import_hash) VALUES ('2026-01-01','Fee',-1.02,'HSBC_UK','fee')")
        before = legacy.read_bytes()
        with patch.object(dashboard, 'DB_PATH', legacy):
            data = self.client.get('/api/planning-data?account_id=HSBC_UK').json
            self.assertEqual(data['grid_pence'], {'Uncategorised': {'2026-01': 102}})
            self.assertIn('transfer_schema_missing_or_unverified', data['warnings'])
            self.assertEqual(self.home_stats()['total_expense'], '1.02')
        self.assertEqual(legacy.read_bytes(), before)

    def test_exact_pence_is_context_independent_and_reads_preserve_fixture(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE transactions SET amount=-1.02 WHERE txn_id=3')
            conn.execute('UPDATE transactions SET amount=20.03 WHERE txn_id=4')
        commit_transfer(prepare_pair(1, 2, db_path=self.db), approved=True)
        before = self.db.read_bytes()
        windows = tuple(CoverageWindow(a, '2026-01-01', '2026-01-31', a) for a in ('HSBC_UK', 'IOM'))
        with localcontext() as context, patch.object(dashboard, 'coverage_windows_from_db', return_value=windows):
            context.prec = 3
            context.traps[Inexact] = context.traps[Rounded] = True
            stats = self.home_stats()
            data = self.client.get('/api/planning-data').json
        self.assertEqual(stats['average_income'], '20.03')
        self.assertEqual(stats['average_expense'], '1.02')
        self.assertEqual(data['grid_pence'], {'Uncategorised': {'2026-01': 102}})
        self.assertEqual(self.db.read_bytes(), before)

    def test_registered_empty_account_blocks_common_baseline_with_visible_account_warning(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO accounts(account_id,display_name,currency,owned,parser_verified,import_enabled) VALUES ('EMPTY','Empty savings','GBP',1,0,0)")
            conn.execute("INSERT INTO accounts(account_id,display_name,currency,owned,parser_verified,import_enabled) VALUES ('FOREIGN','Foreign','USD',1,0,0)")
            conn.execute("INSERT INTO accounts(account_id,display_name,currency,owned,parser_verified,import_enabled) VALUES ('NOT_OWNED','Not owned','GBP',0,0,0)")
        windows = tuple(CoverageWindow(a, '2026-01-01', '2026-01-31', a) for a in ('HSBC_UK', 'IOM'))
        with patch.object(dashboard, 'coverage_windows_from_db', return_value=windows):
            self.assertFalse(self.home_stats()['average_available'])
            html = self.client.get('/').get_data(as_text=True)
            self.assertTrue('Coverage by account' in html)
            self.assertTrue('No verified statement coverage for account EMPTY' in html)
            uk = self.home_stats('?account_id=HSBC_UK')
            self.assertEqual(uk['average_income'], '20.00')
            for account in ('FOREIGN', 'NOT_OWNED'):
                self.assertEqual(self.client.get('/api/planning-data?account_id=' + account).status_code, 400)

    def test_legacy_unknown_accounts_are_not_inferred_owned_from_currency(self):
        legacy = self.db.parent / 'unregistered.db'
        with closing(sqlite3.connect(legacy)) as conn, conn:
            conn.executescript(_SCHEMA_SQL)
            conn.execute("INSERT INTO transactions(txn_date,description_raw,amount,account_id,import_hash) VALUES ('2026-01-01','Other',10,'UNKNOWN','other')")
        with patch.object(dashboard, 'DB_PATH', legacy):
            self.assertEqual(self.client.get('/api/planning-data?account_id=UNKNOWN').status_code, 400)
            self.assertEqual(self.client.get('/api/planning-data').json['movement']['row_count'], 0)

    def test_exact_account_scope_and_invalid_scope_never_falls_back(self):
        commit_transfer(prepare_pair(1, 2, db_path=self.db), approved=True)
        uk = self.home_stats('?account_id=HSBC_UK')
        self.assertEqual(uk['movement']['raw']['credits_pence'], 2000)
        self.assertEqual(uk['total_txns'], 3)
        iom = self.client.get('/api/planning-data?account_id=IOM').json
        self.assertEqual(iom['movement']['raw']['credits_pence'], 1000)
        self.assertEqual(iom['movement']['adjusted']['credits_pence'], 0)
        self.assertIn('confirmed_counterpart_outside_scope', iom['movement']['warnings'])
        self.assertEqual(iom['filters']['account_id'], 'IOM')
        for route in ('/', '/planning', '/api/planning-data'):
            for value in ('unknown', ' HSBC_UK', 'HSBC_UK&account_id=IOM'):
                self.assertEqual(self.client.get(route + '?account_id=' + value).status_code, 400)
