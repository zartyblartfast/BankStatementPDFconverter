"""Data models for the HSBC PDF parser."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Transaction:
    source_file: str
    account_id: str
    txn_date: str
    description_raw: str
    amount: float
    currency: str
    page: int
    row_ref: str


@dataclass
class WordExtractorDiagnostics:
    """Runtime diagnostics for parse_pdf_to_lines_words.

    These are not tests — they surface when y_tolerance grouping
    produces suspicious output (e.g. merging columns that should be
    separate lines).
    """

    y_tolerance: float = 0.0
    total_pdf_words: int = 0
    total_output_lines: int = 0
    lines_per_page: dict[int, int] = field(default_factory=dict)
    tokens_per_line_counts: dict[int, int] = field(default_factory=dict)
    suspect_merged_lines: list[dict[str, object]] = field(default_factory=list)


@dataclass
class _PendingTxn:
    txn_date: str
    desc_parts: list[str] = field(default_factory=list)
    page: int = 0
    row_ref: str = ""
    original_line: str = ""


@dataclass
class _MoneyInterpretation:
    amount: Optional[float] = None
    parsed_balance: Optional[float] = None
    warnings: list[str] = field(default_factory=list)
    recon_gaps: list[dict[str, object]] = field(default_factory=list)
