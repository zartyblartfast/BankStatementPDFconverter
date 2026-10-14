"""Isolated ledgers only; injected extraction is a trusted Python test seam."""
from contextlib import closing
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.init_db import _SCHEMA_SQL
from scripts.hsbc_parser.models import Transaction
from scripts.services.extraction import ExtractionResult
from scripts.services.reconciliation import reconcile_transactions


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'config').mkdir()
        (self.root / 'StatementsPDF').mkdir()
        (self.root / 'config/categories.json').write_text(json.dumps({'Test': ['Valid']}))
        (self.root / 'config/category_rules.csv').write_text('merchant_core,category,subcategory\nSYNTHETIC,Test,Valid\n')
        self.pdf = self.root / 'StatementsPDF/fixture.pdf'
        self.pdf.write_bytes(b'synthetic PDF boundary')
        self.db = self.root / 'ledger.db'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(_SCHEMA_SQL)

    def extraction(self, rows=None, *, warnings=(), coverage=True, account='HSBC_UK', totals=True):
        rows = rows if rows is not None else [('2026-01-02', 'SYNTHETIC', -1, '1:2')]
        txns = [Transaction(str(self.pdf), account, d, text, amount, 'GBP', 1, ref)
                for d, text, amount, ref in rows]
        amounts = [Decimal(str(t.amount)) for t in txns]
        ins = sum((a for a in amounts if a > 0), Decimal(0))
        outs = -sum((a for a in amounts if a < 0), Decimal(0))
        evidence = {'payments_in': ins, 'payments_out': outs,
                    'opening_balance': Decimal(0), 'closing_balance': ins-outs} if totals else {}
        rec = reconcile_transactions(txns, evidence, warnings=warnings)
        return ExtractionResult(txns, list(warnings), set(), [], [], evidence, rec, 'test',
                                coverage={'start': '2026-01-01', 'end': '2026-01-31',
                                          'source_row_ref': '1:1'} if coverage else None)

    def migrate(self):
        from scripts.migrate_db import migrate_db
        migrate_db(self.db, migrations_dir=Path(__file__).resolve().parents[1] / 'scripts/migrations')
        # Empty scratch ledgers need explicitly registered synthetic accounts;
        # production imports never infer or create accounts from a selection.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executemany("INSERT OR IGNORE INTO accounts(account_id,display_name,currency,owned,parser_key,parser_verified,import_enabled) VALUES (?,?,'GBP',1,'hsbc_uk',1,1)",
                             [('HSBC_UK','Synthetic UK'), ('OTHER','Synthetic other UK-format')])

    def prepare(self, extraction=None, **kwargs):
        from scripts.services.ingestion import prepare_import
        kwargs.setdefault('account_id', 'HSBC_UK')
        return prepare_import(self.pdf, db_path=self.db, root=self.root,
                              extraction=extraction or self.extraction(), **kwargs)

    def snapshot(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return tuple(conn.iterdump())

    def commit(self, preview=None):
        from scripts.services.ingestion import commit_import
        return commit_import(preview or self.prepare(), approved=True)

    def test_preserves_legacy_edits_resolved_exceptions_audit_and_future_columns(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('ALTER TABLE transactions ADD COLUMN future_metadata TEXT')
            conn.execute("INSERT INTO categories VALUES (1,'Manual')")
            conn.execute("INSERT INTO subcategories VALUES (1,1,'Chosen')")
            conn.execute("INSERT INTO transactions(txn_id,txn_date,description_raw,amount,subcategory_id,source_file,row_ref,page,import_hash,excluded,exclude_reason,user_note,future_metadata) VALUES (42,'2026-01-02','SYNTHETIC',-1,1,'C:/old/StatementsPDF/fixture.pdf','1:2',1,'legacy-hash',1,'private','private-note','future')")
            conn.execute("INSERT INTO exceptions(txn_id,type,status) VALUES (42,'uncategorised','resolved')")
            conn.execute("INSERT INTO edit_log(txn_id,field_changed) VALUES (42,'subcategory_id')")
            conn.execute("INSERT INTO import_log(source_file,row_count) VALUES ('C:/old/StatementsPDF/fixture.pdf',1)")
        self.migrate()
        before = self.snapshot()
        result = self.commit()
        self.assertEqual((result.added, result.matched), (0, 1))
        with closing(sqlite3.connect(self.db)) as conn:
            row = conn.execute('SELECT txn_id,subcategory_id,excluded,exclude_reason,user_note,import_hash,future_metadata FROM transactions').fetchone()
            self.assertEqual(row, (42,1,1,'private','private-note','legacy-hash','future'))
            self.assertEqual(conn.execute('SELECT status FROM exceptions').fetchall(), [('resolved',)])
            self.assertEqual(conn.execute('SELECT txn_id FROM edit_log').fetchall(), [(42,)])
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertTrue(before)
        # Replacement adding a row updates both new and legacy audit counts.
        self.pdf.write_bytes(b'replacement')
        self.commit(self.prepare(self.extraction([('2026-01-02','SYNTHETIC',-1,'1:2'), ('2026-01-03','SYNTHETIC',-2,'1:3')])))
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT DISTINCT row_count FROM import_log').fetchall(), [(2,)])

    def test_replacement_adds_without_reinserting_and_rename_same_content_is_noop(self):
        self.migrate()
        self.commit()
        self.pdf.write_bytes(b'replacement full statement')
        full = self.extraction([('2026-01-02','SYNTHETIC',-1,'1:3'), ('2026-01-03','SYNTHETIC',-2,'1:4')])
        result = self.commit(self.prepare(full))
        self.assertEqual((result.added,result.matched,result.total_current), (1,1,2))
        from scripts.services.ingestion import prepare_import
        renamed = self.pdf.with_name('renamed.pdf')
        renamed.write_bytes(self.pdf.read_bytes())
        preview = prepare_import(renamed, db_path=self.db, root=self.root, account_id='HSBC_UK', extraction=full)
        self.assertEqual(preview.status, 'eligible')
        self.assertEqual(self.commit(preview).added, 0)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM source_documents').fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM source_document_contents').fetchone()[0],2)
            self.assertEqual(conn.execute('SELECT txn_id FROM transactions ORDER BY txn_id').fetchall(), [(1,), (2,)])

    def test_linked_basename_does_not_alias_distinct_nested_document(self):
        self.migrate()
        self.commit()
        self.pdf = self.root / 'StatementsPDF/nested/fixture.pdf'
        self.pdf.parent.mkdir()
        self.pdf.write_bytes(b'distinct nested statement')
        rows = [('2026-01-02','SYNTHETIC',-1,'1:2'),
                ('2026-01-03','SYNTHETIC',-2,'1:3')]
        preview = self.prepare(self.extraction(rows))
        self.assertIn('other_source_overlap', preview.reasons)
        self.assertIsNone(preview.document_id)
        self.assertEqual(preview.document_key, 'nested/fixture.pdf')
        self.assertEqual(preview.plan.matches, ())
        self.assertEqual(preview.plan.additions, (0, 1))
        self.assert_blocked(preview)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT alias_key FROM source_document_aliases').fetchall(),
                             [('fixture.pdf',)])

    def test_unlinked_legacy_basename_matches_after_root_move_to_nested_path(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO transactions(txn_id,txn_date,description_raw,amount,source_file,row_ref,page,import_hash) VALUES (42,'2026-01-02','SYNTHETIC',-1,'C:/old/fixture.pdf','1:2',1,'legacy')")
        self.migrate()
        self.pdf = self.root / 'StatementsPDF/nested/fixture.pdf'
        self.pdf.parent.mkdir()
        self.pdf.write_bytes(b'moved legacy statement')
        preview = self.prepare()
        self.assertEqual(preview.status, 'eligible')
        self.assertEqual(preview.document_key, 'nested/fixture.pdf')
        self.assertEqual(preview.plan.matches, ((0, 42),))
        self.assertEqual(self.commit(preview).added, 0)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT document_key FROM source_documents').fetchall(),
                             [('nested/fixture.pdf',)])

    def test_distinct_nested_canonical_identity_survives_replacement(self):
        from dataclasses import replace
        self.migrate()
        first = self.commit()
        self.pdf = self.root / 'StatementsPDF/nested/fixture.pdf'
        self.pdf.parent.mkdir()
        self.pdf.write_bytes(b'different February statement')
        extraction = replace(self.extraction([('2026-02-02','SYNTHETIC',-2,'1:2')]),
                             coverage={'start':'2026-02-01','end':'2026-02-28','source_row_ref':'1:1'})
        second = self.commit(self.prepare(extraction))
        self.assertNotEqual(first.document_id, second.document_id)
        self.pdf.write_bytes(b'replacement February statement')
        preview = self.prepare(extraction)
        self.assertEqual(preview.status, 'eligible')
        self.assertEqual(preview.document_id, second.document_id)
        self.assertEqual(preview.plan.matches, ((0, 2),))
        self.assertEqual(self.commit(preview).added, 0)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT document_key FROM source_documents ORDER BY document_id').fetchall(),
                             [('fixture.pdf',), ('nested/fixture.pdf',)])

    def test_nested_same_content_rename_matches_only_within_account(self):
        self.migrate()
        original = self.commit()
        content = self.pdf.read_bytes()
        self.pdf = self.root / 'StatementsPDF/nested/fixture.pdf'
        self.pdf.parent.mkdir()
        self.pdf.write_bytes(content)
        preview = self.prepare()
        self.assertEqual(preview.status, 'eligible')
        self.assertEqual(preview.document_id, original.document_id)
        self.assertEqual(self.commit(preview).added, 0)
        # A known document cannot be redirected to another account.
        other = self.prepare(self.extraction(account='OTHER'), account_id='OTHER')
        self.assertIsNone(other.document_id)
        self.assertEqual(other.plan.matches, ())
        self.assertIn('source_account_mismatch', other.reasons)
        self.assert_blocked(other)

    def test_duplicate_occurrences_and_unknown_rules_link_each_exception_correctly(self):
        (self.root / 'config/category_rules.csv').write_text('merchant_core,category,subcategory\nSYNTHETIC,Test,Unknown\n')
        self.migrate()
        pairs = self.extraction([('2026-01-02','SYNTHETIC',-1,'1:2'), ('2026-01-02','SYNTHETIC',-1,'1:3')])
        self.assertEqual(self.commit(self.prepare(pairs)).exceptions_created, 2)
        self.assertEqual(self.commit(self.prepare(pairs)).exceptions_created, 0)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT txn_id,type FROM exceptions ORDER BY txn_id').fetchall(), [(1,'unknown_subcategory'),(2,'unknown_subcategory')])
            self.assertEqual(conn.execute('SELECT subcategory_id FROM transactions').fetchall(), [(None,), (None,)])

    def test_account_scope_is_literal_and_distinct(self):
        self.migrate()
        self.commit()
        self.pdf = self.pdf.with_name('other-account.pdf')
        self.pdf.write_bytes(b'distinct synthetic account statement')
        preview = self.prepare(self.extraction(account='OTHER'), account_id='OTHER')
        self.assertEqual(self.commit(preview).added,1)
        with self.assertRaises(ValueError):
            self.prepare(account_id=' HSBC_UK')
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM source_documents').fetchone()[0],2)

    def test_shifted_indistinguishable_edited_duplicates_require_review(self):
        self.migrate()
        rows = [('2026-01-02','SYNTHETIC',-1,'1:2'), ('2026-01-02','SYNTHETIC',-1,'1:3')]
        self.commit(self.prepare(self.extraction(rows)))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE transactions SET user_note='one' WHERE txn_id=1")
        self.pdf.write_bytes(b'reordered duplicate extraction')
        shifted = self.prepare(self.extraction([('2026-01-02','SYNTHETIC',-1,'1:3'),('2026-01-02','SYNTHETIC',-1,'1:4')]))
        self.assertIn('ambiguous_occurrences',shifted.reasons)
        self.assertEqual(len(shifted.plan.ambiguous),2)
        self.assert_blocked(shifted)

    def import_uncategorised_duplicates(self):
        (self.root / 'config/category_rules.csv').write_text('merchant_core,category,subcategory\n')
        self.migrate()
        pairs = [('2026-01-02','SYNTHETIC',-1,'1:2'),
                 ('2026-01-02','SYNTHETIC',-1,'1:3')]
        self.commit(self.prepare(self.extraction(pairs)))
        self.pdf.write_bytes(b'shifted duplicate statement')
        return self.extraction([('2026-01-02','SYNTHETIC',-1,'1:3'),
                                ('2026-01-02','SYNTHETIC',-1,'1:4')])

    def test_shifted_duplicates_with_resolved_exception_and_audit_require_review(self):
        shifted = self.import_uncategorised_duplicates()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE exceptions SET status='resolved',resolved_at='2026-02-01' WHERE txn_id=1")
            conn.execute("INSERT INTO edit_log(txn_id,field_changed,old_value,new_value) VALUES (1,'subcategory_id','1','2')")
        preview = self.prepare(shifted)
        self.assertIn('ambiguous_occurrences', preview.reasons)
        self.assertEqual(preview.plan.matches, ())
        self.assertEqual(preview.plan.ambiguous, (0, 1))
        self.assertEqual(preview.plan.potential_removals, (1, 2))
        self.assert_blocked(preview)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT txn_id,row_ref FROM transactions ORDER BY txn_id').fetchall(),
                             [(1,'1:2'), (2,'1:3')])
            self.assertEqual(conn.execute('SELECT txn_id,status FROM exceptions ORDER BY txn_id').fetchall(),
                             [(1,'resolved'), (2,'open')])
            self.assertEqual(conn.execute('SELECT txn_id,old_value,new_value FROM edit_log').fetchall(),
                             [(1,'1','2')])

    def test_shifted_duplicates_with_transfer_only_difference_require_review(self):
        from scripts.services.transfers import prepare_unmatched, commit_transfer
        shifted = self.import_uncategorised_duplicates()
        commit_transfer(prepare_unmatched(1,'OTHER',db_path=self.db),approved=True)
        preview = self.prepare(shifted)
        self.assertIn('ambiguous_occurrences',preview.reasons)
        self.assertEqual(preview.plan.matches,())
        self.assert_blocked(preview)

    def test_equal_transfer_decisions_ignore_generated_ids_times_and_preserve_both_duplicates(self):
        from scripts.services.transfers import prepare_unmatched, commit_transfer
        shifted = self.import_uncategorised_duplicates()
        for txn_id in (1,2):
            commit_transfer(prepare_unmatched(txn_id,'OTHER',db_path=self.db),approved=True)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE transfer_members SET created_at='first' WHERE txn_id=1")
            conn.execute("UPDATE transfer_members SET created_at='second' WHERE txn_id=2")
            before = conn.execute('SELECT * FROM transfer_members ORDER BY member_id').fetchall()
            audits = conn.execute('SELECT * FROM transfer_audit ORDER BY audit_id').fetchall()
        preview = self.prepare(shifted)
        self.assertEqual(preview.status,'eligible')
        self.assertEqual(self.commit(preview).matched,2)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT * FROM transfer_members ORDER BY member_id').fetchall(),before)
            self.assertEqual(conn.execute('SELECT * FROM transfer_audit ORDER BY audit_id').fetchall(),audits)

    def test_revoked_transfer_audit_difference_still_blocks_shifted_duplicate_swapping(self):
        from scripts.services.transfers import prepare_unmatched, prepare_revoke, commit_transfer
        shifted = self.import_uncategorised_duplicates()
        result = commit_transfer(prepare_unmatched(1,'OTHER',db_path=self.db),approved=True)
        commit_transfer(prepare_revoke(result.group_id,db_path=self.db),approved=True)
        preview = self.prepare(shifted)
        self.assertIn('ambiguous_occurrences',preview.reasons)
        self.assert_blocked(preview)

    def test_transfer_timestamp_changes_stale_existing_import_preview(self):
        from scripts.services.transfers import prepare_unmatched, commit_transfer
        self.migrate()
        self.commit()
        commit_transfer(prepare_unmatched(1,'OTHER',db_path=self.db),approved=True)
        preview = self.prepare()
        with closing(sqlite3.connect(self.db)) as conn,conn:
            conn.execute("UPDATE transfer_members SET created_at='changed' WHERE txn_id=1")
        self.assert_blocked(preview,'stale_preview')

    def test_real_pdf_reimport_retains_transfer_membership_audit_and_manual_metadata(self):
        from tests.test_extraction import synthetic_pdf, LINES
        from scripts.services.ingestion import prepare_import
        from scripts.services.transfers import prepare_pair, commit_transfer
        self.pdf.write_bytes(synthetic_pdf([(1,'Statement period: 01 Jan 2026 to 31 Jan 2026')]+LINES))
        self.migrate()
        prepare = lambda: prepare_import(self.pdf,db_path=self.db,root=self.root,account_id='HSBC_UK')
        first = self.commit(prepare())
        self.assertEqual(first.added,2)
        with closing(sqlite3.connect(self.db)) as conn,conn:
            conn.execute("UPDATE transactions SET user_note='manual',excluded=1 WHERE txn_id=1")
            conn.execute("INSERT INTO edit_log(txn_id,field_changed,new_value) VALUES (1,'user_note','manual')")
            conn.execute("INSERT INTO transactions(txn_date,description_raw,amount,account_id,import_hash) VALUES ('2026-02-02','synthetic counter leg',-100,'OTHER','counter-leg')")
        commit_transfer(prepare_pair(1,3,db_path=self.db),approved=True)
        with closing(sqlite3.connect(self.db)) as conn:
            originals = {table:conn.execute('SELECT * FROM '+table+' ORDER BY 1').fetchall()
                         for table in ('transactions','edit_log','transfer_groups','transfer_members','transfer_audit','transaction_sources')}
        preview = prepare()
        self.assertEqual(preview.status,'eligible')
        result = self.commit(preview)
        self.assertEqual((result.added,result.matched),(0,2))
        with closing(sqlite3.connect(self.db)) as conn:
            for table, rows in originals.items():
                self.assertEqual(conn.execute('SELECT * FROM '+table+' ORDER BY 1').fetchall(),rows,table)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])

    def test_shifted_duplicates_with_audit_only_difference_require_review(self):
        shifted = self.import_uncategorised_duplicates()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO edit_log(txn_id,field_changed,old_category,new_category) VALUES (1,'subcategory_id','Test / Before','Test / After')")
        preview = self.prepare(shifted)
        self.assertIn('ambiguous_occurrences', preview.reasons)
        self.assertEqual(preview.plan.matches, ())
        self.assert_blocked(preview)

    def test_shifted_duplicates_with_exception_detail_difference_require_review(self):
        shifted = self.import_uncategorised_duplicates()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE exceptions SET detail=? WHERE txn_id=1',
                         (json.dumps({'channel': None, 'target': 'Manual / Chosen'}),))
        preview = self.prepare(shifted)
        self.assertIn('ambiguous_occurrences', preview.reasons)
        self.assertEqual(preview.plan.matches, ())
        self.assert_blocked(preview)

    def test_shifted_duplicates_with_resolved_exception_only_require_review(self):
        shifted = self.import_uncategorised_duplicates()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE exceptions SET status='resolved' WHERE txn_id=1")
        preview = self.prepare(shifted)
        self.assertIn('ambiguous_occurrences', preview.reasons)
        self.assertEqual(preview.plan.matches, ())
        self.assert_blocked(preview)

    def test_unedited_duplicates_ignore_exception_ids_times_and_json_formatting(self):
        shifted = self.import_uncategorised_duplicates()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE exceptions SET created_at=?,detail=? WHERE txn_id=1',
                         ('2026-01-01', '{"channel":null,"context":"equal"}'))
            conn.execute('UPDATE exceptions SET created_at=?,detail=? WHERE txn_id=2',
                         ('2026-01-02', '{ "context": "equal", "channel": null }'))
        preview = self.prepare(shifted)
        self.assertEqual(preview.status, 'eligible')
        self.assertEqual(preview.plan.ambiguous, ())
        self.assertEqual(self.commit(preview).total_current, 2)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT txn_id FROM transactions ORDER BY txn_id').fetchall(), [(1,), (2,)])
            self.assertEqual(conn.execute('SELECT txn_id,status FROM exceptions ORDER BY txn_id').fetchall(),
                             [(1,'open'), (2,'open')])

    def test_equal_related_decisions_allow_duplicate_order_changes_preserving_ids(self):
        from dataclasses import replace
        shifted = self.import_uncategorised_duplicates()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            for txn_id in (1, 2):
                conn.execute("UPDATE exceptions SET status='resolved',resolved_at=? WHERE txn_id=?",
                             (f'2026-02-0{txn_id}', txn_id))
                conn.execute("INSERT INTO edit_log(txn_id,field_changed,old_value,new_value,changed_at) VALUES (?,'subcategory_id','1','2',?)",
                             (txn_id, f'2026-02-0{txn_id}'))
            relations_before = (conn.execute('SELECT * FROM exceptions ORDER BY exception_id').fetchall(),
                                conn.execute('SELECT * FROM edit_log ORDER BY edit_id').fetchall())
        reordered = replace(shifted, transactions=list(reversed(shifted.transactions)))
        preview = self.prepare(reordered)
        self.assertEqual(preview.status, 'eligible')
        self.assertEqual(preview.plan.ambiguous, ())
        result = self.commit(preview)
        self.assertEqual((result.added, result.matched, result.total_current), (0, 2, 2))
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT txn_id FROM transactions ORDER BY txn_id').fetchall(), [(1,), (2,)])
            self.assertEqual(relations_before,
                             (conn.execute('SELECT * FROM exceptions ORDER BY exception_id').fetchall(),
                              conn.execute('SELECT * FROM edit_log ORDER BY edit_id').fetchall()))

    def test_related_state_changes_invalidate_signed_preview_before_writes(self):
        self.import_uncategorised_duplicates()
        original_refs = self.extraction([('2026-01-02','SYNTHETIC',-1,'1:2'),
                                         ('2026-01-02','SYNTHETIC',-1,'1:3')])
        for sql in ("UPDATE exceptions SET status='resolved' WHERE txn_id=1",
                    "INSERT INTO edit_log(txn_id,field_changed,new_value) VALUES (1,'subcategory_id','2')",
                    "UPDATE exceptions SET created_at='different time' WHERE txn_id=2"):
            with self.subTest(sql=sql):
                preview = self.prepare(original_refs)
                self.assertEqual(preview.status, 'eligible')
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute(sql)
                self.assert_blocked(preview, 'stale_preview')

    def assert_blocked(self, preview, code='blocked'):
        from scripts.services.ingestion import ImportServiceError
        before = self.snapshot()
        with self.assertRaises(ImportServiceError) as error:
            self.commit(preview)
        self.assertEqual(error.exception.code,code)
        self.assertEqual(before,self.snapshot())

    def test_removals_and_changed_financial_fields_block_not_overwrite(self):
        self.migrate()
        self.commit()
        self.pdf.write_bytes(b'changed')
        changed = self.prepare(self.extraction([('2026-01-02','CHANGED',-2,'1:2')]))
        self.assertIn('potential_removals',changed.reasons)
        self.assertIn('changed_source_candidates',changed.reasons)
        self.assert_blocked(changed)

    def test_real_pdf_reader_prepare_commit_and_no_artifacts(self):
        from tests.test_extraction import synthetic_pdf, LINES
        from scripts.services.ingestion import prepare_import, preview_summary
        self.pdf.write_bytes(synthetic_pdf([(1,'Statement period: 01 Jan 2026 to 31 Jan 2026')]+LINES))
        self.migrate()
        before = self.snapshot()
        files_before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        preview = prepare_import('StatementsPDF/fixture.pdf', db_path=self.db, root=self.root, account_id='HSBC_UK')
        self.assertEqual(preview.status,'eligible')
        self.assertEqual(preview_summary(preview)['coverage_status'],'evidenced')
        self.assertEqual(before,self.snapshot())
        self.assertEqual(files_before,{str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        result = self.commit(preview)
        self.assertEqual(result.added,2)
        self.assertFalse((self.root/'finance').exists())
        self.assertEqual(self.pdf.read_bytes(),files_before['StatementsPDF\\fixture.pdf'] if 'StatementsPDF\\fixture.pdf' in files_before else files_before['StatementsPDF/fixture.pdf'])
        safe = json.dumps(preview_summary(preview))
        self.assertNotIn('SYNTHETIC CREDIT',safe)
        self.assertNotIn('SYNTHETIC SHOP',safe)

    def test_extraction_mutation_after_prepare_cannot_change_approved_rows(self):
        self.migrate()
        extraction = self.extraction()
        preview = self.prepare(extraction)
        extraction.transactions.clear()
        extraction.statement_totals.clear()
        extraction.coverage.clear()
        self.assertEqual(self.commit(preview).added,1)

    def test_missing_rules_create_one_exception_per_new_occurrence(self):
        (self.root/'config/category_rules.csv').write_text('merchant_core,category,subcategory\n')
        self.migrate()
        result = self.commit()
        self.assertEqual(result.exceptions_created,1)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT txn_id,type FROM exceptions').fetchall(),[(1,'uncategorised')])

    def test_historical_file_hash_identity_rename_conflict_blocks(self):
        self.migrate()
        self.commit()
        original = self.pdf.read_bytes()
        self.pdf = self.pdf.with_name('second.pdf')
        self.pdf.write_bytes(b'other content')
        # Distinct evidenced period and different rows form a second document.
        from dataclasses import replace
        extraction = self.extraction([('2026-02-02','DIFFERENT',-2,'1:2')])
        extraction = replace(extraction,coverage={'start':'2026-02-01','end':'2026-02-28','source_row_ref':'1:1'})
        self.commit(self.prepare(extraction))
        self.pdf.write_bytes(original)
        preview = self.prepare()
        self.assertIn('ambiguous_document_identity',preview.reasons)
        self.assert_blocked(preview)

    def test_non_string_signature_is_invalid_preview_error(self):
        from dataclasses import replace
        self.migrate()
        self.assert_blocked(replace(self.prepare(),signature=None),'invalid_preview')

    def test_missing_coverage_other_statement_date_overlap_requires_review(self):
        self.migrate()
        self.commit(self.prepare(self.extraction(coverage=False)))
        self.pdf = self.pdf.with_name('new-source.pdf')
        self.pdf.write_bytes(b'another statement with missing period')
        preview = self.prepare(self.extraction([('2026-01-02','DIFFERENT',-8,'1:2')],coverage=False))
        self.assertIn('missing_coverage_date_overlap',preview.reasons)
        self.assert_blocked(preview)

    def test_cross_source_coverage_and_missing_coverage_duplicates_block(self):
        self.migrate()
        self.commit()
        self.pdf = self.pdf.with_name('other.pdf')
        self.pdf.write_bytes(b'different statement')
        overlap = self.prepare(self.extraction([('2026-01-05','DIFFERENT',-3,'1:2')]))
        self.assertIn('other_source_overlap',overlap.reasons)
        self.assert_blocked(overlap)
        missing = self.prepare(self.extraction(coverage=False))
        self.assertIn('missing_coverage_row_overlap',missing.reasons)
        self.assert_blocked(missing)

    def test_unavailable_review_and_failed_reconciliation_block(self):
        from dataclasses import replace
        self.migrate()
        for extraction in (self.extraction(totals=False), self.extraction(warnings=('synthetic warning',)),
                           replace(self.extraction(),statement_totals={'payments_in':0,'payments_out':99,'opening_balance':0,'closing_balance':-99})):
            with self.subTest(status=extraction.reconciliation.status):
                self.assert_blocked(self.prepare(extraction))

    def test_tampered_preview_and_json_extraction_rejected(self):
        from dataclasses import replace
        self.migrate()
        preview = self.prepare()
        self.assert_blocked(replace(preview,account_id='OTHER'), 'invalid_preview')
        self.assert_blocked(replace(preview,rows=()),'invalid_preview')
        with self.assertRaises(TypeError):
            self.prepare({'transactions': []})

    def test_stale_pdf_config_and_ledger_reject_before_writes(self):
        self.migrate()
        for mode in ('pdf','rules','categories','ledger','future_table'):
            preview = self.prepare()
            if mode == 'pdf':
                self.pdf.write_bytes(self.pdf.read_bytes()+b'x')
            elif mode in ('rules','categories'):
                path = self.root / ('config/category_rules.csv' if mode == 'rules' else 'config/categories.json')
                path.write_text(path.read_text()+'\n')
            else:
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    if mode == 'ledger':
                        conn.execute("INSERT INTO categories(name) VALUES ('changed')")
                    else:
                        conn.execute('CREATE TABLE future_table(data TEXT)')
            self.assert_blocked(preview,'stale_preview')

    def test_concurrent_previews_one_commit_other_stale(self):
        from concurrent.futures import ThreadPoolExecutor
        from scripts.services.ingestion import ImportServiceError
        self.migrate()
        previews = [self.prepare(),self.prepare()]
        def attempt(preview):
            try:
                return self.commit(preview).added
            except ImportServiceError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt,previews))
        self.assertCountEqual(results,[1,'stale_preview'])

    def test_sql_failure_after_writes_rolls_back_every_table(self):
        self.migrate()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("CREATE TRIGGER injected_failure BEFORE INSERT ON import_batches BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
        preview = self.prepare()
        before = self.snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            self.commit(preview)
        self.assertEqual(before,self.snapshot())

    def test_commit_never_implicitly_migrates(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO transactions(txn_date,description_raw,amount,source_file,import_hash) VALUES ('2026-02-02','OBSERVED',-2,'old.pdf','observed')")
        self.assert_blocked(self.prepare(),'schema_not_ready')

    def test_nonfinite_invalid_dates_and_account_mismatch_rejected(self):
        from dataclasses import replace
        self.migrate()
        valid = self.extraction()
        invalid = replace(valid, transactions=[replace(valid.transactions[0], amount=float('nan'))])
        with self.assertRaises(ValueError):
            self.prepare(invalid)
        with self.assertRaises(ValueError):
            self.prepare(self.extraction([('2026-1-02','SYNTHETIC',-1,'1:2')],totals=False))
        with self.assertRaises(ValueError):
            self.prepare(self.extraction(account='OTHER'))

    def test_explicit_atomic_commit_and_replay_preserve_ids(self):
        from scripts.services.ingestion import commit_import, ImportServiceError
        self.migrate()
        preview = self.prepare()
        before = self.snapshot()
        with self.assertRaises(ImportServiceError) as error:
            commit_import(preview)
        self.assertEqual(error.exception.code, 'approval_required')
        self.assertEqual(before, self.snapshot())
        result = commit_import(preview, approved=True)
        self.assertEqual((result.added, result.matched, result.total_current), (1, 0, 1))
        after = self.snapshot()
        repeated = commit_import(preview, approved=True)
        self.assertTrue(repeated.replayed)
        self.assertEqual(after, self.snapshot())
        duplicate = commit_import(self.prepare(), approved=True)
        self.assertEqual((duplicate.added, duplicate.matched), (0, 1))
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(conn.execute('SELECT row_count FROM import_log').fetchone()[0], 1)

    def test_additive_migration_preserves_legacy_and_enables_preview(self):
        before = self.snapshot()
        self.migrate()
        self.assertTrue(self.prepare().schema_ready)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 0)
        self.assertTrue(any('CREATE TABLE transactions' in line for line in before))
        from scripts.migrate_db import migrate_db
        self.assertEqual(migrate_db(self.db, migrations_dir=Path(__file__).resolve().parents[1] / 'scripts/migrations'), [])

    def test_legacy_preview_readonly_and_absent_db_not_created(self):
        from scripts.services.ingestion import prepare_import, preview_summary, ImportServiceError
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO transactions(txn_date,description_raw,amount,source_file,import_hash) VALUES ('2026-02-02','OBSERVED',-2,'old.pdf','observed')")
        before = self.snapshot()
        db_bytes_before = self.db.read_bytes()
        preview = self.prepare()
        self.assertEqual(self.db.read_bytes(),db_bytes_before)
        self.assertEqual(preview_summary(preview)['counts']['additions'], 1)
        self.assertFalse(preview.schema_ready)
        self.assertEqual(before, self.snapshot())
        missing = self.root / 'absent/db.sqlite'
        with self.assertRaises(ImportServiceError):
            prepare_import(self.pdf, db_path=missing, root=self.root, account_id='HSBC_UK', extraction=self.extraction())
        self.assertFalse(missing.parent.exists())


if __name__ == '__main__':
    unittest.main()
