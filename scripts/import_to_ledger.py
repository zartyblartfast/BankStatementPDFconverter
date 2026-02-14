"""Import categorised transactions into the ledger database.

Usage:  python -m scripts.import_to_ledger [--force]

Reads finance/exports/transactions_categorised.csv, populates:
  - categories / subcategories lookup tables (idempotent)
  - transactions table (dedup via import_hash)
  - import_log (one row per source_file)

The --force flag wipes and rebuilds the database before importing.
"""

import argparse
import csv
import hashlib
import json
import sqlite3
from pathlib import Path

from scripts.init_db import DB_PATH, init_db

CATEGORISED_CSV = (
    Path(__file__).resolve().parent.parent
    / "finance"
    / "exports"
    / "transactions_categorised.csv"
)
CATEGORIES_JSON = (
    Path(__file__).resolve().parent.parent / "config" / "categories.json"
)


def _build_import_hash(txn_date: str, amount: str, description_raw: str,
                       source_file: str, row_ref: str) -> str:
    """Deterministic hash for dedup: SHA-256 of date|amount|description|source|row_ref.

    Including source_file and row_ref ensures that genuinely distinct
    transactions with identical date/amount/description (e.g. multiple
    Non-Sterling fees on the same day) are not collapsed.
    """
    amt = f"{float(amount):.2f}"
    payload = f"{txn_date}|{amt}|{description_raw}|{source_file}|{row_ref}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _seed_categories(conn: sqlite3.Connection) -> dict[tuple[str, str], int]:
    """Seed categories and subcategories from categories.json.

    Returns a mapping of (category_name, subcategory_name) -> subcategory_id.
    """
    with open(CATEGORIES_JSON, encoding="utf-8") as f:
        tree = json.load(f)

    lookup: dict[tuple[str, str], int] = {}

    for cat_name, subs in tree.items():
        # Upsert category
        conn.execute(
            "INSERT OR IGNORE INTO categories (name) VALUES (?)", (cat_name,)
        )
        row = conn.execute(
            "SELECT category_id FROM categories WHERE name = ?", (cat_name,)
        ).fetchone()
        cat_id = row[0]

        for sub_name in subs:
            conn.execute(
                "INSERT OR IGNORE INTO subcategories (category_id, name) VALUES (?, ?)",
                (cat_id, sub_name),
            )
            row2 = conn.execute(
                "SELECT subcategory_id FROM subcategories "
                "WHERE category_id = ? AND name = ?",
                (cat_id, sub_name),
            ).fetchone()
            lookup[(cat_name, sub_name)] = row2[0]

    return lookup


def _import_transactions(conn: sqlite3.Connection,
                         lookup: dict[tuple[str, str], int]) -> dict:
    """Read the categorised CSV and insert into the transactions table.

    Returns summary stats.
    """
    stats = {"total": 0, "inserted": 0, "skipped_dup": 0, "skipped_no_subcat": 0}
    source_counts: dict[str, int] = {}

    with open(CATEGORISED_CSV, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            stats["total"] += 1
            txn_date = row["txn_date"]
            description_raw = row["description_raw"]
            amount_raw = row["amount"]
            amount = round(float(amount_raw), 2)
            currency = row.get("currency", "GBP")
            channel = row.get("channel", "")
            merchant_core = row.get("merchant_core", "")
            category = row.get("category", "")
            subcategory = row.get("subcategory", "")
            account_id = row.get("account_id", "HSBC_UK")
            source_file = row.get("source_file", "")
            page = int(row["page"]) if row.get("page") else None
            row_ref = row.get("row_ref", "")

            import_hash = _build_import_hash(txn_date, amount_raw, description_raw,
                                               source_file, row_ref)

            # Resolve subcategory_id
            subcat_id = lookup.get((category, subcategory))
            if subcat_id is None and category and subcategory:
                # Category/sub exists in CSV but not in categories.json — warn
                stats["skipped_no_subcat"] += 1
                print(f"  WARNING: no subcategory_id for ({category}, {subcategory})")
                continue

            try:
                conn.execute(
                    """INSERT INTO transactions
                       (txn_date, description_raw, amount, currency, channel,
                        merchant_core, subcategory_id, account_id, source_file,
                        page, row_ref, import_hash)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (txn_date, description_raw, amount, currency, channel,
                     merchant_core, subcat_id, account_id, source_file,
                     page, row_ref, import_hash),
                )
                stats["inserted"] += 1
                source_counts[source_file] = source_counts.get(source_file, 0) + 1
            except sqlite3.IntegrityError:
                stats["skipped_dup"] += 1

    # Update import_log
    for sf, count in source_counts.items():
        conn.execute(
            "INSERT OR REPLACE INTO import_log (source_file, row_count) VALUES (?, ?)",
            (sf, count),
        )

    return stats


def main(force: bool = False) -> None:
    if force or not DB_PATH.exists():
        init_db(force=force)

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA foreign_keys=ON")

    print("Seeding categories...")
    lookup = _seed_categories(conn)
    print(f"  {len(lookup)} (category, subcategory) pairs loaded")

    print(f"Importing from {CATEGORISED_CSV.name}...")
    stats = _import_transactions(conn, lookup)

    conn.commit()
    conn.close()

    print(f"\nResults:")
    print(f"  Total rows:     {stats['total']}")
    print(f"  Inserted:       {stats['inserted']}")
    print(f"  Skipped (dup):  {stats['skipped_dup']}")
    if stats["skipped_no_subcat"]:
        print(f"  Skipped (no subcat): {stats['skipped_no_subcat']}")
    print(f"\nLedger database: {DB_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import transactions into ledger")
    parser.add_argument("--force", action="store_true",
                        help="Wipe and rebuild the database before importing")
    args = parser.parse_args()
    main(force=args.force)
