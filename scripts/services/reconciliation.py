"""Cent-exact reconciliation against independently extracted statement evidence."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping, Literal

from scripts.hsbc_parser.models import Transaction

Status = Literal['passed', 'failed', 'unavailable', 'needs_review']
Money = Decimal | float | int | str
CENT = Decimal('0.01')


def _money(value: Money) -> Decimal:
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError('Money must be finite')
    return number.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class ReconciliationResult:
    """Amounts are Decimal internally and fixed-point strings at the JSON boundary."""
    status: Status
    parsed_totals: dict[str, Decimal]
    statement_totals: dict[str, Decimal]
    differences: dict[str, Decimal]
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            'status': self.status,
            'parsed_totals': {k: format(v, '.2f') for k, v in self.parsed_totals.items()},
            'statement_totals': {k: format(v, '.2f') for k, v in self.statement_totals.items()},
            'differences': {k: format(v, '.2f') for k, v in self.differences.items()},
            'reasons': list(self.reasons),
        }


def reconcile_transactions(
    txns: Iterable[Transaction],
    statement_totals: Mapping[str, Money],
    *,
    warnings: Iterable[str] = (),
    recon_gaps: Iterable[Mapping[str, object]] = (),
    suspects: Iterable[Mapping[str, object]] = (),
) -> ReconciliationResult:
    """Quantize each input to cents using ROUND_HALF_UP before summing.

    Differences are parsed minus statement, with payments_out a positive gross
    debit total. Passing requires both gross totals and opening/closing movement;
    a net-only or partial check never passes. Missing evidence is unavailable,
    parser uncertainty needs review, and definite mismatches/no rows fail.
    'passed' certifies these amount checks, not verified date coverage or the
    absence of equal-and-opposite missing rows. Coverage is separate evidence.
    """
    amounts = [_money(t.amount) for t in txns]
    credits = sum((a for a in amounts if a > 0), Decimal('0.00'))
    debits = -sum((a for a in amounts if a < 0), Decimal('0.00'))
    parsed = {'payments_in': credits, 'payments_out': debits, 'net': credits - debits}
    evidence = {k: _money(v) for k, v in statement_totals.items()
                if k in ('payments_in', 'payments_out', 'opening_balance', 'closing_balance')}
    differences = {k: parsed[k] - evidence[k]
                   for k in ('payments_in', 'payments_out') if k in evidence}
    if 'opening_balance' in evidence and 'closing_balance' in evidence:
        differences['balance_movement'] = parsed['net'] - (
            evidence['closing_balance'] - evidence['opening_balance'])
    status: Status = 'passed' if differences else 'unavailable'
    reasons = () if differences else ('no_statement_checks',)
    if differences and len(differences) < 3:
        status = 'needs_review'
        reasons = ('incomplete_statement_checks',)
    uncertainty = []
    if tuple(warnings):
        uncertainty.append('parser_warnings')
    if tuple(recon_gaps):
        uncertainty.append('parser_reconciliation_gaps')
    if tuple(suspects):
        uncertainty.append('suspect_rows')
    if uncertainty:
        status = 'needs_review'
        reasons += tuple(uncertainty)
    if any(differences.values()):
        status = 'failed'
        reasons += ('statement_mismatch',)
    if not amounts:
        status = 'failed'
        reasons += ('no_transactions',)
    return ReconciliationResult(status, parsed, evidence, differences, reasons)
