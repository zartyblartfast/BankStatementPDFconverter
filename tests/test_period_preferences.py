"""Synthetic route/template and production-controller checks."""
from contextlib import closing
from datetime import date
from html import unescape
from pathlib import Path
import json
import re
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from scripts import dashboard
from scripts.services.periods import resolve_period_filter
from tests import test_movement_policy as movement_fixture


class PeriodPreferenceTests(unittest.TestCase):
    temp: tempfile.TemporaryDirectory
    db: Path
    setUp_fixture = movement_fixture.MovementPolicyTests.setUp

    def setUp(self):
        self.setUp_fixture()
        self.reports = Path(self.temp.name) / 'reports'
        self.reports.mkdir()
        self.target = self.reports / 'income_vs_expense.html'
        self.target.write_text('<html><head></head><body><script>const ALL_TXNS = ' + json.dumps([
            {'txn_id': 1, 'account_id': 'HSBC_UK', 'date': '2026-01-01'},
            {'txn_id': 2, 'account_id': 'IOM', 'date': '2026-01-01'},
            {'txn_id': 3, 'account_id': 'HSBC_UK', 'date': '2026-02-01'}]) +
            '; const ALL_MONTHS = [];</script></body></html>', encoding='utf-8')
        for name, value in [('DB_PATH', self.db), ('REPORTS_DIR', self.reports)]:
            p = patch.object(dashboard, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(dashboard, 'resolve_period_filter',
                         side_effect=lambda period: resolve_period_filter(period, today=date(2026, 2, 17)))
        p.start()
        self.addCleanup(p.stop)
        self.client = dashboard.app.test_client()

    def test_production_preference_controller(self):
        result = subprocess.run(['node', str(Path(__file__).with_name('period_preferences_probe.js'))],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_preference_metadata_is_early_minimal_and_only_on_target_pages(self):
        for route in ('/', '/report'):
            html = self.client.get(route + '?period=last_month').get_data(as_text=True)
            head = html.split('</head>', 1)[0]
            match = re.search(r'<script id="period-preference" type="application/json">(.*?)</script>', head, re.S)
            self.assertIsNotNone(match, 'accepted date-only metadata must precede the synchronous controller')
            data = json.loads(match.group(1))
            self.assertEqual(set(data), {'kind', 'year', 'month', 'start', 'end', 'error'})
            self.assertEqual(data['kind'], 'last_month')
            self.assertEqual(data['end'], '2026-01-31')
            tag = re.search(r'<script[^>]*src="/static/period_preferences.js"[^>]*>', head)
            self.assertIsNotNone(tag)
            self.assertNotIn('defer', tag.group())
            self.assertNotIn('async', tag.group())
            self.assertLess(match.end(), tag.start())
        for route in ('/planning', '/report/raw/income_vs_expense'):
            self.assertNotIn('period_preferences.js', self.client.get(route).get_data(as_text=True))
        self.assertEqual(self.client.get('/?period=bogus').status_code, 400)

    def test_report_controller_disables_irrelevant_fields_and_keeps_programmatic_submit(self):
        html = self.client.get('/report?period=last_month').get_data(as_text=True)
        scripts = re.findall(r'<script>(.*?)</script>', html, re.S)
        script = next(s for s in scripts if 'resizeFrame' in s)
        result = subprocess.run(['node', str(Path(__file__).with_name('period_preferences_probe.js')), '--report-form'],
                                input=json.dumps({'script':script}), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('<noscript><style>[data-period-fields][hidden] { display:inline !important; }</style></noscript>', html)
        self.assertNotRegex(html, r'<(?:input|select)[^>]*\sdisabled',
                            'no-JS controls stay enabled; JS disables only irrelevant fields')

    def test_actual_routes_filter_calendar_dates_and_accounts_without_writes(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE transactions SET txn_date='2026-02-01' WHERE txn_id=4")
        before_db, before_report = self.db.read_bytes(), self.target.read_bytes()
        for account, expected_count, expected_ids in [('HSBC_UK', 2, [1]), ('IOM', 1, [2]), ('', 3, [1,2])]:
            query = '?period=last_month&account_id=' + account
            with patch.object(dashboard, 'render_template', side_effect=lambda template, **values: values):
                response = self.client.get('/' + query)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['stats']['total_txns'], expected_count)
            self.assertEqual(response.json['period']['kind'], 'last_month')
            wrapper = self.client.get('/report' + query)
            self.assertEqual(wrapper.status_code, 200)
            html = wrapper.get_data(as_text=True)
            src = unescape(re.search(r'src="(/report/raw/[^\"]+)"', html).group(1))
            self.assertIn('period=custom&start=2026-01-01&end=2026-01-31', src)
            frozen = self.client.get(src).get_data(as_text=True)
            direct = self.client.get('/report/raw/income_vs_expense' + query).get_data(as_text=True)
            for raw in (frozen, direct):
                match = re.search(r'const\s+ALL_TXNS\s*=\s*', raw)
                rows = json.JSONDecoder().raw_decode(raw, match.end())[0]
                self.assertEqual([row['txn_id'] for row in rows], expected_ids)
                self.assertNotIn('period_preferences.js', raw)
        for route in ('/', '/report', '/report/raw/income_vs_expense'):
            for query in ('?period=last_month&period=all', '?period=last_month&year=1&year=2',
                          '?period=last_month&account_id=unknown', '?period=bogus'):
                self.assertEqual(self.client.get(route + query).status_code, 400)
        self.assertEqual(self.db.read_bytes(), before_db)
        self.assertEqual(self.target.read_bytes(), before_report)

    def test_overview_controller_hides_relative_fields_without_saving_edits(self):
        from tests.test_home_usability import Document
        html = self.client.get('/?period=last_month&account_id=IOM').get_data(as_text=True)
        form = Document(html).root.all('form')[0]
        nodes = form.all('input') + form.all('select')
        controls = [dict(node.attrs) for node in nodes]
        for node, attrs in zip(nodes, controls):
            if node.tag == 'select':
                attrs['value'] = next((o.attrs['value'] for o in node.all('option') if 'selected' in o.attrs), '')
        result = subprocess.run(['node', str(Path(__file__).with_name('period_preferences_probe.js')), '--home-form'],
                                input=json.dumps({'script': (Path(__file__).parents[1]/'static/period_scope.js').read_text(),
                                                  'controls':controls}), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_overview_and_report_offer_rolling_choices_not_planning(self):
        for route in ('/', '/report'):
            response = self.client.get(route + '?period=last_month&account_id=HSBC_UK')
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            for kind in ('this_month', 'last_month', 'last_3_months', 'last_quarter'):
                self.assertIn('value="' + kind + '"', html)
            self.assertIn('2026-01-01 to 2026-01-31', html)
        html = self.client.get('/planning').get_data(as_text=True)
        self.assertNotIn('value="last_month"', html)
        self.assertNotIn('period_preferences.js', html)
