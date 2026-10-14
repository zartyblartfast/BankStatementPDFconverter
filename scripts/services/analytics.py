from __future__ import annotations

from dataclasses import dataclass, field, replace
from calendar import monthrange
from decimal import Decimal, ROUND_HALF_UP, Context, localcontext
from typing import Iterable

from scripts.services.coverage import CoverageWindow, MonthCoverage, month_coverage
from scripts.services.movement_policy import read_snapshot, summarise_movement
from scripts.services.transfers import exact_pence


@dataclass(frozen=True)
class MonthlyCashFlow:
    month: str
    income: Decimal
    expenditure: Decimal
    net: Decimal
    transaction_count: int
    uncategorised_count: int
    coverage_status: str
    movement: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CashFlowSummary:
    start: str
    end: str
    complete_month_count: int
    average_income: Decimal | None
    average_expenditure: Decimal | None
    average_net: Decimal | None
    months: tuple[MonthlyCashFlow, ...]
    partial_months: tuple[str, ...]
    missing_months: tuple[str, ...]
    warnings: tuple[str, ...]


_CENT = Decimal('0.01')


def pence_decimal(pence: int) -> Decimal:
    """Exact two-place Decimal construction, independent of ambient context."""
    return Decimal(f'{"-" if pence < 0 else ""}{abs(pence)//100}.{abs(pence)%100:02d}')


def _money(value: object) -> Decimal:
    number = Decimal(str(value or '0'))
    with localcontext(Context(prec=max(28, len(number.as_tuple().digits) + abs(number.adjusted()) + 4))):
        return number.quantize(_CENT, rounding=ROUND_HALF_UP)


def _average(values: Iterable[Decimal]) -> Decimal | None:
    values = tuple(values)
    if not values:
        return None
    total = sum(exact_pence(v) for v in values)
    quotient, remainder = divmod(abs(total), len(values))
    cents = quotient + (remainder * 2 >= len(values))
    return pence_decimal(-cents if total < 0 else cents)


def _format_count(count: int, singular: str, plural: str | None = None) -> str:
    if count == 1:
        return f'1 {singular}'
    return f'{count} {plural or singular + "s"}'


def analytical_accounts(conn, windows=()):
    """All-account analytical scope includes observed, covered and registered owned accounts.

    No invented opening date or absence of transactions makes an account covered.
    """
    accounts = {r[0] for r in conn.execute('SELECT DISTINCT account_id FROM transactions')}
    accounts.update(w.account_id for w in windows)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone():
        accounts.update(r[0] for r in conn.execute('SELECT account_id FROM accounts WHERE owned=1'))
    return tuple(sorted(accounts))


def _coverage_by_month(
    windows: Iterable[CoverageWindow],
    *,
    start: str,
    end: str,
    account_id: str | None = None,
    account_ids: Iterable[str] | None = None,
) -> dict[str, MonthCoverage]:
    windows = tuple(windows)
    accounts = (account_id,) if account_id is not None else tuple(sorted(
        set(account_ids or ()) | {w.account_id for w in windows}))
    if not accounts:
        return {m.month: m for m in month_coverage((), start=start, end=end)}
    by_account = [month_coverage(windows, start=start, end=end, account_id=a) for a in accounts]
    result = {}
    for values in zip(*by_account):
        base = values[0]
        covered = min(v.covered_days for v in values)
        status = 'complete' if all(v.status == 'complete' for v in values) else (
            'missing' if not any(v.covered_days for v in values) else 'partial')
        result[base.month] = MonthCoverage(base.month, status, covered, base.total_days,
            base.total_days-covered, tuple(sorted({s for v in values for s in v.sources})))
    return result


def cashflow_summary(conn, windows: Iterable[CoverageWindow], *, start: str, end: str,
                     account_id: str | None = None, include_partial: bool = False) -> CashFlowSummary:
    """Transfer-adjusted covered-month baseline, retaining raw per-month metadata.

    Borrow an active caller snapshot unchanged, otherwise own one read transaction.
    All-account completeness requires every observed/covered account separately;
    no account opening dates are inferred.
    """
    with read_snapshot(conn=conn):
        return _cashflow_summary(conn, windows, start=start, end=end,
                                 account_id=account_id, include_partial=include_partial)


