# Read-only agent analysis — version 1.1

## Status and scope

The optional read-only MCP adapter exposes deterministic ledger analysis over local stdio. It makes no model requests itself, and enabling it does not require a dashboard restart. Connecting it to an agent is a separate, explicit disclosure decision: returned aggregates may reach that agent's configured hosted model. This document describes the setup contract, not an installed connection or consent record.

- `scripts/services/agent_analysis.py`: deterministic analytical functions, standard library plus existing app services.
- `scripts/agent_mcp.py`: thin stdio MCP adapter, four named tools only.
- `scripts/agent_contracts.py`: typed response metadata; tool data fields are documented below.
- `requirements-agent.txt`: optional isolated MCP SDK dependency (pinned version 1.30.0).

No HTTP listener, arbitrary SQL, shell/file tools, ledger/config mutations, recategorisation, imports, model sampling, or automatic agent scheduling are exposed. This does not sandbox a general-purpose agent that separately has terminal or file-write tools.

## Tools

| Tool | Data returned | Policy |
|---|---|---|
| `get_data_status()` | Transaction/uncategorised/exception counts, latest import, statement document count, reconciliation status counts, per-account coverage | No transaction descriptions or filenames |
| `list_categories(page=1,page_size=100)` | Flat category/subcategory definitions, transaction counts and credit/debit/net pence totals; includes unused definitions | Includes report-excluded transactions; max 100 definitions per page |
| `search_transactions(q='',start='',end='',amount='',direction='all',include_details=False,page=1,page_size=50)` | Match count and cash totals; optional paginated transaction details only with startup permission | Same literal text, inclusive date, unsigned exact-amount and direction semantics as dashboard search; includes report-excluded rows |
| `summarise_transactions(group_by='category',...,include_excluded=False,page=1,page_size=100)` | Overall totals, paginated category/subcategory/month groups and per-account coverage | Includes Uncategorised; excludes report-excluded rows by default; group_by is restricted to three choices |

`amount` is a decimal **string**, e.g. `"600.00"`, without £ or commas. Dates are ISO `YYYY-MM-DD`. `direction` is `all`, `credit` or `debit`. Search keywords and all other filters combine. Text is literal, not a regex or SQL wildcard. Maximum search text length is 200 characters. Page size is bounded to 100, page to 100000, and coverage/explicit date spans to 3660 days. Coverage is limited to 10 accounts per ledger in version 1; larger scopes are refused rather than returning an oversized or silently incomplete account list. Empty matches are valid results, not proof that a contractual obligation was unpaid.

Version 1.1 supports GBP only. Mixed/unknown currencies are refused, not added together. Money fields are signed/unsigned integer pence, never floating-point output values. Positive amounts are credits and negative amounts debits. This is **cash movement**, not a determination of earned income or consumption. Search and category listing remain **raw**. Summary totals and groups are **transfer-adjusted**: only active, explicitly confirmed paired/unmatched principal is omitted. No category, account text or merchant hint automatically classifies a transfer. Fees, uncategorised rows, crypto and credit-card movements otherwise retain their existing policies. Revoking a decision restores the principal to adjusted movement.

Amounts must be finite exact pence; fractional-penny values are refused rather than silently rounded. Aggregation and average rounding are independent of the caller's Decimal context, including low precision and enabled Inexact/Rounded traps.

No averages, comparisons, forecasts, recurring-payment inference or missing-payment judgements are implemented in this first slice. Month groups contain observed matching records, not a complete-month baseline. Coverage is calculated from reconciled statement periods using the existing coverage service, separately per account; accounts are not unioned to manufacture complete coverage.

## Response envelope

Every successful tool returns structured JSON:

```json
{
  "meta": {
    "schema_version": "1.1",
    "calculation_basis": "transfer_adjusted",
    "as_of_utc": "<query timestamp>",
    "data_revision": "<digest of ledger state and category definitions>",
    "currency": "GBP",
    "money_unit": "pence",
    "filters": {},
    "include_excluded": false,
    "uncategorised_included": true,
    "transaction_details_enabled": false,
    "contains_transaction_details": false,
    "method": "<explicit cash-movement calculation policy>",
    "warnings": ["<scope and interpretation warnings>"],
    "evidence_ref": "ledger:<revision>"
  },
  "data": {}
}
```

This is a schema illustration, not a real query result. MCP advertises the typed envelope as an output schema and the protocol test validates responses against it. IDs like `txn:<id>`, `category:<digest>`, `group:<digest>` and `account:<digest>` provide local evidence references without leaking source filenames/account identifiers. `data_revision` changes on transaction/category/source/exception/import metadata changes. Queries run inside a consistent SQLite read snapshot.

