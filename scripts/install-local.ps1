# Install the local editable packages into the existing project .venv.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Create the project .venv first.' }
& $python -c "import setuptools, wheel" *> $null
if ($LASTEXITCODE -ne 0) { throw 'Install setuptools and wheel in the project .venv first.' }
$packages = @(
    $root,
    (Join-Path $root 'components/vtb_reports'),
    (Join-Path $root 'components/google_workspace'),
    (Join-Path $root 'components/telegram')
)
foreach ($package in $packages) {
    & $python -m pip install --no-build-isolation --editable $package
    if ($LASTEXITCODE -ne 0) { throw "Package installation failed: $package" }
}
& $python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Installed package requirements are inconsistent.' }

