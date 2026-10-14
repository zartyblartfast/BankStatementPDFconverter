-- Explicit confirmed decisions only: no seed, categories or ledger rewrites.
CREATE TABLE transfer_groups (
    group_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('paired','unmatched','revoked')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE transfer_members (
    member_id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES transfer_groups(group_id),
    txn_id INTEGER NOT NULL REFERENCES transactions(txn_id),
    counterpart_account_id TEXT NOT NULL REFERENCES accounts(account_id),
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ended_at TEXT,
    UNIQUE(group_id,txn_id)
);
CREATE UNIQUE INDEX UX_transfer_active_txn ON transfer_members(txn_id) WHERE active=1;
CREATE TABLE transfer_audit (
    audit_id INTEGER PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES transfer_groups(group_id),
    action TEXT NOT NULL CHECK(action IN ('pair','unmatched','revoke')),
    preview_token TEXT NOT NULL UNIQUE,
    payload_signature TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TRIGGER transfer_audit_no_update BEFORE UPDATE ON transfer_audit BEGIN
    SELECT RAISE(ABORT,'transfer audit is immutable');
END;
CREATE TRIGGER transfer_audit_no_delete BEFORE DELETE ON transfer_audit BEGIN
    SELECT RAISE(ABORT,'transfer audit is immutable');
END;
