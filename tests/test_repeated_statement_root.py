"""Root identity regressions; all files and ledgers are synthetic scratch fixtures."""
from contextlib import closing
from pathlib import Path
import re
import sqlite3
from unittest.mock import patch

from scripts import dashboard as d
from scripts.migrate_db import migrate_db
from tests.test_dashboard_security import DashboardFixture
from tests import test_ingestion_service as ingestion_tests
from scripts.services.ingestion import prepare_import, commit_import, ImportServiceError
from tests.test_extraction import synthetic_pdf, LINES


import unittest
from scripts.services.source_matching import source_identity


class RepeatedStatementRootImport(unittest.TestCase):
    root: Path
    db: Path
    pdf: Path

    setUp = ingestion_tests.IngestionTests.setUp
    migrate = ingestion_tests.IngestionTests.migrate
    snapshot = ingestion_tests.IngestionTests.snapshot
    commit = ingestion_tests.IngestionTests.commit
    extraction = ingestion_tests.IngestionTests.extraction
    prepare = ingestion_tests.IngestionTests.prepare

    def test_configured_project_ancestor_named_statements_does_not_enter_identity(self):
        import shutil
        self.migrate()
        previous = self.root
        self.root = previous / 'StatementsPDF' / 'project'
        self.root.mkdir()
        shutil.copytree(previous / 'config', self.root / 'config')
        self.db = self.root / 'ledger.db'
        shutil.copyfile(previous / 'ledger.db', self.db)
        self.pdf = self.root / 'StatementsPDF' / 'StatementsPDF' / 'fixture.pdf'
        self.pdf.parent.mkdir(parents=True)
        self.pdf.write_bytes(b'synthetic ancestor fixture')
        with closing(sqlite3.connect(self.db)) as conn, conn:
            txn = conn.execute("INSERT INTO transactions(txn_date,description_raw,amount,source_file,account_id,row_ref,page,import_hash) VALUES ('2026-01-02','SYNTHETIC',-1,?,'HSBC_UK','1:2',1,'legacy')", (str(self.pdf),)).lastrowid
        before = self.snapshot()
        preview = self.prepare()
        self.assertEqual(preview.document_key, 'statementspdf/fixture.pdf')
        self.assertEqual(preview.plan.matches, ((0, txn),))
        wrong = self.prepare(self.extraction(account='OTHER'), account_id='OTHER')
        self.assertIn('source_account_mismatch', wrong.reasons)
        self.assertEqual(self.snapshot(), before)

    def test_moved_legacy_nested_path_does_not_claim_other_account_root(self):
        self.migrate()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            txn = conn.execute("INSERT INTO transactions(txn_date,description_raw,amount,source_file,account_id,row_ref,page,import_hash) VALUES ('2026-01-02','SYNTHETIC',-1,'C:/old/StatementsPDF/StatementsPDF/fixture.pdf','HSBC_UK','1:2',1,'legacy')").lastrowid
        before = self.snapshot()
        root_other = self.prepare(self.extraction(account='OTHER'), account_id='OTHER')
        self.assertNotIn('source_account_mismatch', root_other.reasons)
        self.assertEqual(root_other.plan.matches, ())
        self.pdf = self.pdf.parent / 'StatementsPDF' / 'fixture.pdf'
        self.pdf.parent.mkdir()
        self.pdf.write_bytes(b'moved synthetic legacy')
        nested = self.prepare()
        self.assertEqual(nested.document_key, 'statementspdf/fixture.pdf')
        self.assertEqual(nested.plan.matches, ((0, txn),))
        wrong_account = self.prepare(self.extraction(account='OTHER'), account_id='OTHER')
        self.assertIn('source_account_mismatch', wrong_account.reasons)
        self.assertEqual(self.snapshot(), before)

    def test_historic_collapsed_link_does_not_reassign_root_by_raw_filename(self):
        self.migrate()
        nested = self.pdf.parent / 'StatementsPDF' / 'fixture.pdf'
        nested.parent.mkdir()
        nested.write_bytes(b'historic synthetic nested')
        with closing(sqlite3.connect(self.db)) as conn, conn:
            txn = conn.execute("INSERT INTO transactions(txn_date,description_raw,amount,source_file,account_id,row_ref,page,import_hash) VALUES ('2026-01-02','SYNTHETIC',-1,?,'HSBC_UK','1:2',1,'historic')", (str(nested),)).lastrowid
            doc = conn.execute("INSERT INTO source_documents(account_id,document_key,source_identity,content_hash,reconciliation_status) VALUES ('HSBC_UK','fixture.pdf','historic-identity','historic-hash','passed')").lastrowid
            conn.execute("INSERT INTO transaction_sources(txn_id,document_id,row_ref,occurrence) VALUES (?,?,'1:2',0)", (txn, doc))
        # An explicit canonical alias remains authoritative (no migration), but
        # removing that alias/key match must NOT let a linked raw path redirect it.
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE source_documents SET document_key='historic.pdf' WHERE document_id=?", (doc,))
            conn.execute("INSERT INTO source_document_aliases(account_id,alias_key,document_id) VALUES ('HSBC_UK','statementspdf/fixture.pdf',?)", (doc,))
        before = self.snapshot()
        preview = self.prepare()
        self.assertIsNone(preview.document_id)
        self.assertEqual(preview.plan.matches, ())
        self.assertEqual(self.snapshot(), before)
        self.pdf = nested
        known = self.prepare()
        self.assertEqual(known.document_id, doc)
        self.assertEqual(known.plan.matches, ((0, txn),))
        self.assertEqual(self.snapshot(), before)

    def test_real_nested_pdf_keeps_identity_separate_from_linked_root(self):
        self.migrate()
        self.pdf.write_bytes(synthetic_pdf([(1, 'Statement period: 01 Jan 2026 to 31 Jan 2026')] + LINES))
        root_preview = prepare_import(self.pdf, db_path=self.db, root=self.root, account_id='HSBC_UK')
        self.assertEqual(root_preview.status, 'eligible', root_preview.reasons)
        imported = self.commit(root_preview)
        nested = self.pdf.parent / 'StatementsPDF' / 'fixture.pdf'
        nested.parent.mkdir()
        nested.write_bytes(synthetic_pdf([(1, 'SYNTHETIC DISTINCT NESTED'),
                            (1, 'Statement period: 01 Jan 2026 to 31 Jan 2026')] + LINES))
        before = self.snapshot()
        preview = prepare_import(nested, db_path=self.db, root=self.root, account_id='HSBC_UK')
        self.assertEqual(preview.document_key, 'statementspdf/fixture.pdf')
        self.assertNotEqual(source_identity(preview.account_id, preview.document_key),
                            source_identity(root_preview.account_id, root_preview.document_key))
        self.assertIsNone(preview.document_id)
        self.assertEqual(preview.plan.matches, ())
        self.assertEqual(preview.plan.additions, (0, 1))
        self.assertIn('other_source_overlap', preview.reasons)
        with self.assertRaises(ImportServiceError) as error:
            commit_import(preview, approved=True)
        self.assertEqual(error.exception.code, 'blocked')
        self.assertEqual(self.snapshot(), before)
        reimport = prepare_import(self.pdf, db_path=self.db, root=self.root, account_id='HSBC_UK')
        self.assertEqual(reimport.document_id, imported.document_id)
        self.assertEqual(len(reimport.plan.matches), 2)


