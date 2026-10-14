"""Classic generated layouts, with current policy rather than category heuristics."""
import importlib
import re
from tests import test_transfer_reports as fixtures
from tests.test_transfer_reports import MODULES


import unittest


class ClassicReportTests(unittest.TestCase):
    setUp = fixtures.TransferReportTests.setUp
    query = fixtures.TransferReportTests.query

    def test_classic_scripts_execute_exact_policy_and_closed_grouped_rows(self):
        import json
        import subprocess
        from pathlib import Path
        rows, months = self.query()
        for record in rows:
            if record['txn_id'] == '1': record['merchant'] = 'Zed'
            if record['txn_id'] == '2': record['merchant'] = 'Alpha'
        for name in MODULES:
            with self.subTest(report=name):
                html = importlib.import_module('scripts.' + name)._build_html(rows, months)
                result = subprocess.run(['node', str(Path(__file__).with_name('report_classic_probe.js'))],
                    input=json.dumps({'html': html, 'scenario': 'sorting', 'scripts': re.findall(r'<script>(.*?)</script>', html, re.S),
                        'helper': str(Path(__file__).parents[1] / 'static/report-classic.js')}),
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_original_layouts_replace_generic_chart_and_flat_drilldown(self):
        rows, months = self.query()
        expected = {
            'report_income_vs_expense': ['incomeDonut', 'expenseDonut', 'incomeBody', 'expenseBody'],
            'report_income_by_category': ['donutChart', 'barChart', 'tableBody'],
            'report_expenditure_by_category': ['donutChart', 'barChart', 'tableBody'],
            'report_expenditure_heatmap': ['heatmapWrap'],
        }
        for name in MODULES:
            with self.subTest(report=name):
                html = importlib.import_module('scripts.' + name)._build_html(rows, months)
                for element in expected[name]:
                    self.assertIn('id="' + element + '"', html)
                self.assertNotIn('id="movementChart"', html)
                self.assertNotIn('id="drilldown"', html)
                self.assertNotRegex(html, r'<details\b[^>]*\bopen\b')
                self.assertIn('/static/report-classic.js', html)
