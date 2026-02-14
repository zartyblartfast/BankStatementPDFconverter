# HSBC UK PDF Statement Converter & Ledger Dashboard

End-to-end personal finance pipeline: extract transactions from HSBC UK statement PDFs, categorise them, store in a SQLite ledger, and explore via an interactive dashboard with reports.

## Security

- No credentials required.
- Processing is entirely local — no external API calls.
- The extractor ignores statement header data (name/address/account details) and only emits transaction rows.
- Statement PDFs and the ledger database are gitignored.

## Requirements

- Python 3.11+
- `pdfplumber` for PDF text extraction
- `flask` for the web dashboard

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

## Quick Start

### 1. Dashboard (recommended)

The Flask dashboard provides a web UI for ingestion, exception handling, and reports:

```bash
python -m scripts.dashboard
```

Open http://127.0.0.1:5000 — from here you can:

- **Ingest PDFs** — select a statement from `StatementsPDF/`, process it through the full pipeline
- **Review Exceptions** — assign categories to unrecognised transactions via dropdown, with optional "Remember rule" to update `category_rules.csv` automatically
- **View Reports** — interactive Income vs Expenditure report with dual donut charts and drill-down tables

### 2. Command-line pipeline

Each step can also be run individually:

```bash
# Step 1: Extract transactions from a PDF
python -m scripts.hsbc_pdf_to_txn --pdf StatementsPDF/2026-01-10_Statement.pdf

# Step 2: Categorise all extracted transactions using rules
python -m scripts.apply_categories

# Step 3: Initialise the ledger database (first time only)
python -m scripts.init_db

# Step 4: Import categorised transactions into the ledger
python -m scripts.import_to_ledger

# Step 5: Generate the HTML report
python -m scripts.report_income_vs_expense
```

Or run the full pipeline for a single PDF in one step:

```bash
python -m scripts.ingest_pipeline --pdf StatementsPDF/2026-02-10_Statement.pdf
```

## Project Structure

```
├── config/
│   ├── categories.json          # Category tree (category → subcategories)
│   └── category_rules.csv       # Merchant → category/subcategory mapping rules
├── finance/
│   ├── exports/                  # CSV/JSON outputs from extraction (gitignored)
│   ├── ledger.db                 # SQLite ledger database (gitignored)
│   └── reports/                  # Generated HTML reports (gitignored)
├── scripts/
│   ├── hsbc_pdf_to_txn.py       # PDF → transaction extraction
│   ├── triage_descriptions.py   # Description normalisation & merchant_core extraction
│   ├── apply_categories.py      # Apply category_rules.csv to extracted transactions
│   ├── init_db.py               # SQLite schema definition & initialisation
│   ├── import_to_ledger.py      # Import categorised CSV into ledger database
│   ├── ingest_pipeline.py       # End-to-end pipeline orchestrator
│   ├── report_income_vs_expense.py  # Generate interactive HTML report
│   ├── ledger_query.py          # CLI queries against the ledger
│   ├── dashboard.py             # Flask web dashboard
│   └── categorise.py            # Category rule matching logic
├── templates/                    # Jinja2 templates for the dashboard
├── StatementsPDF/               # Bank statement PDFs (gitignored)
├── tests/                        # Unit tests
└── run_all_statements.py        # Batch reconciliation test runner
```

## Features

### PDF Extraction (`hsbc_pdf_to_txn.py`)

Flags:

- `--pdf` — path to the HSBC statement PDF (required)
- `--outdir` — directory for all output files (default `finance/exports`)
- `--account-id` — account identifier (default `HSBC_UK`)
- `--extractor` — PDF extraction method: `words` (default) or `text`. The `words` extractor groups pdfplumber word objects by Y coordinate for better column alignment; falls back to `text` automatically if `words` yields no lines.

Outputs (same basename, written under `--outdir`):

- `{basename}_transactions.csv` — normalized transactions
- `{basename}_transactions.json` — same data in JSON
- `{basename}_warnings.txt` — lines that could not be parsed
- `{basename}_suspects.csv` — transactions flagged for QA review
- `{basename}_suspects_context.txt` — raw PDF lines around each suspect
- `{basename}_running_balance_walk.csv` — per-transaction running balance walk comparing computed balance to PDF balance. Includes `balance_row_ref` (the actual line where the balance token was found, which may differ from the transaction start line)
- `{basename}_extractor_diagnostics.csv` — word-extractor metrics (lines per page, token histogram, suspect merged lines)

Additional debug exports (only written when reconciliation diffs are non-zero):

- `{basename}_unmatched_amount_lines.csv`
- `{basename}_keyword_lines.csv`
- `{basename}_used_non_txn_amount_lines.csv`
- `{basename}_balance_recon_gaps.csv`
- `{basename}_balance_delta_mismatches.csv`
- `{basename}_nonprefix_continuation_txns.csv`

### Categorisation

- **180 rules** in `config/category_rules.csv` mapping `merchant_core` to category/subcategory
- 100% coverage across 1,028 transactions from 14 statements
- Rules can be added manually or via the dashboard's "Remember rule" feature

### Ledger Database

SQLite database (`finance/ledger.db`) with tables:

- **transactions** — all imported transactions with category, exclusion flags
- **categories / subcategories** — lookup tables seeded from `categories.json`
- **import_log** — tracks which source files have been imported
- **exceptions** — uncategorised transactions flagged for user resolution