def _cashflow_summary(
    conn,
    windows: Iterable[CoverageWindow],
    *,
    start: str,
    end: str,
    account_id: str | None = None,
    include_partial: bool = False,
) -> CashFlowSummary:
    """Summarise cash flow using verified complete calendar months by default.

    Coverage decides the averaging denominator. Transaction aggregation uses left
    joins so uncategorised transactions remain visible in totals instead of being
    dropped by category joins.
    """
    windows = tuple(windows)
    accounts = (account_id,) if account_id is not None else analytical_accounts(conn, windows)
    coverage = _coverage_by_month(windows, start=start, end=end, account_id=account_id, account_ids=accounts)
    for month, cov in tuple(coverage.items()):
        year, number = map(int, month.split('-'))
        if cov.status == 'complete' and (start > month+'-01' or end < f'{month}-{monthrange(year,number)[1]:02d}'):
            coverage[month] = replace(cov, status='partial')
    params: list[object] = [start, end]
    account_clause = ''
    if account_id is not None:
        account_clause = ' AND t.account_id = ?'
        params.append(account_id)

    rows = conn.execute(
        'SELECT substr(t.txn_date,1,7),t.txn_id,t.excluded,t.subcategory_id '
        'FROM transactions t WHERE t.txn_date >= ? AND t.txn_date <= ?'
        + account_clause + ' ORDER BY t.txn_date,t.txn_id', params).fetchall()
    rows_by_month = {}
    for row in rows:
        rows_by_month.setdefault(row[0], []).append(row)
    complete: list[MonthlyCashFlow] = []
    partial_months: list[str] = []
    missing_months: list[str] = []
    uncategorised_total = 0

    policy_warnings = set()
    for month, month_cov in sorted(coverage.items()):
        matching = rows_by_month.get(month, [])
        movement = summarise_movement([r[1] for r in matching], conn=conn, include_excluded=False)
        policy_warnings.update(movement['warnings'])
        income = pence_decimal(movement['adjusted']['credits_pence'])
        expenditure = pence_decimal(movement['adjusted']['debits_pence'])
        net = pence_decimal(movement['adjusted']['net_pence'])
        txn_count = sum(not r[2] for r in matching)
        uncategorised_count = sum(not r[2] and r[3] is None for r in matching)
        monthly = MonthlyCashFlow(
            month=month,
            income=income,
            expenditure=expenditure,
            net=net,
            transaction_count=txn_count,
            uncategorised_count=uncategorised_count,
            coverage_status=month_cov.status,
            movement=movement,
        )
        if month_cov.status == 'complete' or (include_partial and month_cov.status == 'partial'):
            complete.append(monthly)
            uncategorised_total += uncategorised_count
        elif month_cov.status == 'partial':
            partial_months.append(month)
        elif month_cov.status == 'missing':
            missing_months.append(month)

    warnings: list[str] = [f'No verified statement coverage for account {a}' for a in accounts
                           if not any(w.account_id == a for w in windows)]
    if include_partial and any(m.coverage_status == 'partial' for m in complete):
        warnings.append('Partial months explicitly included in averages; this is an incomplete-coverage baseline')
    if partial_months and not include_partial:
        warnings.append(
            f'{_format_count(len(partial_months), "partial month")} excluded from averages: '
            + ', '.join(partial_months)
        )
    if missing_months:
        warnings.append(
            f'{_format_count(len(missing_months), "missing month")} excluded from averages: '
            + ', '.join(missing_months)
        )
    if not complete:
        warnings.append('No complete covered months available for averages')
    if uncategorised_total:
        warnings.append(f'{_format_count(uncategorised_total, "uncategorised transaction")} included in totals')

    warnings.extend(sorted(policy_warnings))
    return CashFlowSummary(
        start=start,
        end=end,
        complete_month_count=len(complete),
        average_income=_average(m.income for m in complete),
        average_expenditure=_average(m.expenditure for m in complete),
        average_net=_average(m.net for m in complete),
        months=tuple(complete),
        partial_months=tuple(partial_months),
        missing_months=tuple(missing_months),
        warnings=tuple(warnings),
    )
