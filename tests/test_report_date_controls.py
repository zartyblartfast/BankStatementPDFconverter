"""Real generated reports/routes; browser visibility is additionally checked in QA."""
from contextlib import closing
from html.parser import HTMLParser
import importlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import unittest
from unittest.mock import patch

from scripts import report_assets
from scripts.report_data import query_report
from tests import test_transfer_report_routes as route_fixtures
from tests.test_transfer_report_routes import payload
from flask.testing import FlaskClient
from tests.test_transfer_reports import MODULES


class Markup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class ReportDateControlsTests(unittest.TestCase):
    db: Path
    reports: Path
    before: dict[str, bytes]
    client: FlaskClient
    setUp_fixture = route_fixtures.TransferReportRouteTests.setUp_fixture
    setUp = route_fixtures.TransferReportRouteTests.setUp

    def assert_dashboard_controls(self, html, *, toggle=True):
        # Explicit CSS contract, not a claim of computed browser visibility.
        style = re.search(r'<style data-dashboard-period-filtered="true">(.*?)</style>', html, re.S)
        self.assertIsNotNone(style, 'dashboard-served generated report still exposes its second date filter')
        assert style is not None
        css = style.group(1)
        for selector in ('#fromMonth', '#toMonth', '.date-bar label[for="fromMonth"]',
                         '.date-bar label[for="toMonth"]', '.date-bar .separator', '#subtitle'):
            self.assertIn(selector, css)
        self.assertIn('display:none!important', css)
        self.assertNotIn('.date-bar,#subtitle', css, 'must not hide Show excluded with the whole date bar')
        self.assertIn('.date-bar:not(:has(#showExcluded))', css, 'date-only legacy bars must not leave empty strips')
        nodes = Markup(html).elements
        for name in ('fromMonth', 'toMonth'):
            self.assertEqual(len([a for t,a in nodes if t == 'select' and a.get('id') == name]), 1)
        if toggle:
            attrs = next(a for t,a in nodes if a.get('id') == 'showExcluded')
            self.assertNotIn('hidden', attrs)
            self.assertNotIn('disabled', attrs)
            self.assertNotEqual(attrs.get('tabindex'), '-1')
            self.assertIn('Show excluded', html)

    def test_actual_current_reports_have_one_authoritative_filter_all_and_bounded(self):
        for module in MODULES:
            name = module.removeprefix('report_')
            for query, bounds in (('period=all', ('','')), ('period=month&year=2026&month=1', ('2026-01-01','2026-01-31'))):
                with self.subTest(report=name, query=query):
                    response = self.client.get('/report/raw/'+name+'?account_id=HSBC_UK&'+query)
                    self.assertEqual(response.status_code, 200)
                    html = response.get_data(as_text=True)
                    self.assert_dashboard_controls(html)
                    with closing(sqlite3.connect(self.db)) as conn:
                        rows, months = query_report(conn=conn, account_ids=['HSBC_UK'], start=bounds[0], end=bounds[1])
                    for key, expected in (('ALL_TXNS', list(rows)), ('ALL_MONTHS', months), ('REPORT_POLICY', rows.policy)):
                        self.assertEqual(payload(html, key), expected)
                    # Execute the actual generated scripts/helper: initial full month
                    # selection and Show excluded raw/principal semantics stay intact.
                    harness = Path(__file__).with_name('report_classic_probe.js').read_text(encoding='utf-8').split('let s=state();', 1)[0]
                    assertions = r'''
const initial=state(), expected=vm.runInContext('REPORT_POLICY',context);
assert.equal(initial.row_count,vm.runInContext('ALL_TXNS.length',context));
assert.equal(elements.fromMonth.value,vm.runInContext('ALL_MONTHS[0]',context));
assert.equal(elements.toMonth.value,vm.runInContext('ALL_MONTHS[ALL_MONTHS.length-1]',context));
for(const field of ['credits_pence','debits_pence','net_pence']){
  assert.equal(initial.raw[field],expected.raw[field]);
  assert.equal(initial.display[field],expected.adjusted[field]);
}
elements.showExcluded.checked=true;elements.showExcluded.listeners.change();
for(const field of ['credits_pence','debits_pence','net_pence'])assert.equal(state().display[field],expected.raw[field]);
assert.equal(state().row_count,initial.row_count);
elements.showExcluded.checked=false;elements.showExcluded.listeners.change();
assert.deepEqual(state().display,initial.display);
'''
                    result = subprocess.run(['node', '-e', harness + assertions],
                        input=json.dumps({'html':html,'scripts':re.findall(r'<script>(.*?)</script>', html, re.S),
                            'helper':str(Path(__file__).parents[1]/'static/report-classic.js')}), text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    wrapper = self.client.get('/report?name='+name+'&account_id=HSBC_UK&'+query).get_data(as_text=True)
                    self.assertIn('id="periodSelect"', wrapper)
                    self.assertIn('/report/raw/'+name, wrapper)
        self.assertEqual(self.before, {p.name:p.read_bytes() for p in self.reports.glob('*.html')})

    def test_legacy_all_and_bounded_hide_dates_but_preserve_existing_toggle(self):
        with closing(sqlite3.connect(self.db)) as conn:
            rows, months = query_report(conn=conn, account_ids=['HSBC_UK'])
        for module in MODULES:
            name = module.removeprefix('report_')
            legacy = importlib.import_module('scripts.'+module)._build_legacy_html(list(rows), months)
            target = self.reports/(name+'.html')
            target.write_text(legacy, encoding='utf-8')
            for query in ('period=all','period=month&year=2026&month=1'):
                with self.subTest(report=name, query=query):
                    html = self.client.get('/report/raw/'+name+'?account_id=HSBC_UK&'+query).get_data(as_text=True)
                    self.assert_dashboard_controls(html, toggle='id="showExcluded"' in legacy)
                    self.assertIn('data-legacy-policy-warning', html)
                    self.assertEqual(payload(html,'ALL_TXNS'), list(rows))
            self.assertEqual(target.read_text(encoding='utf-8'), legacy)

    def test_standalone_generated_and_localized_html_keep_statement_controls(self):
        for module in MODULES:
            name = module.removeprefix('report_')
            generated = self.before[name+'.html'].decode('utf-8')
            for html in (generated, report_assets.localize_report_html(generated)):
                self.assertNotIn('data-dashboard-period-filtered=', html)
                self.assertIn('id="fromMonth"', html)
                self.assertIn('id="toMonth"', html)
                self.assertIn('for="fromMonth">From', html)
                self.assertIn('for="toMonth">To', html)
                self.assertIn('id="showExcluded"', html)
