"""Produce a triage CSV grouping transactions by normalised description.

Reads all *_transactions.csv files from the exports directory and outputs
a single summary CSV with one row per unique merchant_core (channel-stripped
normalised description).

Usage:
    python -m scripts.triage_descriptions [--exports finance/exports] [--out finance/exports/triage_descriptions.csv]
"""

import argparse
import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Description normalisation
# ---------------------------------------------------------------------------

# FX conversion tail: "USD 12.00 @ 1.3289 Visa Rate" or similar
_FX_TAIL_RE = re.compile(
    r"\s+[A-Z]{3}\s+[\d,.]+\.\d{2}\s+@\s+.*$",
    re.IGNORECASE,
)

# "DR/CR Non-Sterling Transaction Fee" (standalone fee lines)
_NON_STERLING_FEE_RE = re.compile(
    r"^[CD]R\s+Non-Sterling\s+Transaction\s+Fee$",
    re.IGNORECASE,
)

# Trailing alphanumeric reference codes (5+ chars, mixed letters+digits)
_TRAILING_REF_RE = re.compile(
    r"\s+[A-Z0-9]{5,}$",
    re.IGNORECASE,
)

# Trailing pure-digit sequences (phone numbers, long references: 8+ digits)
_TRAILING_DIGITS_RE = re.compile(
    r"\s+\d{8,}$",
)

# Trailing short digit sequences after * (e.g. "PAYPAL *GFM INTERN 35314369001")
_TRAILING_PHONE_RE = re.compile(
    r"\s+\d{10,}$",
)

# Amazon-style reference after asterisk: "AMAZON UK* Z93QC9H"
_AMAZON_REF_RE = re.compile(
    r"(\*)\s*[A-Z0-9]{5,}\b",
    re.IGNORECASE,
)

# Trailing statement-period date ranges: "11 JULY TO 10 AUGUST 2025"
_DATE_RANGE_TAIL_RE = re.compile(
    r"\s+\d{1,2}\s+[A-Z]+\s+TO\s+\d{1,2}\s+[A-Z]+(\s+\d{2,4})?$",
    re.IGNORECASE,
)

# Leading reference-like tokens (pure digits 6+ chars, or mixed alphanum 6+ chars)
_LEADING_REF_RE = re.compile(
    r"^[A-Z0-9]{6,}\s+",
    re.IGNORECASE,
)

# Channel prefixes in match order (longest first to avoid partial matches)
_CHANNEL_PREFIXES = [
    "VIS INT'L",
    "VIS",
    "DD",
    "BP",
    "OBP",
    "TFR",
    "ATM",
    "CR",
    "DR",
    "FPI",
    "FPO",
    "BGC",
    "CHG",
    "CHARGE",
    "SO",
    "CASH",
    "DEP",
    "POS",
    "CARD",
    ")))",
]


@dataclass
class DecomposedDescription:
    """Result of decomposing a raw transaction description."""
    description_norm: str   # full normalised (with channel)
    channel: str            # VIS, DD, BP, ))), etc. or ""
    merchant_core: str      # normalised with channel + leading refs stripped


def _extract_channel(s: str) -> tuple[str, str]:
    """Extract channel prefix from an uppercased normalised string.

    Returns (channel, remainder). If no known prefix, returns ("", s).
    """
    for prefix in _CHANNEL_PREFIXES:
        if s.startswith(prefix + " ") or s == prefix:
            remainder = s[len(prefix):].strip()
            return prefix, remainder
    return "", s


def _strip_leading_refs(s: str) -> str:
    """Strip leading reference-number tokens from merchant_core.

    E.g. '0011840346 WINDSURF WINDSURF.COM' -> 'WINDSURF WINDSURF.COM'
         'WA888685A DWP SP' -> 'DWP SP'
    """
    while True:
        m = _LEADING_REF_RE.match(s)
        if not m:
            break
        token = m.group().strip()
        has_letters = any(c.isalpha() for c in token)
        has_digits = any(c.isdigit() for c in token)
        # Strip if: pure digits, or mixed alphanum (reference codes)
        if (not has_letters) or (has_letters and has_digits):
            s = s[m.end():].strip()
        else:
            break
    return s


