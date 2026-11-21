---
name: google-gmail-oauth
description: Use for local Google Workspace Gmail OAuth setup, status checks, token refresh behavior, or an owner-authorized users.getProfile diagnostic in the MSP Bonds MCP project.
---

# Gmail OAuth in the MSP Bonds MCP

1. Read components/google_workspace/docs/GMAIL_OAUTH_SETUP.md and check the current task authorization before any live account operation.
2. Use the existing msp_bonds_google.auth implementation. Gmail API clients go through service_client() and load_credentials(); do not create another OAuth or token-storage path.
3. Use the project's .venv Python module commands from the project root. Calling Python directly avoids dependence on PowerShell script-execution policy:
   - Status only: python -B -m msp_bonds_google.cli status. This checks local record presence; it does not prove the token works.
   - Owner-authorized initial login: python -B -m msp_bonds_google.cli login gmail. Set MSP_GOOGLE_OAUTH_CLIENT only to the owner's local Desktop OAuth JSON path. The owner completes browser consent. Never request credentials in chat.
   - Owner-authorized API test: python -B -m msp_bonds_google.gmail_profile_diagnostic. It reads the saved Gmail record, does not refresh it, and makes at most one fixed users.getProfile request when the access token is valid. It returns only PASS/ERROR.
4. Normal Gmail API tools refresh an expired access token on demand when a refresh token exists and save updated credentials through Windows Credential Manager. This does not run in the background. Do not create a daily-login workaround or change scopes.
5. Gmail scope must remain gmail.readonly. Do not call login gmail_write, access messages or attachments, or modify labels unless a separate task explicitly authorizes that exact action.
6. Diagnose code 15 or TransportError as an unresolved transport failure. It does not prove whether DNS, TLS, proxy or connection setup failed. Do not bypass or modify proxy/security settings.
7. Offline tests must mock credentials, refresh requests and keyring. Never read the real Credential Locker during tests. Do not include tokens, OAuth JSON, client secrets, mailbox addresses, private paths or financial contents in logs, GitHub or reports.
8. After changes, run the relevant offline tests and the MCP stdio contract check. A Gmail API result and a successful MCP tool-discovery check are separate facts.

This skill documents the current procedure; implementation remains in the Google Workspace package and its setup document.