Tool-specific data:

- Status: `transaction_count`, `uncategorised_count`, `open_exceptions`, transaction date bounds, `statement_document_count`, `latest_import_utc`, `reconciliation_counts`, `coverage`.
- Category listing: `items`, `total_items`, `page`, `page_size`, `has_more`, `uncategorised_count`. Each item has category/subcategory labels and cash totals.
- Search: `transaction_count`, `match_count`, `credits_pence`, `debits_pence`, `net_pence`. No row list by default. With authorised details: `transactions`, `page`, `page_size`, `has_more`.
- Summary: adjusted cash totals, `transaction_count`, `adjusted_transaction_count`, `raw`, `adjusted`, `transfers`, `transfer_schema_ready`, `transfer_evidence_ref`, `warnings`, `group_by`, `groups`, `total_groups`, paging metadata and `coverage`. Every group carries the same numerical metadata.

### Version 1.1 numerical and scope contract

- `meta.calculation_basis` is `raw` for status/category/search and `transfer_adjusted` for summary. A raw search total must be compared with `summary.raw`, not its top-level adjusted money fields.
- Top-level/group `credits_pence`, `debits_pence`, `net_pence` equal the corresponding `adjusted` fields. Debits/credits are nonnegative; net is credits minus debits. `transaction_count` counts matching rows after the requested report-exclusion filter, **before** removing principal; `adjusted_transaction_count` counts the remaining rows contributing to adjusted totals.
- `raw` has `transaction_count`, `credits_pence`, `debits_pence`, `net_pence` for **all** literal/date/amount/direction matches, independently of `include_excluded`. `adjusted` has the three money fields after explicit transfer and report-exclusion policy. Groups consisting only of excluded/confirmed principal can therefore have zero adjusted totals but nonzero raw evidence.
- `transfers` has `in_count`, `out_count`, `in_pence`, `out_pence`, `unmatched_count`. These cover **all matching raw rows**, including report-excluded confirmed legs. Their values are not added to adjusted totals, and the raw-adjusted difference can also contain report-excluded nontransfer rows.
- A confirmed leg remains adjusted away when its counterpart is outside the requested month/account/search scope. Counterparts are never imported into totals. Warning codes distinguish `explicit_unmatched_transfers`, `confirmed_counterpart_outside_scope`, and `confirmed_transfer_different_posting_dates` (a posting-date timing difference, **not** a claim that funds are currently in transit). `selected_excluded_rows_retained` refers to raw evidence, not adjusted totals.
- Missing/unverified migration 004 yields `transfer_schema_ready=false`, `transfer_schema_missing_or_unverified`, and no transfer adjustment. Reads never initialise a database, run a migration or seed a decision. `transfer_evidence_ref` is an aggregate digest of group/member/audit state, never a transaction/member list.
- `data_revision` includes transaction/category/exception/import/source metadata, accounts/folder policy, transfer groups/members/audit and migration state, the single category-definition snapshot, fixed classification-rule-file digests and the analytical-policy version. SQLite reads share one snapshot. External category/rule files are each snapshotted once per request; this is not an atomic filesystem/SQLite transaction against unrelated editors.
- Per-account `coverage` includes `coverage_available` and `warnings`. The scope includes observed transaction accounts, covered accounts and registered owned accounts. Accounts with no verified coverage are explicitly present with missing months; transaction date bounds describe the query range only and never prove coverage. No opening dates are guessed.

### Shared local Python calculation API (not an MCP tool)

```python
from scripts.services.movement_policy import read_snapshot, summarise_movement
from scripts.services.analytics import cashflow_summary
from scripts.services.coverage import coverage_windows_from_db

with read_snapshot(conn=conn):
    windows = coverage_windows_from_db(conn)
    baseline = cashflow_summary(conn, windows, start='2026-01-01', end='2026-03-31',
                                account_id='HSBC_UK')
    movement = summarise_movement(explicit_txn_ids, conn=conn, include_excluded=False)
```

`summarise_movement(ids, db_path=existing_path)` or `conn=...` requires exactly one explicit path/connection and at most 50,000 unique positive SQLite integer IDs. Active caller transactions are borrowed without BEGIN/COMMIT/ROLLBACK on success **or failure**; idle/new connections own only their read transaction and roll it back. Caller authorizers, query-only settings and connection lifetime are preserved. The service performs no ledger writes. Choose explicit account/date/literal scope **without** first dropping excluded rows if raw evidence is needed; `include_excluded=False` filters only adjusted totals. The return contains `raw`, `adjusted`, `transfers`, `row_count`, `adjusted_row_count`, `excluded_count`, `unclassified_count`, readiness/evidence/warnings and aggregate outside-scope/different-posting-date group counts.

