# Explicit opt-in migrations

The shipped migration chain contains `001`–`004`. The runner requires an explicit
existing database path; CLI application also requires `--apply`:

```bash
python -m scripts.migrate_db --db EXPLICIT_EXISTING_DATABASE --apply
```

Importing the runner, dashboard or a read service never applies migrations.

## Shipped migrations

- `001_import_metadata.sql` adds source documents, aliases/content history,
  transaction-source links and approved import-batch metadata. It does not backfill
  those tables or rewrite existing transactions.
- `002_accounts.sql` adds the account registry and registers only account identifiers
  already observed in transactions or source documents, preserving historical rows.
  Currency evidence is account-local and must be unanimous valid text. Only an
  observed `HSBC_UK` account with GBP evidence receives the verified UK parser and
  enabled import flags; other observed accounts require reviewed setup. An empty
  ledger gains no account rows. This is not general account provisioning or Isle of
  Man parser verification.
- `003_account_statement_folders.sql` adds the unique per-account saved-folder
  mapping. It seeds no mappings, moves/imports no files and establishes no ownership.
- `004_internal_transfers.sql` adds transfer groups, members, active-member uniqueness
  and immutable decision-audit rows. It seeds no decisions, infers no pairs and
  changes no transaction amounts, categories, notes or exclusion flags. Classification
  requires a separate explicit reviewed decision.

The runner records applied versions and original-byte checksums in its own
`schema_migrations` table. Account provisioning, folder assignment, statement import,
transfer confirmation and report generation remain separate operations.

## Authoring and integrity

SQL files must have positive numeric versions (at least three digits), a lowercase
underscore-separated description and `.sql` suffix: `005_example.sql`. Numeric
versions must be unique. Original UTF-8 file bytes are SHA-256 checksummed; do not
edit, rename or remove an applied migration. Add a new migration instead. The runner
refuses changed/missing applied files and unapplied versions at or below the highest
applied version; rerunning an unchanged applied chain is a no-op.

The runner executes SQLite-complete statements individually, not `executescript`, so
DDL, DML and tracking stay in one `BEGIN IMMEDIATE` transaction for the entire pending
batch. Quoted semicolons and trigger bodies are supported. Do not include transaction
control, PRAGMA, ATTACH/DETACH or references to the tracking table. This is a trusted,
reviewed local migration mechanism, not a sandbox for untrusted SQL.

Run synthetic-fixture tests, verify a protected backup and independently review
pending migrations before explicitly applying them to a live ledger.
