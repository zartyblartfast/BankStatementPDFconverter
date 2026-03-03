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

logger = logging.getLogger("hsbc_parser")


def main(argv: Optional[list[str]] = None) -> int:
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

    extractor_diag: Optional[WordExtractorDiagnostics] = None
    if args.extractor == "words":
        lines, extractor_diag = parse_pdf_to_lines_words(str(pdf_path))
        if not lines:
            lines = parse_pdf_to_lines(str(pdf_path))
            extractor_diag = None
    else:
        lines = parse_pdf_to_lines(str(pdf_path))

    indexed_all_lines = _index_lines(lines)
    statement_totals = _extract_statement_totals(indexed_all_lines)
    txns, warnings, used_row_refs, recon_gaps = parse_lines_to_transactions(
        lines, source_file=str(pdf_path), account_id=args.account_id
    )

    if len(txns) < args.min_txns:
        raise RuntimeError(
            f"Only {len(txns)} transactions found (minimum: {args.min_txns}). "
            "Extraction likely failed (PDF may be scanned/image-based). OCR not implemented in v1."
        )

    write_output(
        outdir=outdir,
        base=pdf_path.stem,
        txns=txns,
        warnings=warnings,
        used_row_refs=used_row_refs,
        recon_gaps=recon_gaps,
        indexed_all_lines=indexed_all_lines,
        statement_totals=statement_totals,
        extractor_diag=extractor_diag,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
