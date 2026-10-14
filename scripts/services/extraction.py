"""Read-only PDF extraction with local provenance and safe summary serialization.

The service neither creates output directories nor opens a ledger. Reader/parser
errors propagate to the caller; a parsed but unreconciled statement is a result,
not an exception. Raw lines, warnings and transactions stay in this local object.
"""
from dataclasses import dataclass, field
from decimal import Decimal
from datetime import datetime
from pathlib import Path
import re

from scripts.hsbc_parser.models import Transaction, WordExtractorDiagnostics
from scripts.hsbc_parser.exporters import _build_suspects
from scripts.hsbc_parser.extractors import parse_pdf_to_lines, parse_pdf_to_lines_words
from scripts.hsbc_parser.parser import _index_lines, _extract_statement_totals, parse_lines_to_transactions
from scripts.services.reconciliation import ReconciliationResult, reconcile_transactions


@dataclass(frozen=True)
class ExtractionResult:
    transactions: list[Transaction] = field(repr=False)
    warnings: list[str] = field(repr=False)
    used_row_refs: set[str]
    recon_gaps: list[dict[str, object]] = field(repr=False)
    indexed_all_lines: list[tuple[int, int, str]] = field(repr=False)
    statement_totals: dict[str, Decimal]
    reconciliation: ReconciliationResult
    extractor: str
    extractor_diag: WordExtractorDiagnostics | None = field(default=None, repr=False)
    coverage: dict[str, str] | None = None
    suspects: list[dict[str, object]] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict[str, object]:
        """Summary only: never serialize descriptions, raw lines or diagnostic text."""
        return {
            'transaction_count': len(self.transactions),
            'warnings_count': len(self.warnings),
            'recon_gaps_count': len(self.recon_gaps),
            'extractor': self.extractor,
            'suspects_count': len(self.suspects),
            'extractor_diagnostics': None if self.extractor_diag is None else {
                'total_pdf_words': self.extractor_diag.total_pdf_words,
                'total_output_lines': self.extractor_diag.total_output_lines,
                'y_tolerance': self.extractor_diag.y_tolerance,
                'lines_per_page': dict(self.extractor_diag.lines_per_page),
                'tokens_per_line_counts': dict(self.extractor_diag.tokens_per_line_counts),
                'suspect_merged_line_count': len(self.extractor_diag.suspect_merged_lines),
            },
            'coverage': dict(self.coverage) if self.coverage is not None else None,
            'statement_totals': {k: format(v, '.2f') for k, v in self.statement_totals.items()},
            'reconciliation': self.reconciliation.to_dict(),
        }


def _extract_coverage(indexed: list[tuple[int, int, str]], warnings: list[str]) -> dict[str, str] | None:
    """Recognize labelled periods or standalone HSBC ranges, not txn min/max."""
    coverage = None
    invalid = False

    def parse_period_dates(first: str, second: str) -> tuple[str, str]:
        def parse_with_optional_year(value: str, fallback_year: int | None = None):
            cleaned = value.strip()
            for fmt in ('%d %B %Y', '%d %b %Y'):
                try:
                    return datetime.strptime(cleaned, fmt).date()
                except ValueError:
                    pass
            if fallback_year is None:
                raise ValueError(cleaned)
            for fmt in ('%d %B', '%d %b'):
                try:
                    partial = datetime.strptime(cleaned, fmt)
                    return partial.date().replace(year=fallback_year)
                except ValueError:
                    pass
            raise ValueError(cleaned)

        end_date = parse_with_optional_year(second)
        first_has_year = bool(re.search(r'\b\d{4}\b', first))
        start_date = parse_with_optional_year(first, end_date.year)
        if start_date > end_date:
            if first_has_year:
                raise ValueError(first)
            start_date = start_date.replace(year=start_date.year - 1)
        return start_date.isoformat(), end_date.isoformat()

    for page, row, line in indexed:
        match = re.match(
            r'^Statement period\s*:?\s*(\d{1,2}\s+[A-Za-z]{3,9}(?:\s+\d{4})?)\s+to\s+(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})\b',
            line.strip(), re.I)
        if match is None:
            match = re.match(
                r'^(\d{1,2}\s+[A-Za-z]{3,9}(?:\s+\d{4})?)\s+to\s+(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})\b',
                line.strip(), re.I)
        if match is None and re.match(r'^Statement period\s*:', line.strip(), re.I):
            invalid = True
            continue
        if match:
            try:
                start, end = parse_period_dates(match[1], match[2])
            except ValueError:
                invalid = True
                continue
            if start > end or (coverage and (coverage['start'], coverage['end']) != (start, end)):
                invalid = True
            else:
                coverage = {'start': start, 'end': end, 'source_row_ref': f'{page}:{row}'}
    if invalid:
        warnings.append('Statement period invalid or conflicting; coverage unavailable')
        return None
    return coverage


def extract_statement(
    pdf_path: str | Path,
    account_id: str = 'HSBC_UK',
    extractor: str = 'words',
    source_file: str | None = None,
) -> ExtractionResult:
    if extractor not in ('words', 'text'):
        raise ValueError('Extractor must be words or text')
    diag = None
    actual_extractor = extractor
    if extractor == 'words':
        lines, diag = parse_pdf_to_lines_words(str(pdf_path))
        if not lines:
            lines = parse_pdf_to_lines(str(pdf_path))
            diag = None
            actual_extractor = 'text'
    else:
        lines = parse_pdf_to_lines(str(pdf_path))
    indexed = _index_lines(lines)
    totals = _extract_statement_totals(indexed)
    txns, warnings, used_row_refs, gaps = parse_lines_to_transactions(
        lines, source_file=str(pdf_path) if source_file is None else source_file, account_id=account_id)
    seen_totals: dict[str, float] = {}
    for row in indexed:
        for key, value in _extract_statement_totals([row]).items():
            if key in seen_totals and Decimal(str(seen_totals[key])) != Decimal(str(value)):
                warnings.append(f'Conflicting statement total: {key}; row {row[0]}:{row[1]}')
            seen_totals[key] = value
    suspects = _build_suspects(txns)
    coverage = _extract_coverage(indexed, warnings)
    reconciliation = reconcile_transactions(
        txns, totals, warnings=warnings, recon_gaps=gaps,
        suspects=suspects + (diag.suspect_merged_lines if diag is not None else []))
    return ExtractionResult(txns, warnings, used_row_refs, gaps, indexed,
                            reconciliation.statement_totals, reconciliation, actual_extractor, diag,
                            coverage=coverage,
                            suspects=suspects)
