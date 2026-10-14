import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import ingest_pipeline as pipeline
from scripts import dashboard as d
from tests.test_dashboard_security import DashboardFixture


class IngestSelectionTests(DashboardFixture):
    def setUp(self):
        super().setUp()
        from scripts.migrate_db import migrate_db
        from contextlib import closing
        import sqlite3
        migrate_db(self.paths.db_path)
        # Explicit scratch-only file-scope fixture; production never seeds maps.
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute("INSERT INTO account_statement_folders VALUES ('HSBC_UK','.')")

    def test_nested_pdf_is_distinguished_and_review_uses_exact_selected_path(self):
        nested = self.paths.statements_dir / 'HSBC-IOM' / 'archive' / 'fixture.pdf'
        nested.parent.mkdir(parents=True)
        nested.write_bytes(b'nested synthetic PDF')
        root_pdf = self.paths.statements_dir / nested.name
        root_pdf.write_bytes(b'root synthetic PDF')
        relative = str(nested.relative_to(self.root))
        response = self.client.get('/ingest?account_id=HSBC_UK', base_url=self.base)
        html = response.get_data(as_text=True)
        self.assertIn('value="' + relative + '"', html)
        self.assertIn('>HSBC-IOM/archive/fixture.pdf<', html)
        self.assertIn('>fixture.pdf<', html)
        self.assertIn('value="">Select an account', html)
        with patch.object(d, 'prepare_import', side_effect=RuntimeError('synthetic blocked')) as prepare:
            response = self.client.post('/ingest', base_url=self.base, data={
                'csrf_token': self.token(), 'pdf': relative, 'account_id': 'HSBC_UK'})
        self.assertEqual(response.status_code, 200)
        prepare.assert_called_once_with(relative, db_path=d.DB_PATH,
                                       root=d.PROJECT_ROOT, account_id='HSBC_UK')
        self.assertIn('value="' + relative + '" checked', response.get_data(as_text=True))

    def test_directory_junction_is_not_offered_or_reviewed_even_inside_root(self):
        import subprocess
        target = self.paths.statements_dir / 'regular'
        target.mkdir(parents=True)
        (target / 'fixture.pdf').write_bytes(b'synthetic PDF')
        linked = self.paths.statements_dir / 'linked'
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(linked), str(target)],
                                capture_output=True)
        self.assertEqual(result.returncode, 0, 'synthetic junction creation failed')
        self.addCleanup(lambda: os.rmdir(linked) if linked.exists() else None)
        offered = str((linked / 'fixture.pdf').relative_to(self.root))
        html = self.client.get('/ingest', base_url=self.base).get_data(as_text=True)
        self.assertNotIn('value="' + offered + '"', html)
        with patch.object(d, 'prepare_import') as prepare:
            response = self.client.post('/ingest', base_url=self.base, data={
                'csrf_token': self.token(), 'pdf': offered, 'account_id': 'HSBC_UK'})
        self.assertEqual(response.status_code, 400)
        prepare.assert_not_called()

    def test_escaping_and_cyclic_junctions_are_pruned_before_descent(self):
        import subprocess
        self.paths.statements_dir.mkdir()
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        (outside / 'escape.pdf').write_bytes(b'synthetic PDF')
        for name, target in (('escape', outside), ('cycle', self.paths.statements_dir)):
            linked = self.paths.statements_dir / name
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(linked), str(target)],
                                    capture_output=True)
            self.assertEqual(result.returncode, 0, 'synthetic junction creation failed')
            self.addCleanup(lambda p=linked: os.rmdir(p) if p.exists() else None)
        with patch.object(d, 'prepare_import') as prepare:
            self.assertEqual(d._available_statement_pdfs(), {})
            response = self.client.post('/ingest', base_url=self.base, data={
                'csrf_token': self.token(),
                'pdf': str(Path('StatementsPDF/escape/escape.pdf')), 'account_id': 'HSBC_UK'})
        self.assertEqual(response.status_code, 400)
        prepare.assert_not_called()

    def test_linked_statement_root_is_rejected_before_resolution(self):
        import subprocess
        target = self.root / 'regular'
        target.mkdir()
        (target / 'fixture.pdf').write_bytes(b'synthetic PDF')
        linked = self.paths.statements_dir
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(linked), str(target)],
                                capture_output=True)
        self.assertEqual(result.returncode, 0, 'synthetic junction creation failed')
        self.addCleanup(lambda: os.rmdir(linked) if linked.exists() else None)
        self.assertEqual(d._available_statement_pdfs(), {})

    def test_file_links_and_reparse_points_are_not_offered(self):
        import stat
        self.paths.statements_dir.mkdir()
        candidate = self.paths.statements_dir / 'fixture.pdf'
        candidate.write_bytes(b'synthetic PDF')
        original = Path.lstat
        for mode, attributes in ((stat.S_IFLNK, 0), (stat.S_IFREG, 0x400)):
            with self.subTest(mode=mode, attributes=attributes):
                def lstat(path):
                    if path == candidate:
                        return SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
                    return original(path)
                with patch.object(Path, 'lstat', lstat):
                    self.assertEqual(d._available_statement_pdfs(), {})

    def test_nested_literal_selection_and_unknown_account_are_rejected(self):
        selected = self.paths.statements_dir / 'HSBC-IOM' / 'fixture.pdf'
        selected.parent.mkdir(parents=True)
        selected.write_bytes(b'synthetic PDF')
        exact = str(selected.relative_to(self.root))
        with patch.object(d, 'prepare_import') as prepare:
            for value, account in ((exact, ''), (exact, 'HSBC_IOM'),
                    (str(selected), 'HSBC_UK'),
                    (str(Path('StatementsPDF/HSBC-IOM/../HSBC-IOM/fixture.pdf')), 'HSBC_UK'),
                    (str(Path('StatementsPDF/missing/fixture.pdf')), 'HSBC_UK')):
                response = self.client.post('/ingest', base_url=self.base, data={
                    'csrf_token': self.token(), 'pdf': value, 'account_id': account})
                self.assertEqual(response.status_code, 400)
            prepare.assert_not_called()

    def test_dashboard_previews_then_commits_server_held_import(self):
        self.paths.statements_dir.mkdir()
        selected = self.paths.statements_dir / 'fixture.pdf'
        selected.write_bytes(b'synthetic PDF')
        relative = str(selected.relative_to(self.root))
        preview = SimpleNamespace(token='server-held-token')
        summary = {'token': 'server-held-token', 'status': 'eligible', 'reasons': [],
                   'schema_ready': True, 'account_id':'HSBC_UK', 'account_display_name':'HSBC UK',
                   'reconciliation': {'status': 'passed', 'reasons': []},
                   'coverage_status': 'evidenced',
                   'counts': {'extracted': 2, 'additions': 2, 'matches': 0,
                              'changed_source_fields': 0, 'potential_removals': 0,
                              'changed_source_candidates': 0, 'ambiguous': 0,
                              'overlaps': 0, 'categorised': 2, 'uncategorised': 0}}
        commit = SimpleNamespace(added=2, matched=0, total_current=2,
                                 exceptions_created=0, replayed=False)
        with patch.object(d, 'prepare_import', return_value=preview) as prepare, \
                patch.object(d, 'preview_summary', return_value=summary) as summarise, \
                patch.object(d, 'commit_import', return_value=commit) as commit_import, \
                patch.object(d, '_regenerate_report') as regenerate:
            response = self.client.post('/ingest', base_url=self.base,
                data={'csrf_token': self.token(), 'pdf': relative, 'action': 'preview', 'account_id': 'HSBC_UK'})
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn('Import Review', html)
            self.assertIn('Ready to import <strong>fixture.pdf</strong>', html)
            self.assertIn('value="StatementsPDF\\fixture.pdf" checked', html)
            prepare.assert_called_once_with(relative, db_path=d.DB_PATH, root=d.PROJECT_ROOT, account_id='HSBC_UK')
            summarise.assert_called_once_with(preview)
            commit_import.assert_not_called()
            regenerate.assert_not_called()

            blocked = self.client.post('/ingest', base_url=self.base,
                data={'csrf_token': self.token(), 'action': 'commit', 'import_token': 'not-held', 'approve': 'yes'})
            self.assertEqual(blocked.status_code, 400)
            commit_import.assert_not_called()

            response = self.client.post('/ingest', base_url=self.base,
                data={'csrf_token': self.token(), 'action': 'commit', 'import_token': 'server-held-token', 'approve': 'yes'})
            self.assertEqual(response.status_code, 200)
            self.assertIn('Imported fixture.pdf', response.get_data(as_text=True))
            commit_import.assert_called_once_with(preview, approved=True)
            regenerate.assert_called_once()

    def test_fully_matched_imported_statement_is_review_only(self):
        self.paths.statements_dir.mkdir()
        selected = self.paths.statements_dir / 'fixture.pdf'
        selected.write_bytes(b'synthetic PDF')
        relative = str(selected.relative_to(self.root))
        preview = SimpleNamespace(token='already-imported-token')
        summary = {'token': 'already-imported-token', 'status': 'eligible', 'reasons': [],
                   'schema_ready': True, 'account_id':'HSBC_UK', 'account_display_name':'HSBC UK',
                   'reconciliation': {'status': 'passed', 'reasons': []},
                   'coverage_status': 'evidenced',
                   'counts': {'extracted': 2, 'additions': 0, 'matches': 2,
                              'changed_source_fields': 0, 'potential_removals': 0,
                              'changed_source_candidates': 0, 'ambiguous': 0,
                              'overlaps': 0, 'categorised': 2, 'uncategorised': 0}}
        with patch.object(d, 'prepare_import', return_value=preview), \
                patch.object(d, 'preview_summary', return_value=summary), \
                patch.object(d, 'commit_import') as commit_import:
            response = self.client.post('/ingest', base_url=self.base,
                data={'csrf_token': self.token(), 'pdf': relative, 'action': 'preview', 'account_id': 'HSBC_UK'})
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('Already imported', html)
        self.assertIn('This statement is already fully imported', html)
        self.assertIn('fixture.pdf', html)
        self.assertNotIn('Approve and Import', html)
        self.assertNotIn('Ready to import', html)
        commit_import.assert_not_called()

        response = self.client.post('/ingest', base_url=self.base,
            data={'csrf_token': self.token(), 'action': 'commit', 'import_token': 'already-imported-token', 'approve': 'yes'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('already fully imported', response.get_data(as_text=True))
        commit_import.assert_not_called()

    def test_m1_imported_statement_status_uses_source_documents(self):
        from contextlib import closing
        import sqlite3
        from scripts.migrate_db import migrate_db

        self.paths.statements_dir.mkdir()
        selected = self.paths.statements_dir / 'fixture.pdf'
        selected.write_bytes(b'synthetic PDF')
        migrate_db(self.paths.db_path, migrations_dir=Path(__file__).resolve().parents[1] / 'scripts/migrations')
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            doc_id = conn.execute(
                "INSERT INTO source_documents(account_id,document_key,source_identity,content_hash,reconciliation_status) VALUES ('HSBC_UK','fixture.pdf','fixture-identity','hash','passed')"
            ).lastrowid
            conn.execute("INSERT INTO transaction_sources(txn_id,document_id,row_ref,occurrence) VALUES (1,?,'1:1',0)", (doc_id,))

        response = self.client.get('/ingest?account_id=HSBC_UK', base_url=self.base)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('Imported (1 txns)', html)
        self.assertNotIn('○ Not imported', html)

    def test_unlisted_or_outside_selection_is_rejected_without_extraction(self):
        self.paths.statements_dir.mkdir()
        selected = self.paths.statements_dir / 'fixture.pdf'
        selected.write_bytes(b'synthetic PDF')
        outside = Path(self.temp.name) / 'outside.pdf'
        outside.write_bytes(b'outside synthetic PDF')
        invalid = [str(Path('StatementsPDF') / 'unknown.pdf'), str(outside),
                   str(selected), str(Path('StatementsPDF') / '..' / 'StatementsPDF' / selected.name),
                   str(Path('..') / outside.name)]
        with patch.object(pipeline, 'PROJECT_ROOT', self.root), \
                patch.object(pipeline, 'parse_pdf_to_lines_words', return_value=(['synthetic'], {})) as extract, \
                patch.object(pipeline, 'parse_lines_to_transactions', return_value=([], [], None, None)):
            for value in invalid:
                with self.subTest(selection=value):
                    response = self.client.post('/ingest', base_url=self.base,
                        data={'csrf_token': self.token(), 'pdf': value, 'account_id': 'HSBC_UK'})
                    self.assertEqual(response.status_code, 400)
            extract.assert_not_called()

    def test_cli_absolute_pdf_remains_usable_with_absolute_source_identity(self):
        outside = Path(self.temp.name) / 'cli.pdf'
        outside.write_bytes(b'synthetic PDF')
        with patch.object(pipeline, 'PROJECT_ROOT', self.root), \
                patch.object(pipeline, 'parse_pdf_to_lines_words', return_value=(['synthetic'], {})) as extract, \
                patch.object(pipeline, 'parse_lines_to_transactions', return_value=([], [], None, None)) as parse:
            result = pipeline.run_pipeline(str(outside))
        extract.assert_called_once_with(str(outside))
        parse.assert_called_once_with(['synthetic'], source_file=str(outside))
        self.assertEqual(result.source_file, str(outside))
        self.assertFalse(self.paths.exports_dir.exists())

    def test_configured_statement_selected_from_other_cwd_keeps_source_identity(self):
        self.paths.statements_dir.mkdir()
        selected = self.paths.statements_dir / 'fixture.pdf'
        selected.write_bytes(b'configured synthetic PDF')
        elsewhere = Path(self.temp.name) / 'elsewhere'
        (elsewhere / 'StatementsPDF').mkdir(parents=True)
        (elsewhere / 'StatementsPDF' / selected.name).write_bytes(b'wrong synthetic PDF')
        relative = str(selected.relative_to(self.root))
        original_cwd = Path.cwd()
        self.addCleanup(os.chdir, original_cwd)
        os.chdir(elsewhere)

        preview = SimpleNamespace(token='cwd-token')
        summary = {'token': 'cwd-token', 'status': 'blocked', 'reasons': ['schema_not_ready'],
                   'schema_ready': False,
                   'reconciliation': {'status': 'unavailable', 'reasons': []},
                   'coverage_status': 'unavailable',
                   'counts': {'extracted': 0, 'additions': 0, 'matches': 0,
                              'changed_source_fields': 0, 'potential_removals': 0,
                              'changed_source_candidates': 0, 'ambiguous': 0,
                              'overlaps': 0, 'categorised': 0, 'uncategorised': 0}}

        with patch.dict(os.environ, {'BANKSTATEMENT_PROJECT_ROOT': str(self.root)}), \
                patch.object(d, 'prepare_import', return_value=preview) as prepare, \
                patch.object(d, 'preview_summary', return_value=summary):
            response = self.client.post('/ingest', base_url=self.base,
                data={'csrf_token': self.token(), 'pdf': relative, 'account_id': 'HSBC_UK'})
        self.assertEqual(response.status_code, 200)
        prepare.assert_called_once_with(relative, db_path=d.DB_PATH, root=d.PROJECT_ROOT, account_id='HSBC_UK')
        self.assertIn('schema_not_ready', response.get_data(as_text=True))
        self.assertIn('database metadata migration has not been applied', response.get_data(as_text=True))
        self.assertFalse(self.paths.exports_dir.exists())
