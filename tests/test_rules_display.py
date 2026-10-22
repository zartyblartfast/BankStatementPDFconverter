"""Synthetic account-scoped display; rule approvals remain global."""
from contextlib import closing
import sqlite3
from tests.test_dashboard_security import DashboardFixture


class RulesDisplayTests(DashboardFixture):
    def setUp(self):
        super().setUp()
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute('''CREATE TABLE accounts(account_id TEXT PRIMARY KEY, display_name TEXT,
                currency TEXT, institution TEXT, owned INTEGER, opened_on TEXT, closed_on TEXT,
                parser_key TEXT, parser_verified INTEGER, import_enabled INTEGER)''')
            conn.executemany('INSERT INTO accounts VALUES (?, ?, "GBP", NULL, 1, NULL, NULL, NULL, 0, 0)',
                             [('ALPHA', 'First <literal>'), ('BETA', 'Second & account'), ('EMPTY', 'Empty registered')])
            conn.execute("UPDATE transactions SET account_id='ALPHA'")
            conn.execute('''INSERT INTO transactions(txn_id,txn_date,description_raw,amount,source_file,row_ref,import_hash,account_id)
                VALUES (9223372036854775807,'2026-01-02','synthetic',-2,'other.pdf','2','second','BETA')''')
        self.paths.category_rules_path.write_text('merchant_core,pattern,category,subcategory,notes\nSYNTHETIC,,Fixture,Test,\nNO HISTORY,,Fixture,Test,\n')

    def get(self, url):
        return self.client.get(url, base_url=self.base)

    def test_filter_uses_registered_names_and_keeps_empty_account(self):
        all_html = self.get('/rules').get_data(as_text=True)
        self.assertIn('First &lt;literal&gt;', all_html)
        self.assertIn('Second &amp; account', all_html)
        self.assertIn('Empty registered', all_html)
        self.assertIn('data-key="NO HISTORY"', all_html)
        self.assertIn('No historical matches', all_html)
        selected = self.get('/rules?account_id=ALPHA&q=SYN').get_data(as_text=True)
        self.assertIn('data-key="SYNTHETIC"', selected)
        self.assertNotIn('data-key="NO HISTORY"', selected)
        self.assertIn('value="ALPHA" selected', selected)
        self.assertNotIn('class="rule-row"', self.get('/rules?account_id=EMPTY').get_data(as_text=True))
        # Even an agreeing rule is visible when it has matches in this account.
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute("INSERT INTO categories VALUES (1,'Fixture')")
            conn.execute("INSERT INTO subcategories VALUES (1,1,'Test')")
            conn.execute('UPDATE transactions SET subcategory_id=1')
        self.assertIn('data-key="SYNTHETIC"', self.get('/rules?account_id=ALPHA').get_data(as_text=True))

    def test_read_only_matches_filter_and_label_accounts_losslessly(self):
        before = self.paths.db_path.read_bytes(), self.paths.category_rules_path.read_bytes()
        detail = self.get('/api/rules?key=SYNTHETIC&account_id=BETA')
        self.assertEqual(detail.status_code, 200)
        self.assertEqual([(r['txn_id'], r['account_id'], r['account_name']) for r in detail.json['matches']],
                         [('9223372036854775807', 'BETA', 'Second & account')])
        self.assertEqual(self.get('/api/rules?key=SYNTHETIC&account_id=EMPTY').json['matches'], [])
        self.assertEqual(len(self.get('/api/rules?key=SYNTHETIC').json['matches']), 2)
        self.assertEqual(before, (self.paths.db_path.read_bytes(), self.paths.category_rules_path.read_bytes()))

    def test_invalid_and_repeated_selectors_fail_closed(self):
        for selector in ('UNKNOWN', 'alpha', '%20ALPHA', '%3Cscript%3E', 'ALPHA&account_id=BETA', '&account_id='):
            for path in ('/rules?', '/api/rules?key=SYNTHETIC&'):
                with self.subTest(path=path, selector=selector):
                    self.assertEqual(self.get(path+'account_id='+selector).status_code, 400)

    def test_shared_confirmation_changes_one_rule_not_account_storage_or_transactions(self):
        from scripts.services.category_rules import load_rules
        self.get('/rules?account_id=ALPHA')
        before = self.paths.db_path.read_bytes()
        preview = self.post('/api/rules/preview', dict(key='SYNTHETIC', category='Fixture', subcategory='Test', enabled=False))
        result = self.post('/api/rules/confirm', dict(preview_token=preview.json['preview_token'], approved=True))
        self.assertEqual(result.status_code, 200)
        self.assertNotIn('SYNTHETIC', load_rules(self.paths.category_rules_path))
        self.assertEqual(self.paths.db_path.read_bytes(), before)
        self.assertNotIn('account_id', self.paths.category_rules_path.read_text().splitlines()[0])
        self.assertEqual(self.get('/api/rules?key=SYNTHETIC&account_id=BETA').json['rule']['enabled'], False)

    def test_missing_registry_has_all_accounts_only_and_no_implicit_setup(self):
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute('DROP TABLE accounts')
        # Other account tests reload the service; exception identity must not be cached.
        import importlib
        from scripts.services import accounts
        importlib.reload(accounts)
        before = self.paths.db_path.read_bytes()
        self.assertEqual(self.get('/rules').status_code, 200)
        self.assertIn('ALPHA (not registered)', self.get('/api/rules?key=SYNTHETIC').get_data(as_text=True))
        self.assertEqual(self.get('/rules?account_id=ALPHA').status_code, 400)
        self.assertEqual(self.paths.db_path.read_bytes(), before)

    def test_compact_list_uses_details_and_edit_dialog(self):
        import re
        page = self.get('/rules').get_data(as_text=True)
        table = page.split('<table id="rulesTable">')[1].split('</table>')[0]
        self.assertEqual(len(re.findall(r'<th(?:\s|>)', table)), 5)
        self.assertNotIn('<select', table)
        for row in re.findall(r'<tr class="rule-row".*?</tr>', table, re.S):
            self.assertEqual(re.findall(r'<button[^>]*>(.*?)</button>', row), ['View details', 'Edit rule'])
        self.assertIn('<dialog id="ruleEdit" aria-labelledby=', page)
        for name, label in [('ruleEditSave', 'Save changes'), ('rulePreviewApprove', 'Confirm change'), ('rulePreviewCancel', 'Cancel')]:
            self.assertRegex(page, 'id="'+name+'"[^>]*>'+label+'</button>')
        self.assertNotIn('Proposed destination', page)
        self.assertNotIn('Preview historical', page)

    def test_missing_rule_uses_add_dialog_without_writing_on_view(self):
        self.categorise_fixture()
        self.paths.category_rules_path.write_text('merchant_core,pattern,category,subcategory,notes\n')
        before = self.paths.db_path.read_bytes(), self.paths.category_rules_path.read_bytes()
        html = self.get('/rules').get_data(as_text=True)
        self.assertIn('>Add rule</button>', html)
        self.assertNotIn('>Edit rule</button>', html)
        self.assertEqual(before, (self.paths.db_path.read_bytes(), self.paths.category_rules_path.read_bytes()))

    def categorise_fixture(self):
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute("INSERT INTO categories VALUES (1,'Fixture')")
            conn.execute("INSERT INTO subcategories VALUES (1,1,'Test')")
            conn.execute('UPDATE transactions SET subcategory_id=1')

    def test_saved_rules_and_optional_suggestions_are_separate_and_read_only(self):
        self.categorise_fixture()
        self.paths.category_rules_path.write_text('merchant_core,pattern,category,subcategory,notes\nNO HISTORY,,Fixture,Test,\n')
        before = self.paths.db_path.read_bytes(), self.paths.category_rules_path.read_bytes()
        html = self.get('/rules').get_data(as_text=True)
        saved = html.split('<tbody id="rulesBody">')[1].split('</tbody>')[0]
        self.assertIn('data-key="NO HISTORY"', saved)
        self.assertNotIn('data-key="SYNTHETIC"', saved)
        self.assertIn('Saved rules', html)
        self.assertIn('Suggested rules (1)', html)
        self.assertIn('Optional: these transactions are already categorised; add a rule only if future matches should use the same category', html)
        suggestions = html.split('<tbody id="suggestionsBody">')[1].split('</tbody>')[0]
        self.assertIn('data-key="SYNTHETIC"', suggestions)
        self.assertIn('Fixture / Test (2)', suggestions)
        suggestion_table = html.split('<table id="suggestionsTable">')[1].split('</table>')[0]
        import re
        self.assertEqual(len(re.findall(r'<th(?:\s|>)', suggestion_table)), 3)
        self.assertIn('Saved category / subcategory', suggestion_table)
        linked = self.get('/rules?key=SYNTHETIC').get_data(as_text=True)
        self.assertIn('<details id="ruleSuggestions" class="card" open>', linked)
        self.assertNotIn('<details id="ruleSuggestions" class="card" open>', html)
        self.assertIn('>Add rule</button>', suggestions)
        self.assertNotIn('No rule (not an error)', html)
        self.assertIn('Previous rule edits', html)
        self.assertIn('This log shows edits to the rule. It does not change past transactions.', html)
        self.assertEqual(before, (self.paths.db_path.read_bytes(), self.paths.category_rules_path.read_bytes()))

    def test_uncategorised_suggestion_directs_transaction_categorisation_first(self):
        self.paths.category_rules_path.write_text('merchant_core,pattern,category,subcategory,notes\n')
        html = self.get('/rules').get_data(as_text=True)
        self.assertNotIn('these transactions are already categorised', html)
        self.assertIn('Uncategorised (2)', html)
        self.assertIn('Categorise transactions first', html)
        self.assertNotIn('>Add rule</button>', html)
        self.assertNotIn('id="rulesBody"', html)
        self.categorise_fixture()
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute("UPDATE transactions SET subcategory_id=NULL WHERE account_id='BETA'")
        mixed = self.get('/rules').get_data(as_text=True)
        self.assertIn('Fixture / Test (1)', mixed)
        self.assertIn('Uncategorised (1)', mixed)
        self.assertNotIn('these transactions are already categorised', mixed)
        self.assertNotIn('>Add rule</button>', mixed)
        selected = self.get('/rules?account_id=ALPHA').get_data(as_text=True)
        self.assertIn('these transactions are already categorised', selected)
        self.assertIn('>Add rule</button>', selected)

    def test_exceptions_counts_only_open_transactions_not_optional_rule_review(self):
        self.categorise_fixture()
        self.paths.category_rules_path.write_text('merchant_core,pattern,category,subcategory,notes\n')
        before = self.paths.db_path.read_bytes(), self.paths.category_rules_path.read_bytes()
        html = self.get('/exceptions').get_data(as_text=True)
        self.assertIn('Open transaction exceptions: 0', html)
        self.assertIn('Optional rule review (1)', html)
        self.assertIn('These are not open transaction exceptions.', html)
        self.assertIn('Optional future rule', html)
        self.assertNotIn('Rules needing review', html)
        self.assertNotIn('intentional transaction exception', html)
        self.assertEqual(before, (self.paths.db_path.read_bytes(), self.paths.category_rules_path.read_bytes()))
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute("UPDATE transactions SET subcategory_id=NULL WHERE account_id='ALPHA'")
            conn.execute("INSERT INTO exceptions(exception_id,txn_id,type,status,merchant_core,amount) VALUES (1,1,'uncategorised','open','SYNTHETIC',-1)")
        page = self.get('/exceptions').get_data(as_text=True)
        self.assertIn('Open transaction exceptions: 1', page)
        self.assertIn('Optional rule review (1)', page)
        self.assertIn('Categorise transactions first', page)
        self.assertNotIn('these transactions are already categorised', page)

    def test_suggestions_disclose_all_saved_assignments_and_filter_each_view(self):
        self.categorise_fixture()
        self.paths.category_rules_path.write_text('merchant_core,pattern,category,subcategory,notes\nNO HISTORY,,Fixture,Test,\n')
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute("INSERT INTO categories VALUES (2,'Other & category')")
            conn.execute("INSERT INTO subcategories VALUES (2,2,'Alternative <literal>')")
            conn.execute("UPDATE transactions SET subcategory_id=2 WHERE account_id='BETA'")
        before = self.paths.db_path.read_bytes(), self.paths.category_rules_path.read_bytes()
        html = self.get('/rules').get_data(as_text=True)
        suggestions = html.split('<tbody id="suggestionsBody">')[1].split('</tbody>')[0]
        for text in ['Fixture / Test (1)', 'Other &amp; category / Alternative &lt;literal&gt; (1)',
                     'Different saved assignments', 'No category is selected automatically.']:
            self.assertIn(text, suggestions)
        # Future-rule edit datasets remain empty: saved transaction categories must not impersonate a rule.
        self.assertIn('data-category="" data-subcategory=""', suggestions)
        self.assertIn('data-key="SYNTHETIC"', self.get('/rules?q=Alternative').get_data(as_text=True))
        self.assertNotIn('data-key="SYNTHETIC"', self.get('/rules?account_id=ALPHA&q=Alternative').get_data(as_text=True))
        self.assertIn('data-key="SYNTHETIC"', self.get('/rules?account_id=BETA&q=Alternative').get_data(as_text=True))
        self.assertIn('data-key="NO HISTORY"', self.get('/rules?q=Test').get_data(as_text=True))
        self.assertNotIn('data-key="NO HISTORY"', self.get('/rules?account_id=EMPTY').get_data(as_text=True))
        self.assertNotIn('data-key="SYNTHETIC"', self.get('/rules?account_id=EMPTY').get_data(as_text=True))
        self.assertEqual(before, (self.paths.db_path.read_bytes(), self.paths.category_rules_path.read_bytes()))

    def test_disabled_rules_and_conflicts_remain_saved_not_suggestions(self):
        self.categorise_fixture()
        self.paths.category_rules_path.write_text('merchant_core,pattern,category,subcategory,notes,enabled\nSYNTHETIC,,Fixture,Test,,false\n')
        html = self.get('/rules').get_data(as_text=True)
        saved = html.split('<tbody id="rulesBody">')[1].split('</tbody>')[0]
        self.assertIn('data-key="SYNTHETIC"', saved)
        self.assertIn('>Disabled</td>', saved)
        self.assertIn('>Edit rule</button>', saved)
        self.assertNotIn('id="ruleSuggestions"', html)
        self.paths.category_rules_path.write_text('merchant_core,pattern,category,subcategory,notes\nSYNTHETIC,,Other,Alternative,\n')
        html = self.get('/rules').get_data(as_text=True)
        self.assertIn('2 saved transactions use a different category. This can be intentional', html)
        self.assertNotIn('id="ruleSuggestions"', html)
        self.assertIn('Open transaction exceptions: 0', self.get('/exceptions').get_data(as_text=True))

    def test_real_javascript_controller(self):
        import subprocess
        from pathlib import Path
        result = subprocess.run(['node', 'tests/rules_display_probe.js'], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_global_approval_evidence_survives_filter_and_stales_on_other_account(self):
        self.get('/rules?account_id=ALPHA')
        preview = self.post('/api/rules/preview', dict(key='SYNTHETIC', category='Fixture', subcategory='Test', enabled=False))
        self.assertEqual(preview.status_code, 200)
        self.assertEqual({row['account_id'] for row in preview.json['matches']}, {'ALPHA', 'BETA'})
        self.assertEqual({row['account_name'] for row in preview.json['matches']}, {'First <literal>', 'Second & account'})
        before = self.paths.category_rules_path.read_bytes()
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute("UPDATE transactions SET txn_date='2026-02-01' WHERE account_id='BETA'")
        confirm = self.post('/api/rules/confirm', dict(preview_token=preview.json['preview_token'], approved=True))
        self.assertEqual(confirm.status_code, 409)
        self.assertEqual(self.paths.category_rules_path.read_bytes(), before)
        # A view filter is never an accepted rule-write condition.
        invalid = self.post('/api/rules/preview', dict(key='SYNTHETIC', category='Fixture', subcategory='Test', enabled=False, account_id='ALPHA'))
        self.assertEqual(invalid.status_code, 400)
