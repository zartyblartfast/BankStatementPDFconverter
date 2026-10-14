# Standalone bank CSV validation

This tool reads bank CSV downloads and **already normalized PDF transactions**. It imports no extraction, dashboard, database, ingestion, or ledger modules. It cannot import CSV transactions into a ledger. No external APIs are used.

## CLI

From the repository root (Windows project virtual environment):

```bash
.venv/Scripts/python.exe -m scripts.csv_validation \
  --pdf-json synthetic-pdf.json --csv synthetic-bank.csv \
  --account-id test-account --from-date 2025-01-01 --to-date 2025-01-31 \
  --out synthetic-validation.json
```

`--out` is optional. Without it, only status and finding counts are printed (no transaction details, amounts, account identifier, or source filenames). An explicit `.json` or `.html` output path creates a **new** report; existing files are never overwritten and directories are not automatically created. HTML renders escaped JSON without JavaScript. Detailed reports contain sensitive source paths and transaction findings: keep them local and protected; do not commit real reports.

Exit codes: `0` passed, `1` failed, `2` needs review, `3` unavailable. Missing/invalid command-line arguments are argparse usage errors (exit `2`), not comparison reports. Report-write failure exits `3`, even if the comparison passed.

Normalized PDF JSON must be an object, not a naked transaction list:

```json
{
  "account_id": "test-account",
  "coverage_start": "2025-01-01",
  "coverage_end": "2025-01-31",
  "complete": true,
  "transactions": [
    {
      "txn_date": "2025-01-01",
      "description_raw": "Synthetic shop",
      "amount": "-12.30",
      "source": "synthetic-pdf",
      "row_ref": "line-42"
    }
  ]
}
```

`complete` must be literal boolean `true`, representing a trusted, complete normalized extraction. This tool does **not** certify PDF extraction itself. Missing/incomplete extraction metadata can never produce a pass. Prefer string amounts; numeric JSON decimals are parsed directly as Decimal, but must still have exactly two decimal places.

CLI dates declare both the comparison interval and each bank download's requested interval. Do not relabel a narrower download as covering that interval. For multiple files with different download windows, use the Python API to supply per-file coverage. Multiple differing CLI files with the same declared window require overlap review rather than being summed.

## Python API

```python
from scripts.csv_validation import load_csv_transactions, compare_transactions

doc = load_csv_transactions(
    "synthetic-bank.csv", "test-account",
    coverage_start="2025-01-01", coverage_end="2025-01-31",
)
report = compare_transactions(
    [{"txn_date": "2025-01-01", "description_raw": "Synthetic shop", "amount": "-12.30"}],
    [doc], account_id="test-account",
    coverage_start="2025-01-01", coverage_end="2025-01-31",
    pdf_coverage_start="2025-01-01", pdf_coverage_end="2025-01-31",
    pdf_complete=True, pdf_account_id="test-account",
)
```

### Input contracts

- `load_csv_transactions(path, account_id, coverage_start=None, coverage_end=None)` returns frozen `CSVDocument(account_id, coverage_start, coverage_end, transactions, source, sha256, complete=True)`.
- `transactions` is a tuple of frozen `Transaction(txn_date, description_raw, amount, account_id, source, row_ref, occurrence_id)` values. Dates are `datetime.date`, money is `Decimal`. Coverage accepts ISO strings or date objects and is stored as dates.
- Bank format is headerless UTF-8 (optional leading UTF-8 BOM), exactly three comma-separated columns: strict `DD/MM/YYYY`, nonempty description, signed two-decimal money (`-12.30`, `12.30`, `+12.30`). Negative amounts are expenses; positive amounts are credits. Quoted commas and multiline descriptions are supported. Double quotes may appear only in quoted fields, escaped as doubled quotes (`""`); a closing quote must be followed by a comma, record ending, or end of file. Bare quotes in unquoted descriptions and characters (including spaces) after closing quotes are rejected.
- `row_ref` preserves each record's one-based **start physical line**. `occurrence_id` is file SHA-256 plus that line, so legitimate repeated payments remain separate. `source` preserves the supplied path.
- Blank rows, headers, wrong column counts, malformed quoted records, invalid dates, non-UTF-8, fractions of a cent, exponent notation, whitespace money, and nonfinite amounts fail the entire load with `CSVValidationError`; no partial rows are returned. Error messages omit paths and financial contents. Files are opened only for reading.
- Missing coverage is permitted at load time but makes comparison unavailable. Coverage comes from the actual bank download request, never from earliest/latest transaction dates. Caller-supplied declarations are trusted; the three-column CSV cannot independently prove account identity or completeness.
- PDF transaction dictionaries require `txn_date` (ISO/date), `description_raw`, and `amount` (Decimal or string with exactly two decimals). Optional `account_id` must agree, `source` and `row_ref` are retained. Float money is rejected. Parser transaction objects or their direct `asdict()` results are not this API contract: a future integration adapter must supply these mapping keys, convert parser money to canonical two-decimal strings, and map source metadata to `source` without changing extraction here. Occurrence identity uses input position, even if row refs repeat.
- `compare_transactions(pdf_transactions, csv_documents, *, account_id=None, coverage_start=None, coverage_end=None, pdf_coverage_start=None, pdf_coverage_end=None, pdf_complete=False, pdf_account_id=None)` returns a JSON-safe dictionary. Supplying `account_id` explicitly assigns otherwise unlabelled PDF rows to that account; optional PDF-level metadata, if supplied, must agree. CLI always requires PDF account metadata.

