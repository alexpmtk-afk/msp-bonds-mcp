# bond_search — read-only research component (v0.1.0)

Status: **public branch code skeleton only**. This file does not establish the current HOME installation or live MCP registry. No trading authorization.

## Objective

Single independent package under `components/bond_search/`. It does not alter
`google_workspace`, `vtb_reports`, the shared MCP core or any notification
component. Separate MCP server is forbidden.

Research workflow (future stages):

1. An authenticated user issues an explicit research task.
2. The shared Google Workspace component loads both canonical spreadsheet tabs,
   “логика поиска” and “ресурсы поиска”, into **seven-column** row arrays.
3. `PolicySourceSnapshot.from_sheet_rows()` validates and fingerprints the
   complete snapshot. Expired/unavailable policy **blocks discovery**.
4. Initial discovery: Smart-Lab `https://smart-lab.ru/q/bonds/`. If unavailable
   or no parsable ISIN rows, fall back to public MOEX ISS bonds listing.
5. Discovery returns preliminary leads using the authorized policy. A separate route planner provides applicable evidence checks, sources and unresolved facts without mass-prefetching documents.
6. ChatGPT investigates the selected leads, reusing current verified issuer-level evidence and checking ISIN-specific terms separately. Targeted read-only adapters support individual evidence requests; a route is not evidence.
7. Later: read-only portfolio budget simulation with actual holdings through a
   separately approved shared interface. No portfolio writes/trades by default.

**Current implementation covers steps 2–4 only when the caller supplies the
two authorized tab snapshots, and returns discovery-only leads.** It does not
translate free-text policy into an executable full risk engine. No real
securities were evaluated, and no live website calls were verified.

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
- Outbound requests limited to HTTPS and recognized public hostnames, 10-second
  timeout and 3 MB response cap, no redirect/login/cookies.
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
or actual portfolio facts are included. Tests have **not** been executed on
HOME as part of this commit.

## Next implementation milestones

- Review published code for fit with HOME Python and actual core contracts.
- Test Smart-Lab HTML parser against permitted and current real responses;
  review source access terms and fallback behavior before enabling.
- Implement field-level evidence record with per-value source, observation time,
  quote time, status and discrepancies.
- Add machine-readable rule approval mapping, with owner sign-off. Do not
  silently execute natural-language historical or unapproved rules.
- Implement targeted evidence providers and deterministic calculation helpers only as needed; ChatGPT performs substantive interpretation. Test route-only behavior, deduplication, provenance, freshness and honest early-stop states.
- Coordinate the only existing MCP core's registration with its owner.

## Approved target workflow — 2026-11-24

MCP orchestrates preliminary discovery and evidence routing; the AI analyst performs substantive analysis. Do not automatically download every document for every candidate. Preserve verified evidence records (source, observation date, period, method, status) rather than bulk raw material; group by proven legal issuer identity, not name similarity. Missing evidence is not a negative fact. Stop only with an evidenced violation of an applicable mandatory rule or an explicit task boundary; record remaining checks. This describes a target design, not a completed installation. No portfolio writes or trades are implied.
