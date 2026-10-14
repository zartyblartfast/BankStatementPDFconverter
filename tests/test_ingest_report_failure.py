"""Saved-import responses use real synthetic PDF imports and scratch ledgers only."""
from contextlib import closing
from pathlib import Path
import re
import sqlite3
from unittest.mock import patch

from scripts import dashboard as d
from scripts.migrate_db import migrate_db
from tests.test_dashboard_security import DashboardFixture
from tests.test_extraction import LINES, synthetic_pdf


class IngestReportFailureTests(DashboardFixture):
    def setUp(self):
        super().setUp()
        migrate_db(self.paths.db_path, migrations_dir=Path(__file__).resolve().parents[1] / 'scripts/migrations')
        with closing(sqlite3.connect(self.paths.db_path)) as conn, conn:
            conn.execute('DELETE FROM transactions')
            conn.execute("INSERT INTO account_statement_folders VALUES ('HSBC_UK','.')")
        self.paths.statements_dir.mkdir()
        pdf = self.paths.statements_dir / 'synthetic.pdf'
        pdf.write_bytes(synthetic_pdf([(1, 'Statement period: 01 Jan 2026 to 31 Jan 2026')] + LINES))
        self.relative = str(pdf.relative_to(self.root))
        previews = patch.object(d, '_IMPORT_PREVIEWS', {})
        previews.start()
        self.addCleanup(previews.stop)

    def review(self):
        response = self.client.post('/ingest', base_url=self.base, data={
            'csrf_token': self.token(), 'pdf': self.relative, 'account_id': 'HSBC_UK'})
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('Ready to import', html)
        match = re.search(r'name="import_token" value="([^"]+)"', html)
        self.assertIsNotNone(match)
        assert match is not None
        return match.group(1)

    def approve(self, token):
        return self.client.post('/ingest', base_url=self.base, data={
            'csrf_token': self.token(), 'action': 'commit',
            'import_token': token, 'approve': 'yes'})

    def snapshot(self):
        with closing(sqlite3.connect(self.paths.db_path)) as conn:
            return list(conn.iterdump())

    def test_report_failure_returns_saved_success_and_consumes_preview(self):
        token = self.review()
        with patch.object(d, 'commit_import', wraps=d.commit_import) as commit, \
                patch.object(d, '_regenerate_report', side_effect=OSError('synthetic report failure')) as refresh, \
                self.assertLogs(d.app.logger, level='ERROR'):
            response = self.approve(token)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('Import Committed', html)
        self.assertIn('Imported 2 new transactions', html)
        self.assertIn('Import saved, but reports could not be refreshed.', html)
        self.assertNotIn('Import blocked', html)
        commit.assert_called_once()
        refresh.assert_called_once()
        self.assertNotIn(token, d._IMPORT_PREVIEWS)
        with closing(sqlite3.connect(self.paths.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM source_documents').fetchone()[0], 1)
        saved = self.snapshot()
        with patch.object(d, 'commit_import', wraps=d.commit_import) as replay_commit:
            replay = self.approve(token)
        self.assertEqual(replay.status_code, 400)
        replay_commit.assert_not_called()
        self.assertEqual(self.snapshot(), saved)

    def test_stale_import_remains_blocked_without_saved_warning(self):
        token = self.review()
        (self.root / self.relative).write_bytes(b'changed synthetic statement')
        before = self.snapshot()
        with patch.object(d, '_regenerate_report') as refresh:
            response = self.approve(token)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('Import blocked: stale_preview', html)
        self.assertNotIn('Import Committed', html)
        self.assertNotIn('Import saved', html)
        self.assertIn(token, d._IMPORT_PREVIEWS)
        refresh.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_commit_oserror_is_not_converted_to_saved_success(self):
        token = self.review()
        before = self.snapshot()
        with patch.object(d, 'commit_import', side_effect=OSError('synthetic commit failure')), \
                patch.object(d, '_regenerate_report') as refresh, \
                self.assertLogs(d.app.logger, level='ERROR'):
            response = self.approve(token)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn('Import saved', response.get_data(as_text=True))
        self.assertIn(token, d._IMPORT_PREVIEWS)
        refresh.assert_not_called()
        self.assertEqual(self.snapshot(), before)
