# bond_search — public implementation status

This file documents only public component capabilities. It contains no local
account state, private rules, spreadsheet identifiers, credentials, or
portfolio facts.

## Implemented

- Read-only catalog snapshot validation and explicit source routing.
- Bounded Smart-Lab and MOEX ISS discovery code. External calls are not scheduled.
- Exact ISIN confirmation from an exact MOEX record. If the finder has no exact
  result, a manual fallback scans ISS pages using the documented cursor. It
  records page ranges, row counts, and whether the page set was complete.
  Incomplete scans return `INCONCLUSIVE`; a complete scan returns only
  `NOT_FOUND_IN_SCANNED_RESULTS`, never a claim about the whole MOEX database.
- Evidence stages reject contradictory current evidence. A superseded,
  withdrawn, unclassified, or mismatched-source record cannot establish current
  rating or coupon terms. A required field without eligible confirmation remains
  open; `MISSING` does not contradict a separate confirmation.
- Bounded document capture for caller-supplied bytes from a local file or one
  explicit URL. The capture records issuer and issue identifiers, document type
  and date, registered source, document URL, retrieval time, locator, and a
  SHA-256 content digest. It does not crawl, download automatically, call an
  undocumented API, or parse PDFs automatically.
- Rating records require exact issuer/issue scope, agency, scale, action date
  and type, active status, and confirmation that the action is latest. Known
  status labels are normalized conservatively; unknown values stay unknown.
- Coupon review requires legally defined future terms and confirmation that the
  issue document is current. Unknown coupon terms stay missing. Captured values
  remain `UNVERIFIED` until an independent review changes their evidence state.
- Reuse of the existing Google Workspace reader for the two configured policy
  tabs. The component does not create credentials or start OAuth.

## Not implemented or not confirmed

- No automatic rating-repository, coupon, amortization, offer, default,
  bankruptcy, issuer-finance, or liquidity loader.
- No environment-specific Google Workspace account status is published here.
- No recurring network collection, portfolio read/write, recommendations,
  trading, or shared MCP registration.
- No live market quote is asserted by an ISIN match.

## Verification

65 public tests passed in a clean Python 3.13 environment with
`DeprecationWarning` treated as an error. The component was built as a local
wheel and installed with `pip --no-deps --no-index`; the imported package came
from that environment, `msp_bonds_google` was absent, and no private fixture
module was loaded. Tests use synthetic data and mocked responses; live
public-source requests are not run by the test suite. The reproducible command
is `python -W error::DeprecationWarning -m unittest discover -s tests -p
"test_*.py" -v`, run from this directory in the isolated environment.
