"""Output writers: CSV, JSON, warnings, suspects, balance walk, debug exports."""

import csv
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from scripts.hsbc_parser.models import Transaction, WordExtractorDiagnostics
from scripts.hsbc_parser.patterns import (
    _DATE_RE,
    _DEBUG_AMOUNT_IN_LINE_RE,
    _DEBUG_KEYWORD_LINE_RE,
    _DESCRIPTION_TRUNCATE_KEYWORDS,
    _RECONCILIATION_TOLERANCE,
    _SUSPECT_CSV_FIELDNAMES,
    _SUSPECT_DESC_LEN_THRESHOLD,
    _SUSPECT_NOISE_TOKENS,
    _TXN_PREFIX_RE,
)
from scripts.hsbc_parser.parser import (
    _find_money_tokens_from_end,
    _index_lines,
    _is_noise_line,
    _looks_like_balance_marker,
    _looks_like_header,
    _parse_money_token,
    _parse_uk_date_to_iso,
    _repair_split_money_tokens,
)

logger = logging.getLogger(__name__)


# ── Basic writers ───────────────────────────────────────────

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


# ── Suspects ────────────────────────────────────────────────

def _build_suspects(txns: list[Transaction], *, desc_len_threshold: int = _SUSPECT_DESC_LEN_THRESHOLD) -> list[dict[str, object]]:
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


def _write_suspects_csv(path: Path, suspects: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_SUSPECT_CSV_FIELDNAMES)
        w.writeheader()
        for row in suspects:
            w.writerow(row)


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


# ── Running balance walk ────────────────────────────────────

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


# ── Debug exports ───────────────────────────────────────────

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
    logger.info("nonprefix_continuation_txn_count: %d", len(nonprefix_cont_txns))

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


# ── High-level output orchestrator ──────────────────────────

def write_output(
    *,
    outdir: Path,
    base: str,
    txns: list[Transaction],
    warnings: list[str],
    used_row_refs: set[str],
    recon_gaps: list[dict[str, object]],
    indexed_all_lines: list[tuple[int, int, str]],
    statement_totals: dict[str, float],
    extractor_diag: Optional[WordExtractorDiagnostics],
) -> dict[str, object]:
    """Write all output files and return a summary dict.

    Replaces the inline output logic that was in main().
    """
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

    logger.info("candidate_date_lines_in_pdf: %d",
                sum(1 for _, _, t in indexed_all_lines if (_DATE_RE.match(t) is not None and not _is_noise_line(t))))
    logger.info("transaction_count: %d", len(txns))
    logger.info("total_credits: %.2f", income)
    logger.info("total_debits: %.2f", expenses)
    logger.info("net: %.2f", net)
    logger.info("any_parse_warnings_count: %d", len(warnings))
    logger.info("suspects_count: %d", len(suspects))

    diff_in: Optional[float] = None
    diff_out: Optional[float] = None
    opening_balance: Optional[float] = None
    closing_balance: Optional[float] = None
    if "payments_in" in statement_totals or "payments_out" in statement_totals:
        if "payments_in" in statement_totals:
            pi = statement_totals["payments_in"]
            logger.info("statement_payments_in: %.2f", pi)
            diff_in = (income - pi) + 0.0
            logger.info("diff_credits_vs_statement_in: %.2f", diff_in)
        if "payments_out" in statement_totals:
            po = statement_totals["payments_out"]
            logger.info("statement_payments_out: %.2f", po)
            diff_out = (abs(expenses) - po) + 0.0
            logger.info("diff_debits_vs_statement_out: %.2f", diff_out)

    if "opening_balance" in statement_totals:
        opening_balance = float(statement_totals["opening_balance"])
        logger.info("statement_opening_balance: %.2f", opening_balance)
    if "closing_balance" in statement_totals:
        closing_balance = float(statement_totals["closing_balance"])
        logger.info("statement_closing_balance: %.2f", closing_balance)
    if opening_balance is not None and closing_balance is not None:
        statement_delta = closing_balance - opening_balance
        txn_delta = net
        logger.info("statement_balance_delta: %.2f", statement_delta)
        logger.info("parsed_txn_delta: %.2f", txn_delta)
        logger.info("diff_txn_delta_vs_statement_delta: %.2f", (txn_delta - statement_delta) + 0.0)

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
        logger.info("extractor: words (y_tolerance=%s)", extractor_diag.y_tolerance)
        logger.info("extractor_total_pdf_words: %d", extractor_diag.total_pdf_words)
        logger.info("extractor_total_output_lines: %d", extractor_diag.total_output_lines)
        for pg in sorted(extractor_diag.lines_per_page):
            logger.info("extractor_lines_page_%d: %d", pg, extractor_diag.lines_per_page[pg])
        hist = extractor_diag.tokens_per_line_counts
        for tc in sorted(hist):
            logger.info("extractor_token_hist_%d_tokens: %d", tc, hist[tc])
        if extractor_diag.suspect_merged_lines:
            logger.info("extractor_suspect_merged_line_count: %d", len(extractor_diag.suspect_merged_lines))
            for sm in extractor_diag.suspect_merged_lines:
                logger.warning("merged_balance_marker page=%s tokens=%s: %s",
                               sm['page'], sm['token_count'], str(sm['line'])[:120])

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

    return {
        "transaction_count": len(txns),
        "income": income,
        "expenses": expenses,
        "net": net,
        "warnings_count": len(warnings),
        "suspects_count": len(suspects),
        "diff_in": diff_in,
        "diff_out": diff_out,
        "opening_balance": opening_balance,
        "closing_balance": closing_balance,
    }
