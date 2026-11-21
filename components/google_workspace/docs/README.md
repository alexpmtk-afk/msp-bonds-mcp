# Google Workspace component

Package version: msp-bonds-google-workspace 0.3.1. It is installed beside the root msp-bonds-mcp 0.4.0 package and provides Google Drive, Gmail, OAuth and local approval tools to the single stdio server. It does not start a separate server.

## Contents and dependencies

- Source: src/msp_bonds_google.
- Local commands: scripts/google-local.ps1 and the google-local package entry point.
- Tests: synthetic, offline checks under tests.
- Configuration example: config/.env.example; it is not loaded automatically.
- Dependencies are declared in this component's pyproject.toml: the root server package, Google API client, Google Auth, OAuth library, Auth transport and keyring.

Install the root package first, then VTB and Google components with scripts/install-local.ps1. The component uses the root package's shared runtime-path helper.

## MCP tools

Google tools: google_oauth_status, google_connection_health, google_drive_list, google_drive_metadata, google_drive_read_text, google_sheets_read, google_gmail_labels, google_gmail_search, google_gmail_message, google_gmail_stage_attachment, google_prepare_write, google_write_plan_status, google_execute_approved_write.

Public main currently has three read-only VTB archive tools and the Telegram tool, for 17 tools total. The owner-reported HOME stdio check on 21.11.2026 found 18 tools and matched all names and input schemas against the local HOME fixture. These are separate code snapshots.

## Security and OAuth

OAuth is interactive and is not started by the MCP server. Credentials stay in Windows Credential Manager through keyring; do not place credentials, tokens, email contents or real broker data in this package or repository.

For read-only Gmail login, the component requests only gmail.readonly. A separate gmail_write flow exists in the package but was not used or authorized for this setup. Gmail services call service_client() -> load_credentials(); when an access token expires and a refresh token is available, the existing code refreshes on demand and saves the updated credential record through keyring. There is no background refresh task.

The owner reported that a token check and one read-only users.getProfile call passed in the normal HOME session. The current documentation task did not repeat that live API call. Package tests remain offline and do not prove live authorization.

Google Drive and Gmail changes remain disabled by default. A remote change requires a locally reviewed, single-use approval and the explicit process setting MSP_GOOGLE_WRITES_ENABLED=1; leave it unset or at 0 during setup. See GMAIL_OAUTH_SETUP.md and instructions/05_MCP.txt for setup and security details.
