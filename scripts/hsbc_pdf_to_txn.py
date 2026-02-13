import argparse
import csv
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional


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


_RECONCILIATION_TOLERANCE = 0.01
_DEFAULT_Y_TOLERANCE = 2.5
_SUSPECT_MERGED_TOKEN_THRESHOLD = 6


def _import_pdfplumber():
    """Lazy import with a clear error message."""
    try:
        import pdfplumber  # type: ignore
        return pdfplumber
    except ModuleNotFoundError as e:
        raise ModuleNotFoundError(
            "Missing dependency 'pdfplumber'. Install requirements.txt before running PDF extraction."
        ) from e


_DATE_RE = re.compile(
    r"^\s*(?P<date>(?:\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2})|(?:\d{1,2}/\d{1,2}/\d{2,4}))\s+(?P<rest>.+?)\s*$"
)

_DATE_RANGE_FOOTER_RE = re.compile(
    r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\s+to\s+\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b",
    re.IGNORECASE,
)

_MONEY_TOKEN_RE = re.compile(
    r"^(?:£)?\(?-?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}\)?-?$"
)

_AMOUNT_IN_LINE_RE = re.compile(r"(?:£)?\(?-?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}\)?-?")

_DEBUG_AMOUNT_IN_LINE_RE = re.compile(
    r"(?:£\s*)?\(?-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?\)?-?"
)

_DEBUG_KEYWORD_LINE_RE = re.compile(
    r"\b(charge|charges|fee|fees|interest|overdraft|service)\b", re.IGNORECASE
)

_TXN_PREFIX_RE = re.compile(
    r"^(?:DD|VIS|BP|CR|SO|ATM|DR|FPI|FPO|TFR|TRF|BGC|CHG|CHARGE|CASH|DEP|POS|CARD|OBP)\b|^\)\)\)",
    re.IGNORECASE,
)

_FX_RATE_LINE_RE = re.compile(
    r"^[A-Z]{3}\s+[\d,.]+\.\d{2}\s+@\s+",
)

_NON_STERLING_RE = re.compile(r"^(CR|DR)\s+Non-Sterling\b", re.IGNORECASE)

_NOISE_LINE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"^contact\s+tel\b", re.IGNORECASE),
    re.compile(r"^see\s+reverse\b", re.IGNORECASE),
    re.compile(r"^text\s+phone\b", re.IGNORECASE),
    re.compile(r"^used\s+by\s+deaf\b", re.IGNORECASE),
    re.compile(r"^www\.hsbc\.co\.uk\b", re.IGNORECASE),
    re.compile(r"^your\s+statement\b", re.IGNORECASE),
    re.compile(r"^your\s+hsbc\b", re.IGNORECASE),
    re.compile(r"^account\s+name\b.*sort\s*code\b", re.IGNORECASE),
    re.compile(r"^international\s+bank\s+account\s+number\b", re.IGNORECASE),
    re.compile(r"^bank\s+identifier\s+code\b", re.IGNORECASE),
    re.compile(r"^account\s+summary\b", re.IGNORECASE),
    re.compile(r"^opening\s*balance\b", re.IGNORECASE),
    re.compile(r"^closing\s*balance\b", re.IGNORECASE),
    re.compile(r"^payments\s+in\b", re.IGNORECASE),
    re.compile(r"^payments\s+out\b", re.IGNORECASE),
    re.compile(r"^arranged\s*overdraft\s*limit\b", re.IGNORECASE),
    re.compile(r"^po\s+box\b", re.IGNORECASE),
    re.compile(r"^date\s+payment\s+type\b", re.IGNORECASE),
    re.compile(r"^sheet\s+number\b", re.IGNORECASE),
    re.compile(r"^mr\s+\w", re.IGNORECASE),
    re.compile(r"^information\s+about\s+the\s+financial\s+services\s+compensation\s+scheme\b", re.IGNORECASE),
    re.compile(r"\bfinancial\s+services\s+compensation\s+scheme\b", re.IGNORECASE),
    re.compile(r"\bfscs\b", re.IGNORECASE),
    re.compile(r"^interest\s+and\s+charges\b", re.IGNORECASE),
    re.compile(r"^overdrafts\b", re.IGNORECASE),
    re.compile(r"^additional\s+information\b", re.IGNORECASE),
    re.compile(r"^aer\b", re.IGNORECASE),
    re.compile(r"^ear\b", re.IGNORECASE),
]

_SUSPECT_NOISE_TOKENS = [
    "PO Box",
    "www.hsbc.co.uk",
    "Your Statement",
    "Account Name",
    "Sortcode",
    "Sheet Number",
    "International Bank Account Number",
]

_DESCRIPTION_TRUNCATE_KEYWORDS = [
    "BALANCECARRIEDFORWARD",
    "BALANCEBROUGHTFORWARD",
]


def _is_noise_line(line: str) -> bool:
    s = line.strip()
    if s == "":
        return True
    if _DATE_RANGE_FOOTER_RE.search(s):
        return True
    return any(p.search(s) for p in _NOISE_LINE_PATTERNS)


def _truncate_description(description: str) -> str:
    d = description.strip()
    m = _DATE_RANGE_FOOTER_RE.search(d)
    if m:
        d = d[: m.start()].strip()

    upper = d.upper()
    cut: Optional[int] = None
    for kw in _DESCRIPTION_TRUNCATE_KEYWORDS:
        idx = upper.find(kw)
        if idx != -1:
            cut = idx if cut is None else min(cut, idx)
    if cut is None:
        return d
    return d[:cut].strip()


def _looks_like_credit(description: str) -> bool:
    d = description.strip()
    return d.startswith("CR ") or " CR " in (" " + d + " ")


def _index_lines(lines: Iterable[tuple[int, str]]) -> list[tuple[int, int, str]]:
    indexed: list[tuple[int, int, str]] = []
    line_counter_by_page: dict[int, int] = {}
    for page, text in lines:
        line_counter_by_page[page] = line_counter_by_page.get(page, 0) + 1
        indexed.append((page, line_counter_by_page[page], text))
    return indexed


def _extract_first_money_token(text: str) -> Optional[str]:
    m = re.search(r"£?\(?-?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}\)?-?", text)
    if not m:
        return None
    return m.group(0)


