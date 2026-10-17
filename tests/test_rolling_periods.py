"""Calendar rolling ranges; all clocks and ledgers are synthetic."""
from datetime import date
import unittest
from scripts.services.periods import PeriodFilter, PeriodFilterError, resolve_period_filter


class RollingPeriodTests(unittest.TestCase):
    def test_relative_presets_remain_transaction_calendar_even_for_statement_method(self):
        for kind in ('this_month', 'last_month', 'last_3_months', 'last_quarter'):
            result = resolve_period_filter(PeriodFilter(kind, method='statement_period'), today=date(2026, 10, 10))
            self.assertEqual(result.method, 'transaction_date')
        with self.assertRaises(PeriodFilterError):
            resolve_period_filter(PeriodFilter('last_month', method='unknown'), today=date(2026, 10, 10))
        fixed = PeriodFilter('custom', start='2024-02-02', end='2024-02-29')
        self.assertEqual(resolve_period_filter(fixed, today=date(2026, 1, 1)),
                         resolve_period_filter(fixed, today=date(2027, 1, 1)))

    def test_relative_identity_resolves_against_a_later_clock(self):
        preset = PeriodFilter('last_month')
        first = resolve_period_filter(preset, today=date(2026, 12, 31))
        later = resolve_period_filter(preset, today=date(2027, 1, 1))
        self.assertEqual((first.start, first.end), ('2026-11-01', '2026-11-30'))
        self.assertEqual((later.start, later.end), ('2026-12-01', '2026-12-31'))

    def test_dashboard_relative_context_freezes_iframe_range(self):
        from scripts import dashboard
        from unittest.mock import patch
        resolver = resolve_period_filter
        for kind in ('this_month', 'last_month', 'last_3_months', 'last_quarter'):
            with patch.object(dashboard, 'resolve_period_filter',
                              side_effect=lambda p: resolver(p, today=date(2026, 2, 17))):
                result = dashboard._report_period_context({'period': kind, 'year': '1999', 'start': '1900-01-01'})
            expected = resolver(PeriodFilter(kind), today=date(2026, 2, 17))
            self.assertIsNone(result['error'])
            self.assertEqual(result['kind'], kind)
            self.assertEqual((result['start'], result['end']), (expected.start, expected.end))
            self.assertEqual(result['query'], f'period=custom&start={expected.start}&end={expected.end}')

    def test_unrepresentable_ranges_raise_domain_error_not_overflow(self):
        for kind in ('last_month', 'last_3_months', 'last_quarter'):
            with self.subTest(kind=kind), self.assertRaises(PeriodFilterError):
                resolve_period_filter(PeriodFilter(kind), today=date.min)
        with self.assertRaises(PeriodFilterError):
            resolve_period_filter(PeriodFilter('month', year=2026, month=13))
        for clock in (date.min, date.max):
            result = resolve_period_filter(PeriodFilter('this_month'), today=clock)
            self.assertEqual(result.end, clock.isoformat())
        result = resolve_period_filter(PeriodFilter('last_month'), today=date.max)
        self.assertEqual((result.start, result.end), ('9999-11-01', '9999-11-30'))

    def test_last_quarter_is_previous_completed_calendar_block(self):
        for month, expected in [(1, ('2025-10-01', '2025-12-31')),
                                (4, ('2026-01-01', '2026-03-31')),
                                (7, ('2026-04-01', '2026-06-30')),
                                (10, ('2026-07-01', '2026-09-30'))]:
            for offset in (0, 1, 2):
                result = resolve_period_filter(PeriodFilter('last_quarter'), today=date(2026, month + offset, 1))
                self.assertEqual((result.start, result.end), expected)

    def test_current_month_stops_today(self):
        result = resolve_period_filter(PeriodFilter('this_month'), today=date(2024, 2, 17))
        self.assertEqual((result.start, result.end, result.label),
                         ('2024-02-01', '2024-02-17', 'This month so far'))

    def test_three_full_months_excludes_current_month(self):
        for today, expected in [(date(2026, 2, 17), ('2025-11-01', '2026-01-31')),
                                (date(2024, 5, 1), ('2024-02-01', '2024-04-30'))]:
            result = resolve_period_filter(PeriodFilter('last_3_months'), today=today)
            self.assertEqual((result.start, result.end), expected)

    def test_last_month_rolls_over_year_and_leap_day(self):
        for today, expected in [(date(2026, 1, 10), ('2025-12-01', '2025-12-31')),
                                (date(2024, 3, 10), ('2024-02-01', '2024-02-29'))]:
            result = resolve_period_filter(PeriodFilter('last_month'), today=today)
            self.assertEqual((result.start, result.end), expected)
            self.assertEqual(result.method, 'transaction_date')
