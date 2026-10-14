import unittest
import sqlite3
from datetime import date

from scripts.services.coverage import (
    CoverageWindow, _iter_month_starts, coverage_windows_from_db, month_coverage,
)


class CoverageTests(unittest.TestCase):
    def test_month_iteration_stops_at_final_calendar_month(self):
        months = tuple(_iter_month_starts(date(9999, 1, 1), date.max))
        self.assertEqual(months, tuple(date(9999, month, 1) for month in range(1, 13)))
        self.assertEqual(tuple(_iter_month_starts(date.max, date.max)), (date(9999, 12, 1),))

    def test_verified_coverage_includes_final_calendar_day(self):
        windows = (CoverageWindow('HSBC_UK', '9999-12-01', '9999-12-31', 'final-statement'),)
        month, = month_coverage(windows, start='9999-12-01', end='9999-12-31', account_id='HSBC_UK')
        self.assertEqual((month.month, month.status, month.covered_days, month.total_days, month.missing_days),
                         ('9999-12', 'complete', 31, 31, 0))
        self.assertEqual(month.sources, ('final-statement',))

    def test_complete_months_require_every_day_covered(self):
        windows = (
            CoverageWindow('HSBC_UK', '2026-04-10', '2026-05-09', 'statement-a'),
            CoverageWindow('HSBC_UK', '2026-05-10', '2026-06-09', 'statement-b'),
            CoverageWindow('HSBC_UK', '2026-06-10', '2026-07-09', 'statement-c'),
        )

        months = month_coverage(windows, start='2026-04-01', end='2026-06-30')

        by_month = {m.month: m for m in months}
        self.assertEqual(by_month['2026-04'].status, 'partial')
        self.assertEqual(by_month['2026-04'].covered_days, 21)
        self.assertEqual(by_month['2026-04'].total_days, 30)
        self.assertEqual(by_month['2026-05'].status, 'complete')
        self.assertEqual(by_month['2026-05'].covered_days, 31)
        self.assertEqual(by_month['2026-06'].status, 'complete')
        self.assertEqual(by_month['2026-06'].covered_days, 30)
        self.assertEqual(by_month['2026-06'].total_days, 30)

    def test_gap_inside_month_prevents_complete_status(self):
        windows = (
            CoverageWindow('HSBC_UK', '2026-05-01', '2026-05-10', 'statement-a'),
            CoverageWindow('HSBC_UK', '2026-05-12', '2026-05-31', 'statement-b'),
        )

        months = month_coverage(windows, start='2026-05-01', end='2026-05-31')

        self.assertEqual(len(months), 1)
        self.assertEqual(months[0].month, '2026-05')
        self.assertEqual(months[0].status, 'partial')
        self.assertEqual(months[0].covered_days, 30)
        self.assertEqual(months[0].missing_days, 1)

    def test_partial_query_boundary_does_not_make_partial_calendar_month_complete(self):
        windows = (CoverageWindow('HSBC_UK', '2026-05-11', '2026-05-31', 'statement-a'),)

        months = month_coverage(windows, start='2026-05-11', end='2026-05-31')

        self.assertEqual(months[0].month, '2026-05')
        self.assertEqual(months[0].status, 'partial')
        self.assertEqual(months[0].covered_days, 21)
        self.assertEqual(months[0].total_days, 31)
        self.assertEqual(months[0].missing_days, 10)

    def test_account_filter_keeps_accounts_separate(self):
        windows = (
            CoverageWindow('HSBC_UK', '2026-05-01', '2026-05-31', 'statement-a'),
            CoverageWindow('OTHER', '2026-06-01', '2026-06-30', 'statement-b'),
        )

        months = month_coverage(windows, start='2026-05-01', end='2026-06-30', account_id='HSBC_UK')

        by_month = {m.month: m for m in months}
        self.assertEqual(by_month['2026-05'].status, 'complete')
        self.assertEqual(by_month['2026-06'].status, 'missing')

    def test_db_windows_use_only_reconciled_document_coverage(self):
        conn = sqlite3.connect(':memory:')
        conn.execute('''CREATE TABLE source_documents (
            account_id TEXT NOT NULL,
            document_key TEXT NOT NULL,
            coverage_start TEXT,
            coverage_end TEXT,
            reconciliation_status TEXT NOT NULL
        )''')
        conn.executemany('INSERT INTO source_documents VALUES (?, ?, ?, ?, ?)', [
            ('HSBC_UK', 'StatementsPDF/a.pdf', '2026-05-01', '2026-05-31', 'passed'),
            ('HSBC_UK', 'StatementsPDF/b.pdf', '2026-06-01', '2026-06-30', 'needs_review'),
            ('HSBC_UK', 'StatementsPDF/c.pdf', None, '2026-07-31', 'passed'),
            ('OTHER', 'StatementsPDF/d.pdf', '2026-05-01', '2026-05-31', 'passed'),
        ])

        windows = coverage_windows_from_db(conn, account_id='HSBC_UK')

        self.assertEqual(windows, (
            CoverageWindow('HSBC_UK', '2026-05-01', '2026-05-31', 'StatementsPDF/a.pdf'),
        ))


if __name__ == '__main__':
    unittest.main()
