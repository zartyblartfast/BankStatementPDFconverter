"""Query the ledger database from the command line.

Usage:
  python -m scripts.ledger_query summary
  python -m scripts.ledger_query monthly
  python -m scripts.ledger_query category [--month YYYY-MM]
  python -m scripts.ledger_query subcategory [--category NAME] [--month YYYY-MM]
  python -m scripts.ledger_query search TERM [--limit N]
  python -m scripts.ledger_query top [--n N] [--month YYYY-MM] [--direction debit|credit]
  python -m scripts.ledger_query sql "SELECT ..."

Examples:
  python -m scripts.ledger_query summary
  python -m scripts.ledger_query monthly
  python -m scripts.ledger_query category --month 2025-06
  python -m scripts.ledger_query subcategory --category Tech
  python -m scripts.ledger_query search "AMAZON"
  python -m scripts.ledger_query top --n 20 --direction debit
  python -m scripts.ledger_query sql "SELECT txn_date, amount FROM transactions WHERE amount > 500"
"""

import argparse
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "finance" / "ledger.db"


def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Ledger database not found: {DB_PATH}")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _print_table(headers: list[str], rows: list[tuple], align: dict[int, str] | None = None):
    """Simple aligned table printer."""
    align = align or {}
    col_widths = [len(h) for h in headers]
    str_rows = []
    for row in rows:
        str_row = [str(v) if v is not None else "" for v in row]
        str_rows.append(str_row)
        for i, v in enumerate(str_row):
            col_widths[i] = max(col_widths[i], len(v))

    # Header
    hdr = "  ".join(
        h.ljust(col_widths[i]) if align.get(i, "l") == "l" else h.rjust(col_widths[i])
        for i, h in enumerate(headers)
    )
    print(hdr)
    print("  ".join("-" * col_widths[i] for i in range(len(headers))))

    # Rows
    for sr in str_rows:
        line = "  ".join(
            sr[i].ljust(col_widths[i]) if align.get(i, "l") == "l" else sr[i].rjust(col_widths[i])
            for i in range(len(headers))
        )
        print(line)


# ── Commands ──────────────────────────────────────────────────────────

def cmd_summary(args):
    """Overall database summary."""
    conn = _connect()
    row = conn.execute("SELECT COUNT(*) AS n, MIN(txn_date) AS first, MAX(txn_date) AS last FROM transactions").fetchone()
    total = row["n"]
    first = row["first"]
    last = row["last"]

    debits = conn.execute("SELECT SUM(amount) FROM transactions WHERE amount < 0").fetchone()[0] or 0
    credits = conn.execute("SELECT SUM(amount) FROM transactions WHERE amount > 0").fetchone()[0] or 0

    cats = conn.execute("SELECT COUNT(DISTINCT c.name) FROM categories c JOIN subcategories s ON c.category_id = s.category_id JOIN transactions t ON t.subcategory_id = s.subcategory_id").fetchone()[0]
    imports = conn.execute("SELECT COUNT(*) FROM import_log").fetchone()[0]

    print(f"Ledger Summary")
    print(f"  Transactions:  {total:,}")
    print(f"  Date range:    {first} to {last}")
    print(f"  Total debits:  £{debits:,.2f}")
    print(f"  Total credits: £{credits:,.2f}")
    print(f"  Net:           £{debits + credits:,.2f}")
    print(f"  Categories:    {cats}")
    print(f"  Source files:  {imports}")
    conn.close()


def cmd_monthly(args):
    """Monthly totals: debits, credits, net."""
    conn = _connect()
    rows = conn.execute("""
        SELECT substr(txn_date, 1, 7) AS month,
               COUNT(*) AS txns,
               SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END) AS debits,
               SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) AS credits,
               SUM(amount) AS net
        FROM transactions
        GROUP BY month
        ORDER BY month
    """).fetchall()
    conn.close()

    _print_table(
        ["Month", "Txns", "Debits", "Credits", "Net"],
        [(r["month"], r["txns"], f"£{r['debits']:,.2f}", f"£{r['credits']:,.2f}", f"£{r['net']:,.2f}") for r in rows],
        align={1: "r", 2: "r", 3: "r", 4: "r"},
    )


def cmd_category(args):
    """Spending by category, optionally filtered to a month."""
    conn = _connect()
    where = ""
    params: list = []
    if args.month:
        where = "WHERE substr(t.txn_date, 1, 7) = ?"
        params.append(args.month)

    rows = conn.execute(f"""
        SELECT c.name AS category,
               COUNT(*) AS txns,
               SUM(CASE WHEN t.amount < 0 THEN t.amount ELSE 0 END) AS debits,
               SUM(CASE WHEN t.amount > 0 THEN t.amount ELSE 0 END) AS credits,
               SUM(t.amount) AS net
        FROM transactions t
        JOIN subcategories s ON t.subcategory_id = s.subcategory_id
        JOIN categories c ON s.category_id = c.category_id
        {where}
        GROUP BY c.name
        ORDER BY debits ASC
    """, params).fetchall()
    conn.close()

    title = f"Category breakdown"
    if args.month:
        title += f" ({args.month})"
    print(title)
    _print_table(
        ["Category", "Txns", "Debits", "Credits", "Net"],
        [(r["category"], r["txns"], f"£{r['debits']:,.2f}", f"£{r['credits']:,.2f}", f"£{r['net']:,.2f}") for r in rows],
        align={1: "r", 2: "r", 3: "r", 4: "r"},
    )


