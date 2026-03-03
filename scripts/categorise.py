"""Apply merchant rules to the triage CSV and produce a review queue.

Reads config/merchant_rules.json + finance/exports/triage_descriptions.csv
and outputs a review queue CSV with suggested categories and confidence levels.

Usage:
    python -m scripts.categorise [--triage finance/exports/triage_descriptions.csv] [--rules config/merchant_rules.json] [--out finance/exports/category_review_queue.csv]
"""

import argparse
import csv
import json
from pathlib import Path
from typing import Optional


def _load_rules(rules_path: Path) -> list[dict]:
    """Load merchant rules from JSON config."""
    with open(rules_path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("rules", [])


def _match_rule(merchant_core: str, rule: dict) -> bool:
    """Check if a merchant_core matches a single rule."""
    match_type = rule.get("match", "")
    pattern = rule.get("pattern", "").upper()
    mc = merchant_core.upper()

    if match_type == "startswith":
        return mc.startswith(pattern)
    elif match_type == "contains":
        return pattern in mc
    elif match_type == "exact":
        return mc == pattern
    return False


def _categorise_row(merchant_core: str, rules: list[dict]) -> dict:
    """Find the first matching rule for a merchant_core.

    Returns a dict with category, subcategory, confidence, notes.
    """
    for rule in rules:
        if _match_rule(merchant_core, rule):
            return {
                "suggested_category": rule.get("category", ""),
                "suggested_subcategory": rule.get("subcategory", ""),
                "confidence": "high",
                "notes": rule.get("notes", ""),
            }
    return {
        "suggested_category": "UNASSIGNED",
        "suggested_subcategory": "",
        "confidence": "",
        "notes": "",
    }


def _build_review_queue(triage_path: Path, rules: list[dict]) -> list[dict]:
    """Read triage CSV and apply rules to produce the review queue."""
    rows = []
    assigned = 0
    unassigned = 0

    with open(triage_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            merchant_core = row.get("merchant_core", "")
            result = _categorise_row(merchant_core, rules)

            rows.append({
                "merchant_core": merchant_core,
                "channels": row.get("channels", ""),
                "example_description": row.get("example_description", ""),
                "count_txns": row.get("count_txns", ""),
                "sum_amount": row.get("sum_amount", ""),
                "suggested_category": result["suggested_category"],
                "suggested_subcategory": result["suggested_subcategory"],
                "confidence": result["confidence"],
                "notes": result["notes"],
            })

            if result["suggested_category"] != "UNASSIGNED":
                assigned += 1
            else:
                unassigned += 1

    print(f"Categorised: {assigned} assigned, {unassigned} unassigned (out of {len(rows)} groups)")
    return rows


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Apply merchant rules and produce a category review queue.",
    )
    p.add_argument("--triage", default="finance/exports/triage_descriptions.csv",
                   help="input triage CSV (default: finance/exports/triage_descriptions.csv)")
    p.add_argument("--rules", default="config/merchant_rules.json",
                   help="merchant rules JSON (default: config/merchant_rules.json)")
    p.add_argument("--out", default="finance/exports/category_review_queue.csv",
                   help="output review queue CSV (default: finance/exports/category_review_queue.csv)")
    args = p.parse_args(argv)

    triage_path = Path(args.triage)
    rules_path = Path(args.rules)
    out_path = Path(args.out)

    if not triage_path.exists():
        print(f"Triage CSV not found: {triage_path}")
        print("Run: python -m scripts.triage_descriptions")
        return 1

    if not rules_path.exists():
        print(f"Rules file not found: {rules_path}")
        return 1

    rules = _load_rules(rules_path)
    print(f"Loaded {len(rules)} rules from {rules_path}")

    rows = _build_review_queue(triage_path, rules)

    fieldnames = [
        "merchant_core", "channels", "example_description", "count_txns",
        "sum_amount", "suggested_category", "suggested_subcategory",
        "confidence", "notes",
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
