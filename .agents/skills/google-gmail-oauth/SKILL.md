---
name: google-gmail-oauth
description: Use for local Google Workspace Gmail OAuth setup, status checks, token refresh behavior, or an owner-authorized users.getProfile diagnostic in the MSP Bonds MCP project.
---

# Gmail OAuth in the MSP Bonds MCP

1. Read components/google_workspace/docs/GMAIL_OAUTH_SETUP.md and check the current task authorization before any live account operation.
2. Use the existing msp_bonds_google.auth implementation. Gmail API clients go through service_client() and load_credentials(); do not create another OAuth or token-storage path.
3. Use the project's .venv Python module commands from the project root. Calling Python directly avoids dependence on PowerShell script-execution policy:
   - Status only: python -B -m msp_bonds_google.cli status. This checks local record presence; it does not prove the token works.
   - Owner-authorized initial login: python -B -m msp_bonds_google.cli login gmail. Use the permanent owner-local Desktop OAuth JSON under <HOME_MCP_ROOT>/secrets/google/; MSP_GOOGLE_OAUTH_CLIENT is a Windows user environment variable pointing to that file. Never use Downloads or TEMP as permanent storage. The owner completes browser consent. Never request credentials in chat.
   - Owner-authorized API test: python -B -m msp_bonds_google.gmail_profile_diagnostic. It reads the saved Gmail record, does not refresh it, and makes at most one fixed users.getProfile request when the access token is valid. It returns only PASS/ERROR.
4. Normal Gmail API tools refresh an expired access token on demand when a refresh token exists and save updated credentials through Windows Credential Manager. This does not run in the background. Do not create a daily-login workaround or change scopes.
5. Gmail scope must remain gmail.readonly. Do not call login gmail_write, access messages or attachments, or modify labels unless a separate task explicitly authorizes that exact action.
6. Diagnose code 15 or TransportError as an unresolved transport failure. It does not prove whether DNS, TLS, proxy or connection setup failed. Do not bypass or modify proxy/security settings.
7. Offline tests must mock credentials, refresh requests and keyring. Never read the real Credential Locker during tests. Do not include tokens, OAuth JSON, client secrets, mailbox addresses, private paths or financial contents in logs, GitHub or reports.
8. After changes, run the relevant offline tests and the MCP stdio contract check. A Gmail API result and a successful MCP tool-discovery check are separate facts.

This skill documents the current procedure; implementation remains in the Google Workspace package and its setup document.

## Shared Gmail and Drive recovery

- Inspect only file presence and restricted ACL, never contents or credential values. Check that secrets/ and OAuth client JSON names are excluded from Git. Do not place credentials in shared storage, either GitHub repository, or diagnostics.
- When one service fails, check the other separately. Use google_oauth_status for presence; use google_connection_health for authorized live reachability and same-account checks. An OAuth RefreshError alone is not proof of expiry or revocation. Check Google Auth Platform Audience publishing status; external apps in Testing can have seven-day refresh tokens for non-basic scopes. Do not alter publishing or scope without the owner's decision.
- If the Desktop JSON is lost, the owner may recover the existing client in Google Auth Platform → Clients and add a new secret if the old one cannot be downloaded. Preserve the old working secret until all dependent services are validated. Store the new JSON securely immediately. Use the existing CLI login drive or login gmail for the affected service only, then repeat live checks. Never ask for a consent URL, secret, or JSON in chat.

## Long-lived personal authorization

For a single owner, use Google Auth Platform Personal Mode where available and appropriate, rather than External/Testing with seven-day refresh-token expiry. Owner approval is required for client migration. Preserve the previous client until dependent services have been checked. Keep the new JSON in the existing protected folder and persist MSP_GOOGLE_OAUTH_CLIENT after both Drive and Gmail login. On-demand refresh through load_credentials() remains the normal path. A separately owner-authorized local acceptance check may simulate access-token expiry in memory and confirm a real refresh and protected-storage writeback; never output credential values. Do not use live credentials in automated tests. On 2026-11-25 both live refresh checks passed; later MCP Drive/Gmail/Sheets checks passed. No fixed expiry does not mean irrevocable authorization.
