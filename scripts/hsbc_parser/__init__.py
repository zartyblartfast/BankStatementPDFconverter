"""HSBC UK PDF statement parser — modular package.

Public API re-exported here for convenience.
"""

from scripts.hsbc_parser.models import (
    Transaction,
    WordExtractorDiagnostics,
)
from scripts.hsbc_parser.patterns import (
    _CREDIT_PREFIXES,
    _DATE_RE,
    _FX_RATE_LINE_RE,
    _MIN_TXN_COUNT_DEFAULT,
    _NON_STERLING_RE,
    _RECONCILIATION_TOLERANCE,
    _TXN_PREFIX_RE,
)
from scripts.hsbc_parser.extractors import (
    parse_pdf_to_lines,
    parse_pdf_to_lines_words,
)
from scripts.hsbc_parser.parser import (
    _find_money_tokens_from_end,
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
from scripts.hsbc_parser.exporters import (
    write_output,
)

__all__ = [
    "Transaction",
    "WordExtractorDiagnostics",
    "parse_pdf_to_lines",
    "parse_pdf_to_lines_words",
    "parse_lines_to_transactions",
    "write_output",
    # Private helpers re-exported for tests / backward compat
    "_find_money_tokens_from_end",
    "_interpret_money_tokens",
    "_is_noise_line",
    "_lookahead_sign_correction",
    "_looks_like_balance_marker",
    "_looks_like_credit",
    "_parse_money_token",
    "_repair_split_money_tokens",
    "_truncate_description",
    "_CREDIT_PREFIXES",
    "_DATE_RE",
    "_FX_RATE_LINE_RE",
    "_MIN_TXN_COUNT_DEFAULT",
    "_NON_STERLING_RE",
    "_RECONCILIATION_TOLERANCE",
    "_TXN_PREFIX_RE",
]
