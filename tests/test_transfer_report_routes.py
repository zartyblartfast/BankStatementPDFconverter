"""Actual Flask routes on generated scratch reports; never live report writes."""
from contextlib import closing
import importlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from tests import test_transfer_reports as report_fixtures
from tests.test_transfer_reports import MODULES
from scripts import dashboard, report_income_vs_expense


def payload(html, key):
    match = re.search(r'const\s+' + key + r'\s*=\s*', html)
    return json.JSONDecoder().raw_decode(html, match.end())[0]


class TransferReportRouteTests(unittest.TestCase):
    temp: tempfile.TemporaryDirectory
    db: Path
    setUp_fixture = report_fixtures.TransferReportTests.setUp

    def setUp(self):
        self.setUp_fixture()
        self.reports = Path(self.temp.name) / 'reports'
        with patch.object(report_income_vs_expense, 'DB_PATH', self.db):
            for name in MODULES:
                module = importlib.import_module('scripts.' + name)
                with patch.object(module, 'REPORTS_DIR', self.reports), patch.object(module.webbrowser, 'open'):
                    module.main()
        self.before = {p.name: p.read_bytes() for p in self.reports.glob('*.html')}
        for name, value in (('DB_PATH', self.db), ('REPORTS_DIR', self.reports)):
            p = patch.object(dashboard, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.client = dashboard.app.test_client()

    def post(self, route, data):
        base = 'http://127.0.0.1:5000'
        token = self.client.get('/api/csrf-token', base_url=base).json['csrf_token']
        return self.client.post(route, json=data, base_url=base,
            headers={'Origin':base, 'X-CSRF-Token':token})

    def test_note_saved_refresh_failure_is_success_and_current_response_updates(self):
        with patch.object(dashboard, '_regenerate_report', side_effect=RuntimeError('fixture refresh failure')), self.assertLogs(dashboard.app.logger, level='ERROR'):
            response = self.post('/api/update-note', {'txn_id':3, 'note':'safe </script> &', 'expected_note':'keep </ScRiPt> &'})
        self.assertTrue(response.json['ok'], response.json)
        self.assertIn('saved', response.json['warning'].lower())
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT user_note FROM transactions WHERE txn_id=3').fetchone()[0], 'safe </script> &')
        html = self.client.get('/report/raw/income_vs_expense?account_id=HSBC_UK').get_data(as_text=True)
        self.assertEqual(next(r for r in payload(html, 'ALL_TXNS') if r['txn_id']=='3')['user_note'], 'safe </script> &')
        self.assertEqual(self.before, {p.name:p.read_bytes() for p in self.reports.glob('*.html')})

    def test_note_rejects_invalid_payloads_missing_row_and_stale_note_atomically(self):
        with patch.object(dashboard, '_regenerate_report') as refresh:
            for data in (None, [], {'txn_id':True,'note':'x'}, {'txn_id':'3','note':'x'},
                         {'txn_id':9223372036854775808,'note':'x'}, {'txn_id':3,'note':12},
                         {'txn_id':3,'note':'x'*10001}, {'txn_id':3,'note':'x','expected_note':12}):
                response = self.post('/api/update-note', data)
                self.assertEqual(response.status_code, 400, data)
            response = self.post('/api/update-note', {'txn_id':999,'note':'x'})
            self.assertEqual(response.status_code, 404)  # existing mutation guard
            response = self.post('/api/update-note', {'txn_id':3,'note':'x','expected_note':'stale'})
            self.assertEqual(response.status_code, 409)
            refresh.assert_not_called()
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT user_note FROM transactions WHERE txn_id=3').fetchone()[0], 'keep </ScRiPt> &')

    def test_route_validation_missing_resources_empty_scope_and_legacy_bytes(self):
        for route in ('/report', '/report/raw/income_vs_expense'):
            for query in ('account_id=missing', 'account_id=HSBC_UK&account_id=IOM',
                          'period=year&year=10000', 'period=custom&start=2026-02-01&end=2026-01-01',
                          'period=all&period=month', 'year=2026&year=2025'):
                self.assertEqual(self.client.get(route+'?'+query).status_code, 400, query)
        self.assertEqual(self.client.get('/report/raw/no-such-report').status_code, 404)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE accounts SET owned=0')
        response = self.client.get('/report/raw/income_vs_expense?account_id=')
        self.assertEqual(payload(response.get_data(as_text=True), 'ALL_TXNS'), [])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE accounts SET owned=1')
        legacy = '<html><head></head><body><script>\nconst ALL_TXNS = '+json.dumps([
            {'txn_id':1,'account_id':'HSBC_UK','date':'2026-01-31','statement_date':'2026-02-10','user_note':'const ALL_MONTHS = [];'},
            {'txn_id':2,'account_id':'IOM','date':'2026-02-02'}, {'txn_id':3,'date':'2026-01-31'}])+ ';\nconst ALL_MONTHS = [];\n</script></body></html>'
        target = self.reports/'income_vs_expense.html'
        target.write_text(legacy, encoding='utf-8')
        html = self.client.get('/report/raw/income_vs_expense?account_id=HSBC_UK&period=month&year=2026&month=1').get_data(as_text=True)
        self.assertEqual([r['txn_id'] for r in payload(html,'ALL_TXNS')], [1])
        self.assertEqual(html.count('data-legacy-policy-warning='), 1)
        self.assertNotIn('data-movement-policy="confirmed-v1"', html)
        self.assertEqual(target.read_text(encoding='utf-8'),legacy)
        with patch.object(dashboard, 'DB_PATH', Path(self.temp.name)/'missing.db'):
            self.assertEqual(self.client.get('/report/raw/income_vs_expense').status_code,409)
            self.assertEqual(self.client.get('/report').status_code,200)
        target.unlink()
        self.assertEqual(self.client.get('/report/raw/income_vs_expense').status_code,404)
        self.assertEqual(self.client.get('/report').status_code,302)

    def test_single_row_category_and_note_preserve_money_transfer_and_source_and_stale_preview(self):
        from scripts.services import transfers
        choices = Path(self.temp.name)/'categories.json'
        choices.write_text('{"__proto__":["constructor"]}', encoding='utf-8')
        preview = transfers.prepare_revoke(1, db_path=self.db)
        with closing(sqlite3.connect(self.db)) as conn:
            before = conn.execute('SELECT txn_id,txn_date,amount,account_id,source_file,import_hash FROM transactions ORDER BY txn_id').fetchall()
            groups = conn.execute('SELECT * FROM transfer_groups').fetchall()
        with patch.object(dashboard, 'CATEGORIES_JSON', choices), patch.object(dashboard, '_regenerate_report'):
            response = self.post('/api/search-recategorize', {'txn_id':3,'old_subcategory_id':None,'category':'__proto__','subcategory':'constructor'})
            self.assertTrue(response.json['ok'],response.json)
            stale = self.post('/api/search-recategorize', {'txn_id':3,'old_subcategory_id':None,'category':'__proto__','subcategory':'constructor'})
            self.assertEqual(stale.status_code,409)
            self.assertTrue(self.post('/api/update-note', {'txn_id':3,'note':'legacy compatible'}).json['ok'])
        with self.assertRaises(ValueError):
            transfers.commit_transfer(preview, approved=True)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(before, conn.execute('SELECT txn_id,txn_date,amount,account_id,source_file,import_hash FROM transactions ORDER BY txn_id').fetchall())
            self.assertEqual(groups, conn.execute('SELECT * FROM transfer_groups').fetchall())
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM edit_log').fetchone()[0],1)
        html = self.client.get('/report/raw/income_vs_expense').get_data(as_text=True)
        records=payload(html,'ALL_TXNS')
        row=next(r for r in records if r['txn_id']=='3')
        self.assertEqual((row['category'],row['subcategory'],row['user_note']),('__proto__','constructor','legacy compatible'))
        self.assertEqual(next(r for r in records if r['txn_id']=='1')['transfer']['status'],'paired')

    def test_note_exact_unsafe_id_alone_stales_preview_and_legacy_clear_remains_compatible(self):
        from scripts.services import transfers
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE transactions SET txn_id=9007199254740993 WHERE txn_id=3')
        preview=transfers.prepare_revoke(1,db_path=self.db)
        with patch.object(dashboard, '_regenerate_report'):
            response=self.post('/api/update-note',{'txn_id':9007199254740993,'note':'  <img> & exact  ','expected_note':'keep </ScRiPt> &'})
            self.assertTrue(response.json['ok'])
            with self.assertRaises(ValueError):
                transfers.commit_transfer(preview,approved=True)
            with closing(sqlite3.connect(self.db)) as conn:
                self.assertEqual(conn.execute('SELECT user_note FROM transactions WHERE txn_id=9007199254740993').fetchone()[0],'<img> & exact')
                self.assertEqual(conn.execute('SELECT user_note FROM transactions WHERE txn_id=1').fetchone()[0],'keep </ScRiPt> &')
            response=self.post('/api/update-note',{'txn_id':9007199254740993,'note':'  '})
            self.assertTrue(response.json['ok'])
        html=self.client.get('/report/raw/income_vs_expense').get_data(as_text=True)
        row=next(r for r in payload(html,'ALL_TXNS') if r['txn_id']=='9007199254740993')
        self.assertIsNone(row['user_note'])
        self.assertEqual(row['amount_pence'],'-123')

    def test_report_routes_distinguish_current_policy_and_warned_legacy(self):
        html=self.client.get('/report/raw/income_vs_expense').get_data(as_text=True)
        self.assertIn('data-movement-policy="confirmed-v1"',html)
        self.assertNotIn('data-legacy-policy-warning',html)
        target=self.reports/'income_vs_expense.html'
        legacy='<html><head></head><body><script>\nconst ALL_TXNS = [];\nconst ALL_MONTHS = [];\n</script></body></html>'
        target.write_text(legacy,encoding='utf-8')
        html=self.client.get('/report/raw/income_vs_expense').get_data(as_text=True)
        self.assertIn('Legacy report: confirmed-transfer adjustment is not verified.',html)
        self.assertNotIn('data-movement-policy="confirmed-v1"',html)
        self.assertEqual(target.read_text(encoding='utf-8'),legacy)

    def test_note_security_guards_unchanged(self):
        for headers in ({}, {'Origin':'null'}, {'Origin':'https://foreign.example'}):
            response=self.client.post('/api/update-note', json={'txn_id':3,'note':'x'}, headers=headers)
            self.assertEqual(response.status_code,403)

    def test_actual_all_four_routes_scope_current_snapshot_and_wrapper(self):
        from scripts.services.movement_policy import summarise_movement
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE transactions SET excluded=1 WHERE txn_id=3')
        expected = summarise_movement([1,3,4,5], db_path=self.db, include_excluded=False)
        for module in MODULES:
            key = module.removeprefix('report_')
            query = '?account_id=HSBC_UK&period=month&year=2026&month=1'
            response = self.client.get('/report/raw/' + key + query)
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            rows, policy = payload(html, 'ALL_TXNS'), payload(html, 'REPORT_POLICY')
            self.assertEqual([r['txn_id'] for r in rows], ['1','3','4','5'])
            self.assertEqual(policy['scope']['account_ids'], ['HSBC_UK'])
            self.assertEqual((policy['scope']['start'],policy['scope']['end']),('2026-01-01','2026-01-31'))
            all_html=self.client.get('/report/raw/'+key).get_data(as_text=True)
            self.assertEqual(set(payload(all_html,'REPORT_POLICY')['scope']['account_ids']),{'HSBC_UK','IOM'})
            scoped_html=self.client.get('/report/raw/'+key+'?account_id=IOM&period=custom&start=2026-02-02&end=2026-02-02').get_data(as_text=True)
            self.assertEqual([r['txn_id'] for r in payload(scoped_html,'ALL_TXNS')],['2'])
            self.assertEqual(payload(scoped_html,'REPORT_POLICY')['adjusted']['credits_pence'],'0')
            for field in ('credits_pence','debits_pence','net_pence'):
                self.assertEqual(int(policy['adjusted'][field]), expected['adjusted'][field])
            self.assertIn('confirmed_counterpart_outside_scope', policy['warnings'])
            wrapper = self.client.get('/report' + query + '&name=' + key)
            self.assertEqual(wrapper.status_code, 200)
            self.assertIn('value="HSBC_UK" selected', wrapper.get_data(as_text=True))
            self.assertIn('account_id=HSBC_UK', wrapper.get_data(as_text=True))
        self.assertEqual(self.before, {p.name:p.read_bytes() for p in self.reports.glob('*.html')})
