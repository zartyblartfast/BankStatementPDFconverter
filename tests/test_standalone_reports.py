"""Exercise CLI file viewing without touching a live ledger or starting a server."""
from pathlib import Path
import importlib
import json
import re
import subprocess
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import url2pathname

from tests import test_transfer_reports as fixtures
from tests.test_transfer_reports import MODULES


class StandaloneReportTests(unittest.TestCase):
    setUp = fixtures.TransferReportTests.setUp
    query = fixtures.TransferReportTests.query

    def test_cli_reports_resolve_every_required_script_to_local_assets(self):
        root = Path(__file__).resolve().parents[1]
        output = Path(self.temp.name) / 'reports with spaces & symbols'
        query_module = importlib.import_module('scripts.report_income_vs_expense')
        expected_rows, expected_months = self.query()
        with patch.object(query_module, 'DB_PATH', self.db):
            for name in MODULES:
                with self.subTest(report=name):
                    module = importlib.import_module('scripts.' + name)
                    with patch.object(module, 'REPORTS_DIR', output), patch.object(module.webbrowser, 'open') as opened:
                        module.main()
                    report = output / (name.removeprefix('report_') + '.html')
                    opened.assert_called_once_with(report.as_uri())
                    html = report.read_text(encoding='utf-8')
                    sources = re.findall(r'<script\b[^>]*\bsrc="([^"]+)"', html)
                    required = {'report-actions.js', 'report-classic.js'}
                    if name == 'report_income_vs_expense':
                        required.add('report-client.js')
                    if name != 'report_expenditure_heatmap':
                        required.add('chart.umd-4.4.8.js')
                    resolved = []
                    for source in sources:
                        url = urlsplit(urljoin(report.as_uri(), source))
                        self.assertEqual(url.scheme, 'file', 'standalone assets must not require a server or CDN')
                        asset = Path(url2pathname(unquote(url.path)))
                        self.assertTrue(asset.is_file(), f'missing file-view asset: {source}')
                        self.assertTrue(asset.resolve().is_relative_to(root / 'static'))
                        resolved.append(asset)
                    self.assertEqual({asset.name for asset in resolved}, required)
                    # Asset adaptation must leave the shared policy/data and served builders unchanged.
                    served = module._build_html(expected_rows, expected_months)
                    for key in ('ALL_TXNS', 'ALL_MONTHS', 'REPORT_POLICY'):
                        pattern = re.compile(r'const ' + key + r'\s*=\s*')
                        payload = json.JSONDecoder().raw_decode(html, pattern.search(html).end())[0]
                        original = json.JSONDecoder().raw_decode(served, pattern.search(served).end())[0]
                        self.assertEqual(payload, original)
                    self.assertIn('/static/report-classic.js', served)
                    self.assertNotIn('keep </ScRiPt>', html)
                    self.assertIn('Standalone HTML editing is unsupported', html)
                    helper = next(asset for asset in resolved if asset.name == 'report-classic.js')
                    result = subprocess.run(['node', str(Path(__file__).with_name('report_classic_probe.js'))],
                        input=json.dumps({'html': html, 'protocol': 'file:',
                            'scripts': re.findall(r'<script>(.*?)</script>', html, re.S),
                            'helper': str(helper)}), text=True, capture_output=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
