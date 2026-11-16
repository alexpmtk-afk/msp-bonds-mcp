# bond_search — read-only research component (v0.1.0)

Status: **Read-only research component installed locally on HOME, but not registered in the root MCP.** A clean installation of the exact current PR commit has not been verified. No portfolio writes or trades are enabled.

## Objective

Single independent package under `components/bond_search/`. It does not alter
`google_workspace`, `vtb_reports`, the shared MCP core or any notification
component. Separate MCP server is forbidden.

## Current local status

A read-only bond_search component is installed on HOME, but it is not
registered in the root MCP. The existing Google Workspace component has loaded
the two canonical policy tabs, and `PolicySourceSnapshot.from_sheet_rows()`
validated and fingerprinted that snapshot. This local result does not prove
that the exact current PR commit installed successfully. The component does not
write to the spreadsheet.

The component contains bounded Smart-Lab and MOEX ISS discovery adapters.
They are not scheduled. The PR validation did not run a live bond search.

## Remaining workflow (requires shared-MCP integration and authorization)

1. An authenticated user issues an explicit research task.
2. Expired or unavailable policy blocks discovery; load and validate the
   complete seven-column snapshot before using a discovery adapter.
3. Leads remain research-only until independent verification of all applicable
   requirements in the canonical rule sheet, including instrument identity,
   issuer country, currency, credit rating, issuer finances, defaults, legally
   complete coupon/redemption terms, current bid/ask, charges, liquidity and
   existing holdings.
4. Later: yield/cashflow calculation and side-by-side risk-adjusted comparison.
5. Later: read-only portfolio budget simulation with actual holdings through a
   separately approved shared interface. No portfolio writes/trades by default.

The current component validates caller-supplied snapshots, provides bounded
discovery adapters, and checks exact ISINs through MOEX ISS. If the exact finder
returns no row, the fallback scans pages using the ISS cursor. It returns
`INCONCLUSIVE` when page coverage cannot be proven; a complete scan is reported
only as `NOT_FOUND_IN_SCANNED_RESULTS`, limited to that scanned list. Manual
rating and issue-document capture uses a fresh catalog snapshot and the
registered source for the matching evidence route. The official document
capture records issue/issuer identifiers, document type/date, source link,
retrieval time, a page/section locator, and a SHA-256 digest of supplied bytes.
It accepts a local file or bytes already retrieved from one explicit URL; it
does not crawl, fetch undocumented APIs, or parse PDFs automatically. An
identity mismatch or a document marked withdrawn/superseded emits no claims.
Captured values remain `UNVERIFIED` and missing values remain `MISSING`.
Rating evidence needs exact issuer/issue scope, active status, and confirmation
that the action is latest. Coupon evidence needs legally defined future terms
and confirmation that the issue document is current. Unknown currentness or
coupon terms stays open. No securities have been evaluated as fully verified,
and the component is not registered in the shared MCP.

The `routes.py` planner now maps evidence needs to sources that actually exist in
 the live resource tab. Only discovery adapters for Smart-Lab and MOEX ISS are
 implemented; other routes are **plans**, not working website integrations.

## Source routing and required evidence (design)

| Evidence / stage | First preference | Alternative | Never silently infer |
| --- | --- | --- | --- |
| Discovery / leads | Smart-Lab public corporate bonds table | MOEX ISS; other aggregators only after explicit adapter/API validation | Current tradability, issuer type, confirmed rating |
| Security / price / liquidity | MOEX ISS by ISIN and trading board | Other verified exchange/broker feeds | Executable ASK or trade depth from last price |
| Coupon / amortization / call / put | Issue documents / NSD disclosures | Official issuer disclosure; exchange bond documents | Future coupons with no binding formula |
| Ratings | Central Bank rating repository | Official agency releases (AKRA, Expert RA, NCR, NRA) | Ratings from stale aggregator |
| Financials / legal entity | Audited issuer filings / FNS | Official consolidated statements and verified agency analysis | Financial metrics from unavailable records |
| Bankruptcy / legal events | Fedresurs, court cases, issuer filings | Other official legally verifiable disclosures | Absence of events from unavailable site |
| Portfolio and execution | Broker documents / approved canonical sheet | None without explicit authorization | Holdings, cash, fills, risk capacity |

A secondary source is **not automatically equal in authority** to a primary
document. Fallback failures must be visible; material gaps block a positive
recommendation. Source selection, approval thresholds and resource definitions
must come from the authorized canonical workspace, NOT source-code literals.
Only technical adapter hostnames are shipped publicly; user policy, account
information, spreadsheet identifiers and secrets must not be in PUBLIC GitHub.

## Safeguards

- No background scans, no schedule, no brokerage credentials, no order APIs.
- `discover(task_authorized=False)` raises. The MCP host must independently
  authenticate the request: the flag is a workflow guard, not authentication.
- Outbound requests use HTTPS and recognized public hostnames, with a 3 MB
  response cap and no redirect/login/cookies. A manual MOEX ISIN diagnostic is
  limited by a 45-second request-start budget and at most 20 pages, with at most
  10 seconds per HTTP operation. The in-flight operation may finish after the
  budget; no further request starts after it is exhausted.
- Caller must rate-limit requests and check site terms and `robots.txt` before
  enabling Smart-Lab access. Pages requiring login or actively blocking requests
  must be treated as unavailable; no bypass.
- No historical webpage value is a live quote; fetched_at is acquisition time,
  not the exchange's price timestamp.
- Shortlist status `RESEARCH_ONLY_NOT_VERIFIED` **never means BUY**.
- Binding any tools to shared MCP core, shared Google/Telegram, or HOME code
  requires agreement with core maintainer and local check.

## Quick offline testing

From this directory on a compatible Python installation:

```sh
python -m pip install -e .
python -m unittest discover -s tests -p 'test_*.py' -v
```

All tests use synthetic in-memory data and mock HTTP responses. No credentials
or actual portfolio facts are included. The test suite does not make live
network calls. See `IMPLEMENTATION_STATUS_PUBLIC.md` for test results and installation limits. The public tests passed from the source tree, but a clean wheel installation of the current PR commit has not been verified.

## Next implementation milestones

- Validate integration with HOME Python and root-MCP contracts before registration; registration has not happened.
- Test the Smart-Lab HTML parser against permitted current responses; review source terms and fallback behavior before enabling requests.
- Audit field-level evidence for source, observation time, quote time, status and discrepancies; keep unsupported fields open.
- Add machine-readable rule approval mapping with owner sign-off. Do not silently execute natural-language historical or unapproved rules.
- Implement secondary legally reliable providers by evidence type, credit checks and cash-flow math, with synthetic tests and explicit stops.
- Register only through the existing MCP core after agreement with its maintainer and owner.
