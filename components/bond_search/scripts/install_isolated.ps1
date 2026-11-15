# Offline, opt-in installer. Review on HOME before running.
# Copies ONLY components/bond_search; refuses to overwrite existing work.
# Never edits the MCP core, package registrations, Google or VTB components.
param(
    [Parameter(Mandatory=$true)][string]$RepoCheckout,
    [Parameter(Mandatory=$true)][string]$HomeRoot
)
$ErrorActionPreference = "Stop"
$source = Join-Path $RepoCheckout "components\bond_search"
$parent = Join-Path $HomeRoot "components"
$target = Join-Path $parent "bond_search"
if (!(Test-Path -PathType Container $source)) { throw "Source component missing: $source" }
if (!(Test-Path -PathType Container $parent)) { throw "Existing components directory missing: $parent" }
if (Test-Path $target) { throw "Target exists; no overwrite: $target" }
Copy-Item -Path $source -Destination $target -Recurse -ErrorAction Stop
if (!(Test-Path (Join-Path $target "pyproject.toml"))) { throw "Copy incomplete: pyproject.toml absent" }
Write-Output "COPIED_ONLY_BOND_SEARCH: $target"
Write-Output "NOT_INSTALLED_OR_REGISTERED: no shared MCP files changed"
