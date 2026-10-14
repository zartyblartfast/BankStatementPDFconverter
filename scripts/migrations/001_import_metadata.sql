-- Additive M1 metadata. No backfill or updates to any legacy table.
CREATE TABLE source_documents (
    document_id INTEGER PRIMARY KEY,
    account_id TEXT NOT NULL,
    document_key TEXT NOT NULL,
    source_identity TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    coverage_start TEXT,
    coverage_end TEXT,
    coverage_row_ref TEXT,
    reconciliation_status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(account_id, document_key)
);
CREATE TABLE source_document_aliases (
    account_id TEXT NOT NULL,
    alias_key TEXT NOT NULL,
    document_id INTEGER NOT NULL REFERENCES source_documents(document_id),
    PRIMARY KEY(account_id, alias_key)
);
CREATE TABLE source_document_contents (
    document_id INTEGER NOT NULL REFERENCES source_documents(document_id),
    content_hash TEXT NOT NULL,
    PRIMARY KEY(document_id, content_hash)
);
CREATE INDEX IX_source_document_contents_hash ON source_document_contents(content_hash);
CREATE TABLE transaction_sources (
    txn_id INTEGER PRIMARY KEY REFERENCES transactions(txn_id),
    document_id INTEGER NOT NULL REFERENCES source_documents(document_id),
    row_ref TEXT NOT NULL,
    occurrence INTEGER NOT NULL,
    UNIQUE(document_id, row_ref, occurrence)
);
CREATE INDEX IX_transaction_sources_document ON transaction_sources(document_id);
CREATE TABLE import_batches (
    batch_id INTEGER PRIMARY KEY,
    preview_token TEXT NOT NULL UNIQUE,
    approved_payload_hash TEXT NOT NULL,
    document_id INTEGER NOT NULL REFERENCES source_documents(document_id),
    input_versions TEXT NOT NULL,
    added_count INTEGER NOT NULL,
    matched_count INTEGER NOT NULL,
    total_current INTEGER NOT NULL,
    exceptions_created INTEGER NOT NULL,
    approved INTEGER NOT NULL CHECK(approved=1),
    reconciliation_status TEXT NOT NULL CHECK(reconciliation_status='passed'),
    imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
