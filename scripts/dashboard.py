"""Flask dashboard for the bank statement ledger.

Usage:  python -m scripts.dashboard

Runs a local web server at http://127.0.0.1:5000
"""

import csv
import json
import re
import sqlite3
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file

from scripts.init_db import DB_PATH
from scripts.ingest_pipeline import run_pipeline

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = PROJECT_ROOT / "templates"
STATEMENTS_DIR = PROJECT_ROOT / "StatementsPDF"
REPORTS_DIR = PROJECT_ROOT / "finance" / "reports"
RULES_CSV = PROJECT_ROOT / "config" / "category_rules.csv"
CATEGORIES_JSON = PROJECT_ROOT / "config" / "categories.json"

app = Flask(__name__, template_folder=str(TEMPLATES_DIR))
app.secret_key = "ledger-dashboard-local-dev"


# ── Helpers ──

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _open_exception_count() -> int:
    """Count open exceptions for the nav badge."""
    if not DB_PATH.exists():
        return 0
    conn = _get_db()
    count = conn.execute("SELECT COUNT(*) FROM exceptions WHERE status = 'open'").fetchone()[0]
    conn.close()
    return count


def _load_cat_tree() -> dict[str, list[str]]:
    """Load categories.json as {category: [subcategories]}."""
    with open(CATEGORIES_JSON, encoding="utf-8") as f:
        return json.load(f)


@app.context_processor
def inject_globals():
    """Make exception_count available to all templates (for nav badge)."""
    return {"exception_count": _open_exception_count()}


# ── Routes ──

@app.route("/")
def home():
    stats = {
        "total_txns": 0,
        "statements": 0,
        "statement_range": "—",
        "open_exceptions": 0,
        "total_income": "0.00",
        "total_expense": "0.00",
        "net": "0.00",
        "net_raw": 0,
        "last_import": "—",
    }
    recent_imports = []

    if DB_PATH.exists():
        conn = _get_db()

        row = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()
        stats["total_txns"] = row[0]

        row = conn.execute(
            "SELECT COUNT(DISTINCT source_file) FROM transactions"
        ).fetchone()
        stats["statements"] = row[0]

        row = conn.execute(
            "SELECT MIN(source_file), MAX(source_file) FROM transactions"
        ).fetchone()
        if row[0]:
            # Extract YYYY-MM from filenames like 'StatementsPDF\2025-01-10_Statement.pdf'
            dates = [re.search(r'(\d{4}-\d{2})', f) for f in (row[0], row[1])]
            if dates[0] and dates[1]:
                stats["statement_range"] = f"{dates[0].group(1)} to {dates[1].group(1)}"

        row = conn.execute("SELECT COUNT(*) FROM exceptions WHERE status = 'open'").fetchone()
        stats["open_exceptions"] = row[0]

        row = conn.execute(
            "SELECT SUM(CASE WHEN amount > 0 AND excluded = 0 THEN amount ELSE 0 END),"
            "       SUM(CASE WHEN amount < 0 AND excluded = 0 THEN ABS(amount) ELSE 0 END)"
            " FROM transactions"
        ).fetchone()
        income = row[0] or 0
        expense = row[1] or 0
        net = income - expense
        stats["total_income"] = f"{income:,.2f}"
        stats["total_expense"] = f"{expense:,.2f}"
        stats["net"] = f"{'+' if net >= 0 else '-'}{abs(net):,.2f}"
        stats["net_raw"] = net

        row = conn.execute(
            "SELECT imported_at FROM import_log ORDER BY imported_at DESC LIMIT 1"
        ).fetchone()
        if row:
            stats["last_import"] = row[0][:16].replace("T", " ")

        imports = conn.execute(
            "SELECT source_file, row_count, imported_at FROM import_log ORDER BY source_file"
        ).fetchall()
        recent_imports = [dict(r) for r in imports]

        conn.close()

    return render_template("home.html", active_page="home", stats=stats,
                           recent_imports=recent_imports)


@app.route("/ingest", methods=["GET", "POST"])
def ingest():
    result = None

    if request.method == "POST":
        pdf_path = request.form.get("pdf", "")
        if pdf_path:
            result = run_pipeline(pdf_path)
            # Update import_log
            if not result.error:
                conn = _get_db()
                conn.execute(
                    "INSERT OR REPLACE INTO import_log (source_file, row_count) VALUES (?, ?)",
                    (result.source_file, result.imported_new),
                )
                conn.commit()
                conn.close()
                flash(f"Processed {result.extracted} transactions from {Path(pdf_path).name}", "success")
            else:
                flash(result.error, "error")
        else:
            flash("No PDF selected.", "error")

    # List available PDFs
    pdfs = []
    if STATEMENTS_DIR.exists():
        imported_files = set()
        imported_counts = {}
        if DB_PATH.exists():
            conn = _get_db()
            rows = conn.execute(
                "SELECT source_file, COUNT(*) as cnt FROM transactions GROUP BY source_file"
            ).fetchall()
            for r in rows:
                imported_files.add(r["source_file"])
                imported_counts[r["source_file"]] = r["cnt"]
            conn.close()

        for pdf_file in sorted(STATEMENTS_DIR.glob("*.pdf")):
            rel_path = str(pdf_file.relative_to(PROJECT_ROOT))
            pdfs.append({
                "name": pdf_file.name,
                "path": rel_path,
                "size_kb": f"{pdf_file.stat().st_size / 1024:.0f}",
                "imported": rel_path in imported_files,
                "txn_count": imported_counts.get(rel_path, 0),
            })

    return render_template("ingest.html", active_page="ingest", pdfs=pdfs, result=result)


