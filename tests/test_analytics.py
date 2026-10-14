import sqlite3
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from scripts.init_db import init_db
from scripts.services.analytics import cashflow_summary
from scripts.services.coverage import CoverageWindow


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'ledger.db'
        init_db(db_path=self.db)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO categories(category_id,name) VALUES (1,'Income')")
            conn.execute("INSERT INTO categories(category_id,name) VALUES (2,'Bills')")
            conn.execute("INSERT INTO subcategories(subcategory_id,category_id,name) VALUES (1,1,'Salary')")
            conn.execute("INSERT INTO subcategories(subcategory_id,category_id,name) VALUES (2,2,'Rent')")

    def tearDown(self):
        self.tmp.cleanup()

    def _insert_txn(self, conn, txn_id, txn_date, amount, subcategory_id, *, excluded=0):
        conn.execute(
            """
            INSERT INTO transactions(
                txn_id, txn_date, description_raw, amount, subcategory_id,
                source_file, row_ref, import_hash, excluded, merchant_core
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (txn_id, txn_date, f'txn {txn_id}', amount, subcategory_id,
             'fixture.pdf', str(txn_id), f'hash-{txn_id}', excluded, 'FIXTURE'),
        )

    def test_cashflow_averages_use_only_complete_covered_months_and_report_partial_warnings(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self._insert_txn(conn, 1, '2026-01-05', 1000.00, 1)
            self._insert_txn(conn, 2, '2026-01-10', -250.00, 2)
            self._insert_txn(conn, 3, '2026-02-05', 1200.00, 1)
            self._insert_txn(conn, 4, '2026-02-10', -300.00, 2)
            self._insert_txn(conn, 5, '2026-03-05', 900.00, 1)
            self._insert_txn(conn, 6, '2026-03-10', -999.00, 2)
            windows = (
                CoverageWindow('HSBC_UK', '2026-01-01', '2026-02-28', 'jan-feb.pdf'),
                CoverageWindow('HSBC_UK', '2026-03-10', '2026-03-31', 'mar-partial.pdf'),
            )
            summary = cashflow_summary(conn, windows, start='2026-01-01', end='2026-03-31')

        self.assertEqual(summary.complete_month_count, 2)
        self.assertEqual(summary.partial_months, ('2026-03',))
        self.assertEqual(summary.missing_months, ())
        self.assertEqual(summary.average_income, Decimal('1100.00'))
        self.assertEqual(summary.average_expenditure, Decimal('275.00'))
        self.assertEqual(summary.average_net, Decimal('825.00'))
        self.assertEqual([m.month for m in summary.months], ['2026-01', '2026-02'])
        self.assertIn('1 partial month excluded from averages: 2026-03', summary.warnings)

    def test_cashflow_left_joins_categories_so_uncategorised_transactions_remain_visible(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self._insert_txn(conn, 1, '2026-01-05', 1000.00, 1)
            self._insert_txn(conn, 2, '2026-01-10', -250.00, 2)
            self._insert_txn(conn, 3, '2026-01-20', -75.00, None)
            self._insert_txn(conn, 4, '2026-01-21', -20.00, None, excluded=1)
            windows = (CoverageWindow('HSBC_UK', '2026-01-01', '2026-01-31', 'jan.pdf'),)
            summary = cashflow_summary(conn, windows, start='2026-01-01', end='2026-01-31')

        self.assertEqual(summary.complete_month_count, 1)
        self.assertEqual(summary.average_income, Decimal('1000.00'))
        self.assertEqual(summary.average_expenditure, Decimal('325.00'))
        self.assertEqual(summary.average_net, Decimal('675.00'))
        self.assertEqual(summary.months[0].uncategorised_count, 1)
        self.assertIn('1 uncategorised transaction included in totals', summary.warnings)

    def test_all_account_coverage_requires_each_account_not_union(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self._insert_txn(conn, 1, '2026-01-05', 1000, 1)
            self._insert_txn(conn, 2, '2026-01-20', -100, 2)
            conn.execute("UPDATE transactions SET account_id='SECOND' WHERE txn_id=2")
            windows = (CoverageWindow('HSBC_UK','2026-01-01','2026-01-15','a'),
                       CoverageWindow('SECOND','2026-01-16','2026-01-31','b'))
            summary = cashflow_summary(conn, windows, start='2026-01-01', end='2026-01-31')
        self.assertEqual(summary.complete_month_count, 0)
        self.assertIsNone(summary.average_income)
        self.assertEqual(summary.partial_months, ('2026-01',))

    def test_coverage_missing_for_transaction_account_prevents_complete_average(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self._insert_txn(conn, 1, '2026-01-05', 1000, 1)
            conn.execute("UPDATE transactions SET account_id='SECOND' WHERE txn_id=1")
            windows = (CoverageWindow('HSBC_UK','2026-01-01','2026-01-31','a'),)
            summary = cashflow_summary(conn, windows, start='2026-01-01', end='2026-01-31')
        self.assertEqual(summary.complete_month_count, 0)
        self.assertTrue(any('coverage' in w.lower() and 'SECOND' in w for w in summary.warnings))

    def test_clipped_calendar_month_is_not_a_complete_month_average(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self._insert_txn(conn, 1, '2026-01-20', 1000, 1)
            windows = (CoverageWindow('HSBC_UK','2026-01-01','2026-01-31','a'),)
            summary = cashflow_summary(conn, windows, start='2026-01-15', end='2026-01-31')
        self.assertEqual(summary.complete_month_count, 0)
        self.assertIn('2026-01', summary.partial_months)

    def test_explicit_partial_average_warns_about_incomplete_baseline(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self._insert_txn(conn, 1, '2026-01-20', 1000, 1)
            windows = (CoverageWindow('HSBC_UK','2026-01-15','2026-01-31','a'),)
            summary = cashflow_summary(conn, windows, start='2026-01-01', end='2026-01-31', include_partial=True)
        self.assertEqual(summary.average_income, Decimal('1000.00'))
        self.assertTrue(any('partial' in w.lower() and 'included' in w.lower() for w in summary.warnings))

    def test_cashflow_reports_missing_coverage_without_silent_average(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self._insert_txn(conn, 1, '2026-01-05', 1000.00, 1)
            summary = cashflow_summary(conn, (), start='2026-01-01', end='2026-01-31')

        self.assertEqual(summary.complete_month_count, 0)
        self.assertEqual(summary.average_income, None)
        self.assertEqual(summary.average_expenditure, None)
        self.assertEqual(summary.average_net, None)
        self.assertEqual(summary.missing_months, ('2026-01',))
        self.assertIn('No complete covered months available for averages', summary.warnings)


if __name__ == '__main__':
    unittest.main()
