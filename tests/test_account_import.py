"""Account integration uses only scratch ledgers and synthetic UK parser fixtures."""
from contextlib import closing
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from scripts import dashboard as d
from tests.test_dashboard_security import DashboardFixture
from tests import test_ingestion_service as ingestion_tests


class AccountImportUI(DashboardFixture):
    def setUp(self):
        super().setUp()
        self.paths.statements_dir.mkdir()
        self.pdf = self.paths.statements_dir / 'fixture.pdf'
        self.pdf.write_bytes(b'synthetic fixture')
        self.relative = str(self.pdf.relative_to(self.root))

    def folder(self, account, folder):
        from scripts.services.statement_folders import save_mapping
        save_mapping(account,folder, db_path=self.paths.db_path, root=self.paths.statements_dir)

    def test_preview_and_commit_show_frozen_account_despite_override(self):
        from scripts.migrate_db import migrate_db
        from tests.test_extraction import synthetic_pdf, LINES
        migrate_db(self.paths.db_path, migrations_dir=Path(__file__).resolve().parents[1] / 'scripts/migrations')
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute('DELETE FROM transactions')
            conn.execute("INSERT INTO accounts(account_id,display_name,currency,owned,parser_key,parser_verified,import_enabled) VALUES ('SECOND','Synthetic second UK-format','GBP',1,'hsbc_uk',1,1)")
        self.folder('SECOND','.')
        self.pdf.write_bytes(synthetic_pdf([(1,'Statement period: 01 Jan 2026 to 31 Jan 2026')]+LINES))
        page = self.client.get('/ingest', base_url=self.base).get_data(as_text=True)
        self.assertNotIn('value="HSBC_UK" selected', page)
        self.assertNotIn('value="SECOND" selected', page)
        response = self.client.post('/ingest', base_url=self.base, data={
            'csrf_token': self.token(), 'pdf': self.relative, 'account_id':'SECOND'})
        html = response.get_data(as_text=True)
        self.assertIn('Account scope:', html)
        self.assertIn('Synthetic second UK-format (SECOND)', html)
        import re
        token = re.search(r'name="import_token" value="([^"]+)"', html).group(1)
        response = self.client.post('/ingest', base_url=self.base, data={
            'csrf_token':self.token(), 'action':'commit', 'import_token':token,
            'approve':'yes', 'account_id':'HSBC_UK', 'pdf':'other.pdf'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Synthetic second UK-format (SECOND)', response.get_data(as_text=True))
        with closing(sqlite3.connect(self.paths.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT DISTINCT account_id FROM transactions').fetchall(), [('SECOND',)])

    def test_status_counts_are_selected_account_scoped_including_aliases(self):
        from scripts.migrate_db import migrate_db
        migrate_db(self.paths.db_path, migrations_dir=Path(__file__).resolve().parents[1] / 'scripts/migrations')
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute("INSERT INTO accounts(account_id,display_name,currency,owned,parser_key,parser_verified,import_enabled) VALUES ('SECOND','Synthetic second UK-format','GBP',1,'hsbc_uk',1,1)")
            doc = conn.execute("INSERT INTO source_documents(account_id,document_key,source_identity,content_hash,reconciliation_status) VALUES ('HSBC_UK','canonical.pdf','fixture-doc','hash','passed')").lastrowid
            conn.execute("INSERT INTO source_document_aliases(account_id,alias_key,document_id) VALUES ('HSBC_UK','fixture.pdf',?)", (doc,))
            conn.execute("INSERT INTO transaction_sources(txn_id,document_id,row_ref,occurrence) VALUES (1,?,'1',0)",(doc,))
            conn.execute("UPDATE transactions SET source_file='stale-name.pdf'")
        other_pdf = self.paths.statements_dir / 'other' / 'fixture.pdf'
        other_pdf.parent.mkdir()
        other_pdf.write_bytes(b'synthetic other')
        self.folder('HSBC_UK','.')
        self.folder('SECOND','other')
        selected = self.client.get('/ingest?account_id=HSBC_UK', base_url=self.base).get_data(as_text=True)
        self.assertIn('Imported (1 txns)', selected)
        other = self.client.get('/ingest?account_id=SECOND', base_url=self.base).get_data(as_text=True)
        self.assertNotIn('Imported (1 txns)', other)
        self.assertIn('Not imported', other)
        unselected = self.client.get('/ingest', base_url=self.base).get_data(as_text=True)
        self.assertIn('Select an available, verified account', unselected)
        self.assertNotIn('name="pdf"', unselected)

    def test_nested_registry_identities_do_not_mark_same_basename_root_imported(self):
        from scripts.migrate_db import migrate_db
        import re
        migrate_db(self.paths.db_path, migrations_dir=Path(__file__).resolve().parents[1] / 'scripts/migrations')
        (self.paths.statements_dir / 'root-alias.pdf').write_bytes(b'synthetic alias')
        nested = self.paths.statements_dir / 'nested' / 'fixture.pdf'
        nested.parent.mkdir()
        nested.write_bytes(b'distinct synthetic nested document')

        def statuses(account):
            response = self.client.get('/ingest?account_id=' + account, base_url=self.base)
            self.assertEqual(response.status_code, 200)
            return {
                Path(path).relative_to('StatementsPDF').as_posix(): (int(count) if count else 0)
                for path, count in re.findall(
                    r'<tr>\s*<td><input type="radio" name="pdf" value="([^"]+)".*?'
                    r'(?:Imported \((\d+) txns\)|Not imported).*?</tr>',
                    response.get_data(as_text=True), re.S)
            }

        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute("INSERT INTO accounts(account_id,display_name,currency,owned,parser_key,parser_verified,import_enabled) VALUES ('SECOND','Synthetic second UK-format','GBP',1,'hsbc_uk',1,1)")
            # Linked stale basenames must never act as historical fallback.
            conn.execute("UPDATE transactions SET source_file='C:/old/fixture.pdf'")
            for txn_id, account in ((2, 'HSBC_UK'), (3, 'HSBC_UK'), (4, 'SECOND')):
                conn.execute("INSERT INTO transactions(txn_id,txn_date,description_raw,amount,source_file,account_id,import_hash) VALUES (?,'2026-01-01','synthetic',-1,'fixture.pdf',?,?)", (txn_id, account, 'fixture-' + str(txn_id)))

        other_pdf = self.paths.statements_dir / 'other' / 'fixture.pdf'
        other_pdf.parent.mkdir()
        other_pdf.write_bytes(b'synthetic other')
        self.folder('HSBC_UK','.')
        self.folder('SECOND','other')
        for key, nested_alias in (('nested/fixture.pdf', 'deeper/fixture.pdf'),
                                  ('canonical.pdf', 'nested/fixture.pdf')):
            with self.subTest(canonical=key, alias=nested_alias):
                with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
                    conn.execute('DELETE FROM transaction_sources')
                    conn.execute('DELETE FROM source_document_aliases')
                    conn.execute('DELETE FROM source_documents')
                    nested_doc = conn.execute("INSERT INTO source_documents(account_id,document_key,source_identity,content_hash,reconciliation_status) VALUES ('HSBC_UK',?,'nested-doc','nested-hash','passed')", (key,)).lastrowid
                    conn.executemany("INSERT INTO source_document_aliases(account_id,alias_key,document_id) VALUES ('HSBC_UK',?,?)", [(nested_alias, nested_doc), ('root-alias.pdf', nested_doc), (key, nested_doc)])
                    root_doc = conn.execute("INSERT INTO source_documents(account_id,document_key,source_identity,content_hash,reconciliation_status) VALUES ('HSBC_UK','fixture.pdf','root-doc','root-hash','passed')").lastrowid
                    other_doc = conn.execute("INSERT INTO source_documents(account_id,document_key,source_identity,content_hash,reconciliation_status) VALUES ('SECOND','other/fixture.pdf','other-doc','other-hash','passed')").lastrowid
                    pending_doc = conn.execute("INSERT INTO source_documents(account_id,document_key,source_identity,content_hash,reconciliation_status) VALUES ('HSBC_UK','pending.pdf','pending-doc','pending-hash','passed')").lastrowid
                    # Link UK root rows elsewhere initially so legacy fallback is excluded.
                    conn.executemany("INSERT INTO transaction_sources(txn_id,document_id,row_ref,occurrence) VALUES (?,?,'1',?)", [(1, nested_doc, 0), (2, pending_doc, 1), (3, pending_doc, 2), (4, other_doc, 3)])
                before = self.paths.db_path.read_bytes()
                self.assertEqual(statuses('HSBC_UK'), {'fixture.pdf': 0, 'root-alias.pdf': 1, 'nested/fixture.pdf': 1})
                self.assertEqual(statuses('SECOND'), {'other/fixture.pdf': 1})
                self.assertEqual(self.paths.db_path.read_bytes(), before)
                with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
                    conn.execute('UPDATE transaction_sources SET document_id=? WHERE txn_id IN (2,3)', (root_doc,))
                    conn.execute("INSERT INTO source_document_aliases(account_id,alias_key,document_id) VALUES ('HSBC_UK','fixture.pdf',?)", (root_doc,))
                self.assertEqual(statuses('HSBC_UK'), {'fixture.pdf': 2, 'root-alias.pdf': 1, 'nested/fixture.pdf': 1})
                self.assertEqual(statuses('SECOND'), {'other/fixture.pdf': 1})

    def test_browser_requires_explicit_account_before_extraction(self):
        with patch.object(d, 'prepare_import', side_effect=ValueError('should not parse')) as prepare:
            response = self.client.post('/ingest', base_url=self.base,
                data={'csrf_token': self.token(), 'pdf': self.relative})
        self.assertEqual(response.status_code, 400)
        prepare.assert_not_called()
        html = self.client.get('/ingest', base_url=self.base).get_data(as_text=True)
        self.assertIn('Import Statements', html)
        self.assertIn('name="account_id"', html)
        self.assertIn('HSBC UK', html)
        self.assertIn('setup pending', html.lower())
        self.assertIn('approved migration', html)
        self.assertIn('IoM parser verification', html)


class AccountImportService(unittest.TestCase):
    root: Path
    db: Path
    pdf: Path
    setUp = ingestion_tests.IngestionTests.setUp
    extraction = ingestion_tests.IngestionTests.extraction
    prepare = ingestion_tests.IngestionTests.prepare
    snapshot = ingestion_tests.IngestionTests.snapshot
    commit = ingestion_tests.IngestionTests.commit

    def migrate(self):
        from scripts.migrate_db import migrate_db
        migrate_db(self.db, migrations_dir=Path(__file__).resolve().parents[1] / 'scripts/migrations')
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT OR IGNORE INTO accounts(account_id,display_name,currency,institution,owned,parser_key,parser_verified,import_enabled) VALUES ('HSBC_UK','Synthetic UK','GBP','Synthetic',1,'hsbc_uk',1,1)")

    def test_service_rejects_unavailable_accounts_before_extraction(self):
        from scripts.services.ingestion import prepare_import, ImportServiceError
        self.migrate()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO accounts(account_id,display_name,currency,owned,parser_key,parser_verified,import_enabled) VALUES ('SECOND','Synthetic unverified',NULL,1,NULL,0,0)")
        before = self.snapshot()
        with patch('scripts.services.ingestion.extract_statement') as extract:
            for account in ('UNKNOWN', 'SECOND', 'hsbc_uk', 'HSBC_UK ', '', None):
                with self.subTest(account=account):
                    with self.assertRaises(ImportServiceError):
                        prepare_import(self.pdf, db_path=self.db, root=self.root, account_id=account)
            extract.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def test_missing_and_disabled_policy_cannot_use_trusted_extraction(self):
        from scripts.services.ingestion import prepare_import, ImportServiceError
        self.migrate()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executemany("INSERT INTO accounts(account_id,display_name,currency,owned,parser_key,parser_verified,import_enabled) VALUES (?,?,?,1,?, ?,0)", [
                ('DISABLED','Synthetic disabled','GBP','hsbc_uk',1),
                ('USD','Synthetic USD','USD',None,0),
                ('UNVERIFIED','Synthetic unverified','GBP','hsbc_uk',0)])
        with patch('scripts.services.ingestion.extract_statement') as extract:
            for account in (None, 'DISABLED', 'USD', 'UNVERIFIED'):
                with self.subTest(account=account), self.assertRaises(ImportServiceError):
                    prepare_import(self.pdf, db_path=self.db, root=self.root, account_id=account)
                with self.subTest(trusted_account=account), self.assertRaises(ImportServiceError):
                    prepare_import(self.pdf, db_path=self.db, root=self.root,
                                   account_id=account, extraction=self.extraction())
            extract.assert_not_called()
        with self.assertRaises(ImportServiceError):
            prepare_import(self.pdf, db_path=self.db, root=self.root, extraction=self.extraction())

    def test_preview_summary_freezes_account_identity(self):
        from scripts.services.ingestion import preview_summary
        self.migrate()
        preview = self.prepare(account_id='HSBC_UK')
        summary = preview_summary(preview)
        self.assertEqual(summary['account_id'], 'HSBC_UK')
        self.assertEqual(summary['account_display_name'], 'Synthetic UK')

    def register_second(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO accounts(account_id,display_name,currency,owned,parser_key,parser_verified,import_enabled) VALUES ('SECOND','Synthetic second UK-format','GBP',1,'hsbc_uk',1,1)")

    def test_known_document_wrong_account_is_blocked(self):
        self.migrate()
        self.register_second()
        self.commit(self.prepare(account_id='HSBC_UK'))
        before = self.snapshot()
        preview = self.prepare(self.extraction(account='SECOND'), account_id='SECOND')
        self.assertEqual(preview.status, 'blocked')
        self.assertIn('source_account_mismatch', preview.reasons)
        from scripts.services.ingestion import commit_import, ImportServiceError
        with self.assertRaises(ImportServiceError):
            commit_import(preview, approved=True)
        self.assertEqual(before, self.snapshot())
        # Account-scoped aliases and content renames must also protect identity.
        self.pdf = self.pdf.with_name('renamed.pdf')
        self.pdf.write_bytes(b'synthetic PDF boundary')
        preview = self.prepare(self.extraction(account='SECOND'), account_id='SECOND')
        self.assertIn('source_account_mismatch', preview.reasons)

    def test_selected_account_path_cannot_mask_other_account_real_pdf_content(self):
        from scripts.services.ingestion import prepare_import, preview_summary, commit_import, ImportServiceError
        from tests.test_extraction import synthetic_pdf, LINES
        self.migrate()
        self.register_second()
        own = self.pdf
        own.write_bytes(synthetic_pdf([(1, 'Statement period: 01 Jan 2026 to 31 Jan 2026')] + LINES))
        other = own.with_name('other.pdf')
        other_lines = [(page, line.replace('Payments out 30.00', 'Payments out 35.00')
                       .replace('Closing balance 70.00', 'Closing balance 65.00'))
                       for page, line in LINES]
        other.write_bytes(synthetic_pdf([(1, 'Statement period: 01 Jan 2026 to 31 Jan 2026')]
                                       + other_lines + [(1, '03 Jan 26 VIS SYNTHETIC OTHER 5.00')]))
        for pdf, account, count in ((own, 'HSBC_UK', 2), (other, 'SECOND', 3)):
            preview = prepare_import(pdf, db_path=self.db, root=self.root, account_id=account)
            self.assertEqual(preview.status, 'eligible', preview.reasons)
            self.assertEqual(self.commit(preview).added, count)
        own.write_bytes(other.read_bytes())
        before = self.snapshot()
        preview = prepare_import(own, db_path=self.db, root=self.root, account_id='HSBC_UK')
        summary = preview_summary(preview)
        self.assertEqual(summary['reconciliation']['status'], 'passed')
        self.assertEqual(summary['counts']['extracted'], 3)
        self.assertEqual(summary['counts']['additions'], 1)
        with self.subTest(boundary='preview'):
            self.assertEqual(summary['status'], 'blocked')
            self.assertIn('source_account_mismatch', summary['reasons'])
        with self.subTest(boundary='commit'):
            with self.assertRaises(ImportServiceError) as error:
                commit_import(preview, approved=True)
            self.assertEqual(error.exception.code, 'blocked')
        self.assertEqual(before, self.snapshot())

    def test_conflicting_alias_legacy_and_shared_content_require_review(self):
        from scripts.services.ingestion import prepare_import, commit_import, ImportServiceError
        from tests.test_extraction import synthetic_pdf, LINES
        self.migrate()
        self.register_second()
        self.pdf.write_bytes(synthetic_pdf([(1, 'Statement period: 01 Jan 2026 to 31 Jan 2026')] + LINES))
        own_preview = prepare_import(self.pdf, db_path=self.db, root=self.root, account_id='HSBC_UK')
        own = self.commit(own_preview)
        # Each independent other-account identity must veto a selected-account
        # path/content match, including bytes historically registered to both.
        for evidence in ('canonical', 'alias', 'legacy', 'shared_content'):
            with self.subTest(evidence=evidence):
                legacy = None
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    doc = conn.execute(
                        "INSERT INTO source_documents(account_id,document_key,source_identity,content_hash,reconciliation_status) VALUES ('SECOND',?,?,'distinct-known-content','passed')",
                        ('fixture.pdf' if evidence == 'canonical' else 'other.pdf', 'second-' + evidence)).lastrowid
                    if evidence == 'alias':
                        conn.execute("INSERT INTO source_document_aliases(account_id,alias_key,document_id) VALUES ('SECOND','fixture.pdf',?)", (doc,))
                    elif evidence == 'legacy':
                        legacy = conn.execute("INSERT INTO transactions(txn_date,description_raw,amount,account_id,source_file,import_hash) VALUES ('2026-01-03','SYNTHETIC LEGACY',-5,'SECOND','C:/old/fixture.pdf','other-legacy')").lastrowid
                    elif evidence == 'shared_content':
                        conn.execute('INSERT INTO source_document_contents(document_id,content_hash) VALUES (?,?)', (doc, own_preview.content_hash))
                before = self.snapshot()
                preview = prepare_import(self.pdf, db_path=self.db, root=self.root, account_id='HSBC_UK')
                self.assertEqual(preview.document_id, own.document_id)
                self.assertEqual(preview.status, 'blocked')
                self.assertIn('source_account_mismatch', preview.reasons)
                with self.assertRaises(ImportServiceError) as error:
                    commit_import(preview, approved=True)
                self.assertEqual(error.exception.code, 'blocked')
                self.assertEqual(before, self.snapshot())
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute('DELETE FROM source_document_aliases WHERE document_id=?', (doc,))
                    conn.execute('DELETE FROM source_document_contents WHERE document_id=?', (doc,))
                    conn.execute('DELETE FROM source_documents WHERE document_id=?', (doc,))
                    if evidence == 'legacy':
                        conn.execute('DELETE FROM transactions WHERE txn_id=?', (legacy,))

    def test_selected_alias_and_legacy_cannot_mask_other_account_content(self):
        from scripts.services.ingestion import prepare_import, commit_import, ImportServiceError
        from tests.test_extraction import synthetic_pdf, LINES
        self.migrate()
        self.register_second()
        self.pdf.write_bytes(synthetic_pdf([(1, 'Statement period: 01 Jan 2026 to 31 Jan 2026')] + LINES))
        own = self.commit(prepare_import(self.pdf, db_path=self.db, root=self.root, account_id='HSBC_UK'))
        alias = self.pdf.with_name('selected-alias.pdf')
        alias.write_bytes(self.pdf.read_bytes())
        self.commit(prepare_import(alias, db_path=self.db, root=self.root, account_id='HSBC_UK'))
        other = self.pdf.with_name('other.pdf')
        # Distinct bytes with identical financial rows are legitimate per-account
        # documents, not a global row deduplication opportunity.
        other.write_bytes(synthetic_pdf([(1, 'SYNTHETIC SECOND ACCOUNT'),
                         (1, 'Statement period: 01 Jan 2026 to 31 Jan 2026')] + LINES))
        second = self.commit(prepare_import(other, db_path=self.db, root=self.root, account_id='SECOND'))
        self.assertNotEqual(own.document_id, second.document_id)
        alias.write_bytes(other.read_bytes())
        for evidence in ('alias', 'legacy'):
            with self.subTest(selected_evidence=evidence):
                if evidence == 'legacy':
                    with closing(sqlite3.connect(self.db)) as conn, conn:
                        conn.execute('DELETE FROM source_document_aliases WHERE document_id=?', (own.document_id,))
                        conn.execute('DELETE FROM transaction_sources WHERE document_id=?', (own.document_id,))
                        conn.execute("UPDATE transactions SET source_file='C:/old/selected-alias.pdf' WHERE account_id='HSBC_UK'")
                before = self.snapshot()
                preview = prepare_import(alias, db_path=self.db, root=self.root, account_id='HSBC_UK')
                self.assertEqual(preview.status, 'blocked')
                self.assertIn('source_account_mismatch', preview.reasons)
                with self.assertRaises(ImportServiceError) as error:
                    commit_import(preview, approved=True)
                self.assertEqual(error.exception.code, 'blocked')
                self.assertEqual(before, self.snapshot())

    def test_policy_change_during_extraction_cannot_make_eligible_preview(self):
        from scripts.services.ingestion import prepare_import, ImportServiceError
        self.migrate()
        def extract(*args, **kwargs):
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute("UPDATE accounts SET import_enabled=0 WHERE account_id='HSBC_UK'")
            return self.extraction()
        with patch('scripts.services.ingestion.extract_statement', side_effect=extract):
            with self.assertRaises(ImportServiceError):
                prepare_import(self.pdf, db_path=self.db, root=self.root, account_id='HSBC_UK')

    def test_identical_rows_on_two_accounts_keep_distinct_ids_hashes_links(self):
        self.migrate()
        self.register_second()
        self.commit(self.prepare(account_id='HSBC_UK'))
        self.pdf = self.pdf.with_name('second-account.pdf')
        self.pdf.write_bytes(b'distinct synthetic second-account source')
        second = self.prepare(self.extraction(account='SECOND'), account_id='SECOND')
        self.assertEqual(second.status, 'eligible')
        self.assertEqual(self.commit(second).added, 1)
        with closing(sqlite3.connect(self.db)) as conn:
            rows = conn.execute('SELECT t.txn_id,t.import_hash,t.account_id,d.account_id,d.document_id FROM transactions t JOIN transaction_sources s ON s.txn_id=t.txn_id JOIN source_documents d ON d.document_id=s.document_id ORDER BY t.txn_id').fetchall()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0][0], rows[1][0])
        self.assertNotEqual(rows[0][1], rows[1][1])
        self.assertNotEqual(rows[0][4], rows[1][4])
        self.assertEqual([(r[2],r[3]) for r in rows], [('HSBC_UK','HSBC_UK'), ('SECOND','SECOND')])

    def test_legacy_wrong_account_source_is_blocked(self):
        self.migrate()
        self.register_second()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO transactions(txn_date,description_raw,amount,source_file,import_hash) VALUES ('2026-01-02','SYNTHETIC',-1,'C:/old/fixture.pdf','legacy')")
        preview = self.prepare(self.extraction(account='SECOND'), account_id='SECOND')
        self.assertIn('source_account_mismatch', preview.reasons)

    def test_commit_rejects_disabled_policy_and_account_tampering(self):
        from dataclasses import replace
        from scripts.services.ingestion import commit_import, ImportServiceError
        self.migrate()
        preview = self.prepare(account_id='HSBC_UK')
        with self.assertRaises(ImportServiceError) as error:
            commit_import(replace(preview, account_id='OTHER'), approved=True)
        self.assertEqual(error.exception.code, 'invalid_preview')
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE accounts SET import_enabled=0 WHERE account_id='HSBC_UK'")
        before = self.snapshot()
        with self.assertRaises(ImportServiceError) as error:
            commit_import(preview, approved=True)
        self.assertEqual(error.exception.code, 'stale_preview')
        self.assertEqual(before, self.snapshot())

    def test_legacy_compatibility_requires_observed_uk_gbp(self):
        from scripts.services.ingestion import prepare_import, ImportServiceError
        with patch('scripts.services.ingestion.extract_statement') as extract:
            for account, currency in [('HSBC_UK','USD'), ('UNKNOWN','GBP'), ('bad-id','GBP')]:
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute('DELETE FROM transactions')
                    conn.execute("INSERT INTO transactions(txn_date,description_raw,amount,account_id,currency,import_hash) VALUES ('2026-01-02','fixture',-1,?,?,'fixture')", (account,currency))
                with self.assertRaises(ImportServiceError):
                    prepare_import(self.pdf, db_path=self.db, root=self.root, account_id=account)
            extract.assert_not_called()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DELETE FROM transactions')
            conn.execute("INSERT INTO transactions(txn_date,description_raw,amount,account_id,currency,import_hash) VALUES ('2026-01-02','fixture',-1,'HSBC_UK','GBP','fixture')")
        preview = self.prepare(account_id='HSBC_UK')
        self.assertEqual(preview.account_display_name, 'HSBC UK (inferred; setup pending)')
        self.assertFalse(preview.schema_ready)
        self.assertEqual(preview.account_id, 'HSBC_UK')