@app.route("/exceptions")
def exceptions_page():
    exceptions = []
    if DB_PATH.exists():
        conn = _get_db()
        rows = conn.execute(
            "SELECT * FROM exceptions WHERE status = 'open' ORDER BY txn_date, merchant_core"
        ).fetchall()
        exceptions = [dict(r) for r in rows]
        conn.close()

    cat_tree = _load_cat_tree()
    categories = sorted(cat_tree.keys())

    return render_template("exceptions.html", active_page="exceptions",
                           exceptions=exceptions, categories=categories, cat_tree=cat_tree)


@app.route("/api/resolve-exception", methods=["POST"])
def resolve_exception():
    """AJAX endpoint to resolve an exception — assign category/subcategory."""
    data = request.get_json()
    exc_id = data.get("exception_id")
    category = data.get("category", "")
    subcategory = data.get("subcategory", "")
    remember = data.get("remember_rule", False)
    merchant_core = data.get("merchant_core", "")

    if not exc_id or not category or not subcategory:
        return jsonify({"ok": False, "error": "Missing required fields."})

    conn = _get_db()

    try:
        # Look up subcategory_id
        row = conn.execute(
            "SELECT s.subcategory_id FROM subcategories s "
            "JOIN categories c ON s.category_id = c.category_id "
            "WHERE c.name = ? AND s.name = ?",
            (category, subcategory),
        ).fetchone()

        if not row:
            return jsonify({"ok": False, "error": f"Unknown category/subcategory: {category}/{subcategory}"})

        subcat_id = row[0]

        # Get the exception
        exc = conn.execute(
            "SELECT * FROM exceptions WHERE exception_id = ?", (exc_id,)
        ).fetchone()
        if not exc:
            return jsonify({"ok": False, "error": "Exception not found."})

        # Update the transaction's subcategory
        if exc["txn_id"]:
            conn.execute(
                "UPDATE transactions SET subcategory_id = ?, merchant_core = ? WHERE txn_id = ?",
                (subcat_id, merchant_core, exc["txn_id"]),
            )

        # Mark exception resolved
        conn.execute(
            "UPDATE exceptions SET status = 'resolved', resolved_at = strftime('%Y-%m-%dT%H:%M:%S','now') "
            "WHERE exception_id = ?",
            (exc_id,),
        )

        # Bulk resolve: if same merchant_core has other open exceptions, resolve them too
        bulk_resolved = []
        if merchant_core:
            other_excs = conn.execute(
                "SELECT exception_id, txn_id FROM exceptions "
                "WHERE merchant_core = ? AND status = 'open' AND exception_id != ?",
                (merchant_core, exc_id),
            ).fetchall()

            for other in other_excs:
                if other["txn_id"]:
                    conn.execute(
                        "UPDATE transactions SET subcategory_id = ? WHERE txn_id = ?",
                        (subcat_id, other["txn_id"]),
                    )
                conn.execute(
                    "UPDATE exceptions SET status = 'resolved', "
                    "resolved_at = strftime('%Y-%m-%dT%H:%M:%S','now') "
                    "WHERE exception_id = ?",
                    (other["exception_id"],),
                )
                bulk_resolved.append(other["exception_id"])

        # Remember rule — add to category_rules.csv
        if remember and merchant_core:
            _add_category_rule(merchant_core, category, subcategory)

        conn.commit()
        return jsonify({"ok": True, "bulk_resolved": bulk_resolved})

    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)})
    finally:
        conn.close()


def _add_category_rule(merchant_core: str, category: str, subcategory: str):
    """Append a new rule to category_rules.csv (if not already present)."""
    existing = set()
    if RULES_CSV.exists():
        with open(RULES_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing.add(row.get("merchant_core", "").strip().upper())

    mc_upper = merchant_core.strip().upper()
    if mc_upper in existing:
        return  # already has a rule

    with open(RULES_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([merchant_core, "", category, subcategory, "auto-added by dashboard"])


@app.route("/report")
def report():
    """Show the report wrapped in the dashboard nav."""
    report_path = REPORTS_DIR / "income_vs_expense.html"
    if not report_path.exists():
        flash("Report not found. Please generate it first.", "error")
        return redirect(url_for("home"))
    return render_template("report.html", active_page="report")


@app.route("/report/raw")
def report_raw():
    """Serve the raw HTML report (used by the iframe)."""
    report_path = REPORTS_DIR / "income_vs_expense.html"
    if not report_path.exists():
        return "Report not found.", 404
    return send_file(str(report_path))


if __name__ == "__main__":
    print(f"\n  Ledger Dashboard starting...")
    print(f"  Database: {DB_PATH}")
    print(f"  Statements: {STATEMENTS_DIR}")
    print(f"  Open: http://127.0.0.1:5000\n")
    app.run(debug=True, port=5000)
