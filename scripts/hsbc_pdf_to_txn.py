"""HSBC UK PDF Statement Converter — CLI entry point.

This module is a thin facade that re-exports the public API from the
hsbc_parser sub-package for backward compatibility, and provides the
main() CLI function with structured logging.

The actual implementation lives in:
  scripts/hsbc_parser/models.py      — data models
  scripts/hsbc_parser/patterns.py    — regex patterns & constants
  scripts/hsbc_parser/extractors.py  — PDF → lines
  scripts/hsbc_parser/parser.py      — lines → transactions
  scripts/hsbc_parser/exporters.py   — file writers & diagnostics
"""

import argparse
import logging
from pathlib import Path
from typing import Optional

# ── Re-exports for backward compatibility ───────────────────
from scripts.hsbc_parser.models import Transaction, WordExtractorDiagnostics  # noqa: F401
from scripts.hsbc_parser.patterns import (  # noqa: F401
    _CREDIT_PREFIXES,
    _DATE_RE,
    _FX_RATE_LINE_RE,
    _MIN_TXN_COUNT_DEFAULT,
    _NON_STERLING_RE,
    _RECONCILIATION_TOLERANCE,
    _TXN_PREFIX_RE,
)
from scripts.hsbc_parser.extractors import (  # noqa: F401
    parse_pdf_to_lines,
    parse_pdf_to_lines_words,
)
from scripts.hsbc_parser.parser import (  # noqa: F401
    _extract_statement_totals,
    _find_money_tokens_from_end,
    _index_lines,
    _interpret_money_tokens,
    _is_noise_line,
    _lookahead_sign_correction,
    _looks_like_balance_marker,
    _looks_like_credit,
    _parse_money_token,
    _repair_split_money_tokens,
    _truncate_description,
    parse_lines_to_transactions,
)
from scripts.hsbc_parser.exporters import write_output  # noqa: F401
from scripts.services.extraction import extract_statement

logger = logging.getLogger("hsbc_parser")


def main(argv: Optional[list[str]] = None) -> int:
    """Exit 0: reconciled; 1: extraction/failure; 2: review/unavailable evidence.

    Explicit exports retain their legacy schema. Review/unavailable exports are
    diagnostics, not verified import candidates; failed results are not exported.
    """
    p = argparse.ArgumentParser(
        description="Convert an HSBC UK statement PDF into normalized transactions.",
    )
    p.add_argument("--pdf", required=True, help="path to the HSBC statement PDF")
    p.add_argument("--outdir", default="finance/exports", help="output directory (default: finance/exports)")
    p.add_argument("--account-id", default="HSBC_UK", help="account identifier (default: HSBC_UK)")
    p.add_argument("--extractor", choices=["text", "words"], default="words", help="PDF extraction method (default: words)")
    p.add_argument("--min-txns", type=int, default=_MIN_TXN_COUNT_DEFAULT,
                   help=f"minimum transactions expected; fewer raises an error (default: {_MIN_TXN_COUNT_DEFAULT})")
    p.add_argument("--verbose", "-v", action="store_true", help="enable debug-level logging")
    p.add_argument("--quiet", "-q", action="store_true", help="suppress info-level output (errors only)")

    args = p.parse_args(argv)

    # ── Configure logging ───────────────────────────────────
    if args.verbose:
        level = logging.DEBUG
    elif args.quiet:
        level = logging.ERROR
    else:
        level = logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[logging.StreamHandler()],
    )

    pdf_path = Path(args.pdf)
    if pdf_path.suffix.lower() != ".pdf":
        logger.error("Error: expected a .pdf file, got '%s' — %s", pdf_path.suffix, pdf_path)
        return 1
    if not pdf_path.exists():
        logger.error("Error: file not found — %s", pdf_path)
        return 1
    outdir = Path(args.outdir)

    try:
        result = extract_statement(pdf_path, account_id=args.account_id, extractor=args.extractor)
    except Exception as exc:
        # PDF libraries expose several exception types. Do not echo raw parser
        # content or a traceback into a dashboard/batch runner's error stream.
        logger.error('Error: PDF extraction failed (%s)', type(exc).__name__)
        return 1
    txns = result.transactions
    reconciliation = result.reconciliation
    if not args.quiet:
        metrics = {
            'transaction_count': len(txns),
            'total_credits': format(reconciliation.parsed_totals['payments_in'], '.2f'),
            'total_debits': format(-reconciliation.parsed_totals['payments_out'], '.2f'),
            'net': format(reconciliation.parsed_totals['net'], '.2f'),
            'any_parse_warnings_count': len(result.warnings),
            'suspects_count': len(result.suspects),
            'reconciliation_status': reconciliation.status,
        }
        for evidence, metric in (
            ('payments_in', 'diff_credits_vs_statement_in'),
            ('payments_out', 'diff_debits_vs_statement_out'),
            ('balance_movement', 'diff_txn_delta_vs_statement_delta'),
        ):
            if evidence in reconciliation.differences:
                metrics[metric] = format(reconciliation.differences[evidence], '.2f')
        for key, value in metrics.items():
            print(f'{key}: {value}')

    if len(txns) < args.min_txns:
        logger.error('Error: only %d transactions found (minimum: %d); extraction requires review',
                     len(txns), args.min_txns)
        return 1
    if reconciliation.status == 'failed':
        logger.error('Error: statement reconciliation failed; no exports written')
        return 1

    try:
        write_output(
            outdir=outdir,
            base=pdf_path.stem,
            txns=txns,
            warnings=result.warnings,
            used_row_refs=result.used_row_refs,
            recon_gaps=result.recon_gaps,
            indexed_all_lines=result.indexed_all_lines,
            statement_totals={k: float(v) for k, v in result.statement_totals.items()},
            extractor_diag=result.extractor_diag,
        )
    except OSError as exc:
        logger.error('Error: export failed (%s); partial diagnostic files may exist', type(exc).__name__)
        return 1

    return 0 if reconciliation.status == 'passed' else 2


if __name__ == "__main__":
    raise SystemExit(main())
