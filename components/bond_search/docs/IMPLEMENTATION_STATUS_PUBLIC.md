# bond_search — public implementation status

This file documents only public component capabilities. It contains no local account state, private rules, spreadsheet identifiers, credentials, or portfolio facts.

## Implemented

- Read-only catalog snapshot validation and explicit source routing.
- Bounded Smart-Lab and MOEX ISS discovery code. External calls are not scheduled.
- Exact ISIN confirmation from an exact MOEX record. If the finder has no exact result, a manual fallback scans ISS pages using the documented cursor. It records page ranges, row counts, and whether the page set was complete. Incomplete scans return `INCONCLUSIVE`; a complete scan returns only `NOT_FOUND_IN_SCANNED_RESULTS`, never a claim about the whole MOEX database.
- Evidence stages reject contradictory confirmed values and explicit `CONFLICT` evidence. A required field without eligible confirmation remains open. `MISSING` evidence does not contradict a separate confirmation.
- Manual capture of official rating and issue-document fields. Supplied values remain `UNVERIFIED`; absent required values remain `MISSING`. There is no web scraping or automatic document extraction for those fields.
- Reuse of the existing Google Workspace reader for the two configured policy tabs. This component does not create credentials or start OAuth.

## Not implemented or not confirmed

- No automatic rating repository, coupon, amortization, offer, default, bankruptcy, issuer-finance, or liquidity loader.
- No proof of Google Workspace access until the existing HOME credential is restored and both intended tabs are read back successfully.
- No recurring network collection, portfolio read/write, recommendations, trading, or shared MCP registration.
- No live market quote is asserted by an ISIN match.

## Verification

44 public tests passed in a clean Python 3.13 environment with `DeprecationWarning` treated as an error. The package was installed from a local wheel with `pip --no-deps --no-index`; no credentials, private fixtures, or Google Workspace package were present.

The public component tests use standard-library `unittest`, synthetic data, and mocked network responses. They do not import the HOME policy fixture module or the HOME Google Workspace package. The public test command is `python -W error::DeprecationWarning -m unittest discover -s tests -p "test_*.py" -v`, run from `components/bond_search` in the isolated environment.
