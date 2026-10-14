from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable


@dataclass(frozen=True)
class CoverageWindow:
    account_id: str
    start: str
    end: str
    source: str


@dataclass(frozen=True)
class MonthCoverage:
    month: str
    status: str
    covered_days: int
    total_days: int
    missing_days: int
    sources: tuple[str, ...]


def _parse_day(value: str) -> date:
    return date.fromisoformat(value)


def _month_start(value: date) -> date:
    return date(value.year, value.month, 1)


def _next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _iter_month_starts(start: date, end: date):
    current = _month_start(start)
    last = _month_start(end)
    while current <= last:
        yield current
        if current == last:
            break
        current = _next_month(current)


def month_coverage(
    windows: Iterable[CoverageWindow],
    *,
    start: str,
    end: str,
    account_id: str | None = None,
) -> tuple[MonthCoverage, ...]:
    """Return calendar-month coverage from inclusive source coverage windows.

    This deliberately tracks days, not transactions: a no-spend covered day must
    count as covered, while a month with one missing source day is not complete.
    """
    range_start = _parse_day(start)
    range_end = _parse_day(end)
    if range_end < range_start:
        raise ValueError('end must be on or after start')

    parsed_windows = []
    for window in windows:
        if account_id is not None and window.account_id != account_id:
            continue
        window_start = _parse_day(window.start)
        window_end = _parse_day(window.end)
        if window_end < window_start:
            raise ValueError('coverage window end must be on or after start')
        parsed_windows.append((window_start, window_end, window.source))

    result: list[MonthCoverage] = []
    for month_start in _iter_month_starts(range_start, range_end):
        month_end = date(
            month_start.year,
            month_start.month,
            calendar.monthrange(month_start.year, month_start.month)[1],
        )
        total_days = (month_end - month_start).days + 1

        covered: set[date] = set()
        sources: set[str] = set()
        for window_start, window_end, source in parsed_windows:
            overlap_start = max(month_start, window_start)
            overlap_end = min(month_end, window_end)
            if overlap_end < overlap_start:
                continue
            sources.add(source)
            current = overlap_start
            while current <= overlap_end:
                covered.add(current)
                if current == overlap_end:
                    break
                current += timedelta(days=1)

        covered_days = len(covered)
        missing_days = total_days - covered_days
        if covered_days == 0:
            status = 'missing'
        elif missing_days == 0:
            status = 'complete'
        else:
            status = 'partial'
        result.append(MonthCoverage(
            month=f'{month_start:%Y-%m}',
            status=status,
            covered_days=covered_days,
            total_days=total_days,
            missing_days=missing_days,
            sources=tuple(sorted(sources)),
        ))
    return tuple(result)


def coverage_windows_from_db(conn, *, account_id: str | None = None) -> tuple[CoverageWindow, ...]:
    """Read trusted, reconciled statement coverage windows from source metadata."""
    sql = (
        "SELECT account_id, coverage_start, coverage_end, document_key "
        "FROM source_documents "
        "WHERE reconciliation_status = 'passed' "
        "AND coverage_start IS NOT NULL AND coverage_end IS NOT NULL"
    )
    params: tuple[str, ...] = ()
    if account_id is not None:
        sql += " AND account_id = ?"
        params = (account_id,)
    sql += " ORDER BY account_id, coverage_start, coverage_end, document_key"

    windows: list[CoverageWindow] = []
    for row in conn.execute(sql, params):
        windows.append(CoverageWindow(row[0], row[1], row[2], row[3]))
    return tuple(windows)
