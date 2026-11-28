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

Feature commit `66aff7ac1e617de9bf5f8e42d45fa57683141677` passed 65 public
tests from the source tree in a fresh clone, with `DeprecationWarning` treated
as an error. Tests used synthetic fixtures or mocked network responses; they
did not run live public-source requests or a live bond search.

A separate HOME component suite passed 90 tests. A separate live HOME check
read only the two policy tabs through the existing
`msp_bonds_google.workspace.sheets_read` component. The normal policy
interpreter accepted the result and produced a snapshot fingerprint; no fixture
reader was injected. Spreadsheet identifiers, row contents, and the
account-specific fingerprint are omitted.

A clean wheel installation of feature commit
`66aff7ac1e617de9bf5f8e42d45fa57683141677` has not been confirmed. Isolated
environment setup failed during `ensurepip` with `PermissionError`, and
cleanup returned `WinError 5`. The cause is unknown and is not attributed to
`bond_search`.

The reproducible public test command is
`python -W error::DeprecationWarning -m unittest discover -s tests -p
"test_*.py" -v`, run from the source tree in this directory.