### Coverage and matching

All intervals are **inclusive**. Only the requested interval is compared, and outside rows are counted under `excluded_outside_period`, not classified as missing. Both sides must explicitly cover the whole requested interval on the same account. Adjacent nonoverlapping CSV windows can cover it jointly; gaps, unknown windows, incomplete documents, and account mismatches return `unavailable` with no matches. Rows outside their own source's declared coverage also invalidate comparison.

Exact matching uses date, Decimal amount, and description normalized only by case-folding and collapsing whitespace. Equal duplicate groups match as multisets with all PDF/CSV refs retained: no invented one-to-one occurrence pairing. Unequal exact-group multiplicities are a definitive failure with ambiguous identities (`multiplicity_mismatch`). Remaining same-date/same-amount groups are uncertain `description_candidate` groups, never forced matches. With complete compatible nonoverlapping coverage, unequal candidate-group occurrence counts are nevertheless a definitive failure, even if shortages on opposite sides cancel in the overall count and monetary totals. All candidate refs remain unpaired in `ambiguous`; no arbitrary row is assigned to `pdf_only` or `csv_only`. Equal-cardinality description candidates alone require review, not failure. References are not stripped, merchant types are not inferred, and dates/signs/amounts are not fuzzed.

Only byte-identical entire files with the **same account and coverage declaration** are skipped, with `skipped_duplicates` provenance. Equal individual transaction fingerprints are never deduplicated. Differing overlapping downloads require `needs_review`, reason `overlap_requires_review`, with interval/source/row details even if no rows share a fingerprint. No combined CSV totals or matches are asserted in this case; individual document totals are supplied instead. `excluded_outside_period` is still populated for both sides after exact-file duplicate skipping. Its CSV value counts source-row occurrences across retained documents, not a deduplicated transaction total; overlap multiplicity remains unresolved. Overlaps outside the requested comparison interval do not interfere with an otherwise nonoverlapping requested interval. This intentionally conservative policy does not attempt to prove overlap multiplicity from partial downloads.

### Report contract

Always-present fields:

- `status`: `passed`, `failed`, `needs_review`, or `unavailable`; `reason`, `account_id`, `coverage_start`, `coverage_end`.
- `matched`: equal exact groups (`count`, `pdf` refs, `csv` refs).
- `pdf_only`, `csv_only`: unmatched rows with no candidate; `ambiguous`: unpaired groups with a reason and both sides' refs.
- `counts`: matched **occurrences**, PDF-only rows, CSV-only rows, ambiguous **groups** (not rows).
- `coverage_warnings`, `overlap_requires_review`, `skipped_duplicates`, `csv_document_totals`.
- `totals`: `pdf`/`csv` each with `count`, `income`, `expense` (positive gross), `net` (signed), or `null` when unavailable. Monetary totals are decimal strings and computed under an input-sized Decimal context, not float/default-precision arithmetic.
- `totals_provisional`, `excluded_outside_period`.

A pass requires complete comparable coverage and all occurrences exact-matched. With that coverage established and overlap uncertainty excluded, mismatched counts, gross credits (`income`), gross debits (`expense`), or net totals fail independently of ambiguous descriptions. Matching net totals alone cannot pass or hide gross discrepancies. Definitive differences take precedence over candidate review; incomplete/unavailable input takes precedence over all matching. Overlap review deliberately stops matching, so zero finding counts in such a report do not mean the datasets agree.

## Synthetic tests

```bash
# Optionally set TMPDIR to an existing writable temporary directory.
  .venv/Scripts/python.exe -m unittest discover -s tests -p test_csv_validation.py -v
```

Tests create only synthetic files in temporary directories (honouring caller TMPDIR), including real CLI subprocess runs and local JSON/HTML report reads. They verify input hashes remain unchanged and private details are absent from terminal summaries.
