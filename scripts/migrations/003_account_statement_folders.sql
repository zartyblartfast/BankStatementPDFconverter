-- UI file scope only: not evidence of bank-account ownership. No seeded mapping.
CREATE TABLE account_statement_folders (
    account_id TEXT PRIMARY KEY NOT NULL REFERENCES accounts(account_id),
    folder_path TEXT NOT NULL UNIQUE CHECK(typeof(folder_path) = 'text' AND length(folder_path) > 0)
);
