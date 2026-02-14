"""End-to-end ingestion pipeline: PDF → extract → categorise → import → exceptions.

Called by the Flask dashboard.  Can also be run standalone:

    python -m scripts.ingest_pipeline --pdf StatementsPDF/2026-02-10_Statement.pdf

The pipeline:
  1. Extract transactions from PDF  (hsbc_pdf_to_txn)
  2. Categorise with rules           (apply_categories logic)
  3. Delete-and-reimport for seamless dedup (preserves excluded flags)
  4. Flag uncategorised transactions as exceptions
"""

import argparse
import csv
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from scripts.hsbc_pdf_to_txn import (
    Transaction,
    main as extract_pdf,
    parse_lines_to_transactions,
    parse_pdf_to_lines_words,
    parse_pdf_to_lines,
)
from scripts.apply_categories import _load_rules
from scripts.triage_descriptions import decompose_description
from scripts.init_db import DB_PATH, init_db

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RULES_CSV = PROJECT_ROOT / "config" / "category_rules.csv"
CATEGORIES_JSON = PROJECT_ROOT / "config" / "categories.json"
EXPORTS_DIR = PROJECT_ROOT / "finance" / "exports"


@dataclass
class IngestResult:
    """Summary returned by run_pipeline()."""
    pdf_path: str = ""
    source_file: str = ""
    extracted: int = 0
    categorised: int = 0
    uncategorised: int = 0
    imported_new: int = 0
    imported_replaced: int = 0
    exceptions_created: int = 0
    warnings: list[str] = None
    recon_ok: bool = True
    error: str = ""

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []


def _build_import_hash(txn_date: str, amount: float, description_raw: str,
                       source_file: str, row_ref: str) -> str:
    """Deterministic hash matching import_to_ledger logic."""
    amt = f"{amount:.2f}"
    payload = f"{txn_date}|{amt}|{description_raw}|{source_file}|{row_ref}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _seed_categories(conn: sqlite3.Connection) -> dict[tuple[str, str], int]:
    """Ensure all categories/subcategories from categories.json exist in DB.

    Returns mapping (category_name, subcategory_name) -> subcategory_id.
    """
    with open(CATEGORIES_JSON, encoding="utf-8") as f:
        tree = json.load(f)

    lookup: dict[tuple[str, str], int] = {}
    for cat_name, subs in tree.items():
        conn.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (cat_name,))
        cat_id = conn.execute(
            "SELECT category_id FROM categories WHERE name = ?", (cat_name,)
        ).fetchone()[0]
        for sub_name in subs:
            conn.execute(
                "INSERT OR IGNORE INTO subcategories (category_id, name) VALUES (?, ?)",
                (cat_id, sub_name),
            )
            sub_id = conn.execute(
                "SELECT subcategory_id FROM subcategories WHERE category_id = ? AND name = ?",
                (cat_id, sub_name),
            ).fetchone()[0]
            lookup[(cat_name, sub_name)] = sub_id
    return lookup


