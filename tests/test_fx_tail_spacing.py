"""FX-tail spacing must not create rate-specific exact merchant keys."""
from contextlib import closing
import sqlite3
import unittest
from scripts.triage_descriptions import decompose_description
from scripts.services.rule_review import merchant_key
from tests.test_dashboard_security import DashboardFixture

KEY = 'OPENAI * SU OPENAI.COM'
VARIANTS = [f'{KEY} USD 120.00 @{space}{rate} VISA RATE 89.69'
            for space, rate in [('', '1.3379'), ('', '1.2510'), ('', '1.3000'),
                                ('', '1.2759'), ('', '1.4200'), ('', '1.2111'),
                                (' ', '1.3379'), ('  ', '1.2510'), ('\t', '1.3000')]]

class FXTailSpacingTests(unittest.TestCase):
    def test_rate_variants_share_the_existing_spaced_merchant_key(self):
        for raw in VARIANTS:
            with self.subTest(raw=raw):
                self.assertEqual(decompose_description(raw).merchant_core, KEY)
                self.assertEqual(merchant_key(raw), KEY)

    def test_other_merchants_products_and_non_fx_text_remain_distinct(self):
        for raw, expected in [
            ('OPENAI * API OPENAI.COM USD 15.00 @1.2 VISA RATE', 'OPENAI * API OPENAI.COM'),
            ('OTHER SHOP EUR 1,200.00 @1.2 VISA RATE', 'OTHER SHOP'),
            ('VIS 0011840346 OPENAI * SU OPENAI.COM usd 120.00 @1.2 VISA RATE', KEY),
            ('OPENAI * SU OPENAI.COM USD 120.00', KEY + ' USD 120.00'),
            ('OPENAI * SU OPENAI.COM USD 120.00@1.2', KEY + ' USD 120.00@1.2'),
            ('OPENAI * SU OPENAI.COM USD 120 @1.2', KEY + ' USD 120 @1.2'),
            ('OPENAI * SU OPENAI.COM @support', KEY + ' @SUPPORT'),
            ('DR Non-Sterling Transaction Fee', 'NON-STERLING TRANSACTION FEE'),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(merchant_key(raw), expected)
        self.assertNotEqual(merchant_key('OPENAI * API OPENAI.COM'), KEY)

    def test_future_import_prepare_and_commit_use_shared_key_and_keep_other_products_unassigned(self):
        from tests import test_ingestion_service as fixtures
        fixture = fixtures.IngestionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.migrate()
        (fixture.root / 'config/category_rules.csv').write_text(
            'merchant_core,category,subcategory\n' + KEY + ',Test,Valid\n', encoding='utf-8')
        rows = [('2026-01-02', raw, -1, f'1:{i+2}') for i,raw in enumerate(VARIANTS)]
        rows.append(('2026-01-03','OPENAI * API OPENAI.COM USD 15.00 @1.2 VISA RATE',-2,'1:99'))
        preview = fixture.prepare(fixture.extraction(rows))
        self.assertEqual(preview.status, 'eligible')
        result = fixture.commit(preview)
        self.assertEqual(result.added,len(rows))
        with closing(sqlite3.connect(fixture.db)) as c:
            assigned = c.execute('SELECT t.merchant_core,c.name,s.name FROM transactions t LEFT JOIN subcategories s USING(subcategory_id) LEFT JOIN categories c USING(category_id) ORDER BY txn_id').fetchall()
        self.assertEqual(assigned[:-1],[(KEY,'Test','Valid')]*len(VARIANTS))
        self.assertEqual(assigned[-1],('OPENAI * API OPENAI.COM',None,None))

class FXTailReviewTests(DashboardFixture):
    def test_rate_variants_are_one_suggestion_or_existing_rule_group_without_ledger_edits(self):
        from scripts.services import rule_review
        from scripts.services.category_rules import snapshot
        with closing(sqlite3.connect(self.paths.db_path)) as c, c:
            c.execute('DELETE FROM transactions')
            for i, raw in enumerate(VARIANTS):
                c.execute("INSERT INTO transactions(txn_date,description_raw,amount,source_file,row_ref,import_hash,merchant_core) VALUES('2026-01-01',?,-1,'fixture.pdf',?,?,'UNCHANGED STORED KEY')", (raw,str(i),str(i)))
        before = self.paths.db_path.read_bytes()
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.row_factory = sqlite3.Row
            groups = rule_review.candidates(c, {})
            self.assertEqual([(g['key'],g['count']) for g in groups], [(KEY,len(VARIANTS))])
        html = self.client.get('/rules?q=OPENAI',base_url=self.base)
        self.assertEqual(html.status_code,200)
        self.assertNotIn('USD 120.00',html.get_data(as_text=True))
        self.paths.category_rules_path.write_text('merchant_core,pattern,category,subcategory,notes\n'+KEY+',,Fixture,Test,\n',encoding='utf-8')
        with closing(sqlite3.connect(self.paths.db_path)) as c:
            c.row_factory=sqlite3.Row
            groups=rule_review.candidates(c,snapshot(self.paths.category_rules_path)['rules'])
            self.assertEqual([(g['key'],g['count'],g['reason']) for g in groups],[(KEY,len(VARIANTS),'disagreement')])
        detail=self.client.get('/api/rules',query_string={'key':KEY},base_url=self.base)
        self.assertEqual(detail.status_code,200)
        self.assertEqual(len(detail.json['matches']),len(VARIANTS))
        self.assertEqual(self.paths.db_path.read_bytes(),before)
