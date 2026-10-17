from contextlib import closing
import sqlite3
from tests.test_dashboard_security import DashboardFixture


class RuleReviewTests(DashboardFixture):
    def test_disagreement_and_disabled_candidates_do_not_automatically_change_assignments(self):
        from scripts.services import rule_review
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.row_factory=sqlite3.Row
            conn.execute("INSERT INTO categories VALUES (1,'Fixture')")
            conn.execute("INSERT INTO subcategories VALUES (1,1,'Test')")
            conn.execute('UPDATE transactions SET subcategory_id=1')
        before=self.paths.db_path.read_bytes()
        with closing(sqlite3.connect(self.paths.db_path)) as conn:
            conn.row_factory=sqlite3.Row
            rule=dict(merchant_core='SYNTHETIC',category='Fixture',subcategory='Other',enabled='true')
            self.assertEqual(rule_review.candidates(conn,{'SYNTHETIC':rule})[0]['reason'],'disagreement')
            rule['subcategory']='Test'
            self.assertEqual(rule_review.candidates(conn,{'SYNTHETIC':rule}),[])
            rule['enabled']='false'
            self.assertEqual(rule_review.candidates(conn,{'SYNTHETIC':rule})[0]['reason'],'disabled')
        self.assertEqual(self.paths.db_path.read_bytes(),before)

    def test_candidates_and_historical_scope_use_actual_import_normalizer_read_only(self):
        from scripts.services import rule_review
        from scripts.services.category_rules import snapshot
        from scripts.triage_descriptions import decompose_description
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute("UPDATE transactions SET merchant_core='WRONG STORED KEY'")
            conn.execute("INSERT INTO categories VALUES (1,'Fixture')")
            conn.execute("INSERT INTO subcategories VALUES (1,1,'Test')")
            conn.execute('UPDATE transactions SET subcategory_id=1')
        actual = decompose_description('synthetic').merchant_core.upper()
        before = self.paths.db_path.read_bytes()
        with closing(sqlite3.connect(f'file:{self.paths.db_path.as_posix()}?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA query_only=ON')
            candidates = rule_review.candidates(conn, snapshot(self.paths.category_rules_path)['rules'])
            self.assertEqual(candidates[0]['key'], actual)
            self.assertEqual(candidates[0]['reason'], 'missing')
            matches = rule_review.matches(conn, actual)
            self.assertEqual(matches[0]['txn_id'], '1')
            self.assertEqual(matches[0]['category'], 'Fixture')
        self.assertEqual(self.paths.db_path.read_bytes(), before)
