# Multi-account statements and confirmed transfers

## Setup contract

The dashboard supports separately scoped HSBC UK and HSBC Isle of Man GBP accounts.
It does not infer ownership, currency or parser suitability from a folder or category.
The existing parser policy uses `hsbc_uk` for verified GBP accounts; an Isle of Man
label is not an independent parser certification.

From the repository root, use the environment installation and explicit base-ledger
initialisation/migration commands in the README. The opt-in migrations are:

- `001_import_metadata.sql`: document identity and edit-preserving import metadata.
- `002_accounts.sql`: account registry derived from existing account evidence.
- `003_account_statement_folders.sql`: saved folder mappings, initially empty.
- `004_internal_transfers.sql`: transfer decisions and immutable audit, initially empty.

A fresh empty ledger remains without accounts after migration. There is no shipped
account-provisioning CLI or wizard. Before importing, an administrator must review
and provision each registry record against `scripts/services/accounts.py` policy:
stable account ID, display name, currency, institution, ownership, lifecycle dates,
parser key, parser verification and import permission. Import requires an owned,
open GBP account with verified `hsbc_uk` policy and imports enabled. Do not invent
verification evidence simply to enable a selector. Existing non-UK accounts remain
setup-pending unless separately reviewed; migration only enables the legacy UK
account when its existing currency evidence qualifies.

For an existing ledger, take a verified protected backup and rehearse changes on an
isolated copy first. Startup and read-only pages never apply migrations. Do not use
legacy pipeline/CSV imports to bypass setup or dashboard review.

## Saved folders and import review

1. Keep source PDFs under `StatementsPDF/`, optionally in generic account subfolders.
2. On **Import Statements**, select the intended enabled account and offered folder,
   then **Save folder**. `.` means the statement root. Saving persists the mapping;
   it neither moves files nor imports them.
3. The list includes safe nested PDFs but excludes subtrees mapped to another account.
   An unmapped, missing or unsafe folder does not fall back to all statements.
4. Choose **Review Selected PDF**, inspect account scope and reconciliation, then
   explicitly approve an eligible preview. Blocked previews require investigation.
   Changed ledger, source or account/folder policy can require a fresh review.

Re-import matching is account/document-scoped. Safe matches preserve IDs and user
metadata; ambiguous changes are not silently delete/reinserted.

## Confirm and undo a transfer

Use **Transaction search**, not a separate transfer-review page. Select exactly two
individual rows on the current page from different owned GBP accounts, with equal
and opposite principal amounts. Posting dates may differ. **Select all matching
results** is not a transfer-pair selection.

Choose **Mark as transfer**, inspect the preview, and **Confirm**, or cancel without
saving. Use the confirmed transfer's **Undo** action and confirm its preview to revoke
the decision. Undo restores prior report treatment, including any pre-existing
exclusion, and retains audit history. Categories, notes and future rules are not
changed by pairing or Undo. If a confirmation outcome is unknown, reload and check
saved status before retrying.

A transfer-like category or description is not confirmation. The migration creates
support tables only; it does not classify transactions automatically.

## Totals and classic reports

- Search **Credits** and **Debits** are raw cash movement and include transfers.
- Default income/expenditure totals exclude confirmed internal-transfer principal;
  **Transfers in** and **Transfers out** are shown separately for the selected scope.
- Fees remain expenditure unless separately excluded. Account/date filters may put
  the counterpart outside the visible scope; that does not make principal income.
- **Show excluded** intentionally restores raw rows to charts and totals, including
  confirmed transfer principal and other excluded transactions. Turn it off for
  adjusted totals. It is not merely a row-visibility control.
- The familiar donut/pie, bar, heatmap and grouped drill-down presentation remains;
  transfer policy does not introduce a replacement chart layout.

## Privacy

The entire `StatementsPDF/` tree is ignored, including nested folders and non-PDF
source files. Downloaded CSVs, the ledger, generated outputs and local snapshots are
also ignored. Git ignore rules do not encrypt files or untrack existing content.
Keep documents and backups separately protected, and never publish real account
numbers, source filenames, transaction examples or local user paths in documentation.
