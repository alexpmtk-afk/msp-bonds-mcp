# HOME integration gate for bond_analyze (Draft PR #7)

Status: tested in CI, **NOT deployed on HOME**. PR #7 is based on unmerged PR #4 and must not be merged to main as a shortcut.

## Why an unreviewed file copy is unsafe

The public repo's MCP contract includes 16 pre-existing tools and adds one (`bond_analyze`) for 17 in that tree. The separately reported HOME installation already has additional tools, including Telegram. HOME might also contain a local issuer-only patch not present in the remote PR #4 HEAD. The live HOME sources are not guaranteed to match the public checkout, and HOME may have no `.git` metadata.

**Never overwrite the HOME `src/msp_bonds_mcp/core/server.py` with the PR #7 version or replace the HOME package directory wholesale.** Do not overwrite existing `evidence.py`, Google Workspace, Telegram, or other HOME components.

## Exact change boundary

- `src/msp_bonds_mcp/core/bond_dossier.py`: new read-only file (check for name collision first).
- `src/msp_bonds_mcp/core/server.py`: append only the `bond_analyze` tool registration from PR #7 to the existing HOME implementation; retain every existing tool, decorator, import and schema.
- `components/bond_search`: verify that its installed `google_source.load_from_existing_workspace` and `discovery.lookup_moex_isin` implement the expected contracts. The optional MOEX check is never required for the first offline test.
- `MSP_BONDS_POLICY_SPREADSHEET_ID`: supply the canonical Google Sheet ID via a **private local** MCP client/launcher environment setting, not public source or logs.
- No new OAuth, no broker credentials, no Google writes, no trading, no scheduled tasks.
- Scripts `scripts/install-local.ps1` and `scripts/test-all.ps1` in PR #7 include `bond_search`; do not blindly replace local HOME scripts that contain additional components.

## Required HOME preflight

1. Record exact installed tool names and input schemas BEFORE changes (the previously reported count is 18; accept the actual freshly measured baseline).
2. Read the current HOME shared server, package installer, launcher, environment and installed package versions, preserving all local changes. Compare with this PR; stop on incompatible or overlapping changes.
3. Preserve/backup existing HOME files before any authorized patch, with hashes and a rollback path; do not clean unrelated temp files or change privileges.
4. Verify the canonical sheet is readable with the **existing** Google Workspace credential and contains sequential rules 1..94 and 26 sources. No separate OAuth or public copy of its ID.

## Stage-1 verification

- Offline tests for the new module and existing MCP contract pass, with all baseline tool names and input schemas unchanged.
- New tool appears **exactly once** after reconnecting MCP; expected total is baseline + 1.
- `bond_analyze(isin="RU000A10FGE6", check_moex=False)` returns `RESEARCH_NOT_COMPLETED`, 27 `NOT_COLLECTED` checks, canonical fingerprint and no BUY status. No live market call in this smoke test.
- If the workbook is inaccessible, tool returns a safe error; it must not claim verified credit or legal facts.
- Optional live MOEX lookup requires separate deliberate invocation and source/network permission; no scheduled data gathering.
- Stop and revert the patch if original tools disappear, credentials leak, sources differ, or tests fail. Do not use a force-push, merge PR #4, or change core service permissions.

## CI evidence and limits

GitHub Actions job `Bond research MCP contract tests` on PR #7 checks clean Linux installation and four unittest suites, **not** Windows HOME integration, market-data availability, financial due diligence or Telegram compatibility.

Full research requires later official-source adapters and primary-document evidence verification. This PR's `bond_analyze` is a research planning entry point, not a complete analysis engine.