`cashflow_summary` preserves its existing Decimal-valued average/month API, but `income`/`expenditure`/`net` now mean transfer-adjusted cash credits/debits/net, **not earned income or consumption**. Each included `MonthlyCashFlow.movement` contains the complete shared-policy result, including raw movement and transfer metadata. Default averages use only fully covered calendar months; all-account completeness requires every account separately, never the union of unrelated statement periods. Missing account coverage and clipped calendar-month requests prevent a complete baseline. Single-account selection ignores other accounts' coverage. Partial/missing months remain excluded by default with warnings. Fee and uncategorised movement is included unless explicitly report-excluded. Read coverage windows inside the same `read_snapshot` when deriving them from the database. These APIs do not wire UI/planning/reports or change live state.


## Privacy and connection decision

Local transport does **not** make analysis private from a hosted model. If enabled in Hermes, returned tool data may become input to its configured provider. Aggregates and category labels can themselves be sensitive.

The default server refuses `include_details=true`. Enabling dates/amounts/merchant/descriptions requires **both** the explicit startup flag `--allow-transaction-details` and an explicit detailed search call. Detailed descriptions are named `description_untrusted`/`merchant_untrusted` and must never be treated as instructions. The service does not return user notes, source documents or account numbers.

Configure only the intended analysis profile after explicit approval for aggregate disclosure; transaction-detail disclosure needs separate approval. Preserve unrelated provider/authentication and profile settings. Prefer an analysis-only session/profile with unnecessary write-capable tools disabled. The MCP server's read-only annotation is descriptive; enforcement is a `mode=ro` SQLite connection, `query_only`, and an authorizer rejecting writes, PRAGMA mutations, ATTACH, DETACH and schema operations.

## Install and run

From the project root, using the same Python version as the app:

```bash
uv venv --python .venv/Scripts/python.exe .venv-agent
uv pip install --python .venv-agent/Scripts/python.exe -r requirements-agent.txt
.venv-agent/Scripts/python.exe -m scripts.agent_mcp --root .
```

The last command is a stdio server awaiting MCP requests, not a conversational CLI. The root is fixed at startup; tools cannot change it or supply a database path. Missing ledger/config files are refused and never created.

The following is an **uninstalled configuration example**. Replace every `C:/path/to/project` placeholder with the chosen absolute native project path. Do not copy another user's environment or treat this example as disclosure approval.

```yaml
mcp_servers:
  ledger_analysis:
    enabled: true
    command: C:/path/to/project/.venv-agent/Scripts/python.exe
    args: ["-m", "scripts.agent_mcp", "--root", "C:/path/to/project"]
    env:
      PYTHONPATH: C:/path/to/project
      BANKSTATEMENT_PROJECT_ROOT: C:/path/to/project
    timeout: 60
    connect_timeout: 30
    sampling:
      enabled: false
    tools:
      include: [get_data_status, list_categories, search_transactions, summarise_transactions]
      resources: false
      prompts: false
```

Apply configuration only to the intended profile and preserve unrelated MCP entries. Use `/reload-mcp` in the agent chat, or start a new session, to load configured tools. `hermes mcp test ledger_analysis` checks connection and discovery; exercise it from outside the repository as well. Before any approved real-ledger use, verify aggregate-only defaults and unchanged ledger/category/rule bytes against synthetic fixtures. No installation, live query result or user consent is recorded here.

## Verification

```bash
# Set TMPDIR to an existing writable temporary directory if needed.
.venv/Scripts/python.exe -m unittest tests.test_agent_analysis tests.test_agent_mcp -v
```

The optional protocol test uses `.venv-agent` to launch real server/client sessions against a synthetic temporary ledger in both default and explicitly detail-enabled modes. It discovers exactly four tools, calls all four, validates output schemas, rejects default detail disclosure and an absent write tool, checks invalid-input errors, verifies that detail-enabled servers still return aggregates unless details are requested, and confirms authorised detailed results only in that opt-in mode. Ledger/config bytes must remain unchanged. The test is skipped if the optional environment is absent. Core analytical tests do not require MCP and also check invalid inputs, multi-account coverage, mixed currency, matching exclusion scopes, evidence revisions and read-only/ATTACH protection.
