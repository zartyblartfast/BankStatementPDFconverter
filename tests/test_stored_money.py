"""Synthetic legacy REAL arithmetic tails; never fixtures from a user's ledger."""
from contextlib import closing
from decimal import Decimal, Inexact, Rounded, localcontext
from pathlib import Path
import tempfile
import sqlite3
import unittest
from unittest.mock import patch

from scripts import dashboard
from scripts.services import transfers
from tests import test_movement_policy as fixture


class StoredMoneyTests(unittest.TestCase):
    db: Path
    temp: tempfile.TemporaryDirectory
    setUp = fixture.MovementPolicyTests.setUp

    def api(self):
        from scripts.services.agent_analysis import AgentAnalysis
        definitions = Path(self.temp.name) / 'categories.json'
        definitions.write_text('{"Fixture":["Test"]}')
        api = object.__new__(AgentAnalysis)
        api.db, api.categories, api.allow_details = self.db, definitions, True
        return api

    def test_home_planning_reports_agent_share_legacy_cent_semantics(self):
        from scripts.report_data import query_report
        self.legacy_rows()
        # Test current live shape (004 not yet applied), then isolated confirmed 004.
        from scripts.migrate_db import migrate_db
        with closing(sqlite3.connect(self.db)) as conn, conn:
            for table in ('transfer_audit','transfer_members','transfer_groups'):
                conn.execute('DROP TABLE ' + table)
            conn.execute('DELETE FROM schema_migrations WHERE version=4')
        api = self.api()
        for confirmed in (False, True):
            if confirmed:
                self.assertEqual(migrate_db(self.db), [4])
                transfers.commit_transfer(transfers.prepare_pair(1, 2, db_path=self.db), approved=True)
            before = self.db.read_bytes()
            with self.subTest(confirmed=confirmed), localcontext() as context:
                context.prec = 3
                context.traps[Inexact] = context.traps[Rounded] = True
                with patch.object(dashboard, 'DB_PATH', self.db):
                    client = dashboard.app.test_client()
                    self.assertEqual(client.get('/').status_code, 200)
                    self.assertEqual(client.get('/planning').status_code, 200)
                    planning = client.get('/api/planning-data')
                    self.assertEqual(planning.status_code, 200, planning.get_data(as_text=True))
                    with patch.object(dashboard, 'render_template', side_effect=lambda template, **values: values):
                        movement = client.get('/').json['stats']['movement']
                planning = planning.json
                self.assertEqual(planning['movement'], movement)
                self.assertEqual(planning['grid_pence'], {'Uncategorised': {'2026-01': 102}})
                records, months = query_report(db_path=self.db)
                for basis in ('raw', 'adjusted'):
                    self.assertEqual({k:int(v) for k,v in records.policy[basis].items()}, movement[basis])
                self.assertEqual([r['amount_pence'] for r in records], ['-1001','1001','-102','2003'])
                # Raw REAL evidence stays in report records, not replaced by decimal cents.
                self.assertEqual(records[0]['amount'], 100.00 - 110.01)
                import importlib
                import json
                import re
                for name in ('income_vs_expense','income_by_category','expenditure_by_category','expenditure_heatmap'):
                    html = importlib.import_module('scripts.report_' + name)._build_html(records, months)
                    match = re.search(r'const REPORT_POLICY\s*=\s*', html)
                    self.assertIsNotNone(match)
                    embedded, _ = json.JSONDecoder().raw_decode(html, match.end())
                    self.assertEqual(embedded['raw'], records.policy['raw'])
                    self.assertEqual(embedded['adjusted'], records.policy['adjusted'])
                summary = api.summarise_transactions()['data']
                search = api.search_transactions(include_details=True)['data']
                self.assertEqual({k:summary[k] for k in movement['adjusted']}, movement['adjusted'])
                self.assertEqual({k:search[k] for k in movement['raw']}, movement['raw'])
                self.assertEqual(sorted(r['amount_pence'] for r in search['transactions']), [-1001,-102,1001,2003])
                self.assertEqual(api.list_categories()['data']['uncategorised_count'], 4)
                self.assertEqual(movement['raw'], {'credits_pence':3004,'debits_pence':1103,'net_pence':1901})
                self.assertEqual(movement['adjusted'], {'credits_pence':2003 if confirmed else 3004,
                                                       'debits_pence':102, 'net_pence':1901 if confirmed else 2902})
                self.assertEqual(movement['transfer_schema_ready'], confirmed)
            self.assertEqual(self.db.read_bytes(), before)

    def test_ledger_boundary_is_context_independent_and_does_not_relax_inputs(self):
        import math
        for precision in (3, 28, 60):
            with localcontext() as context:
                context.prec = precision
                context.traps[Inexact] = context.traps[Rounded] = True
                for value, cents in ((100.00-110.01,-1001), (110.01-100.00,1001),
                                     (0.03-0.01,2), (-0.03+0.01,-2), (20.03,2003),
                                     ('10.01',1001), (Decimal('10.01'),1001), (10,1000)):
                    self.assertEqual(transfers.ledger_pence(value), cents)
                for value in (0.005,-0.005,10.001,1.00000002,-1.00000002,
                              math.inf,-math.inf,math.nan,None,True,'',
                              '1.00000000000000000000000000001',
                              Decimal('1.00000000000000000000000000001')):
                    with self.subTest(value=str(value), precision=precision):
                        with self.assertRaisesRegex(transfers.TransferError, 'invalid_amount'):
                            transfers.ledger_pence(value)
                with self.assertRaisesRegex(transfers.TransferError, 'invalid_amount'):
                    transfers.exact_pence(100.00-110.01)
                # A neighbour just beyond the fixed absolute cap remains invalid.
                boundary = 1.0 + 1e-8
                self.assertEqual(transfers.ledger_pence(boundary), 100)
                with self.assertRaisesRegex(transfers.TransferError, 'invalid_amount'):
                    transfers.ledger_pence(math.nextafter(boundary, math.inf))

    def test_suggestions_compare_cent_semantics_not_binary_real_equality(self):
        self.legacy_rows()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            # Independently parsed positive leg has a different binary encoding.
            conn.execute('UPDATE transactions SET amount=10.01 WHERE txn_id=2')
            for txn_id, amount in ((5,10.01), (6,110.02-100.00), (7,10.005)):
                conn.execute("INSERT INTO transactions(txn_id,txn_date,description_raw,amount,account_id,import_hash) VALUES (?,'2026-01-01','Synthetic',?,'IOM',?)", (txn_id,amount,str(txn_id)))
        before = self.db.read_bytes()
        with localcontext() as context:
            context.prec = 3
            context.traps[Inexact] = context.traps[Rounded] = True
            page = transfers.suggest_pairs(1, start_date='2026-01-01', end_date='2026-01-31', limit=1, db_path=self.db)
            self.assertEqual(page['total'], 2)
            self.assertEqual([(r['txn_id'],r['cents']) for r in page['candidates']], [(2,1001)])
            self.assertTrue(page['has_more'])
            second = transfers.suggest_pairs(1, start_date='2026-01-01', end_date='2026-01-31', limit=1, offset=1, db_path=self.db)
            self.assertEqual([(r['txn_id'],r['cents']) for r in second['candidates']], [(5,1001)])
            self.assertFalse(second['has_more'])
            self.assertEqual(transfers.prepare_pair(1,2,db_path=self.db).action, 'pair')
            with self.assertRaisesRegex(transfers.TransferError, 'amount_mismatch'):
                transfers.prepare_pair(1,6,db_path=self.db)
            with self.assertRaisesRegex(transfers.TransferError, 'invalid_amount'):
                transfers.prepare_pair(1,7,db_path=self.db)
        self.assertEqual(self.db.read_bytes(), before)
        # The retired dashboard UI does not list/normalise rows or suggest pairs;
        # the retained service behavior above remains independently exercised.
        with patch.object(dashboard, 'DB_PATH', self.db):
            response = dashboard.app.test_client().get('/transfers?start=2026-01-01&end=2026-01-31&txn_id=1')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers['Location'], '/transactions?start=2026-01-01&end=2026-01-31')
        self.assertEqual(self.db.read_bytes(), before)

    def test_normalisation_metadata_and_audit_keep_original_real_evidence(self):
        import json
        from scripts.report_data import query_report
        from scripts.services.movement_policy import summarise_movement
        self.legacy_rows()
        with closing(sqlite3.connect(self.db)) as conn:
            original = conn.execute('SELECT * FROM transactions ORDER BY txn_id').fetchall()
        before = self.db.read_bytes()
        summary = summarise_movement([1,2,3,4],db_path=self.db)
        self.assertEqual(summary['normalised_amount_count'], 3)
        self.assertEqual(summary['amount_read_method'], 'stored_real_near_cent_1e-6_pence')
        self.assertIn('stored_real_amount_residue_normalised', summary['warnings'])
        records, _ = query_report(db_path=self.db)
        self.assertEqual(records.policy['normalised_amount_count'], 3)
        self.assertIn('stored_real_amount_residue_normalised', self.api().summarise_transactions()['meta']['warnings'])
        self.assertEqual(self.db.read_bytes(), before)
        preview = transfers.prepare_pair(1,2,db_path=self.db)
        with patch.object(dashboard, 'DB_PATH', self.db):
            members = dashboard._transfer_members(preview)
            self.assertEqual([r['cents'] for r in members], [-1001,1001])
        transfers.commit_transfer(preview, approved=True)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT * FROM transactions ORDER BY txn_id').fetchall(), original)
            audit = json.loads(conn.execute('SELECT decision_json FROM transfer_audit').fetchone()[0])
        self.assertEqual([r['cents'] for r in audit['members']], [-1001,1001])
        self.assertEqual([r['stored_amount'] for r in audit['members']], [100.00-110.01,110.01-100.00])

    def legacy_rows(self):
        # Balance subtraction used by old imports: intended -10.01 and -1.02.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE transactions SET amount=? WHERE txn_id=1', (100.00 - 110.01,))
            conn.execute('UPDATE transactions SET amount=? WHERE txn_id=2', (110.01 - 100.00,))
            conn.execute('UPDATE transactions SET amount=? WHERE txn_id=3', (99.00 - 100.02,))
            conn.execute('UPDATE transactions SET amount=? WHERE txn_id=4', (20.03,))

    def test_home_legacy_real_tail_does_not_return_invalid_amount(self):
        self.legacy_rows()
        before = self.db.read_bytes()
        with patch.object(dashboard, 'DB_PATH', self.db):
            client = dashboard.app.test_client()
            response = client.get('/')
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            with patch.object(dashboard, 'render_template', side_effect=lambda template, **values: values):
                stats = client.get('/').json['stats']
        self.assertEqual(stats['movement']['raw'], {'credits_pence': 3004, 'debits_pence': 1103, 'net_pence': 1901})
        self.assertEqual(self.db.read_bytes(), before)
