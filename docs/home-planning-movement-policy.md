# Home and Planning cash-movement policy

Home (`/`) and Planning (`/planning`, `/api/planning-data`) read the existing ledger only. They do not apply migrations, confirm transfers, infer ownership or create reports. No server restart is part of this change.

## Exact scope

- `account_id` is the literal account identifier. Omitted or empty selects every registered owned GBP account, including owned accounts with no transactions or coverage. Other explicit values and repeated account selectors return HTTP 400, never a silent all-account fallback.
- Before account setup, only the existing observed-GBP `HSBC_UK` compatibility path is available. GBP alone never establishes ownership for an unknown legacy account.
- The same selectors apply to Home, Planning HTML and the Planning API. `period=all|custom|year|month` uses the shared period resolver; a custom range without `period` can use inclusive `start` and `end`. Invalid or incomplete periods return HTTP 400. Calendar presets use `year` and, for month, `month`.
- Financial reads borrow one shared read snapshot. The Home import history, last-import indicator and exception badge remain explicitly ledger-wide; the financial movement and coverage are account/date scoped.

## Policy and amounts

The shared movement policy retains all raw movements in the scope, including report-hidden rows. The adjusted default excludes confirmed active paired/unmatched principal and report-hidden rows. It does not infer transfers from amounts, category names or hints. Ordinary fees and uncategorised debits remain. A confirmed leg is removed even when its counterpart is outside the account/date filter; counterpart, different-posting-date, unmatched and missing-migration warnings remain visible.

Planning keeps its `categories`, `months`, `grid` response shape and adds exact integer `grid_pence`, string `grid_pence_text`, `movement`, string `movement_pence_text`, applied `filters`, `complete_months`, per-account `coverage`, `warnings` and `method`. The historical `grid` pound-valued numbers are compatibility output only; browser calculations and CSV formatting use integer-pence strings and BigInt, not floating-point source values. Categories are HTML-escaped.

Each category/month cell is a negative-only, left-joined debit group evaluated by the shared movement policy. A debit remains a debit even if its category is named Income. The raw/adjusted full-scope summary is separate from category scenario toggles.

## Verified averages

Coverage is evaluated independently for each selected account with shared `cashflow_summary`; common complete months are intersected, never unioned. Accounts lacking verified coverage block the all-account average rather than being omitted or given an invented opening date. Select a covered historical account to retain its valid average. Custom windows clipping a complete calendar month cannot turn that partial window into a complete-month baseline.

Home rounds only the exact combined common-month integer-pence numerator. Planning averages and medians use those common complete months, including verified no-debit months. Partial/missing coverage produces unavailable averages (`—`), not a fabricated zero. Displayed transaction-month totals remain observed debit movement, not inferred consumption or account balances.

## Verification and rollout boundary

`tests/test_transfer_dashboard.py` uses scratch synthetic ledgers. `tests/planning_ui_probe.js` executes the rendered Planning JavaScript against a local DOM/fetch harness, including amounts above the Number safe-integer range, category escaping, deselection, unavailable averages and API errors. This is executable JavaScript verification, not a full browser-layout claim.

Generated reports are a separate workstream. The Transfers information banner accurately states Home/Planning support while retaining the report-rollout warning. Remove or revise that warning only after the report workstream verifies every affected generator and served report path. Migration 004 remains explicit opt-in; these pages never install or seed it.