def cmd_subcategory(args):
    """Spending by subcategory, optionally filtered to a category and/or month."""
    conn = _connect()
    clauses = []
    params: list = []
    if args.category:
        clauses.append("c.name = ?")
        params.append(args.category)
    if args.month:
        clauses.append("substr(t.txn_date, 1, 7) = ?")
        params.append(args.month)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    rows = conn.execute(f"""
        SELECT c.name AS category, s.name AS subcategory,
               COUNT(*) AS txns,
               SUM(CASE WHEN t.amount < 0 THEN t.amount ELSE 0 END) AS debits,
               SUM(CASE WHEN t.amount > 0 THEN t.amount ELSE 0 END) AS credits
        FROM transactions t
        JOIN subcategories s ON t.subcategory_id = s.subcategory_id
        JOIN categories c ON s.category_id = c.category_id
        {where}
        GROUP BY c.name, s.name
        ORDER BY c.name, debits ASC
    """, params).fetchall()
    conn.close()

    title = "Subcategory breakdown"
    if args.category:
        title += f" — {args.category}"
    if args.month:
        title += f" ({args.month})"
    print(title)
    _print_table(
        ["Category", "Subcategory", "Txns", "Debits", "Credits"],
        [(r["category"], r["subcategory"], r["txns"], f"£{r['debits']:,.2f}", f"£{r['credits']:,.2f}") for r in rows],
        align={2: "r", 3: "r", 4: "r"},
    )


def cmd_search(args):
    """Search transactions by description or merchant_core."""
    conn = _connect()
    term = f"%{args.term}%"
    limit = args.limit or 25
    rows = conn.execute("""
        SELECT t.txn_date, t.amount, t.merchant_core, c.name AS category, s.name AS subcategory
        FROM transactions t
        LEFT JOIN subcategories s ON t.subcategory_id = s.subcategory_id
        LEFT JOIN categories c ON s.category_id = c.category_id
        WHERE t.merchant_core LIKE ? OR t.description_raw LIKE ?
        ORDER BY t.txn_date DESC
        LIMIT ?
    """, (term, term, limit)).fetchall()
    conn.close()

    print(f"Search: '{args.term}' ({len(rows)} results, limit {limit})")
    _print_table(
        ["Date", "Amount", "Merchant", "Category", "Subcategory"],
        [(r["txn_date"], f"£{r['amount']:,.2f}", r["merchant_core"][:40], r["category"] or "", r["subcategory"] or "") for r in rows],
        align={1: "r"},
    )


def cmd_top(args):
    """Top N transactions by absolute amount."""
    conn = _connect()
    n = args.n or 20
    clauses = []
    params: list = []
    if args.month:
        clauses.append("substr(t.txn_date, 1, 7) = ?")
        params.append(args.month)
    if args.direction == "debit":
        clauses.append("t.amount < 0")
    elif args.direction == "credit":
        clauses.append("t.amount > 0")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    rows = conn.execute(f"""
        SELECT t.txn_date, t.amount, t.merchant_core, c.name AS category, s.name AS subcategory
        FROM transactions t
        LEFT JOIN subcategories s ON t.subcategory_id = s.subcategory_id
        LEFT JOIN categories c ON s.category_id = c.category_id
        {where}
        ORDER BY ABS(t.amount) DESC
        LIMIT ?
    """, params + [n]).fetchall()
    conn.close()

    title = f"Top {n} transactions by amount"
    if args.direction:
        title += f" ({args.direction}s only)"
    if args.month:
        title += f" ({args.month})"
    print(title)
    _print_table(
        ["Date", "Amount", "Merchant", "Category", "Subcategory"],
        [(r["txn_date"], f"£{r['amount']:,.2f}", r["merchant_core"][:40], r["category"] or "", r["subcategory"] or "") for r in rows],
        align={1: "r"},
    )


def cmd_sql(args):
    """Run an arbitrary SQL query."""
    conn = _connect()
    try:
        cursor = conn.execute(args.query)
        if cursor.description:
            headers = [d[0] for d in cursor.description]
            rows = cursor.fetchall()
            _print_table(headers, [tuple(r) for r in rows])
            print(f"\n({len(rows)} rows)")
        else:
            conn.commit()
            print("Query executed (no result set).")
    except sqlite3.Error as e:
        print(f"SQL Error: {e}")
    finally:
        conn.close()


# ── CLI ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Query the ledger database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("summary", help="Overall ledger summary")

    sub.add_parser("monthly", help="Monthly totals")

    p_cat = sub.add_parser("category", help="Spending by category")
    p_cat.add_argument("--month", help="Filter to YYYY-MM")

    p_sub = sub.add_parser("subcategory", help="Spending by subcategory")
    p_sub.add_argument("--category", help="Filter to a specific category")
    p_sub.add_argument("--month", help="Filter to YYYY-MM")

    p_search = sub.add_parser("search", help="Search transactions")
    p_search.add_argument("term", help="Search term (matched against merchant_core and description)")
    p_search.add_argument("--limit", type=int, default=25, help="Max results (default 25)")

    p_top = sub.add_parser("top", help="Top N transactions by amount")
    p_top.add_argument("--n", type=int, default=20, help="Number of results (default 20)")
    p_top.add_argument("--month", help="Filter to YYYY-MM")
    p_top.add_argument("--direction", choices=["debit", "credit"], help="Filter to debits or credits")

    p_sql = sub.add_parser("sql", help="Run arbitrary SQL")
    p_sql.add_argument("query", help="SQL query string")

    args = parser.parse_args()

    commands = {
        "summary": cmd_summary,
        "monthly": cmd_monthly,
        "category": cmd_category,
        "subcategory": cmd_subcategory,
        "search": cmd_search,
        "top": cmd_top,
        "sql": cmd_sql,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