def _extract_statement_totals(indexed_lines: list[tuple[int, int, str]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for _, _, line in indexed_lines:
        s = line.strip()
        low = s.lower()
        norm = re.sub(r"[^a-z0-9]", "", low)
        if low.startswith("payments in"):
            tok = _extract_first_money_token(s)
            if tok:
                totals["payments_in"] = abs(_parse_money_token(tok))
        elif low.startswith("payments out"):
            tok = _extract_first_money_token(s)
            if tok:
                totals["payments_out"] = abs(_parse_money_token(tok))
        elif norm.startswith("openingbalance"):
            tok = _extract_first_money_token(s)
            if tok:
                totals["opening_balance"] = _parse_money_token(tok)
        elif norm.startswith("closingbalance"):
            tok = _extract_first_money_token(s)
            if tok:
                totals["closing_balance"] = _parse_money_token(tok)
    return totals


def parse_pdf_to_lines(pdf_path: str) -> list[tuple[int, str]]:
    pdf_file = Path(pdf_path)
    if not pdf_file.exists():
        resolved = pdf_file.resolve()
        cwd = Path.cwd()
        raise FileNotFoundError(
            f"PDF not found: {pdf_file} (resolved: {resolved}). Current working directory: {cwd}"
        )

    pdfplumber = _import_pdfplumber()

    lines: list[tuple[int, str]] = []
    with pdfplumber.open(str(pdf_file)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            for raw_line in text.splitlines():
                line = raw_line.rstrip("\n").strip("\r")
                if line.strip() == "":
                    continue
                lines.append((i, line))
    return lines


def parse_pdf_to_lines_words(
    pdf_path: str,
    *,
    y_tolerance: float = _DEFAULT_Y_TOLERANCE,
) -> tuple[list[tuple[int, str]], WordExtractorDiagnostics]:
    pdf_file = Path(pdf_path)
    if not pdf_file.exists():
        resolved = pdf_file.resolve()
        cwd = Path.cwd()
        raise FileNotFoundError(
            f"PDF not found: {pdf_file} (resolved: {resolved}). Current working directory: {cwd}"
        )

    pdfplumber = _import_pdfplumber()

    diag = WordExtractorDiagnostics(y_tolerance=y_tolerance)

    out_lines: list[tuple[int, str]] = []
    with pdfplumber.open(str(pdf_file)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            words = page.extract_words() or []
            if not words:
                continue
            diag.total_pdf_words += len(words)

            try:
                words_sorted = sorted(words, key=lambda w: (float(w.get("top", 0.0)), float(w.get("x0", 0.0))))
            except (TypeError, ValueError):
                words_sorted = words

            groups: list[tuple[float, list[dict[str, object]]]] = []
            for w in words_sorted:
                top_v = w.get("top")
                try:
                    top = float(top_v) if top_v is not None else 0.0
                except (TypeError, ValueError):
                    top = 0.0

                if not groups:
                    groups.append((top, [w]))
                    continue

                prev_top, prev_words = groups[-1]
                if abs(top - prev_top) <= y_tolerance:
                    prev_words.append(w)
                else:
                    groups.append((top, [w]))

            page_line_count = 0
            for _top, gwords in groups:
                try:
                    gwords_sorted = sorted(gwords, key=lambda w: float(w.get("x0", 0.0)))
                except (TypeError, ValueError):
                    gwords_sorted = gwords

                texts: list[str] = []
                for w in gwords_sorted:
                    t = str(w.get("text", "")).strip()
                    if t:
                        texts.append(t)
                if not texts:
                    continue

                line = " ".join(texts).strip()
                if not line:
                    continue

                out_lines.append((i, line))
                page_line_count += 1
                token_count = len(texts)
                diag.tokens_per_line_counts[token_count] = (
                    diag.tokens_per_line_counts.get(token_count, 0) + 1
                )

                # Flag lines where a balance marker keyword appears alongside
                # other substantive tokens — strong signal of column mis-merge.
                line_norm = re.sub(r"[^a-z0-9]", "", line.lower())
                has_marker = (
                    "balancecarriedforward" in line_norm
                    or "balancebroughtforward" in line_norm
                )
                if has_marker and (token_count > _SUSPECT_MERGED_TOKEN_THRESHOLD or len(line) > 80):
                    diag.suspect_merged_lines.append(
                        {"page": i, "token_count": token_count, "line": line}
                    )

            diag.lines_per_page[i] = page_line_count

    diag.total_output_lines = len(out_lines)
    return out_lines, diag


def _parse_uk_date_to_iso(date_str: str) -> str:
    s = " ".join(date_str.strip().split())

    fmts = [
        "%d %b %y",
        "%d %b %Y",
        "%d %B %y",
        "%d %B %Y",
        "%d/%m/%y",
        "%d/%m/%Y",
    ]

    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt)
            return dt.date().isoformat()
        except ValueError:
            continue

    raise ValueError(f"Unrecognized date format: {date_str!r}")


def _is_money_token(token: str) -> bool:
    t = token.strip()
    return bool(_MONEY_TOKEN_RE.match(t))


def _repair_split_money_tokens(tokens: list[str]) -> list[str]:
    repaired = tokens[:]
    i = 0
    while i < len(repaired) - 1:
        left = repaired[i]
        mid = repaired[i + 1]

        if (
            left.isdigit()
            and 1 <= len(left) <= 2
            and "," in mid
            and _is_money_token(mid)
            and _is_money_token(left + mid)
        ):
            repaired[i] = left + mid
            del repaired[i + 1]
            continue

        left_has_comma = "," in left
        left_has_dot = "." in left
        left_ends_dot = left.endswith(".")
        left_ends_comma = left.endswith(",")
        left_last_group_len: Optional[int] = None
        if left_has_comma:
            left_last_group_len = len(left.split(",")[-1])

        left_looks_like_fragment = (
            left_ends_dot
            or left_ends_comma
            or (left_has_comma and not left_has_dot and left_last_group_len in (1, 2))
        )

        if left_looks_like_fragment:
            merged2 = left + mid
            if _is_money_token(merged2):
                repaired[i] = merged2
                del repaired[i + 1]
                continue

            if i < len(repaired) - 2:
                right = repaired[i + 2]
                merged3 = left + mid + right
                if _is_money_token(merged3):
                    repaired[i] = merged3
                    del repaired[i + 1 : i + 3]
                    continue

        i += 1
    return repaired


def _lookahead_sign_correction(
    amount: float,
    current_loop_idx: int,
    indexed_lines: list[tuple[int, int, str]],
    max_lookahead: int = 3,
) -> float:
    """Correct sign of a 1-token amount using a nearby 'DR/CR Non-Sterling' line.

    In HSBC FX transactions, the line after 'Visa Rate X.XX' is typically
    'DR Non-Sterling' or 'CR Non-Sterling', which reliably indicates whether
    the parent VIS INT'L transaction was a debit or credit.  Only the
    Non-Sterling pattern is matched to avoid false positives from unrelated
    DR/CR-prefixed transactions on subsequent lines.
    """
    for ahead in range(current_loop_idx + 1,
                       min(current_loop_idx + 1 + max_lookahead, len(indexed_lines))):
        next_text = indexed_lines[ahead][2].strip()
        if not next_text or _is_noise_line(next_text) or _looks_like_header(next_text):
            continue
        m = _NON_STERLING_RE.match(next_text)
        if m:
            if m.group(1).upper() == "CR":
                return abs(amount)
            return -abs(amount)
        break  # non-empty, non-noise line without Non-Sterling — stop looking
    return amount


def _norm_desc(s: str) -> str:
    """Collapse whitespace and uppercase for loose description comparison."""
    return re.sub(r"\s+", " ", s.strip()).upper()


def _parse_money_token(token: str) -> float:
    t = token.strip()
    is_negative = False

    if t.startswith("(") and t.endswith(")"):
        is_negative = True
        t = t[1:-1].strip()

    if t.endswith("-"):
        is_negative = True
        t = t[:-1].strip()

    if t.startswith("-"):
        is_negative = True
        t = t[1:].strip()

    if t.startswith("£"):
        t = t[1:].strip()

    t = t.replace(",", "")
    value = float(t)
    return -value if is_negative else value


def _find_money_tokens_from_end(
    tokens: list[str], max_count: int = 3
) -> tuple[list[str], list[str]]:
    """Return (remaining_tokens, money_tokens) without mutating *tokens*."""
    remaining = tokens[:]
    money_tokens: list[str] = []
    while remaining and len(money_tokens) < max_count and _is_money_token(remaining[-1]):
        money_tokens.insert(0, remaining.pop())
    return remaining, money_tokens


def _looks_like_header(line: str) -> bool:
    s = line.lower()
    if "money out" in s or "money in" in s:
        return True
    if "paid out" in s or "paid in" in s:
        return True
    if "balance" in s and ("money" in s or "debit" in s or "credit" in s or "paid" in s):
        return True
    return False


def _looks_like_balance_marker(description: str) -> bool:
    d = re.sub(r"[^a-z0-9]", "", description.lower())
    return (
        "balancebroughtforward" in d
        or "balancecarriedforward" in d
        or "openingbalance" in d
        or "closingbalance" in d
    )


@dataclass
class _MoneyInterpretation:
    amount: Optional[float] = None
    parsed_balance: Optional[float] = None
    warnings: list[str] = field(default_factory=list)
    recon_gaps: list[dict[str, object]] = field(default_factory=list)


def _interpret_money_tokens(
    money_tokens: list[str],
    *,
    has_balance_column: bool,
    description_full: str,
    last_balance: Optional[float],
    row_ref: str,
    line: str,
    page: int,
    txn_date: str,
    description: str,
    prev_txn: Optional["Transaction"],
) -> _MoneyInterpretation:
    """Unified money-token interpretation.

    Handles 1-, 2-, and 3-token layouts for both balance-column and
    non-balance-column statement formats.  Returns the parsed amount,
    balance, and any warnings / reconciliation gaps.
    """
    result = _MoneyInterpretation()

    if has_balance_column:
        if len(money_tokens) == 3:
            out_v = _parse_money_token(money_tokens[0])
            in_v = _parse_money_token(money_tokens[1])
            result.parsed_balance = _parse_money_token(money_tokens[2])

            out_abs = abs(out_v)
            in_abs = abs(in_v)
            if out_abs == 0 and in_abs == 0:
                result.amount = None
            elif out_abs > 0 and in_abs == 0:
                result.amount = -out_abs
            elif in_abs > 0 and out_abs == 0:
                result.amount = in_abs
            elif out_abs > 0 and in_abs > 0:
                result.warnings.append(f"{row_ref} | both in/out non-zero | {line}")
            else:
                result.warnings.append(f"{row_ref} | no in/out amount | {line}")

        elif len(money_tokens) == 2:
            amt_v = abs(_parse_money_token(money_tokens[0]))
            result.parsed_balance = _parse_money_token(money_tokens[1])

            if last_balance is not None:
                delta = result.parsed_balance - last_balance
                if abs(abs(delta) - amt_v) <= _RECONCILIATION_TOLERANCE:
                    result.amount = float(delta)
                elif abs(delta) <= _RECONCILIATION_TOLERANCE and prev_txn is not None:
                    if (
                        prev_txn.txn_date == txn_date
                        and _norm_desc(prev_txn.description_raw) == _norm_desc(description)
                        and abs(prev_txn.amount) > 0
                        and abs(abs(prev_txn.amount) - amt_v) <= _RECONCILIATION_TOLERANCE
                    ):
                        result.amount = float(prev_txn.amount)
                    else:
                        result.warnings.append(
                            f"{row_ref} | could not reconcile amount with balance | {line}"
                        )
                else:
                    if _looks_like_credit(description_full):
                        result.amount = float(amt_v)
                    else:
                        result.amount = -float(amt_v)

                    if abs(delta) > _RECONCILIATION_TOLERANCE:
                        result.recon_gaps.append(
                            {
                                "row_ref": row_ref,
                                "page": page,
                                "txn_date": txn_date,
                                "description_full": description_full,
                                "last_balance": float(last_balance),
                                "parsed_balance": float(result.parsed_balance),
                                "balance_delta": float(delta),
                                "amt_token": float(amt_v),
                                "inferred_amount": float(result.amount),
                                "gap": float(delta - float(result.amount)),
                            }
                        )
            else:
                if _looks_like_credit(description_full):
                    result.amount = float(amt_v)
                else:
                    result.amount = -float(amt_v)

        elif len(money_tokens) == 1:
            v = _parse_money_token(money_tokens[0])
            if _looks_like_balance_marker(description_full):
                result.parsed_balance = v
                result.amount = None
            else:
                if _looks_like_credit(description_full):
                    result.amount = abs(v)
                else:
                    result.amount = -abs(v)

        else:
            result.warnings.append(f"{row_ref} | unsupported money token count | {line}")

    else:
        if len(money_tokens) == 2:
            out_abs = abs(_parse_money_token(money_tokens[0]))
            in_abs = abs(_parse_money_token(money_tokens[1]))
            if out_abs > 0 and in_abs == 0:
                result.amount = -out_abs
            elif in_abs > 0 and out_abs == 0:
                result.amount = in_abs
            elif out_abs > 0 and in_abs > 0:
                result.warnings.append(f"{row_ref} | both in/out non-zero | {line}")
            else:
                result.warnings.append(f"{row_ref} | no in/out amount | {line}")
        elif len(money_tokens) == 1:
            v = _parse_money_token(money_tokens[0])
            if _looks_like_balance_marker(description_full):
                result.parsed_balance = v
                result.amount = None
            else:
                if _looks_like_credit(description_full):
                    result.amount = abs(v)
                else:
                    result.amount = -abs(v)
        else:
            result.warnings.append(f"{row_ref} | unsupported money token count | {line}")

    return result


def parse_lines_to_transactions(
    lines: Iterable[tuple[int, str]],
    *,
    source_file: str = "",
    account_id: str = "HSBC_UK",
    default_currency: str = "GBP",
) -> tuple[list[Transaction], list[str], set[str], list[dict[str, object]]]:
    warnings: list[str] = []
    used_row_refs: set[str] = set()
    recon_gaps: list[dict[str, object]] = []

    indexed_lines = _index_lines(lines)

    has_balance_column = any(_looks_like_header(t) for _, _, t in indexed_lines)

    for _, _, t in indexed_lines:
        m = _DATE_RE.match(t)
        if not m:
            continue
        tokens = _repair_split_money_tokens(m.group("rest").split())
        _, money_tokens = _find_money_tokens_from_end(tokens, max_count=3)
        if len(money_tokens) == 3:
            has_balance_column = True
            break

    txns: list[Transaction] = []
    last_txn_index: Optional[int] = None

    pending: Optional[_PendingTxn] = None

    last_seen_txn_date: Optional[str] = None

    last_balance: Optional[float] = None

    def flush_pending_as_warning() -> None:
        nonlocal pending
        if pending is None:
            return
        row_ref = pending.row_ref
        original_line = pending.original_line
        warnings.append(f"{row_ref} | no amounts found | {original_line}")
        pending = None

    for loop_idx, (page, line_idx, line) in enumerate(indexed_lines):
        if _is_noise_line(line):
            continue
        m = _DATE_RE.match(line)
        if not m:
            if pending is not None:
                cont = line.strip()
                if cont != "" and not _looks_like_header(cont) and not _is_noise_line(cont):
                    if _looks_like_balance_marker(cont):
                        continue
                    if _FX_RATE_LINE_RE.match(cont):
                        used_row_refs.add(f"{page}:{line_idx}")
                        pending.desc_parts.append(cont)
                        continue
                    tokens = _repair_split_money_tokens(cont.split())
                    remaining, money_tokens = _find_money_tokens_from_end(tokens, max_count=3)
                    if money_tokens:
                        used_row_refs.add(pending.row_ref)
                        used_row_refs.add(f"{page}:{line_idx}")
                        desc_extra = " ".join(remaining).strip()
                        if desc_extra:
                            pending.desc_parts.append(desc_extra)

                        txn_date = pending.txn_date
                        description_full = " ".join(pending.desc_parts).strip()
                        description = _truncate_description(description_full)
                        row_ref = f"{page}:{line_idx}"
                        txn_page = page

                        try:
                            mi = _interpret_money_tokens(
                                money_tokens,
                                has_balance_column=has_balance_column,
                                description_full=description_full,
                                last_balance=last_balance,
                                row_ref=row_ref,
                                line=line,
                                page=txn_page,
                                txn_date=txn_date,
                                description=description,
                                prev_txn=txns[last_txn_index] if last_txn_index is not None else None,
                            )
                        except ValueError:
                            warnings.append(f"{row_ref} | money parse failed | {line}")
                            pending = None
                            continue

                        warnings.extend(mi.warnings)
                        recon_gaps.extend(mi.recon_gaps)

                        if mi.amount is not None and mi.parsed_balance is None:
                            mi.amount = _lookahead_sign_correction(
                                mi.amount, loop_idx, indexed_lines,
                            )

                        if mi.parsed_balance is not None:
                            last_balance = mi.parsed_balance
                        elif mi.amount is not None and last_balance is not None:
                            last_balance = last_balance + mi.amount

                        if mi.amount is not None:
                            txn = Transaction(
                                source_file=source_file,
                                account_id=account_id,
                                txn_date=txn_date,
                                description_raw=description,
                                amount=float(mi.amount),
                                currency=default_currency,
                                page=txn_page,
                                row_ref=row_ref,
                            )
                            txns.append(txn)
                            last_txn_index = len(txns) - 1
                            last_seen_txn_date = txn_date

                        pending = None
                    else:
                        if not _is_noise_line(cont):
                            used_row_refs.add(f"{page}:{line_idx}")
                            pending.desc_parts.append(cont)
                continue

            cont = line.strip()
            if (
                last_seen_txn_date is not None
                and cont != ""
                and not _looks_like_header(cont)
                and not _is_noise_line(cont)
            ):
                tokens_all = _repair_split_money_tokens(cont.split())
                tokens_copy, money_tokens = _find_money_tokens_from_end(tokens_all, max_count=3)

                has_prefix = _TXN_PREFIX_RE.match(cont) is not None

                is_txn_candidate = False
                if has_balance_column:
                    is_txn_candidate = (has_prefix and bool(money_tokens)) or len(
                        money_tokens
                    ) >= 2 or (
                        len(money_tokens) == 1
                        and _looks_like_balance_marker(" ".join(tokens_copy).strip())
                    )
                else:
                    is_txn_candidate = (has_prefix and bool(money_tokens)) or len(money_tokens) == 2

                if not is_txn_candidate:
                    if has_prefix:
                        pending = _PendingTxn(
                            txn_date=last_seen_txn_date,
                            desc_parts=[cont],
                            page=page,
                            row_ref=f"{page}:{line_idx}",
                            original_line=cont,
                        )
                        used_row_refs.add(f"{page}:{line_idx}")
                        last_txn_index = None
                        continue

                else:
                    description_full = " ".join(tokens_copy).strip()
                    description = _truncate_description(description_full)
                    row_ref = f"{page}:{line_idx}"
                    txn_date = last_seen_txn_date

                    try:
                        mi = _interpret_money_tokens(
                            money_tokens,
                            has_balance_column=has_balance_column,
                            description_full=description_full,
                            last_balance=last_balance,
                            row_ref=row_ref,
                            line=line,
                            page=page,
                            txn_date=txn_date,
                            description=description,
                            prev_txn=txns[last_txn_index] if last_txn_index is not None else None,
                        )
                    except ValueError:
                        warnings.append(f"{row_ref} | money parse failed | {line}")
                        continue

                    warnings.extend(mi.warnings)
                    recon_gaps.extend(mi.recon_gaps)

                    if mi.amount is not None and mi.parsed_balance is None:
                        mi.amount = _lookahead_sign_correction(
                            mi.amount, loop_idx, indexed_lines,
                        )

                    if mi.parsed_balance is not None:
                        last_balance = mi.parsed_balance
                    elif mi.amount is not None and last_balance is not None:
                        last_balance = last_balance + mi.amount

                    if mi.amount is not None:
                        used_row_refs.add(row_ref)
                        txn = Transaction(
                            source_file=source_file,
                            account_id=account_id,
                            txn_date=txn_date,
                            description_raw=description,
                            amount=float(mi.amount),
                            currency=default_currency,
                            page=page,
                            row_ref=row_ref,
                        )
                        txns.append(txn)
                        last_txn_index = len(txns) - 1

                    continue

            if last_txn_index is not None:
                if cont != "" and not _looks_like_header(cont) and not _is_noise_line(cont):
                    if _looks_like_balance_marker(cont):
                        continue
                    used_row_refs.add(f"{page}:{line_idx}")
                    prev = txns[last_txn_index]
                    merged_desc = (prev.description_raw + " " + cont).strip()
                    merged_desc = _truncate_description(merged_desc)
                    txns[last_txn_index] = Transaction(
                        source_file=prev.source_file,
                        account_id=prev.account_id,
                        txn_date=prev.txn_date,
                        description_raw=merged_desc,
                        amount=prev.amount,
                        currency=prev.currency,
                        page=prev.page,
                        row_ref=prev.row_ref,
                    )
            continue

        if pending is not None:
            flush_pending_as_warning()

        date_raw = m.group("date")
        rest = m.group("rest").strip()
        if _is_noise_line(rest) and not _looks_like_balance_marker(rest):
            continue
        tokens = _repair_split_money_tokens(rest.split())
        remaining, money_tokens = _find_money_tokens_from_end(tokens, max_count=3)
        if not money_tokens:
            try:
                txn_date = _parse_uk_date_to_iso(date_raw)
            except ValueError:
                warnings.append(f"{page}:{line_idx} | {line}")
                last_txn_index = None
                continue

            pending = _PendingTxn(
                txn_date=txn_date,
                desc_parts=[rest],
                page=page,
                row_ref=f"{page}:{line_idx}",
                original_line=line,
            )
            used_row_refs.add(f"{page}:{line_idx}")
            last_txn_index = None
            last_seen_txn_date = txn_date
            continue

        description_full = " ".join(remaining).strip()
        description = _truncate_description(description_full)
        row_ref = f"{page}:{line_idx}"

        try:
            txn_date = _parse_uk_date_to_iso(date_raw)
        except ValueError:
            warnings.append(f"{row_ref} | {line}")
            last_txn_index = None
            continue

        try:
            mi = _interpret_money_tokens(
                money_tokens,
                has_balance_column=has_balance_column,
                description_full=description_full,
                last_balance=last_balance,
                row_ref=row_ref,
                line=line,
                page=page,
                txn_date=txn_date,
                description=description,
                prev_txn=txns[last_txn_index] if last_txn_index is not None else None,
            )
        except ValueError:
            warnings.append(f"{row_ref} | money parse failed | {line}")
            last_txn_index = None
            continue

        warnings.extend(mi.warnings)
        recon_gaps.extend(mi.recon_gaps)

        if mi.amount is None:
            last_txn_index = None
            if mi.parsed_balance is not None:
                last_balance = mi.parsed_balance
            continue

        txn = Transaction(
            source_file=source_file,
            account_id=account_id,
            txn_date=txn_date,
            description_raw=description,
            amount=float(mi.amount),
            currency=default_currency,
            page=page,
            row_ref=row_ref,
        )
        used_row_refs.add(row_ref)
        txns.append(txn)
        last_txn_index = len(txns) - 1
        last_seen_txn_date = txn_date

        if mi.parsed_balance is not None:
            last_balance = mi.parsed_balance
        elif mi.amount is not None and last_balance is not None:
            last_balance = last_balance + mi.amount

    deduped: list[Transaction] = []
    seen_row_refs: set[str] = set()
    for t in txns:
        # Deduplicate only exact same-row duplicates. Statements can legitimately contain
        # repeated transactions with identical date/description/amount.
        if t.row_ref in seen_row_refs:
            continue
        seen_row_refs.add(t.row_ref)
        deduped.append(t)

    return deduped, warnings, used_row_refs, recon_gaps


_SUSPECT_CSV_FIELDNAMES = [
    "source_file", "account_id", "txn_date", "description_raw",
    "amount", "currency", "page", "row_ref",
    "suspect_reason", "description_len",
]


def _write_suspects_csv(path: Path, suspects: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_SUSPECT_CSV_FIELDNAMES)
        w.writeheader()
        for row in suspects:
            w.writerow(row)


def _build_suspects(txns: list[Transaction], *, desc_len_threshold: int = 160) -> list[dict[str, object]]:
    suspects: list[dict[str, object]] = []
    for t in txns:
        reasons: list[str] = []
        if len(t.description_raw) > desc_len_threshold:
            reasons.append(f"desc_len>{desc_len_threshold}")
        for tok in _SUSPECT_NOISE_TOKENS:
            if tok.lower() in t.description_raw.lower():
                reasons.append(f"noise:{tok}")
        for kw in _DESCRIPTION_TRUNCATE_KEYWORDS:
            if kw in t.description_raw.upper():
                reasons.append(f"contains:{kw}")
        if reasons:
            suspects.append(
                {
                    **asdict(t),
                    "suspect_reason": "|".join(reasons),
                    "description_len": len(t.description_raw),
                }
            )
    return suspects


def _write_suspects_context(
    path: Path,
    suspects: list[dict[str, object]],
    indexed_lines: list[tuple[int, int, str]],
    *,
    window: int = 3,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    by_page: dict[int, dict[int, str]] = {}
    for p, li, txt in indexed_lines:
        by_page.setdefault(p, {})[li] = txt

    with path.open("w", encoding="utf-8") as f:
        for s in suspects:
            row_ref = str(s.get("row_ref", ""))
            try:
                page_s, line_s = row_ref.split(":", 1)
                page = int(page_s)
                line_idx = int(line_s)
            except ValueError:
                continue

            f.write(f"row_ref: {row_ref}\n")
            f.write(f"txn_date: {s.get('txn_date','')}\n")
            f.write(f"amount: {s.get('amount','')}\n")
            f.write(f"reason: {s.get('suspect_reason','')}\n")
            f.write(f"description_raw: {s.get('description_raw','')}\n")
            f.write("context:\n")

            page_map = by_page.get(page, {})
            for i in range(line_idx - window, line_idx + window + 1):
                if i < 1:
                    continue
                txt = page_map.get(i)
                if txt is None:
                    continue
                marker = "=>" if i == line_idx else "  "
                f.write(f"{marker} {page}:{i} {txt}\n")
            f.write("\n")


def _write_running_balance_walk(
    path: Path,
    indexed_lines: list[tuple[int, int, str]],
    txns: list[Transaction],
    *,
    opening_balance: Optional[float],
) -> None:
    has_balance_column = any(_looks_like_header(t) for _, _, t in indexed_lines)
    for _, _, t in indexed_lines:
        m = _DATE_RE.match(t)
        if not m:
            continue
        tokens = m.group("rest").split()
        remaining, money_tokens = _find_money_tokens_from_end(tokens, max_count=3)
        if len(money_tokens) == 3:
            has_balance_column = True
            break

    def parse_pdf_balance(money_tokens: list[str], description_full: str) -> Optional[float]:
        if not has_balance_column:
            return None
        if len(money_tokens) == 3:
            return _parse_money_token(money_tokens[2])
        if len(money_tokens) == 2:
            return _parse_money_token(money_tokens[1])
        if len(money_tokens) == 1 and _looks_like_balance_marker(description_full):
            return _parse_money_token(money_tokens[0])
        return None

    observations: list[dict[str, object]] = []
    current_txn_date: Optional[str] = None
    current_txn_row_ref: Optional[str] = None
    current_desc_parts: list[str] = []

    for p, li, t in indexed_lines:
        if _is_noise_line(t) or _looks_like_header(t):
            continue

        row_ref = f"{p}:{li}"
        m = _DATE_RE.match(t)

        if m:
            date_raw = m.group("date")
            rest = m.group("rest").strip()
            try:
                current_txn_date = _parse_uk_date_to_iso(date_raw)
            except ValueError:
                current_txn_date = None
                current_txn_row_ref = None
                current_desc_parts = []
                continue

            current_txn_row_ref = row_ref
            current_desc_parts = [rest] if rest else []
            text_for_balance = rest
        else:
            cont = t.strip()
            if cont == "":
                continue
            if current_txn_date is None:
                continue

            if _TXN_PREFIX_RE.match(cont) is not None:
                current_txn_row_ref = row_ref
                current_desc_parts = [cont]
                text_for_balance = cont
            else:
                is_balance_marker_line = _looks_like_balance_marker(cont)
                if is_balance_marker_line:
                    text_for_balance = cont
                else:
                    current_desc_parts.append(cont)
                    text_for_balance = cont


        tokens_copy = _repair_split_money_tokens(text_for_balance.split())
        _, money_tokens = _find_money_tokens_from_end(tokens_copy, max_count=3)
        if not money_tokens:
            continue

        description_full = " ".join(current_desc_parts).strip()
        try:
            pdf_balance = parse_pdf_balance(money_tokens, description_full)
        except ValueError:
            continue
        if pdf_balance is None:
            continue

        obs_row_ref = current_txn_row_ref or row_ref
        try:
            obs_page_s, obs_line_s = obs_row_ref.split(":", 1)
            obs_page = int(obs_page_s)
            obs_line_idx = int(obs_line_s)
        except ValueError:
            obs_page = p
            obs_line_idx = li

        observations.append(
            {
                "row_ref": obs_row_ref,
                "balance_row_ref": row_ref,
                "page": p,
                "line_idx": li,
                "txn_date": str(current_txn_date),
                "description_full": description_full,
                "pdf_balance": float(pdf_balance),
            }
        )

    def _parse_row_ref_pos(rr: str) -> Optional[tuple[int, int]]:
        try:
            p_s, li_s = rr.split(":", 1)
            return int(p_s), int(li_s)
        except ValueError:
            return None

    pdf_balance_obs_by_row_ref: dict[str, dict[str, object]] = {}
    for obs in sorted(
        observations,
        key=lambda o: (
            int(o.get("page", 0)),
            int(o.get("line_idx", 0)),
        ),
    ):
        rr = str(obs.get("row_ref", ""))
        if rr:
            pdf_balance_obs_by_row_ref[rr] = {
                "pdf_balance": float(obs["pdf_balance"]),
                "balance_row_ref": str(obs.get("balance_row_ref", rr)),
            }

    txns_sorted = sorted(
        txns,
        key=lambda t: (_parse_row_ref_pos(t.row_ref) or (10**9, 10**9)),
    )

    out_rows: list[dict[str, str]] = []
    running_balance: Optional[float] = opening_balance
    first_divergence_seen = False

    for t in txns_sorted:
        bal_obs = pdf_balance_obs_by_row_ref.get(t.row_ref)
        pdf_balance = float(bal_obs["pdf_balance"]) if bal_obs else None
        balance_row_ref = str(bal_obs["balance_row_ref"]) if bal_obs else ""
        if running_balance is None:
            if pdf_balance is None:
                continue
            computed_before = pdf_balance - float(t.amount)
        else:
            computed_before = running_balance

        computed_after = computed_before + float(t.amount)
        running_balance = computed_after

        if pdf_balance is None:
            diff_s = ""
            diverges = False
            first_divergence = False
            pdf_balance_s = ""
        else:
            diff = computed_after - float(pdf_balance)
            diff_s = f"{diff:.2f}"
            diverges = abs(diff) > _RECONCILIATION_TOLERANCE
            first_divergence = diverges and not first_divergence_seen
            if first_divergence:
                first_divergence_seen = True
            pdf_balance_s = f"{float(pdf_balance):.2f}"

        pos = _parse_row_ref_pos(t.row_ref)
        out_rows.append(
            {
                "row_ref": t.row_ref,
                "balance_row_ref": balance_row_ref,
                "page": str(t.page),
                "line_idx": str(pos[1] if pos else ""),
                "txn_date": t.txn_date,
                "parsed_amount": f"{float(t.amount):.2f}",
                "computed_balance_before": f"{computed_before:.2f}",
                "computed_balance_after": f"{computed_after:.2f}",
                "pdf_balance": pdf_balance_s,
                "diff": diff_s,
                "diverges": str(diverges),
                "first_divergence": str(first_divergence),
                "description_full": t.description_raw,
            }
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "row_ref",
                "balance_row_ref",
                "page",
                "line_idx",
                "txn_date",
                "parsed_amount",
                "computed_balance_before",
                "computed_balance_after",
                "pdf_balance",
                "diff",
                "diverges",
                "first_divergence",
                "description_full",
            ],
        )
        w.writeheader()
        for r in out_rows:
            w.writerow(r)


def _write_csv(path: Path, txns: list[Transaction]) -> None:
    fieldnames = [
        "source_file",
        "account_id",
        "txn_date",
        "description_raw",
        "amount",
        "currency",
        "page",
        "row_ref",
    ]

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for t in txns:
            w.writerow(asdict(t))


def _write_json(path: Path, txns: list[Transaction]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump([asdict(t) for t in txns], f, ensure_ascii=False, indent=2)


def _write_warnings(path: Path, warnings: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for w in warnings:
            f.write(w)
            f.write("\n")


def _write_debug_exports(
    outdir: Path,
    base: str,
    indexed_all_lines: list[tuple[int, int, str]],
    txns: list[Transaction],
    used_row_refs: set[str],
    recon_gaps: list[dict[str, object]],
) -> None:
    """Write diagnostic CSVs when reconciliation diffs are non-zero."""
    row_text_by_ref: dict[str, str] = {f"{p}:{li}": t for p, li, t in indexed_all_lines}
    txn_row_refs = {t.row_ref for t in txns}

    nonprefix_cont_txns: list[Transaction] = []
    for t in txns:
        txt = row_text_by_ref.get(t.row_ref, "")
        if txt and _DATE_RE.match(txt) is None and _TXN_PREFIX_RE.match(txt.strip()) is None:
            nonprefix_cont_txns.append(t)
    print(f"nonprefix_continuation_txn_count: {len(nonprefix_cont_txns)}")

    # Unmatched amount lines
    unmatched_path = outdir / f"{base}_unmatched_amount_lines.csv"
    rows: list[dict[str, str]] = []
    for p, li, t in indexed_all_lines:
        row_ref = f"{p}:{li}"
        if row_ref in used_row_refs:
            continue
        if _is_noise_line(t) or _looks_like_header(t):
            continue
        if _looks_like_balance_marker(t):
            continue
        money_hits = _DEBUG_AMOUNT_IN_LINE_RE.findall(t)
        if not money_hits:
            continue
        rows.append(
            {
                "row_ref": row_ref,
                "page": str(p),
                "line_idx": str(li),
                "date_match": str(_DATE_RE.match(t) is not None),
                "txn_prefix_match": str(_TXN_PREFIX_RE.match(t.strip()) is not None),
                "money_tokens": json.dumps(money_hits, ensure_ascii=False),
                "text": t,
            }
        )
    unmatched_path.parent.mkdir(parents=True, exist_ok=True)
    with unmatched_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["row_ref", "page", "line_idx", "date_match", "txn_prefix_match", "money_tokens", "text"],
        )
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # Keyword lines
    keyword_path = outdir / f"{base}_keyword_lines.csv"
    kw_rows: list[dict[str, str]] = []
    for p, li, t in indexed_all_lines:
        if t.strip() == "":
            continue
        m = _DEBUG_KEYWORD_LINE_RE.search(t)
        if not m:
            continue
        money_hits = _DEBUG_AMOUNT_IN_LINE_RE.findall(t)
        kw_rows.append(
            {
                "row_ref": f"{p}:{li}",
                "page": str(p),
                "line_idx": str(li),
                "keyword_match": m.group(0),
                "money_tokens": json.dumps(money_hits, ensure_ascii=False),
                "text": t,
            }
        )
    keyword_path.parent.mkdir(parents=True, exist_ok=True)
    with keyword_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["row_ref", "page", "line_idx", "keyword_match", "money_tokens", "text"],
        )
        w.writeheader()
        for r in kw_rows:
            w.writerow(r)

    # Used non-txn amount lines
    used_non_txn_path = outdir / f"{base}_used_non_txn_amount_lines.csv"
    used_rows: list[dict[str, str]] = []
    for p, li, t in indexed_all_lines:
        row_ref = f"{p}:{li}"
        if row_ref not in used_row_refs:
            continue
        if row_ref in txn_row_refs:
            continue
        if _is_noise_line(t) or _looks_like_header(t) or _looks_like_balance_marker(t):
            continue
        money_hits = _DEBUG_AMOUNT_IN_LINE_RE.findall(t)
        if not money_hits:
            continue
        used_rows.append(
            {
                "row_ref": row_ref,
                "page": str(p),
                "line_idx": str(li),
                "money_tokens": json.dumps(money_hits, ensure_ascii=False),
                "text": t,
            }
        )
    used_non_txn_path.parent.mkdir(parents=True, exist_ok=True)
    with used_non_txn_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["row_ref", "page", "line_idx", "money_tokens", "text"],
        )
        w.writeheader()
        for r in used_rows:
            w.writerow(r)

    # Balance recon gaps
    gaps_path = outdir / f"{base}_balance_recon_gaps.csv"
    gap_fields = [
        "row_ref", "page", "txn_date", "description_full",
        "last_balance", "parsed_balance", "balance_delta",
        "amt_token", "inferred_amount", "gap",
    ]
    gaps_path.parent.mkdir(parents=True, exist_ok=True)
    with gaps_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=gap_fields)
        w.writeheader()
        for r in recon_gaps:
            g = float(r.get("gap", 0.0))
            if abs(g) <= _RECONCILIATION_TOLERANCE:
                continue
            w.writerow({k: r.get(k, "") for k in gap_fields})

    # Balance delta mismatches
    balance_mismatch_path = outdir / f"{base}_balance_delta_mismatches.csv"
    bal_rows: list[dict[str, object]] = []
    for p, li, t in indexed_all_lines:
        if _is_noise_line(t):
            continue
        m = _DATE_RE.match(t)
        if not m:
            continue
        tokens = m.group("rest").split()
        _, money_tokens = _find_money_tokens_from_end(tokens, max_count=3)
        if len(money_tokens) != 3:
            continue
        try:
            out_v = _parse_money_token(money_tokens[0])
            in_v = _parse_money_token(money_tokens[1])
            bal_v = _parse_money_token(money_tokens[2])
        except ValueError:
            continue
        out_abs = abs(out_v)
        in_abs = abs(in_v)
        implied_amt: Optional[float]
        if out_abs == 0 and in_abs == 0:
            implied_amt = None
        elif out_abs > 0 and in_abs == 0:
            implied_amt = -out_abs
        elif in_abs > 0 and out_abs == 0:
            implied_amt = in_abs
        else:
            implied_amt = None
        bal_rows.append(
            {
                "row_ref": f"{p}:{li}",
                "page": p,
                "line_idx": li,
                "out": float(out_abs),
                "in": float(in_abs),
                "balance": float(bal_v),
                "implied_amount": implied_amt,
                "text": t,
            }
        )

    mismatches: list[dict[str, str]] = []
    prev: Optional[dict[str, object]] = None
    for cur in bal_rows:
        if prev is None:
            prev = cur
            continue
        prev_bal = float(prev["balance"])  # type: ignore[arg-type]
        cur_bal = float(cur["balance"])  # type: ignore[arg-type]
        delta = cur_bal - prev_bal
        implied = cur.get("implied_amount")
        if implied is None:
            prev = cur
            continue
        implied_f = float(implied)
        if abs(delta - implied_f) > _RECONCILIATION_TOLERANCE:
            mismatches.append(
                {
                    "row_ref": str(cur["row_ref"]),
                    "prev_row_ref": str(prev["row_ref"]),
                    "prev_balance": f"{prev_bal:.2f}",
                    "balance": f"{cur_bal:.2f}",
                    "balance_delta": f"{delta:.2f}",
                    "implied_amount": f"{implied_f:.2f}",
                    "delta_minus_implied": f"{(delta - implied_f):.2f}",
                    "text": str(cur["text"]),
                }
            )
        prev = cur

    balance_mismatch_path.parent.mkdir(parents=True, exist_ok=True)
    with balance_mismatch_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "row_ref", "prev_row_ref", "prev_balance", "balance",
                "balance_delta", "implied_amount", "delta_minus_implied", "text",
            ],
        )
        w.writeheader()
        for r in mismatches:
            w.writerow(r)

    nonprefix_path = outdir / f"{base}_nonprefix_continuation_txns.csv"
    if nonprefix_cont_txns:
        _write_csv(nonprefix_path, nonprefix_cont_txns)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Convert an HSBC UK statement PDF into normalized transactions.",
    )
    p.add_argument("--pdf", required=True, help="path to the HSBC statement PDF")
    p.add_argument("--outdir", default="finance/exports", help="output directory (default: finance/exports)")
    p.add_argument("--account-id", default="HSBC_UK", help="account identifier (default: HSBC_UK)")
    p.add_argument("--extractor", choices=["text", "words"], default="words", help="PDF extraction method (default: words)")

    args = p.parse_args(argv)

    pdf_path = Path(args.pdf)
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
    candidate_rows = sum(1 for _, _, t in indexed_all_lines if (_DATE_RE.match(t) is not None and not _is_noise_line(t)))
    txns, warnings, used_row_refs, recon_gaps = parse_lines_to_transactions(
        lines, source_file=str(pdf_path), account_id=args.account_id
    )

    if len(txns) < 5:
        raise RuntimeError(
            "Extraction likely failed (PDF may be scanned/image-based). OCR not implemented in v1."
        )

    base = pdf_path.stem

    csv_path = outdir / f"{base}_transactions.csv"
    json_path = outdir / f"{base}_transactions.json"
    warn_path = outdir / f"{base}_warnings.txt"
    suspects_path = outdir / f"{base}_suspects.csv"
    suspects_ctx_path = outdir / f"{base}_suspects_context.txt"

    _write_csv(csv_path, txns)
    _write_json(json_path, txns)
    _write_warnings(warn_path, warnings)

    suspects = _build_suspects(txns)
    _write_suspects_csv(suspects_path, suspects)
    _write_suspects_context(suspects_ctx_path, suspects, indexed_all_lines)

    income = sum(t.amount for t in txns if t.amount > 0)
    expenses = sum(t.amount for t in txns if t.amount < 0)
    net = income + expenses

    print(f"candidate_date_lines_in_pdf: {candidate_rows}")
    print(f"transaction_count: {len(txns)}")
    print(f"total_credits: {income:.2f}")
    print(f"total_debits: {expenses:.2f}")
    print(f"net: {net:.2f}")
    print(f"any_parse_warnings_count: {len(warnings)}")
    print(f"suspects_count: {len(suspects)}")

    diff_in: Optional[float] = None
    diff_out: Optional[float] = None
    opening_balance: Optional[float] = None
    closing_balance: Optional[float] = None
    if "payments_in" in statement_totals or "payments_out" in statement_totals:
        if "payments_in" in statement_totals:
            pi = statement_totals["payments_in"]
            print(f"statement_payments_in: {pi:.2f}")
            diff_in = (income - pi) + 0.0
            print(f"diff_credits_vs_statement_in: {diff_in:.2f}")
        if "payments_out" in statement_totals:
            po = statement_totals["payments_out"]
            print(f"statement_payments_out: {po:.2f}")
            diff_out = (abs(expenses) - po) + 0.0
            print(f"diff_debits_vs_statement_out: {diff_out:.2f}")

    if "opening_balance" in statement_totals:
        opening_balance = float(statement_totals["opening_balance"])
        print(f"statement_opening_balance: {opening_balance:.2f}")
    if "closing_balance" in statement_totals:
        closing_balance = float(statement_totals["closing_balance"])
        print(f"statement_closing_balance: {closing_balance:.2f}")
    if opening_balance is not None and closing_balance is not None:
        statement_delta = closing_balance - opening_balance
        txn_delta = net
        print(f"statement_balance_delta: {statement_delta:.2f}")
        print(f"parsed_txn_delta: {txn_delta:.2f}")
        print(f"diff_txn_delta_vs_statement_delta: {(txn_delta - statement_delta) + 0.0:.2f}")

    delta_mismatch = False
    if opening_balance is not None and closing_balance is not None:
        statement_delta = closing_balance - opening_balance
        if abs(net - statement_delta) > _RECONCILIATION_TOLERANCE:
            delta_mismatch = True

    if (
        (diff_in is not None and abs(diff_in) > _RECONCILIATION_TOLERANCE)
        or (diff_out is not None and abs(diff_out) > _RECONCILIATION_TOLERANCE)
        or delta_mismatch
    ):
        _write_debug_exports(outdir, base, indexed_all_lines, txns, used_row_refs, recon_gaps)

    walk_path = outdir / f"{base}_running_balance_walk.csv"
    _write_running_balance_walk(
        walk_path,
        indexed_all_lines,
        txns,
        opening_balance=opening_balance,
    )

    if extractor_diag is not None:
        print(f"extractor: words (y_tolerance={extractor_diag.y_tolerance})")
        print(f"extractor_total_pdf_words: {extractor_diag.total_pdf_words}")
        print(f"extractor_total_output_lines: {extractor_diag.total_output_lines}")
        for pg in sorted(extractor_diag.lines_per_page):
            print(f"extractor_lines_page_{pg}: {extractor_diag.lines_per_page[pg]}")
        hist = extractor_diag.tokens_per_line_counts
        for tc in sorted(hist):
            print(f"extractor_token_hist_{tc}_tokens: {hist[tc]}")
        if extractor_diag.suspect_merged_lines:
            print(f"extractor_suspect_merged_line_count: {len(extractor_diag.suspect_merged_lines)}")
            for sm in extractor_diag.suspect_merged_lines:
                print(f"  WARN merged_balance_marker page={sm['page']} tokens={sm['token_count']}: {sm['line'][:120]}")

        diag_path = outdir / f"{base}_extractor_diagnostics.csv"
        diag_path.parent.mkdir(parents=True, exist_ok=True)
        diag_rows: list[dict[str, str]] = []
        for pg in sorted(extractor_diag.lines_per_page):
            diag_rows.append(
                {"metric": f"lines_page_{pg}", "value": str(extractor_diag.lines_per_page[pg])}
            )
        for tc in sorted(hist):
            diag_rows.append(
                {"metric": f"token_hist_{tc}_tokens", "value": str(hist[tc])}
            )
        diag_rows.append(
            {"metric": "total_pdf_words", "value": str(extractor_diag.total_pdf_words)}
        )
        diag_rows.append(
            {"metric": "total_output_lines", "value": str(extractor_diag.total_output_lines)}
        )
        diag_rows.append(
            {"metric": "y_tolerance", "value": str(extractor_diag.y_tolerance)}
        )
        diag_rows.append(
            {"metric": "suspect_merged_line_count", "value": str(len(extractor_diag.suspect_merged_lines))}
        )
        for i, sm in enumerate(extractor_diag.suspect_merged_lines):
            diag_rows.append(
                {
                    "metric": f"suspect_merged_{i}",
                    "value": f"page={sm['page']} tokens={sm['token_count']} | {sm['line']}",
                }
            )
        with diag_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["metric", "value"])
            w.writeheader()
            for r in diag_rows:
                w.writerow(r)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