### Transaction Exclusion

Unusual transactions (e.g. a short-term loan) can be marked as excluded:

- `excluded` flag + `exclude_reason` on each transaction
- Reports filter out excluded transactions by default
- "Show excluded" toggle in the report to reveal them (greyed out with strikethrough + EXCLUDED badge)

### Seamless Dedup on Re-import

When re-processing a PDF (e.g. replacing a partial month with the full statement):

1. Old transactions for that source file are deleted
2. All transactions from the new PDF are imported fresh
3. Any excluded flags are preserved by matching on date/amount/description

No user input required — fully automatic.

### Income vs Expenditure Report

Interactive HTML report (`finance/reports/income_vs_expense.html`):

- **Dual donut charts** — income and expenditure broken down by subcategory
- **Net balance indicator** between the donuts
- **From/To date dropdowns** — filter by month range (interdependent)
- **3-level drill-down tables** — Category → Subcategory → Individual transactions
- **Sort controls** — sort transactions by date or merchant
- **Show excluded toggle** — reveal/hide excluded transactions

### Flask Dashboard

Web UI at http://127.0.0.1:5000 with:

- **Home** — stats overview (transactions, statements, income/expense, exceptions), import history, quick actions
- **Ingest PDF** — select and process PDFs from `StatementsPDF/`
- **Exceptions** — inline category assignment with dropdowns, bulk resolution for same-merchant transactions, "Remember rule" to auto-update `category_rules.csv`
- **Report** — embedded Income vs Expenditure report with dashboard nav bar
- **Nav badge** — live count of open exceptions (amber) or green checkmark when all clear

## QA Workflow (recommended)

1. Run the converter and review the stdout summary:

   - `candidate_date_lines_in_pdf` / `transaction_count`
   - `total_credits` / `total_debits` / `net`
   - `diff_credits_vs_statement_in` / `diff_debits_vs_statement_out` / `diff_txn_delta_vs_statement_delta` — all should be `0.00`
   - `any_parse_warnings_count` / `suspects_count`
   - `extractor_suspect_merged_line_count` — non-zero indicates possible column mis-merge in the word extractor
   - Full extractor diagnostics (lines per page, token histogram) are also printed to stdout when using the `words` extractor

2. If `any_parse_warnings_count > 0`, open `{basename}_warnings.txt`.

3. If `suspects_count > 0`, open:

   - `{basename}_suspects.csv` for the flagged transactions + reasons
   - `{basename}_suspects_context.txt` to see the raw PDF text lines around each suspect's `row_ref`

4. If the statement contains totals like `Payments In` / `Payments Out`, the converter will print them (and the difference vs parsed totals) to help reconciliation.

## Normalized output schema

- `source_file`
- `account_id`
- `txn_date` (ISO `YYYY-MM-DD`)
- `description_raw`
- `amount` (expenses negative, income positive)
- `currency` (defaults to `GBP` in v1)
- `page` (1-based)
- `row_ref` (stable reference like `{page}:{line_index}`)

## Supported HSBC statement formats (v1)

![HSBC statement example](docs/HSBC_Template_Statement.jpg)

This converter targets HSBC UK *text-based* PDFs where transaction tables appear as lines containing:

- Date (e.g. `01 Jan 26` or `01/01/26`)
- Description (may wrap onto following lines)
- `Money Out` / `Money In` (and sometimes `Balance`)

Notes:

- Wrapped descriptions are joined to the previous transaction row.
- Amount parsing supports commas, optional `£`, trailing minus, and parentheses.
- Balance marker lines (`BALANCE BROUGHT FORWARD`, `BALANCE CARRIED FORWARD`, `OPENING BALANCE`, `CLOSING BALANCE`) are automatically excluded from transactions and used only for reconciliation.
- Deduplication is by `row_ref` (`page:line_index`) only. Legitimate repeated transactions on different lines are preserved.
- Recognised transaction prefixes: `DD`, `VIS`, `BP`, `CR`, `SO`, `ATM`, `DR`, `FPI`, `FPO`, `TFR`, `TRF`, `BGC`, `CHG`, `CHARGE`, `CASH`, `DEP`, `POS`, `CARD`, `OBP`, `)))`.
- **Foreign currency transactions** — lines matching the FX conversion pattern (`USD 24.00 @ 1.25…`) are treated as description-only; the GBP-equivalent amount is taken from the subsequent `Visa Rate` line instead. A look-ahead checks for `DR Non-Sterling` / `CR Non-Sterling` to determine debit vs credit direction.
- When a transaction line has no explicit balance column (single money token), the running balance is updated synthetically (`last_balance += amount`) so that subsequent balance-delta sign inference remains accurate.

## Troubleshooting

- If you see:

  `Extraction likely failed (PDF may be scanned/image-based). OCR not implemented in v1.`

  the PDF likely contains images rather than selectable text. OCR is intentionally not implemented in this version.

- Check `{basename}_warnings.txt` for any lines that looked like transactions but could not be parsed.

## Running tests

```bash
python -m unittest discover -s tests -v
```

## Batch statement testing

To run all PDFs in `StatementsPDF/` through the parser and check reconciliation:

```bash
python run_all_statements.py
```

Reports `PASS`/`FAIL` per statement and a final summary. A statement passes when `diff_credits`, `diff_debits`, and `diff_delta` are all within tolerance of zero.
