import unittest

from scripts.services.coverage import CoverageWindow
from scripts.services.periods import (
    PeriodFilter,
    PeriodFilterError,
    complete_covered_month_ranges,
    resolve_period_filter,
)


class PeriodFilterTests(unittest.TestCase):
    def test_full_year_preset_resolves_to_calendar_year(self):
        result = resolve_period_filter(PeriodFilter(kind='year', year=2025))

        self.assertEqual(result.start, '2025-01-01')
        self.assertEqual(result.end, '2025-12-31')
        self.assertEqual(result.label, '2025 full year')
        self.assertEqual(result.method, 'transaction_date')

    def test_calendar_presets_reject_out_of_range_years_with_period_error(self):
        for kind in ('year', 'month'):
            for year in (0, -1, 10000, 1000000):
                with self.subTest(kind=kind, year=year):
                    with self.assertRaises(ValueError) as rejected:
                        resolve_period_filter(PeriodFilter(kind=kind, year=year, month=1))
                    self.assertIsInstance(rejected.exception, PeriodFilterError)

    def test_calendar_boundary_years_resolve_canonical_zero_padded_dates(self):
        for year in (1, 9999):
            with self.subTest(year=year):
                result = resolve_period_filter(PeriodFilter(kind='year', year=year))
                self.assertEqual((result.start, result.end), (f'{year:04d}-01-01', f'{year:04d}-12-31'))
                for month, last in ((1, 31), (2, 28), (12, 31)):
                    result = resolve_period_filter(PeriodFilter(kind='month', year=year, month=month))
                    self.assertEqual((result.start, result.end),
                                     (f'{year:04d}-{month:02d}-01', f'{year:04d}-{month:02d}-{last}'))

    def test_custom_range_is_inclusive_and_literal(self):
        result = resolve_period_filter(PeriodFilter(kind='custom', start='2025-04-01', end='2025-09-30'))

        self.assertEqual(result.start, '2025-04-01')
        self.assertEqual(result.end, '2025-09-30')
        self.assertEqual(result.label, '2025-04-01 to 2025-09-30')

    def test_invalid_custom_range_is_rejected(self):
        with self.assertRaises(PeriodFilterError):
            resolve_period_filter(PeriodFilter(kind='custom', start='2025-10-01', end='2025-09-30'))

    def test_complete_covered_month_ranges_ignore_partial_boundary_months(self):
        windows = [
            CoverageWindow('HSBC_UK', '2024-12-11', '2025-01-10', 'jan-statement'),
            CoverageWindow('HSBC_UK', '2025-01-11', '2025-02-10', 'feb-statement'),
            CoverageWindow('HSBC_UK', '2025-02-11', '2025-03-10', 'mar-statement'),
        ]

        ranges = complete_covered_month_ranges(windows, account_id='HSBC_UK')

        self.assertEqual([(r.start, r.end, r.label) for r in ranges], [
            ('2025-01-01', '2025-01-31', '2025-01'),
            ('2025-02-01', '2025-02-28', '2025-02'),
        ])


if __name__ == '__main__':
    unittest.main()
