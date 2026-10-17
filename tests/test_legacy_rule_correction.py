from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import subprocess
from unittest.mock import patch
from tests.test_dashboard_security import DashboardFixture
from scripts import dashboard as d
from scripts.report_income_vs_expense import _build_legacy_html


class LegacyCorrectionTests(DashboardFixture):
    def test_committed_category_refresh_failure_is_success_with_warning(self):
        with patch.object(d, '_regenerate_report', side_effect=OSError('synthetic refresh failure')):
            response = self.post('/api/recategorize', dict(txn_id=1, category='Fixture', subcategory='Test', scope='single'))
        self.assertTrue(response.json['ok'], response.json)
        self.assertIn('saved', response.json['warning'].lower())
        with closing(sqlite3.connect(self.paths.db_path)) as conn:
            self.assertIsNotNone(conn.execute('SELECT subcategory_id FROM transactions').fetchone()[0])
            self.assertEqual(conn.execute('SELECT count(*) FROM edit_log').fetchone()[0], 1)

    def test_actual_served_legacy_default_save_cancel_future_navigation_and_warning(self):
        self.paths.reports_dir.mkdir(parents=True)
        target = self.paths.reports_dir / 'income_vs_expense.html'
        generated = _build_legacy_html([], [])
        for stored_old in (False, True):
            html = generated
            if stored_old:
                # Stored pre-maintenance control and function: emulate the known old generator,
                # retaining the complete actual report layout and event bindings.
                old = (Path(__file__).parent / 'fixtures/legacy_recat_save.js').read_text(encoding='utf-8')
                html = re.sub(r'async function saveRecat\(\) \{.*?(?=// Wire up modal buttons)', lambda m: old, html, flags=re.S)
                html = re.sub(r'<p class="remember-row">.*?</p>', '<label class="remember-row"><input type="checkbox" id="rcRemember" checked> Remember rule for future imports</label>', html)
                html = html.replace('This transaction only</label>', 'This transaction only</label><label class="scope-opt"><input type="radio" name="rcScope" value="merchant"> All <strong id="rcMerchantCount"></strong> matching</label>')
            target.write_text(html, encoding='utf-8')
            original = target.read_bytes()
            served = self.client.get('/report/raw/income_vs_expense', base_url=self.base)
            self.assertEqual(served.status_code, 200)
            probe = self.root / 'served.html'
            probe.write_bytes(served.data)
            result = subprocess.run(['node', str(Path(__file__).with_name('legacy_rule_probe.js')), str(probe)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            before_rules = self.paths.category_rules_path.read_bytes()
            with patch.object(d, '_regenerate_report', side_effect=OSError('synthetic refresh failure')):
                response = self.post('/api/recategorize', payload)
            self.assertTrue(response.json['ok'], response.json)
            self.assertIn('saved', response.json['warning'].lower())
            self.assertEqual(self.paths.category_rules_path.read_bytes(), before_rules)
            self.assertEqual(target.read_bytes(), original, 'serve adaptation must not rewrite stored reports')
            self.assertEqual(self.client.get('/rules', base_url=self.base).status_code, 200)
