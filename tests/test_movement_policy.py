from contextlib import closing
import importlib
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.init_db import _SCHEMA_SQL
from scripts.migrate_db import migrate_db
from scripts.services.transfers import prepare_pair, prepare_unmatched, prepare_revoke, commit_transfer


class MovementPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name)/'fixture.db'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(_SCHEMA_SQL)
            for txn_id, amount, account in ((1,-10,'HSBC_UK'),(2,10,'IOM'),(3,-1,'HSBC_UK'),(4,20,'HSBC_UK')):
                conn.execute('INSERT INTO transactions(txn_id,txn_date,description_raw,amount,account_id,import_hash) VALUES (?,\'2026-01-01\',\'Own Accounts\',?,?,?)', (txn_id,amount,account,str(txn_id)))
            conn.commit()
        migrate_db(self.db)
        with closing(sqlite3.connect(self.db)) as conn,conn:
            conn.execute("UPDATE accounts SET owned=1,currency='GBP' WHERE account_id='IOM'")
            conn.execute('UPDATE transactions SET excluded=1 WHERE txn_id=1')

    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('scripts.services.movement_policy'), 'shared policy missing')
        return importlib.import_module('scripts.services.movement_policy')

    def test_existing_snapshot_survives_success_and_error_without_transaction_control(self):
        api = self.api()
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('BEGIN')
            conn.execute('SELECT COUNT(*) FROM transactions').fetchone()
            statements = []
            conn.set_trace_callback(statements.append)
            self.assertEqual(api.summarise_movement([1], conn=conn)['raw']['debits_pence'], 1000)
            with self.assertRaisesRegex(ValueError, 'transaction_missing'):
                api.summarise_movement([999], conn=conn)
            self.assertTrue(conn.in_transaction)
            self.assertFalse(any(s.upper().startswith(('BEGIN', 'ROLLBACK', 'COMMIT')) for s in statements))
            conn.rollback()

    def test_borrowed_wal_snapshot_does_not_mix_later_confirmation(self):
        api = self.api()
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('BEGIN')
            conn.execute('SELECT COUNT(*) FROM transactions').fetchone()
            commit_transfer(prepare_pair(1, 2, db_path=self.db), approved=True)
            before = api.summarise_movement([1,2,3,4], conn=conn)
            self.assertEqual(before['raw'], before['adjusted'])
            self.assertTrue(conn.in_transaction)
            conn.rollback()
            after = api.summarise_movement([1,2,3,4], conn=conn)
            self.assertEqual(after['adjusted']['credits_pence'], 2000)
            self.assertFalse(conn.in_transaction)

    def test_idle_error_rolls_back_only_owned_read_transaction_and_keeps_authorizer(self):
        api = self.api()
        with closing(sqlite3.connect(self.db)) as conn:
            allowed = {sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_FUNCTION,sqlite3.SQLITE_TRANSACTION}
            conn.set_authorizer(lambda action,*args: sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY)
            with self.assertRaisesRegex(ValueError, 'transaction_missing'):
                api.summarise_movement([999], conn=conn)
            self.assertFalse(conn.in_transaction)
            api.summarise_movement([1], conn=conn)
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute('DELETE FROM transactions')

    def test_raw_retains_excluded_rows_while_adjusted_respects_explicit_filter(self):
        api = self.api()
        summary = api.summarise_movement([1,3,4], db_path=self.db, include_excluded=False)
        self.assertEqual(summary['raw']['debits_pence'], 1100)
        self.assertEqual(summary['adjusted']['debits_pence'], 100)

    def test_low_decimal_precision_retains_penny_mismatch_in_readonly_movement(self):
        from decimal import localcontext
        api = self.api()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE transactions SET amount=-10.01 WHERE txn_id=1')
            conn.execute('UPDATE transactions SET amount=10.02 WHERE txn_id=2')
        before = self.db.read_bytes()
        with localcontext() as context:
            context.prec = 3
            summary = api.summarise_movement([1, 2], db_path=self.db)
        self.assertEqual(summary['raw'], {'credits_pence': 1002, 'debits_pence': 1001, 'net_pence': 1})
        self.assertEqual(summary['adjusted'], summary['raw'])
        self.assertEqual(summary['transfers']['in_count'], 0)
        self.assertEqual(summary['transfers']['out_count'], 0)
        self.assertEqual(self.db.read_bytes(), before)

    def test_low_decimal_precision_confirmed_pair_adjusts_exact_pence_only(self):
        from decimal import localcontext, Inexact, Rounded
        api = self.api()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE transactions SET amount=-10.01 WHERE txn_id=1')
            conn.execute('UPDATE transactions SET amount=10.01 WHERE txn_id=2')
            conn.execute('UPDATE transactions SET amount=-1.02 WHERE txn_id=3')
            conn.execute('UPDATE transactions SET amount=20.03 WHERE txn_id=4')
        with localcontext() as context:
            context.prec = 3
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            commit_transfer(prepare_pair(1, 2, db_path=self.db), approved=True)
            before = self.db.read_bytes()
            summary = api.summarise_movement([1, 2, 3, 4], db_path=self.db)
        self.assertEqual(summary['raw'], {'credits_pence': 3004, 'debits_pence': 1103, 'net_pence': 1901})
        self.assertEqual(summary['adjusted'], {'credits_pence': 2003, 'debits_pence': 102, 'net_pence': 1901})
        self.assertEqual(summary['transfers'], {'in_count': 1, 'out_count': 1, 'in_pence': 1001,
                                               'out_pence': 1001, 'unmatched_count': 0})
        self.assertEqual(self.db.read_bytes(), before)

    def test_unmatched_leg_adjusts_only_itself_and_warns_while_fees_remain(self):
        api = self.api()
        result = commit_transfer(prepare_unmatched(1,'IOM',db_path=self.db),approved=True)
        before = self.db.read_bytes()
        summary = api.summarise_movement([1,3,4], db_path=self.db)
        self.assertEqual(summary['raw'], {'credits_pence':2000,'debits_pence':1100,'net_pence':900})
        self.assertEqual(summary['adjusted'], {'credits_pence':2000,'debits_pence':100,'net_pence':1900})
        self.assertEqual(summary['transfers']['unmatched_count'],1)
        self.assertIn('explicit_unmatched_transfers',summary['warnings'])
        self.assertEqual(summary['unclassified_count'],3)
        self.assertEqual(self.db.read_bytes(),before)
        commit_transfer(prepare_revoke(result.group_id,db_path=self.db),approved=True)
        summary = api.summarise_movement([1,3,4],db_path=self.db)
        self.assertEqual(summary['adjusted'],summary['raw'])
        self.assertEqual(summary['transfers']['unmatched_count'],0)

    def test_pre_migration_policy_counts_every_raw_row_without_creating_setup(self):
        api = self.api()
        old = Path(self.temp.name)/'legacy.db'
        with closing(sqlite3.connect(old)) as conn,conn:
            conn.executescript(_SCHEMA_SQL)
            conn.execute("INSERT INTO transactions(txn_id,txn_date,description_raw,amount,import_hash) VALUES (1,'2026-01-01','Own Accounts',-10,'old')")
        before = old.read_bytes()
        summary = api.summarise_movement([1],db_path=old)
        self.assertFalse(summary['transfer_schema_ready'])
        self.assertEqual(summary['transfers']['out_count'],0)
        self.assertEqual(summary['raw'],summary['adjusted'])
        self.assertEqual(old.read_bytes(),before)
        missing = Path(self.temp.name)/'absent.db'
        with self.assertRaises(FileNotFoundError):
            api.summarise_movement([],db_path=missing)
        self.assertFalse(missing.exists())

    def test_invalid_missing_duplicate_scope_and_mixed_currency_are_rejected(self):
        api = self.api()
        before = self.db.read_bytes()
        for scope in ([True],[9223372036854775808],[1,1],[999],None):
            with self.subTest(scope=scope), self.assertRaises(ValueError):
                api.summarise_movement(scope,db_path=self.db)
        self.assertEqual(before,self.db.read_bytes())
        with closing(sqlite3.connect(self.db)) as conn,conn:
            conn.execute("UPDATE transactions SET currency='USD' WHERE txn_id=4")
        with self.assertRaisesRegex(ValueError,'unsupported_currency'):
            api.summarise_movement([1,4],db_path=self.db)

    def test_caller_readonly_connection_needs_only_selects_and_returns_empty_scope(self):
        api = self.api()
        with closing(sqlite3.connect(self.db.as_uri()+'?mode=ro',uri=True)) as conn:
            conn.execute('PRAGMA query_only=ON')
            before = self.db.read_bytes()
            summary = api.summarise_movement([],conn=conn)
            self.assertEqual(summary['row_count'],0)
            self.assertEqual(summary['raw']['net_pence'],0)
            self.assertFalse(conn.in_transaction)
            self.assertEqual(self.db.read_bytes(),before)

    def test_only_confirmed_legs_are_adjusted_in_exact_selected_scope(self):
        api = self.api()
        initial = api.summarise_movement([1,2,3,4], db_path=self.db)
        self.assertEqual(initial['raw'], {'credits_pence':3000,'debits_pence':1100,'net_pence':1900})
        self.assertEqual(initial['adjusted'], initial['raw'])
        result = commit_transfer(prepare_pair(1,2,db_path=self.db), approved=True)
        summary = api.summarise_movement([1,2,3,4], db_path=self.db)
        self.assertEqual(summary['raw'], initial['raw'])
        self.assertEqual(summary['adjusted'], {'credits_pence':2000,'debits_pence':100,'net_pence':1900})
        self.assertEqual(summary['transfers'], {'in_count':1,'out_count':1,'in_pence':1000,'out_pence':1000,'unmatched_count':0})
        one = api.summarise_movement([1], db_path=self.db)
        self.assertEqual(one['raw']['debits_pence'],1000)
        self.assertEqual(one['adjusted']['debits_pence'],0)
        self.assertEqual(one['excluded_count'],1)
        commit_transfer(prepare_revoke(result.group_id,db_path=self.db),approved=True)
        self.assertEqual(api.summarise_movement([1,2,3,4],db_path=self.db)['adjusted'],initial['raw'])
