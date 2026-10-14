"""Synthetic transfer decisions through the shared analytical/aggregate boundary."""
from contextlib import closing
from decimal import Decimal, Inexact, Rounded, localcontext
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tests import test_movement_policy as movement_fixture
from scripts.services.analytics import cashflow_summary
from scripts.services.coverage import CoverageWindow
from scripts.services.agent_analysis import AgentAnalysis
from scripts.services.transfers import prepare_pair, prepare_unmatched, commit_transfer, prepare_revoke


class TransferAnalyticsTests(unittest.TestCase):
    db: Path
    temp: tempfile.TemporaryDirectory
    setUp = movement_fixture.MovementPolicyTests.setUp

    def api(self):
        definitions = Path(self.temp.name) / 'categories.json'
        definitions.write_text('{}')
        api = object.__new__(AgentAnalysis)
        api.db, api.categories, api.allow_details = self.db, definitions, False
        return api

    def test_empty_registered_accounts_are_explicitly_missing_not_omitted(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('DELETE FROM transactions')
        coverage = self.api().get_data_status()['data']['coverage']
        self.assertEqual(len(coverage), 2)
        self.assertTrue(all(not item['coverage_available'] and item['warnings'] for item in coverage))
        self.assertTrue(all(item['start'] is None and item['end'] is None for item in coverage))

    def test_owned_account_without_records_or_coverage_blocks_all_account_average(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO accounts(account_id,display_name,currency,owned,parser_verified,import_enabled) VALUES ('EMPTY','Empty','GBP',1,0,0)")
            windows = tuple(CoverageWindow(a,'2026-01-01','2026-01-31',a) for a in ('HSBC_UK','IOM'))
            summary = cashflow_summary(conn, windows, start='2026-01-01', end='2026-01-31')
        self.assertIsNone(summary.average_income)
        self.assertTrue(any('EMPTY' in w for w in summary.warnings))
        self.assertEqual(len(self.api().get_data_status()['data']['coverage']), 3)

    def test_cashflow_confirmed_principal_removed_raw_preserved_and_fee_retained(self):
        commit_transfer(prepare_pair(1, 2, db_path=self.db), approved=True)
        windows = tuple(CoverageWindow(a, '2026-01-01', '2026-01-31', a) for a in ('HSBC_UK', 'IOM'))
        with closing(sqlite3.connect(self.db)) as conn:
            result = cashflow_summary(conn, windows, start='2026-01-01', end='2026-01-31')
            self.assertFalse(conn.in_transaction)
        self.assertEqual(result.average_income, Decimal('20.00'))
        self.assertEqual(result.average_expenditure, Decimal('1.00'))
        self.assertEqual(result.months[0].movement['raw']['credits_pence'], 3000)
        self.assertEqual(result.months[0].movement['raw']['debits_pence'], 1100)
        self.assertEqual(result.months[0].movement['transfers']['in_count'], 1)

    def test_agent_summary_adjusted_but_search_and_category_listing_are_raw(self):
        api = self.api()
        before = api.search_transactions()['data']
        result = commit_transfer(prepare_pair(1, 2, db_path=self.db), approved=True)
        summary = api.summarise_transactions(include_excluded=True)['data']
        self.assertEqual(summary['credits_pence'], 2000)
        self.assertEqual(summary['debits_pence'], 100)
        self.assertEqual(summary['raw']['credits_pence'], before['credits_pence'])
        self.assertEqual(summary['raw']['debits_pence'], before['debits_pence'])
        self.assertEqual(api.search_transactions()['data'], before)
        self.assertEqual(summary['transfers']['in_count'], 1)
        self.assertEqual(summary['groups'][0]['raw'], summary['raw'])
        self.assertEqual(api.search_transactions()['meta']['calculation_basis'], 'raw')
        self.assertEqual(api.list_categories()['meta']['calculation_basis'], 'raw')
        self.assertEqual(api.summarise_transactions()['meta']['schema_version'], '1.1')
        commit_transfer(prepare_revoke(result.group_id, db_path=self.db), approved=True)
        self.assertEqual(api.summarise_transactions(include_excluded=True)['data']['credits_pence'], before['credits_pence'])

    def test_month_counterpart_outside_scope_is_adjusted_and_warned(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE transactions SET txn_date='2026-02-01' WHERE txn_id=2")
        commit_transfer(prepare_pair(1, 2, db_path=self.db), approved=True)
        summary = self.api().summarise_transactions(start='2026-01-01', end='2026-01-31', include_excluded=True)
        self.assertEqual(summary['data']['debits_pence'], 100)
        self.assertEqual(summary['data']['transfers']['out_count'], 1)
        self.assertIn('confirmed_counterpart_outside_scope', summary['meta']['warnings'])
        self.assertIn('confirmed_transfer_different_posting_dates', summary['meta']['warnings'])

    def test_unmatched_and_excluded_policy_metadata_is_not_hidden_by_reporting_filter(self):
        api = self.api()
        commit_transfer(prepare_unmatched(1, 'IOM', db_path=self.db), approved=True)
        result = api.summarise_transactions()['data']
        self.assertEqual(result['raw']['debits_pence'], 1100)
        self.assertEqual(result['debits_pence'], 100)
        self.assertEqual(result['transfers']['unmatched_count'], 1)
        self.assertIn('explicit_unmatched_transfers', result['warnings'])
        self.assertEqual(api.search_transactions()['data']['debits_pence'], 1100)

    def test_single_account_adjusts_counterpart_outside_scope_without_other_coverage(self):
        commit_transfer(prepare_pair(1,2,db_path=self.db), approved=True)
        with closing(sqlite3.connect(self.db)) as conn:
            windows = (CoverageWindow('HSBC_UK','2026-01-01','2026-01-31','uk'),)
            result = cashflow_summary(conn, windows, start='2026-01-01', end='2026-01-31', account_id='HSBC_UK')
        self.assertEqual(result.complete_month_count, 1)
        self.assertEqual(result.average_income, Decimal('20.00'))
        self.assertEqual(result.average_expenditure, Decimal('1.00'))
        self.assertEqual(result.months[0].movement['raw']['debits_pence'], 1100)
        self.assertIn('confirmed_counterpart_outside_scope', result.warnings)

    def test_category_renaming_does_not_confirm_or_revoke_principal(self):
        api = self.api()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO categories VALUES (1,'Own Accounts')")
            conn.execute("INSERT INTO subcategories VALUES (1,1,'Internal Transfer')")
            conn.execute('UPDATE transactions SET subcategory_id=1 WHERE txn_id IN (1,2)')
        self.assertEqual(api.summarise_transactions(include_excluded=True)['data']['credits_pence'], 3000)
        commit_transfer(prepare_pair(1,2,db_path=self.db), approved=True)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE categories SET name='Salary'")
        result = api.summarise_transactions(include_excluded=True)['data']
        self.assertEqual(result['credits_pence'], 2000)
        self.assertEqual(result['debits_pence'], 100)
        self.assertEqual(api.list_categories()['data']['items'][0]['credits_pence'], 1000)

    def test_cashflow_borrows_caller_transaction_on_success_and_currency_error(self):
        windows = (CoverageWindow('HSBC_UK','2026-01-01','2026-01-31','uk'),)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('BEGIN')
            statements = []
            conn.set_trace_callback(statements.append)
            cashflow_summary(conn, windows, start='2026-01-01', end='2026-01-31', account_id='HSBC_UK')
            self.assertTrue(conn.in_transaction)
            conn.execute("UPDATE transactions SET currency='USD' WHERE txn_id=4")
            with self.assertRaisesRegex(ValueError, 'unsupported_currency'):
                cashflow_summary(conn, windows, start='2026-01-01', end='2026-01-31', account_id='HSBC_UK')
            self.assertTrue(conn.in_transaction)
            self.assertEqual(conn.execute('SELECT currency FROM transactions WHERE txn_id=4').fetchone()[0], 'USD')
            self.assertFalse(any(s.upper().startswith(('BEGIN','COMMIT','ROLLBACK')) for s in statements))
            conn.rollback()

    def test_agent_revision_and_totals_share_snapshot_during_concurrent_confirmation(self):
        from unittest.mock import patch
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
        api = self.api()
        initial = api.summarise_transactions(include_excluded=True)
        original = api._rows

        def rows_then_confirm(conn, filters, cents, include_excluded):
            rows = original(conn, filters, cents, include_excluded)
            commit_transfer(prepare_pair(1,2,db_path=self.db), approved=True)
            return rows

        with patch.object(api, '_rows', side_effect=rows_then_confirm):
            during = api.summarise_transactions(include_excluded=True)
        self.assertEqual(initial['meta']['data_revision'], during['meta']['data_revision'])
        self.assertEqual(initial['data'], during['data'])
        after = api.summarise_transactions(include_excluded=True)
        self.assertNotEqual(initial['meta']['data_revision'], after['meta']['data_revision'])
        self.assertEqual(after['data']['credits_pence'], 2000)

    def test_revision_includes_rules_and_transfer_audit_only_changes(self):
        api = self.api()
        initial = api.get_data_status()['meta']['data_revision']
        (api.categories.parent / 'category_rules.csv').write_text('synthetic rule snapshot')
        changed = api.get_data_status()['meta']['data_revision']
        self.assertNotEqual(initial, changed)
        commit_transfer(prepare_pair(1,2,db_path=self.db), approved=True)
        paired = api.get_data_status()['meta']['data_revision']
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO transfer_audit(group_id,action,preview_token,payload_signature,decision_json) VALUES (1,'pair','synthetic-extra','signature','{}')")
        self.assertNotEqual(paired, api.get_data_status()['meta']['data_revision'])

    def test_transfer_audit_and_account_changes_change_revision(self):
        api = self.api()
        initial = api.get_data_status()['meta']['data_revision']
        commit_transfer(prepare_pair(1, 2, db_path=self.db), approved=True)
        paired = api.get_data_status()['meta']['data_revision']
        self.assertNotEqual(initial, paired)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE accounts SET display_name='Renamed' WHERE account_id='IOM'")
        self.assertNotEqual(paired, api.get_data_status()['meta']['data_revision'])

    def test_average_rounds_half_up_with_integer_arithmetic_under_trapped_context(self):
        from scripts.services.analytics import _average
        with localcontext() as ctx:
            ctx.prec = 3
            ctx.traps[Inexact] = ctx.traps[Rounded] = True
            self.assertEqual(_average((Decimal('10.01'),Decimal('10.02'))), Decimal('10.02'))
            self.assertEqual(_average((Decimal('-10.01'),Decimal('-10.02'))), Decimal('-10.02'))

    def test_analytical_money_ignores_low_decimal_context(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE transactions SET amount=20.03 WHERE txn_id=4')
            conn.execute('UPDATE transactions SET amount=-1.02 WHERE txn_id=3')
        with localcontext() as ctx:
            ctx.prec = 3
            ctx.traps[Inexact] = ctx.traps[Rounded] = True
            raw = self.api().search_transactions()['data']
            windows = tuple(CoverageWindow(a, '2026-01-01', '2026-01-31', a) for a in ('HSBC_UK', 'IOM'))
            with closing(sqlite3.connect(self.db)) as conn:
                summary = cashflow_summary(conn, windows, start='2026-01-01', end='2026-01-31')
        self.assertEqual(raw['credits_pence'], 3003)
        self.assertEqual(summary.average_income, Decimal('30.03'))
        self.assertEqual(summary.average_expenditure, Decimal('1.02'))
