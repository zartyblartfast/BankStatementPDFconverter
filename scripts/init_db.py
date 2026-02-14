"""Initialise the ledger SQLite database with a SQL Server-friendly schema.

Usage:  python -m scripts.init_db [--force]

Design notes for future SQL Server migration:
  - All constraints are explicitly named (PK_, FK_, UQ_, IX_).
  - String columns use reasonable max lengths (map to NVARCHAR(N)).
  - DECIMAL(12,2) for monetary amounts.
  - DATE stored as TEXT in ISO-8601 (YYYY-MM-DD); maps directly to DATE.
  - AUTOINCREMENT avoided; INTEGER PRIMARY KEY gives rowid alias in SQLite
    and maps to IDENTITY(1,1) in SQL Server.
  - No SQLite-specific syntax (e.g. no GLOB, no PRAGMA-dependent logic).
"""

import argparse
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "finance" / "ledger.db"

_SCHEMA_SQL = """
-- Categories lookup
CREATE TABLE IF NOT EXISTS categories (
    category_id     INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    CONSTRAINT UQ_categories_name UNIQUE (name)
);

-- Subcategories lookup (child of categories)
CREATE TABLE IF NOT EXISTS subcategories (
    subcategory_id  INTEGER PRIMARY KEY,
    category_id     INTEGER NOT NULL,
    name            TEXT NOT NULL,
    CONSTRAINT FK_subcategories_category
        FOREIGN KEY (category_id) REFERENCES categories (category_id),
    CONSTRAINT UQ_subcategories_cat_name UNIQUE (category_id, name)
);

-- Import log — one row per file import (dedup guard)
CREATE TABLE IF NOT EXISTS import_log (
    import_id       INTEGER PRIMARY KEY,
    source_file     TEXT NOT NULL,
    imported_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    row_count       INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT UQ_import_log_source UNIQUE (source_file)
);

-- Transactions — the core ledger
CREATE TABLE IF NOT EXISTS transactions (
    txn_id          INTEGER PRIMARY KEY,
    txn_date        TEXT    NOT NULL,                -- ISO-8601 date
    description_raw TEXT    NOT NULL,
    amount          REAL    NOT NULL,                -- DECIMAL(12,2) on SQL Server
    currency        TEXT    NOT NULL DEFAULT 'GBP',
    channel         TEXT,
    merchant_core   TEXT,
    subcategory_id  INTEGER,
    account_id      TEXT    NOT NULL DEFAULT 'HSBC_UK',
    source_file     TEXT,
    page            INTEGER,
    row_ref         TEXT,
    import_hash     TEXT    NOT NULL,
    excluded        INTEGER NOT NULL DEFAULT 0,       -- 1 = hide from reports
    exclude_reason  TEXT,
    CONSTRAINT FK_transactions_subcategory
        FOREIGN KEY (subcategory_id) REFERENCES subcategories (subcategory_id),
    CONSTRAINT UQ_transactions_hash UNIQUE (import_hash)
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS IX_transactions_date
    ON transactions (txn_date);
CREATE INDEX IF NOT EXISTS IX_transactions_category
    ON transactions (subcategory_id);
CREATE INDEX IF NOT EXISTS IX_transactions_merchant
    ON transactions (merchant_core);

-- Exceptions — flagged during ingestion for user resolution
CREATE TABLE IF NOT EXISTS exceptions (
    exception_id    INTEGER PRIMARY KEY,
    txn_id          INTEGER,
    type            TEXT NOT NULL,           -- 'uncategorised', 'unknown_subcategory', 'recon_warning'
    status          TEXT NOT NULL DEFAULT 'open',  -- 'open' | 'resolved'
    merchant_core   TEXT,
    description_raw TEXT,
    amount          REAL,
    txn_date        TEXT,
    detail          TEXT,                    -- JSON with extra context
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    resolved_at     TEXT,
    CONSTRAINT FK_exceptions_txn
        FOREIGN KEY (txn_id) REFERENCES transactions (txn_id)
);
CREATE INDEX IF NOT EXISTS IX_exceptions_status
    ON exceptions (status);
"""


def init_db(force: bool = False) -> Path:
    """Create (or recreate) the ledger database and return its path."""
    if force and DB_PATH.exists():
        DB_PATH.unlink()
        print(f"Deleted existing database: {DB_PATH}")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA_SQL)
    conn.close()
    print(f"Ledger database ready: {DB_PATH}")
    return DB_PATH


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initialise the ledger database")
    parser.add_argument("--force", action="store_true",
                        help="Drop and recreate the database")
    args = parser.parse_args()
    init_db(force=args.force)
