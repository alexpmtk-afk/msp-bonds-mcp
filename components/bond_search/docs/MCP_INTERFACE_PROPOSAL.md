# Proposed MCP interface — not registered or active

Integration owner: existing MCP core maintainer. No changes to root server
or shared packages from this component. Data and permissions are supplied by
the already authorized project integrations; this file is a proposal only.

## 1. bond_discover (first read-only interface)

Input:
- `max_items`: integer, 1–100, with explicit user research request.
- `policy_snapshot`: obtained by existing Google Workspace adapter from the
  canonical 7-column rule and source tabs. Never accept arbitrary pasted
  instructions from an unknown web page as the authoritative policy.
- optional observability context: request ID, source availability.

Output:
- `policy_fingerprint`, `discovered_at` and `used_source`;
- `candidates` with ISIN, name, source, fetch time and an *unverified* status;
- `source_errors` and missing follow-up checks;
- status `RESEARCH_ONLY_NOT_VERIFIED` or `UNAVAILABLE_NO_RESULTS`.

Discovery does NOT imply that ISIN belongs to a Russian corporate bond,
has a current quote, acceptable rating or legal coupon schedule.

## 2. bond_evaluate (planned, not implemented)

Input: unique validated ISIN and up-to-date market quote, approved policy,
issuer/source identity. Verify each materially required property using a
primary source or clearly-qualified alternative and keep provenance per
field. Compute full-price entry including accrued interest; coupon/amortization
calendar, optionality and scenario cashflows; ratings, default/legal/financial
quality; market risk and liquidity.

Output: individual evidence objects with `source_url`,
`document_date`, `retrieved_at`, `value`, `unit`, `method`,
`verification_status`, disagreements and material missing fields.

Fail closed: if no legally verified payment terms, current rating, issuer,
material credit condition or valid price, no positive recommendation.

## 3. bond_construct_portfolio_preview (planned, not implemented)

Input: user-provided target budget (e.g. 100000 RUB), authorized current
holdings from shared portfolio interface, verified candidates, and approved
risk limits. Provide a **hypothetical** basket, instrument quantities,
full acquisition cost, risk concentration before/after, cashflow dates,
credit exposures, remaining budget and unresolved limitations.

Without current holdings or approved ruble concentration limits, output
must say `CANNOT_VERIFY_PORTFOLIO_SUITABILITY`, not silently assume an empty
account, invent positions or apply an unapproved model.

No order placement, transfers or changing canonical portfolio sheets.

## Core handshake required before integration

1. Agree tool names, schema version and permissions with the core maintainer.
2. Reuse shared authorized Google Workspace reads, do not implement duplicate
   Google login/OAuth.
3. Agree the canonical sheet snapshot acquisition time, stale-policy behavior
   and no secrets in public logs.
4. Confirm explicit user-request boundaries, rate limits, source terms,
   timeout/retry strategy and output-size limits.
5. Run existing core tests plus offline synthetic component tests and verify
   the original MCP tools/contracts remain unchanged.