def normalise_description(raw: str) -> str:
    """Normalise a transaction description for grouping.

    Strips variable elements (reference numbers, FX details, phone numbers)
    while preserving the meaningful merchant/payee identifier.
    """
    s = raw.strip()

    # 1. Strip FX conversion tail
    s = _FX_TAIL_RE.sub("", s)

    # 2. Strip Amazon-style inline references (e.g. "UK* Z93QC9H" -> "UK*")
    s = _AMAZON_REF_RE.sub(r"\1", s)

    # 3. Strip trailing statement-period date ranges
    s = _DATE_RANGE_TAIL_RE.sub("", s)

    # 4. Uppercase
    s = s.upper()

    # 5. Strip trailing phone numbers / long digit sequences
    s = _TRAILING_PHONE_RE.sub("", s)
    s = _TRAILING_DIGITS_RE.sub("", s)

    # 6. Strip trailing alphanumeric reference codes (but only if mixed)
    # Be conservative — only strip if the token has both letters and digits
    m = _TRAILING_REF_RE.search(s)
    if m:
        token = m.group().strip()
        has_letters = any(c.isalpha() for c in token)
        has_digits = any(c.isdigit() for c in token)
        if has_letters and has_digits:
            s = s[: m.start()]

    # 7. Collapse whitespace and trim
    s = re.sub(r"\s+", " ", s).strip()

    return s


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def decompose_description(raw: str) -> DecomposedDescription:
    """Full decomposition: normalise, extract channel, derive merchant_core."""
    description_norm = normalise_description(raw)
    channel, remainder = _extract_channel(description_norm)
    merchant_core = _strip_leading_refs(remainder)
    return DecomposedDescription(
        description_norm=description_norm,
        channel=channel,
        merchant_core=merchant_core,
    )


def _build_triage(exports_dir: Path) -> list[dict]:
    """Read all transaction CSVs and group by merchant_core."""

    csv_files = sorted(exports_dir.glob("*_transactions.csv"))
    if not csv_files:
        print(f"No *_transactions.csv files found in {exports_dir}")
        return []

    # Accumulate per merchant_core
    groups: dict[str, dict] = defaultdict(lambda: {
        "channels": set(),
        "examples": [],
        "count": 0,
        "sum_amount": 0.0,
        "sum_debits": 0.0,
        "sum_credits": 0.0,
        "dates": [],
    })

    total_txns = 0
    for csv_file in csv_files:
        with open(csv_file, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                raw = row.get("description_raw", "")
                amount = float(row.get("amount", 0))
                txn_date = row.get("txn_date", "")

                d = decompose_description(raw)
                g = groups[d.merchant_core]
                if d.channel:
                    g["channels"].add(d.channel)
                g["count"] += 1
                g["sum_amount"] += amount
                if amount < 0:
                    g["sum_debits"] += amount
                else:
                    g["sum_credits"] += amount
                if txn_date:
                    g["dates"].append(txn_date)
                # Keep first few examples for reference
                if len(g["examples"]) < 3:
                    g["examples"].append(raw)
                total_txns += 1

    print(f"Loaded {total_txns} transactions from {len(csv_files)} files")
    print(f"Unique merchant_core groups: {len(groups)}")

    # Build output rows sorted by absolute sum (largest spend first)
    rows = []
    for mc, g in sorted(groups.items(), key=lambda kv: abs(kv[1]["sum_amount"]), reverse=True):
        dates = sorted(g["dates"])
        rows.append({
            "merchant_core": mc,
            "channels": ", ".join(sorted(g["channels"])),
            "example_description": g["examples"][0] if g["examples"] else "",
            "count_txns": g["count"],
            "sum_amount": f"{g['sum_amount']:.2f}",
            "sum_debits": f"{g['sum_debits']:.2f}",
            "sum_credits": f"{g['sum_credits']:.2f}",
            "avg_amount": f"{g['sum_amount'] / g['count']:.2f}" if g["count"] else "0.00",
            "first_date": dates[0] if dates else "",
            "last_date": dates[-1] if dates else "",
        })

    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Produce a triage CSV grouping transactions by normalised description.",
    )
    p.add_argument("--exports", default="finance/exports", help="directory containing *_transactions.csv files (default: finance/exports)")
    p.add_argument("--out", default="finance/exports/triage_descriptions.csv", help="output CSV path (default: finance/exports/triage_descriptions.csv)")
    args = p.parse_args(argv)

    exports_dir = Path(args.exports)
    out_path = Path(args.out)

    rows = _build_triage(exports_dir)
    if not rows:
        return 1

    fieldnames = [
        "merchant_core", "channels", "example_description", "count_txns",
        "sum_amount", "sum_debits", "sum_credits", "avg_amount",
        "first_date", "last_date",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    print(f"Wrote {len(rows)} rows to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
