"""Transaction parsing: lines → Transaction objects.

Contains all line-classification helpers, money-token parsing,
and the main parse_lines_to_transactions() function.
"""

import re
from datetime import datetime
from typing import Iterable, Optional

from scripts.hsbc_parser.models import (
    Transaction,
    _MoneyInterpretation,
    _PendingTxn,
)
from scripts.hsbc_parser.patterns import (
    _CREDIT_PREFIXES,
    _DATE_RANGE_FOOTER_RE,
    _DATE_RE,
    _DESCRIPTION_TRUNCATE_KEYWORDS,
    _FX_RATE_LINE_RE,
    _MONEY_TOKEN_RE,
    _NOISE_LINE_PATTERNS,
    _NON_STERLING_RE,
    _RECONCILIATION_TOLERANCE,
    _TXN_PREFIX_RE,
)


# ── Line classification helpers ─────────────────────────────

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
    first_word = d.split()[0].upper() if d else ""
    return first_word in _CREDIT_PREFIXES


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


def _index_lines(lines: Iterable[tuple[int, str]]) -> list[tuple[int, int, str]]:
    indexed: list[tuple[int, int, str]] = []
    line_counter_by_page: dict[int, int] = {}
    for page, text in lines:
        line_counter_by_page[page] = line_counter_by_page.get(page, 0) + 1
        indexed.append((page, line_counter_by_page[page], text))
    return indexed


# ── Date parsing ────────────────────────────────────────────

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


# ── Money token helpers ─────────────────────────────────────

def _is_money_token(token: str) -> bool:
    t = token.strip()
    return bool(_MONEY_TOKEN_RE.match(t))


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


def _find_money_tokens_from_end(
    tokens: list[str], max_count: int = 3
) -> tuple[list[str], list[str]]:
    """Return (remaining_tokens, money_tokens) without mutating *tokens*."""
    remaining = tokens[:]
    money_tokens: list[str] = []
    while remaining and len(money_tokens) < max_count and _is_money_token(remaining[-1]):
        money_tokens.insert(0, remaining.pop())
    return remaining, money_tokens


def _norm_desc(s: str) -> str:
    """Collapse whitespace and uppercase for loose description comparison."""
    return re.sub(r"\s+", " ", s.strip()).upper()


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


# ── Sign correction ─────────────────────────────────────────

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


# ── Money interpretation ────────────────────────────────────

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


# ── Main parser ─────────────────────────────────────────────

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
