# Google Workspace component

Current package: msp-bonds-google-workspace 0.3.1. It is installed beside the root msp-bonds-mcp 0.4.0 package and provides Google Drive, Gmail, OAuth and local approval tools to the single stdio server. It does not start a separate server.

## Contents and dependencies

- Source: src/msp_bonds_google.
- Local commands: scripts/google-local.ps1 and the google-local package entry point.
- Tests: synthetic, offline checks under tests.
- Configuration example: config/.env.example; it is not loaded automatically.
- Dependencies are declared in this component's pyproject.toml: the root server package, Google API client, Google Auth, OAuth library, Auth transport and keyring.

Install the root package first, then VTB and Google components with scripts/install-local.ps1. The component uses the root package's shared runtime-path helper.

## MCP tools

Google tools: google_oauth_status, google_connection_health, google_drive_list, google_drive_metadata, google_drive_read_text, google_sheets_read, google_gmail_labels, google_gmail_search, google_gmail_message, google_gmail_stage_attachment, google_prepare_write, google_write_plan_status, google_execute_approved_write.

The root server also exposes three read-only VTB archive tools. The whole-server stdio contract is checked separately against the preserved 16-tool baseline.

## Security and OAuth

OAuth is interactive and is not started by the MCP server. Credentials stay in a protected local location; grants use Windows Credential Manager through keyring. Do not place credentials, tokens, email contents or real broker data in this package or repository.

Google Drive and Gmail changes remain disabled by default. A remote change requires a locally reviewed, single-use approval and the explicit process setting MSP_GOOGLE_WRITES_ENABLED=1; leave it unset or at 0 during setup. No real API call is part of installation or tests.

OAuth authorization and live Google API access are not confirmed by the package tests. Public project status and security boundaries are documented in `instructions/05_MCP.txt` and `instructions/07_SECURITY.txt`.