class RepeatedStatementRootUI(DashboardFixture):
    def setUp(self):
        super().setUp()
        migrate_db(self.paths.db_path)
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute("INSERT INTO account_statement_folders VALUES ('HSBC_UK','.')")

    def test_legacy_status_uses_configured_root_below_same_named_ancestor(self):
        project = self.root / 'StatementsPDF' / 'project'
        statements = project / 'StatementsPDF'
        nested = statements / 'StatementsPDF' / 'fixture.pdf'
        nested.parent.mkdir(parents=True)
        nested.write_bytes(b'synthetic nested')
        (statements / 'fixture.pdf').write_bytes(b'synthetic root')
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute('UPDATE transactions SET source_file=?', (str(nested),))
        before = self.paths.db_path.read_bytes()
        with patch.object(d, 'PROJECT_ROOT', project), patch.object(d, 'STATEMENTS_DIR', statements):
            html = self.client.get('/ingest?account_id=HSBC_UK', base_url=self.base).get_data(as_text=True)
        statuses = {
            Path(path).relative_to('StatementsPDF').as_posix(): int(count or 0)
            for path, count in re.findall(
                r'<tr>\s*<td><input type="radio" name="pdf" value="([^"]+)".*?'
                r'(?:Imported \((\d+) txns\)|Not imported).*?</tr>', html, re.S)
        }
        self.assertEqual(statuses, {'fixture.pdf': 0, 'StatementsPDF/fixture.pdf': 1})
        self.assertEqual(self.paths.db_path.read_bytes(), before)

    def test_only_root_history_marks_root_not_repeated_nested_directory(self):
        nested = self.paths.statements_dir / 'StatementsPDF' / 'fixture.pdf'
        nested.parent.mkdir(parents=True)
        nested.write_bytes(b'synthetic nested')
        (self.paths.statements_dir / 'fixture.pdf').write_bytes(b'synthetic root')
        migrate_db(self.paths.db_path, migrations_dir=Path(__file__).resolve().parents[1] / 'scripts/migrations')
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            doc = conn.execute("INSERT INTO source_documents(account_id,document_key,source_identity,content_hash,reconciliation_status) VALUES ('HSBC_UK','fixture.pdf','root-identity','root-hash','passed')").lastrowid
            conn.execute("INSERT INTO transaction_sources(txn_id,document_id,row_ref,occurrence) VALUES (1,?,'1',0)", (doc,))
        before = self.paths.db_path.read_bytes()
        html = self.client.get('/ingest?account_id=HSBC_UK', base_url=self.base).get_data(as_text=True)
        statuses = {
            Path(path).relative_to('StatementsPDF').as_posix(): int(count or 0)
            for path, count in re.findall(
                r'<tr>\s*<td><input type="radio" name="pdf" value="([^"]+)".*?'
                r'(?:Imported \((\d+) txns\)|Not imported).*?</tr>', html, re.S)
        }
        self.assertEqual(statuses, {'fixture.pdf': 1, 'StatementsPDF/fixture.pdf': 0})
        self.assertEqual(self.paths.db_path.read_bytes(), before)
