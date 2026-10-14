from contextlib import closing
from pathlib import Path
import importlib
import importlib.util
import sqlite3
import tempfile
import unittest

from scripts.init_db import _SCHEMA_SQL
from scripts.migrate_db import migrate_db


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'ledger.db'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(_SCHEMA_SQL)
            conn.execute("INSERT INTO transactions(txn_id,txn_date,description_raw,amount,account_id,import_hash,user_note) VALUES (1,'2026-01-31','Own Accounts',-10,'HSBC_UK','one','keep')")
            conn.execute("INSERT INTO transactions(txn_id,txn_date,description_raw,amount,account_id,import_hash) VALUES (2,'2026-02-02','Own Accounts',10,'IOM','two')")
            conn.commit()
        migrate_db(self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE accounts SET owned=1,currency='GBP' WHERE account_id='IOM'")

    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('scripts.services.transfers'), 'transfer service missing')
        return importlib.import_module('scripts.services.transfers')

    def rows(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute('SELECT * FROM transactions ORDER BY txn_id').fetchall()

    def test_pair_requires_signed_explicit_approval_and_preserves_raw_rows(self):
        api = self.api()
        before = self.rows()
        preview = api.prepare_pair(1, 2, db_path=self.db)
        self.assertEqual(self.rows(), before)
        with self.assertRaisesRegex(ValueError, 'approval_required'):
            api.commit_transfer(preview)
        result = api.commit_transfer(preview, approved=True)
        self.assertFalse(result.replayed)
        self.assertEqual(self.rows(), before)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT status FROM transfer_groups').fetchall(), [('paired',)])
            self.assertEqual(conn.execute('SELECT txn_id,active FROM transfer_members ORDER BY txn_id').fetchall(), [(1,1),(2,1)])
            self.assertEqual(conn.execute('SELECT action FROM transfer_audit').fetchall(), [('pair',)])
        self.assertTrue(api.commit_transfer(preview, approved=True).replayed)

    def test_unmatched_later_pair_revoke_retains_history_without_duplicate_active(self):
        api = self.api()
        self.assertTrue(hasattr(api, 'prepare_unmatched'), 'unmatched lifecycle missing')
        original = self.rows()
        result = api.commit_transfer(api.prepare_unmatched(1, 'IOM', db_path=self.db), approved=True)
        group_id = result.group_id
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT status FROM transfer_groups').fetchall(), [('unmatched',)])
        paired = api.commit_transfer(api.prepare_pair(1, 2, db_path=self.db), approved=True)
        self.assertEqual(paired.group_id, group_id)
        revoked = api.commit_transfer(api.prepare_revoke(group_id, db_path=self.db), approved=True)
        self.assertEqual(revoked.group_id, group_id)
        self.assertEqual(self.rows(), original)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT active FROM transfer_members').fetchall(), [(0,),(0,)])
            self.assertEqual(conn.execute('SELECT action FROM transfer_audit ORDER BY audit_id').fetchall(), [('unmatched',),('pair',),('revoke',)])
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute('DELETE FROM transfer_audit')
        new = api.commit_transfer(api.prepare_pair(1, 2, db_path=self.db), approved=True)
        self.assertNotEqual(new.group_id, group_id)

    def test_connection_preview_cannot_be_committed_on_another_connection(self):
        api = self.api()
        with closing(sqlite3.connect(self.db)) as first, closing(sqlite3.connect(self.db)) as second:
            preview = api.prepare_pair(1, 2, conn=first)
            with self.assertRaisesRegex(ValueError, 'invalid_preview'):
                api.commit_transfer(preview, conn=second, approved=True)
            self.assertEqual(api.commit_transfer(preview, conn=first, approved=True).action, 'pair')

    def dump(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return list(conn.iterdump())

    def test_low_decimal_precision_rejects_penny_mismatch_without_any_ledger_writes(self):
        from decimal import localcontext
        api = self.api()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE transactions SET amount=-10.01 WHERE txn_id=1')
            conn.execute('UPDATE transactions SET amount=10.02 WHERE txn_id=2')
        before = self.dump()
        with localcontext() as context:
            context.prec = 3
            with self.assertRaisesRegex(api.TransferError, 'amount_mismatch'):
                api.prepare_pair(1, 2, db_path=self.db)
        self.assertEqual(self.dump(), before)
        with closing(sqlite3.connect(self.db)) as conn:
            for table in ('transfer_groups', 'transfer_members', 'transfer_audit'):
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)

    def test_exact_pence_rejects_fractional_nonfinite_and_invalid_values(self):
        from decimal import Decimal, localcontext
        api = self.api()
        for precision in (3, 28, 60):
            with localcontext() as context:
                context.prec = precision
                for value in ('1.00000000000000000000000000001',
                              '-1.00000000000000000000000000001',
                              Decimal('0.00000000000000000000000000001'),
                              'NaN', 'sNaN', 'Infinity', '-Infinity', None, True, ''):
                    with self.subTest(precision=precision, value=str(value)):
                        with self.assertRaisesRegex(api.TransferError, 'invalid_amount'):
                            api.exact_pence(value)
                for value, expected in ((-10.01, -1001), ('10.02', 1002),
                                        (Decimal('12345678901234567890.01'), 1234567890123456789001),
                                        ('-0.00', 0), ('1E+3', 100000)):
                    with self.subTest(precision=precision, value=str(value)):
                        self.assertEqual(api.exact_pence(value), expected)

    def test_low_decimal_precision_pair_audits_exact_pence_and_preserves_transactions(self):
        from decimal import localcontext, Inexact, Rounded
        import json
        api = self.api()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE transactions SET amount=-10.01 WHERE txn_id=1')
            conn.execute('UPDATE transactions SET amount=10.01 WHERE txn_id=2')
        original = self.rows()
        with localcontext() as context:
            context.prec = 3
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            preview = api.prepare_pair(1, 2, db_path=self.db)
            result = api.commit_transfer(preview, approved=True)
            self.assertTrue(api.commit_transfer(preview, approved=True).replayed)
        self.assertFalse(result.replayed)
        self.assertEqual(self.rows(), original)
        with closing(sqlite3.connect(self.db)) as conn:
            decisions = conn.execute('SELECT decision_json FROM transfer_audit').fetchall()
            self.assertEqual(len(decisions), 1)
            self.assertEqual([member['cents'] for member in json.loads(decisions[0][0])['members']],
                             [-1001, 1001])

    def test_low_decimal_precision_suggestions_return_exact_pence_without_writes(self):
        from decimal import localcontext
        api = self.api()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE transactions SET amount=-10.01 WHERE txn_id=1')
            conn.execute('UPDATE transactions SET amount=10.01 WHERE txn_id=2')
        before = self.dump()
        with localcontext() as context:
            context.prec = 3
            page = api.suggest_pairs(1, start_date='2026-01-01', end_date='2026-03-01', db_path=self.db)
        self.assertEqual(page['total'], 1)
        self.assertEqual([(row['txn_id'], row['cents']) for row in page['candidates']], [(2, 1001)])
        self.assertEqual(self.dump(), before)

    def test_validation_rejects_non_integer_ids_non_owned_unknown_same_fees_and_fx(self):
        api = self.api()
        before = self.dump()
        for value in (True, False, '1', 1.0, 0, -1, 9223372036854775808):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'invalid_id'):
                api.prepare_pair(value, 2, db_path=self.db)
        for counterpart in ('MISSING', 'HSBC_UK'):
            with self.subTest(counterpart=counterpart), self.assertRaises(ValueError):
                api.prepare_unmatched(1, counterpart, db_path=self.db)
        self.assertEqual(before, self.dump())
        for sql in ("UPDATE accounts SET owned=0 WHERE account_id='IOM'",
                    "UPDATE accounts SET currency='USD' WHERE account_id='IOM'",
                    "UPDATE transactions SET currency='USD' WHERE txn_id=2",
                    'UPDATE transactions SET amount=9.99 WHERE txn_id=2',
                    'UPDATE transactions SET amount=10.001 WHERE txn_id=2',
                    'UPDATE transactions SET amount=-10 WHERE txn_id=2',
                    "UPDATE transactions SET account_id='HSBC_UK' WHERE txn_id=2"):
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute(sql)
            state = self.dump()
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                api.prepare_pair(1, 2, db_path=self.db)
            self.assertEqual(state, self.dump())
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE accounts SET owned=1,currency='GBP' WHERE account_id='IOM'")
                conn.execute("UPDATE transactions SET amount=10,currency='GBP',account_id='IOM' WHERE txn_id=2")

    def test_tampering_competing_previews_and_timestamp_edits_are_stale(self):
        from dataclasses import replace
        api = self.api()
        preview = api.prepare_pair(1,2,db_path=self.db)
        before = self.dump()
        with self.assertRaisesRegex(ValueError, 'invalid_preview'):
            api.commit_transfer(replace(preview, counterparts=('HSBC_UK','IOM')), approved=True)
        self.assertEqual(before, self.dump())
        other = api.prepare_unmatched(1,'IOM',db_path=self.db)
        result = api.commit_transfer(preview,approved=True)
        with self.assertRaisesRegex(ValueError, 'stale_preview'):
            api.commit_transfer(other,approved=True)
        revoke = api.prepare_revoke(result.group_id, db_path=self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE transfer_groups SET updated_at='changed' WHERE group_id=?", (result.group_id,))
        state = self.dump()
        with self.assertRaisesRegex(ValueError, 'stale_preview'):
            api.commit_transfer(revoke, approved=True)
        self.assertEqual(state, self.dump())
        self.assertTrue(api.commit_transfer(preview,approved=True).replayed)

    def test_abort_trigger_rolls_back_all_tables_for_pair_and_revoke(self):
        api = self.api()
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON transfer_audit BEGIN SELECT RAISE(ABORT,'synthetic abort'); END")
        preview = api.prepare_pair(1,2,db_path=self.db)
        before = self.dump()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'synthetic abort'):
            api.commit_transfer(preview, approved=True)
        self.assertEqual(before, self.dump())
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('DROP TRIGGER reject_audit')
        result = api.commit_transfer(api.prepare_pair(1,2,db_path=self.db), approved=True)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON transfer_audit BEGIN SELECT RAISE(ABORT,'synthetic abort'); END")
        preview = api.prepare_revoke(result.group_id,db_path=self.db)
        before = self.dump()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'synthetic abort'):
            api.commit_transfer(preview,approved=True)
        self.assertEqual(before, self.dump())

    def test_suggestions_are_bounded_readonly_keep_ambiguous_matches_and_never_select(self):
        api = self.api()
        self.assertTrue(hasattr(api, 'suggest_pairs'), 'suggestions missing')
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO transactions(txn_date,description_raw,amount,account_id,import_hash) VALUES ('2026-02-02','duplicate',10,'IOM','three')")
            conn.execute("INSERT INTO transactions(txn_date,description_raw,amount,account_id,import_hash) VALUES ('2026-02-02','fee',9.99,'IOM','four')")
        before = self.dump()
        page = api.suggest_pairs(1, start_date='2026-01-01', end_date='2026-03-01', limit=1, db_path=self.db)
        self.assertEqual(page['total'], 2)
        self.assertTrue(page['has_more'])
        self.assertEqual([r['txn_id'] for r in page['candidates']], [2])
        self.assertNotIn('selected', page)
        next_page = api.suggest_pairs(1, start_date='2026-01-01', end_date='2026-03-01', limit=1, offset=1, db_path=self.db)
        self.assertEqual([r['txn_id'] for r in next_page['candidates']], [3])
        self.assertFalse(next_page['has_more'])
        self.assertEqual(before, self.dump())
        with self.assertRaises(ValueError):
            api.suggest_pairs(1, start_date='2026-01-01', end_date='2027-01-01', db_path=self.db)
        for limit in (True,0,101):
            with self.assertRaises(ValueError):
                api.suggest_pairs(1,start_date='2026-01-01',end_date='2026-03-01',limit=limit,db_path=self.db)

    def test_audit_captures_financial_evidence_not_just_generated_membership_ids(self):
        import json
        api = self.api()
        api.commit_transfer(api.prepare_pair(1,2,db_path=self.db),approved=True)
        with closing(sqlite3.connect(self.db)) as conn:
            decision = json.loads(conn.execute('SELECT decision_json FROM transfer_audit').fetchone()[0])
        self.assertIn('members',decision)
        self.assertEqual(decision['members'], [
            {'txn_id':1,'account_id':'HSBC_UK','cents':-1000,'currency':'GBP','txn_date':'2026-01-31'},
            {'txn_id':2,'account_id':'IOM','cents':1000,'currency':'GBP','txn_date':'2026-02-02'}])

    def test_explicit_classification_config_fingerprint_is_checked_at_commit(self):
        api = self.api()
        import json
        root = Path(self.temp.name)
        (root/'config').mkdir()
        categories = root/'config/categories.json'
        categories.write_text(json.dumps({'Own Accounts':['In','Out']}))
        (root/'config/category_rules.csv').write_text('merchant_core,category,subcategory\n')
        self.assertIn('root',__import__('inspect').signature(api.prepare_pair).parameters,'classification fingerprint missing')
        preview = api.prepare_pair(1,2,db_path=self.db,root=root)
        categories.write_text(json.dumps({'Own Accounts':['In','Out','Changed']}))
        before = self.dump()
        with self.assertRaisesRegex(ValueError,'stale_preview'):
            api.commit_transfer(preview,approved=True)
        self.assertEqual(before,self.dump())

    def test_unmatched_rule_atomic_replacement_stales_preview(self):
        api = self.api()
        root = Path(self.temp.name)
        (root / 'config').mkdir()
        rules = root / 'config/category_rules.csv'
        rules.write_text('merchant_core,category,subcategory\n')
        self.assertIn('root', __import__('inspect').signature(api.prepare_unmatched).parameters)
        preview = api.prepare_unmatched(1, 'IOM', db_path=self.db, root=root)
        replacement = rules.with_suffix('.new')
        replacement.write_text('merchant_core,category,subcategory\nOWN,Own Accounts,Out\n')
        replacement.replace(rules)
        before = self.dump()
        with self.assertRaisesRegex(ValueError, 'stale_preview'):
            api.commit_transfer(preview, approved=True)
        self.assertEqual(before, self.dump())

    def test_revoke_new_merchant_config_stales_preview(self):
        api = self.api()
        root = Path(self.temp.name)
        result = api.commit_transfer(api.prepare_pair(1, 2, db_path=self.db, root=root), approved=True)
        self.assertIn('root', __import__('inspect').signature(api.prepare_revoke).parameters)
        preview = api.prepare_revoke(result.group_id, db_path=self.db, root=root)
        self.assertFalse((root / 'config').exists())
        (root / 'config').mkdir()
        (root / 'config/merchant_rules.json').write_text('{}')
        before = self.dump()
        with self.assertRaisesRegex(ValueError, 'stale_preview'):
            api.commit_transfer(preview, approved=True)
        self.assertEqual(before, self.dump())

    def test_disappeared_config_root_is_stale_and_rolls_back(self):
        api = self.api()
        root = Path(self.temp.name) / 'classification'
        root.mkdir()
        preview = api.prepare_pair(1, 2, db_path=self.db, root=root)
        root.rmdir()
        before = self.dump()
        with self.assertRaisesRegex(ValueError, 'stale_preview'):
            api.commit_transfer(preview, approved=True)
        self.assertEqual(before, self.dump())
        self.assertFalse(root.exists())

    def test_invalid_explicit_roots_are_rejected_without_setup(self):
        api = self.api()
        missing = Path(self.temp.name) / 'missing'
        before = self.dump()
        for root in (missing, self.db, True, 123, ''):
            with self.subTest(root=root), self.assertRaisesRegex(ValueError, 'invalid_root'):
                api.prepare_pair(1, 2, db_path=self.db, root=root)
        self.assertFalse(missing.exists())
        self.assertEqual(before, self.dump())

    def test_config_snapshot_is_signed_single_read_and_locked_at_commit(self):
        from dataclasses import replace
        from unittest.mock import patch
        import hashlib
        api = self.api()
        root = Path(self.temp.name)
        config = root / 'config'
        config.mkdir()
        contents = {'categories.json': b'{"Own Accounts":["In","Out"]}',
                    'category_rules.csv': b'merchant_core,category,subcategory\n',
                    'merchant_rules.json': b'{}'}
        for name, content in contents.items():
            (config / name).write_bytes(content)
        original_read = Path.read_bytes
        reads = []
        with closing(sqlite3.connect(self.db)) as conn:
            trace = []
            conn.set_trace_callback(trace.append)
            def read(path):
                data = original_read(path)
                if path.parent == config:
                    reads.append(path.name)
                    self.assertTrue(conn.in_transaction)
                return data
            with patch.object(Path, 'read_bytes', read):
                preview = api.prepare_pair(1, 2, conn=conn, root=root)
                self.assertCountEqual(reads, contents)
                self.assertEqual(len(reads), 3)
                self.assertEqual(dict(preview.file_versions),
                                 {name: hashlib.sha256(content).hexdigest() for name, content in contents.items()})
                before = self.dump()
                for changed in (replace(preview, root=str(root / 'other')),
                                replace(preview, file_versions=())):
                    with self.assertRaisesRegex(ValueError, 'invalid_preview'):
                        api.commit_transfer(changed, conn=conn, approved=True)
                self.assertEqual(before, self.dump())
                reads.clear()
                trace.clear()
                api.commit_transfer(preview, conn=conn, approved=True)
                self.assertEqual(trace[1], 'BEGIN IMMEDIATE')
                self.assertCountEqual(reads, contents)
                self.assertEqual(len(reads), 3)
        for name, content in contents.items():
            self.assertEqual((config / name).read_bytes(), content)

    def test_each_config_atomic_change_or_deletion_stales_without_ledger_writes(self):
        api = self.api()
        root = Path(self.temp.name)
        config = root / 'config'
        config.mkdir()
        for name in ('categories.json', 'category_rules.csv', 'merchant_rules.json'):
            for operation in ('replace', 'delete'):
                with self.subTest(name=name, operation=operation):
                    target = config / name
                    target.write_bytes(b'original')
                    preview = api.prepare_pair(1, 2, db_path=self.db, root=root)
                    if operation == 'replace':
                        replacement = target.with_suffix('.new')
                        replacement.write_bytes(b'changed')
                        replacement.replace(target)
                    else:
                        target.unlink()
                    before = self.dump()
                    with self.assertRaisesRegex(ValueError, 'stale_preview'):
                        api.commit_transfer(preview, approved=True)
                    self.assertEqual(before, self.dump())
                    if target.exists():
                        target.unlink()

    def test_missing_config_stays_absent_and_applied_token_replays_after_config_change(self):
        api = self.api()
        root = Path(self.temp.name)
        preview = api.prepare_pair(1, 2, db_path=self.db, root=root)
        self.assertEqual(dict(preview.file_versions), {name: 'missing' for name in
                         ('categories.json', 'category_rules.csv', 'merchant_rules.json')})
        original = self.rows()
        result = api.commit_transfer(preview, approved=True)
        self.assertFalse((root / 'config').exists())
        self.assertEqual(original, self.rows())
        (root / 'config').mkdir()
        (root / 'config/categories.json').write_text('{}')
        before = self.dump()
        replay = api.commit_transfer(preview, approved=True)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.group_id, result.group_id)
        self.assertEqual(before, self.dump())

    def test_migration_adds_empty_transfer_tables_without_changing_transactions(self):
        with closing(sqlite3.connect(self.db)) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({'transfer_groups','transfer_members','transfer_audit'} <= tables)
            for table in ('transfer_groups','transfer_members','transfer_audit'):
                self.assertEqual(conn.execute('SELECT * FROM '+table).fetchall(), [])
            self.assertEqual(conn.execute('SELECT user_note FROM transactions WHERE txn_id=1').fetchone(), ('keep',))
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
