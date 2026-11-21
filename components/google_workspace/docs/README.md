# Google Workspace component

Package version: msp-bonds-google-workspace 0.3.1. It is installed beside the root msp-bonds-mcp package and provides Google Drive, Gmail, OAuth and local approval tools to one stdio server. It does not start a separate server.

## Contents and dependencies

- Source: src/msp_bonds_google.
- Local commands: scripts/google-local.ps1 and the google-local package entry point.
- Tests: synthetic, offline checks under tests.
- Configuration example: config/.env.example; it is not loaded automatically.
- Dependencies are declared in this component's pyproject.toml: the root server package, Google API client, Google Auth, OAuth library, Auth transport and keyring.

Install the root package first, then VTB and Google components with scripts/install-local.ps1. The component uses the root package's shared runtime-path helper.

## MCP tools

The Google component provides Gmail and Drive tools to the single server. Public main has 17 tools in its current snapshot; the owner-reported HOME stdio check on 21.11.2026 found 18 tools and matched all names and input schemas against the local HOME fixture. These are separate code snapshots.

## Security and OAuth

OAuth is initiated only by an explicit owner action; the MCP server does not start a browser login. Tokens are stored through keyring in Windows Credential Manager, never in source files. Gmail OAuth requests gmail.readonly.

The shared service_client() calls load_credentials(). When an access token has expired and a refresh token exists, Google Auth refreshes on demand and the component saves the updated record through keyring in Windows Credential Manager. There is no background refresh job. The owner reported that a token check and one read-only users.getProfile call passed in the normal HOME session on 21.11.2026. This does not mean that the package test suite performs live API calls.

Google Drive and Gmail changes remain disabled by default. A remote change requires a locally reviewed, single-use approval and the explicit process setting MSP_GOOGLE_WRITES_ENABLED=1; leave it unset or at 0 during setup. See GMAIL_OAUTH_SETUP.md for exact commands, diagnostic codes and safety boundaries.

## Central portfolio sheet

The canonical portfolio sheet and its rules are described in the local registry docs/LOCAL_DATA_SOURCE_REGISTRY.txt. This component does not read or fill it automatically; that integration is not implemented. Do not create a parallel primary sheet or place private links, IDs or real financial data in GitHub PUBLIC.
