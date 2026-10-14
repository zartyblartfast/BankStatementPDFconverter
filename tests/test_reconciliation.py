"""Synthetic amount fixtures only; never opens a ledger or statement."""
import unittest
from decimal import Decimal


def txn(amount):
    from scripts.hsbc_parser.models import Transaction
    return Transaction('synthetic.pdf', 'TEST', '2026-01-01', 'SYNTHETIC', amount, 'GBP', 1, '1:1')


class ReconciliationTests(unittest.TestCase):
    def test_absent_totals_are_unavailable_with_decimal_totals(self):
        from scripts.services.reconciliation import reconcile_transactions
        result = reconcile_transactions([txn(0.1), txn(0.2), txn(-0.1)], {})
        self.assertEqual(result.status, 'unavailable')
        self.assertEqual(result.parsed_totals['payments_in'], Decimal('0.30'))
        self.assertEqual(result.parsed_totals['payments_out'], Decimal('0.10'))
        self.assertEqual(result.to_dict()['parsed_totals']['net'], '0.20')
        self.assertEqual(result.differences, {})

    def test_full_independent_totals_pass(self):
        from scripts.services.reconciliation import reconcile_transactions
        result = reconcile_transactions([txn(100), txn(-30)], {
            'payments_in': 100, 'payments_out': 30,
            'opening_balance': -20, 'closing_balance': 50,
        })
        self.assertEqual(result.status, 'passed')
        self.assertEqual(result.differences, {
            'payments_in': Decimal('0.00'), 'payments_out': Decimal('0.00'),
            'balance_movement': Decimal('0.00'),
        })
        self.assertEqual(result.statement_totals['opening_balance'], Decimal('-20.00'))

    def test_cancelling_gross_errors_fail_despite_matching_net(self):
        from scripts.services.reconciliation import reconcile_transactions
        result = reconcile_transactions([txn(110), txn(-40)], {
            'payments_in': 100, 'payments_out': 30,
            'opening_balance': 0, 'closing_balance': 70,
        })
        self.assertEqual(result.status, 'failed')
        self.assertEqual(result.differences['balance_movement'], Decimal('0.00'))
        self.assertEqual(result.differences['payments_in'], Decimal('10.00'))
        self.assertEqual(result.differences['payments_out'], Decimal('10.00'))

    def test_partial_checks_are_review_not_complete_reconciliation(self):
        from scripts.services.reconciliation import reconcile_transactions
        for totals in ({'payments_in': 100}, {'payments_in': 100, 'payments_out': 30},
                       {'opening_balance': 0, 'closing_balance': 70},
                       {'payments_in': 100, 'payments_out': 30, 'opening_balance': 0}):
            with self.subTest(totals=totals):
                result = reconcile_transactions([txn(100), txn(-30)], totals)
                self.assertEqual(result.status, 'needs_review')
                self.assertIn('incomplete_statement_checks', result.reasons)

    def test_parser_uncertainty_requires_review_even_when_totals_match(self):
        from scripts.services.reconciliation import reconcile_transactions
        totals = {'payments_in': 100, 'payments_out': 30, 'opening_balance': 0, 'closing_balance': 70}
        for flags in ({'warnings': ['synthetic warning']}, {'recon_gaps': [{'gap': 0.01}]}):
            with self.subTest(flags=flags):
                result = reconcile_transactions([txn(100), txn(-30)], totals, **flags)
                self.assertEqual(result.status, 'needs_review')
        self.assertEqual(reconcile_transactions([txn(100)], {}, warnings=['x']).status, 'needs_review')

    def test_no_transactions_fail_even_against_zero_totals(self):
        from scripts.services.reconciliation import reconcile_transactions
        for totals in ({}, {'payments_in': 0, 'payments_out': 0, 'opening_balance': 0, 'closing_balance': 0}):
            self.assertEqual(reconcile_transactions([], totals).status, 'failed')

    def test_per_row_half_up_cents_and_one_cent_mismatch(self):
        from scripts.services.reconciliation import reconcile_transactions
        rounded = reconcile_transactions([txn(1.005), txn(-0.005)], {
            'payments_in': '1.01', 'payments_out': '0.01',
            'opening_balance': '-2.00', 'closing_balance': '-1.00',
        })
        self.assertEqual(rounded.status, 'passed')
        self.assertEqual(rounded.parsed_totals['net'], Decimal('1.00'))
        mismatch = reconcile_transactions([txn(1.00)], {'payments_in': '1.01'})
        self.assertEqual(mismatch.status, 'failed')
        self.assertEqual(mismatch.to_dict()['differences']['payments_in'], '-0.01')

    def test_nonfinite_amount_is_rejected(self):
        from scripts.services.reconciliation import reconcile_transactions
        with self.assertRaises(ValueError):
            reconcile_transactions([txn(float('nan'))], {})
