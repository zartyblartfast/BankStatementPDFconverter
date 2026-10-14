"""Shared period filter helpers for reporting and planning.

These helpers are intentionally generic: they know about calendar periods,
custom date ranges and verified coverage windows, but they do not encode any
user-specific life events or bank-specific statement details.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from scripts.services.coverage import CoverageWindow, month_coverage


class PeriodFilterError(ValueError):
    """Raised when a requested period filter is invalid or incomplete."""


@dataclass(frozen=True)
class PeriodFilter:
    kind: str
    start: str | None = None
    end: str | None = None
    year: int | None = None
    month: int | None = None
    method: str = 'transaction_date'


@dataclass(frozen=True)
class ResolvedPeriod:
    start: str
    end: str
    label: str
    method: str = 'transaction_date'


def _parse_day(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise PeriodFilterError(f'Invalid {field} date') from exc


def _iso(day: date) -> str:
    return day.isoformat()


def _last_day_of_month(year: int, month: int) -> date:
    if not 1 <= month <= 12:
        raise PeriodFilterError('Invalid month')
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def resolve_period_filter(period: PeriodFilter) -> ResolvedPeriod:
    """Resolve a generic period request to an inclusive date range."""
    if period.method not in {'transaction_date', 'statement_period'}:
        raise PeriodFilterError('Invalid period method')
    if period.kind == 'custom':
        if not period.start or not period.end:
            raise PeriodFilterError('Custom period requires start and end')
        start = _parse_day(period.start, 'start')
        end = _parse_day(period.end, 'end')
        if start > end:
            raise PeriodFilterError('Start date must not be after end date')
        return ResolvedPeriod(_iso(start), _iso(end), f'{_iso(start)} to {_iso(end)}', period.method)
    if period.kind == 'year':
        if period.year is None or not 1 <= period.year <= 9999:
            raise PeriodFilterError('Year period requires a year in 1..9999')
        return ResolvedPeriod(f'{period.year:04d}-01-01', f'{period.year:04d}-12-31',
                              f'{period.year:04d} full year', period.method)
    if period.kind == 'month':
        if period.year is None or period.month is None or not 1 <= period.year <= 9999:
            raise PeriodFilterError('Month period requires a year in 1..9999 and month')
        start = date(period.year, period.month, 1)
        end = _last_day_of_month(period.year, period.month)
        return ResolvedPeriod(_iso(start), _iso(end), f'{period.year:04d}-{period.month:02d}', period.method)
    raise PeriodFilterError('Unsupported period kind')


def complete_covered_month_ranges(
    windows: list[CoverageWindow] | tuple[CoverageWindow, ...],
    *,
    account_id: str | None = None,
) -> list[ResolvedPeriod]:
    """Return calendar months fully covered by verified source windows."""
    filtered = [w for w in windows if account_id is None or w.account_id == account_id]
    if not filtered:
        return []
    months = month_coverage(
        filtered,
        start=min(w.start for w in filtered),
        end=max(w.end for w in filtered),
        account_id=account_id,
    )
    return [
        resolve_period_filter(PeriodFilter(kind='month', year=int(m.month[:4]), month=int(m.month[5:7])))
        for m in months
        if m.status == 'complete'
    ]
