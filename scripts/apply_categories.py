"""Join category_rules.csv onto all raw transactions and report coverage.

For each transaction, decomposes the description into merchant_core, looks up
the matching rule in category_rules.csv, and emits:
  1. finance/exports/transactions_categorised.csv  — all txns with category columns
  2. finance/exports/still_unassigned.csv           — merchant_core groups that had no rule

Usage:
    python -m scripts.apply_categories
"""

import argparse
import csv
from collections import defaultdict
from pathlib import Path
from typing import Optional

from scripts.triage_descriptions import decompose_description


def _load_rules(rules_path: Path) -> dict[str, dict]:
    """Load category_rules.csv into a dict keyed by merchant_core (uppercased)."""
    rules: dict[str, dict] = {}
    with open(rules_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            mc = row["merchant_core"].strip().upper()
            rules[mc] = {
                "category": row.get("category", ""),
                "subcategory": row.get("subcategory", ""),
                "notes": row.get("notes", ""),
            }
    return rules


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Join category rules onto all transactions.",
    )
    p.add_argument("--exports", default="finance/exports",
                   help="directory containing *_transactions.csv (default: finance/exports)")
    p.add_argument("--rules", default="config/category_rules.csv",
                   help="category rules CSV (default: config/category_rules.csv)")
    p.add_argument("--out-categorised", default="finance/exports/transactions_categorised.csv",
                   help="output: all transactions with categories (default: finance/exports/transactions_categorised.csv)")
    p.add_argument("--out-unassigned", default="finance/exports/still_unassigned.csv",
                   help="output: unassigned merchant_core groups (default: finance/exports/still_unassigned.csv)")
    args = p.parse_args(argv)

    exports_dir = Path(args.exports)
    rules_path = Path(args.rules)
    out_cat_path = Path(args.out_categorised)
    out_unassigned_path = Path(args.out_unassigned)

    if not rules_path.exists():
        print(f"Rules file not found: {rules_path}")
        return 1

    rules = _load_rules(rules_path)
    print(f"Loaded {len(rules)} category rules from {rules_path}")

    csv_files = sorted(exports_dir.glob("*_transactions.csv"))
    # Exclude our own output files
    csv_files = [f for f in csv_files if f.name not in (
        "transactions_categorised.csv", "still_unassigned.csv",
        "triage_descriptions.csv", "category_review_queue.csv",
    )]
    if not csv_files:
        print(f"No *_transactions.csv files found in {exports_dir}")
        return 1

    # --- Pass 1: categorise every transaction ---
    cat_rows: list[dict] = []
    # Track unassigned groups
    unassigned_groups: dict[str, dict] = defaultdict(lambda: {
        "channels": set(),
        "examples": [],
        "count": 0,
        "sum_amount": 0.0,
        "dates": [],
    })
    assigned_txns = 0
    total_txns = 0

    for csv_file in csv_files:
        with open(csv_file, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                raw = row.get("description_raw", "")
                amount = float(row.get("amount", 0))
                txn_date = row.get("txn_date", "")

                d = decompose_description(raw)
                mc_key = d.merchant_core.upper()
                rule = rules.get(mc_key)

                cat_row = dict(row)  # preserve all original columns
                cat_row["channel"] = d.channel
                cat_row["merchant_core"] = d.merchant_core

                if rule:
                    cat_row["category"] = rule["category"]
                    cat_row["subcategory"] = rule["subcategory"]
                    assigned_txns += 1
                else:
                    cat_row["category"] = ""
                    cat_row["subcategory"] = ""
                    # accumulate for unassigned report
                    g = unassigned_groups[d.merchant_core]
                    if d.channel:
                        g["channels"].add(d.channel)
                    g["count"] += 1
                    g["sum_amount"] += amount
                    if txn_date:
                        g["dates"].append(txn_date)
                    if len(g["examples"]) < 3:
                        g["examples"].append(raw)

                cat_rows.append(cat_row)
                total_txns += 1

    # --- Write categorised transactions ---
    if cat_rows:
        # Build fieldnames: original + new columns
        sample_keys = list(cat_rows[0].keys())
        # Ensure our added columns are at the end
        base_keys = [k for k in sample_keys if k not in ("channel", "merchant_core", "category", "subcategory")]
        fieldnames = base_keys + ["channel", "merchant_core", "category", "subcategory"]

        out_cat_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_cat_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(cat_rows)

    # --- Write still-unassigned groups ---
    unassigned_rows = []
    for mc, g in sorted(unassigned_groups.items(), key=lambda kv: abs(kv[1]["sum_amount"]), reverse=True):
        dates = sorted(g["dates"])
        unassigned_rows.append({
            "merchant_core": mc,
            "channels": ", ".join(sorted(g["channels"])),
            "example_description": g["examples"][0] if g["examples"] else "",
            "count_txns": g["count"],
            "sum_amount": f"{g['sum_amount']:.2f}",
            "first_date": dates[0] if dates else "",
            "last_date": dates[-1] if dates else "",
        })

    if unassigned_rows:
        ua_fieldnames = ["merchant_core", "channels", "example_description",
                         "count_txns", "sum_amount", "first_date", "last_date"]
        with open(out_unassigned_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=ua_fieldnames)
            w.writeheader()
            w.writerows(unassigned_rows)

    unassigned_txns = total_txns - assigned_txns
    pct = assigned_txns / total_txns * 100 if total_txns else 0
    print(f"\nResults:")
    print(f"  Total transactions:    {total_txns}")
    print(f"  Assigned:              {assigned_txns} ({pct:.0f}%)")
    print(f"  Unassigned:            {unassigned_txns} ({100-pct:.0f}%)")
    print(f"  Unassigned groups:     {len(unassigned_rows)}")
    print(f"\nWrote {len(cat_rows)} rows to {out_cat_path}")
    print(f"Wrote {len(unassigned_rows)} rows to {out_unassigned_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
