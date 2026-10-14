-- Additive registry only. Never relabel or rewrite historical rows.
CREATE TABLE accounts (
    account_id TEXT PRIMARY KEY NOT NULL CHECK (
        typeof(account_id) = 'text'
        AND length(account_id) BETWEEN 1 AND 64
        AND account_id GLOB '[A-Z]*'
        AND account_id NOT GLOB '*[^A-Z0-9_]*'
        AND instr(account_id, char(0)) = 0),
    display_name TEXT NOT NULL,
    currency TEXT,
    institution TEXT,
    owned INTEGER NOT NULL CHECK (owned IN (0, 1)),
    opened_on TEXT,
    closed_on TEXT,
    parser_key TEXT,
    parser_verified INTEGER NOT NULL CHECK (parser_verified IN (0, 1)),
    import_enabled INTEGER NOT NULL CHECK (import_enabled IN (0, 1))
);

-- Evidence is account-local. A document-only account has no currency evidence.
INSERT INTO accounts (account_id, display_name, currency, institution, owned,
                      parser_key, parser_verified, import_enabled)
SELECT observed.account_id,
       CASE WHEN observed.account_id = 'HSBC_UK' THEN 'HSBC UK' ELSE 'Account setup pending' END,
       evidence.currency,
       CASE WHEN observed.account_id = 'HSBC_UK' THEN 'HSBC UK' END,
       CASE WHEN observed.account_id = 'HSBC_UK' THEN 1 ELSE 0 END,
       CASE WHEN observed.account_id = 'HSBC_UK' AND evidence.currency = 'GBP' THEN 'hsbc_uk' END,
       CASE WHEN observed.account_id = 'HSBC_UK' AND evidence.currency = 'GBP' THEN 1 ELSE 0 END,
       CASE WHEN observed.account_id = 'HSBC_UK' AND evidence.currency = 'GBP' THEN 1 ELSE 0 END
FROM (SELECT account_id FROM transactions UNION SELECT account_id FROM source_documents) AS observed
LEFT JOIN (
    SELECT account_id,
           CASE WHEN COUNT(DISTINCT currency) = 1 AND COUNT(currency) = COUNT(*)
                AND MIN(typeof(currency) = 'text') = 1
                AND MIN(currency) GLOB '[A-Z][A-Z][A-Z]'
                AND instr(MIN(currency), char(0)) = 0 THEN MIN(currency) END AS currency
    FROM transactions GROUP BY account_id
) AS evidence ON evidence.account_id = observed.account_id;
