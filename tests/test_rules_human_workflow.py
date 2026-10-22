"""Human workflow regression on synthetic dashboard fixtures."""
from contextlib import closing
import sqlite3
from tests import test_dashboard_security as security

class HumanWorkflowTests(security.DashboardFixture):
    def get(self, url):
        return self.client.get(url, base_url=self.base)

    def categorise_fixture(self):
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute("INSERT INTO categories VALUES (1,'Fixture')")
            conn.execute("INSERT INTO subcategories VALUES (1,1,'Test')")
            conn.execute('UPDATE transactions SET subcategory_id=1')
    def test_exceptions_opens_shared_editor_without_rules_navigation(self):
        self.categorise_fixture()
        self.paths.category_rules_path.write_text('merchant_core,pattern,category,subcategory,notes\n')
        page = self.get('/exceptions').get_data(as_text=True)
        self.assertRegex(page, r'class="rule-edit[^\"]*"[^>]*>View suggestion</button>')
        self.assertIn('id="ruleEdit"', page)
        self.assertIn('/static/rules.js', page)
        self.assertNotIn('intent=add_rule', page)
        self.assertIn('No transactions need categorising.', page)

    def test_real_inline_controller_save_and_next_cancel(self):
        import json
        import subprocess
        from pathlib import Path
        self.categorise_fixture()
        suggestion = self.get('/api/rules/suggestion?key=SYNTHETIC').json
        preview = self.post('/api/rules/preview', dict(key='SYNTHETIC', category='Fixture', subcategory='Test', enabled=True, suggestion_revision=suggestion['suggestion_revision'])).json
        result = subprocess.run(['node', 'tests/rules_inline_probe.js'], input=json.dumps(dict(suggestion=suggestion, preview=preview)), cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_existing_edit_history_and_refresh_warning_controller(self):
        import subprocess
        from pathlib import Path
        result = subprocess.run(['node', 'tests/rules_page_probe.js'], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_narrow_lists_scroll_instead_of_crushing_columns(self):
        from pathlib import Path
        css = (Path(__file__).resolve().parents[1] / 'static/rules.css').read_text()
        self.assertIn('min-width:620px', css)
        self.assertIn('rules-filters', self.get('/rules').get_data(as_text=True))

    def test_empty_rules_panels_are_not_tables(self):
        page = self.get('/rules?q=NO-MATCH-AT-ALL').get_data(as_text=True)
        self.assertNotIn('id="rulesTable"', page)
        self.assertNotIn('id="suggestionsTable"', page)
        self.assertIn('No rules or suggestions match these filters.', page)
        page = self.get('/rules').get_data(as_text=True)
        self.assertIn('in this view', page)
        self.assertIn('class="rule-sort"', page)