def run_pipeline(pdf_path: str) -> IngestResult:
    """Run the full ingestion pipeline for a single PDF.

    Returns an IngestResult with stats and any errors.
    """
    result = IngestResult(pdf_path=pdf_path)
    pdf = Path(pdf_path)

    if not pdf.exists():
        result.error = f"PDF not found: {pdf}"
        return result

    # ── Step 1: Extract transactions from PDF ──
    try:
        lines, _diag = parse_pdf_to_lines_words(str(pdf))
        if not lines:
            lines = parse_pdf_to_lines(str(pdf))
    except Exception as e:
        result.error = f"PDF extraction failed: {e}"
        return result

    source_file = str(pdf)
    txns, warnings, _used, _recon = parse_lines_to_transactions(
        lines, source_file=source_file
    )
    result.extracted = len(txns)
    result.source_file = source_file
    result.warnings = warnings

    if not txns:
        result.error = "No transactions extracted from PDF."
        return result

    # Also write the CSV exports (for audit trail)
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    base = pdf.stem
    csv_path = EXPORTS_DIR / f"{base}_transactions.csv"
    _write_txn_csv(csv_path, txns)

    # ── Step 2: Categorise using rules ──
    rules = _load_rules(RULES_CSV)
    categorised_txns: list[dict] = []

    for t in txns:
        d = decompose_description(t.description_raw)
        mc_key = d.merchant_core.upper()
        rule = rules.get(mc_key)

        row = {
            "txn_date": t.txn_date,
            "description_raw": t.description_raw,
            "amount": t.amount,
            "currency": t.currency,
            "channel": d.channel,
            "merchant_core": d.merchant_core,
            "account_id": t.account_id,
            "source_file": t.source_file,
            "page": t.page,
            "row_ref": t.row_ref,
        }

        if rule:
            row["category"] = rule["category"]
            row["subcategory"] = rule["subcategory"]
            result.categorised += 1
        else:
            row["category"] = ""
            row["subcategory"] = ""
            result.uncategorised += 1

        categorised_txns.append(row)

    # ── Step 3: Import to ledger with seamless dedup ──
    if not DB_PATH.exists():
        init_db()

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA foreign_keys=ON")

    try:
        cat_lookup = _seed_categories(conn)

        # Snapshot excluded flags for this source_file
        existing = conn.execute(
            "SELECT txn_date, amount, description_raw, excluded, exclude_reason "
            "FROM transactions WHERE source_file = ?",
            (source_file,)
        ).fetchall()

        excluded_snapshot: dict[tuple[str, str, str], tuple[int, str]] = {}
        for row in existing:
            key = (row[0], f"{row[1]:.2f}", row[2])
            if row[3]:  # excluded == 1
                excluded_snapshot[key] = (row[3], row[4] or "")

        old_count = len(existing)

        # Delete old transactions and their exceptions for this source_file
        if old_count > 0:
            conn.execute(
                "DELETE FROM exceptions WHERE txn_id IN "
                "(SELECT txn_id FROM transactions WHERE source_file = ?)",
                (source_file,)
            )
            conn.execute(
                "DELETE FROM transactions WHERE source_file = ?",
                (source_file,)
            )
            result.imported_replaced = old_count

        # Insert new transactions
        for t in categorised_txns:
            import_hash = _build_import_hash(
                t["txn_date"], t["amount"], t["description_raw"],
                t["source_file"], t["row_ref"],
            )

            subcat_id = cat_lookup.get((t["category"], t["subcategory"]))

            # Restore excluded flag if it was previously set
            excl_key = (t["txn_date"], f"{t['amount']:.2f}", t["description_raw"])
            excluded, exclude_reason = excluded_snapshot.get(excl_key, (0, None))

            try:
                conn.execute(
                    """INSERT INTO transactions
                       (txn_date, description_raw, amount, currency, channel,
                        merchant_core, subcategory_id, account_id, source_file,
                        page, row_ref, import_hash, excluded, exclude_reason)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (t["txn_date"], t["description_raw"], t["amount"],
                     t["currency"], t["channel"], t["merchant_core"],
                     subcat_id, t["account_id"], t["source_file"],
                     t["page"], t["row_ref"], import_hash,
                     excluded, exclude_reason),
                )
                result.imported_new += 1
            except sqlite3.IntegrityError:
                pass  # hash collision within same file — skip

        # ── Step 4: Create exceptions for uncategorised transactions ──
        uncategorised = [t for t in categorised_txns if not t["category"]]
        for t in uncategorised:
            # Find the txn_id we just inserted
            row = conn.execute(
                "SELECT txn_id FROM transactions "
                "WHERE source_file = ? AND txn_date = ? AND amount = ? AND description_raw = ?",
                (t["source_file"], t["txn_date"], t["amount"], t["description_raw"]),
            ).fetchone()
            txn_id = row[0] if row else None

            conn.execute(
                """INSERT INTO exceptions
                   (txn_id, type, merchant_core, description_raw, amount, txn_date, detail)
                   VALUES (?, 'uncategorised', ?, ?, ?, ?, ?)""",
                (txn_id, t["merchant_core"], t["description_raw"],
                 t["amount"], t["txn_date"],
                 json.dumps({"channel": t["channel"]})),
            )
            result.exceptions_created += 1

        conn.commit()

    except Exception as e:
        conn.rollback()
        result.error = f"Import failed: {e}"
    finally:
        conn.close()

    return result


def _write_txn_csv(path: Path, txns: list[Transaction]) -> None:
    """Write extracted transactions to CSV (audit trail)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["source_file", "account_id", "txn_date", "description_raw",
              "amount", "currency", "page", "row_ref"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in txns:
            w.writerow({
                "source_file": t.source_file,
                "account_id": t.account_id,
                "txn_date": t.txn_date,
                "description_raw": t.description_raw,
                "amount": t.amount,
                "currency": t.currency,
                "page": t.page,
                "row_ref": t.row_ref,
            })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest a bank statement PDF")
    parser.add_argument("--pdf", required=True, help="path to PDF file")
    args = parser.parse_args()

    r = run_pipeline(args.pdf)
    print(f"\n{'='*50}")
    print(f"PDF:           {r.pdf_path}")
    print(f"Extracted:     {r.extracted}")
    print(f"Categorised:   {r.categorised}")
    print(f"Uncategorised: {r.uncategorised}")
    print(f"New imports:   {r.imported_new}")
    print(f"Replaced:      {r.imported_replaced}")
    print(f"Exceptions:    {r.exceptions_created}")
    if r.warnings:
        print(f"Warnings:      {len(r.warnings)}")
    if r.error:
        print(f"ERROR:         {r.error}")
    print(f"{'='*50}")
