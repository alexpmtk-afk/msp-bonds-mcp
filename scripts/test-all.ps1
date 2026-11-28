# Run common MCP, Google, VTB, and Telegram tests in the same .venv.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Create the project .venv first.' }
$suites = @('tests', 'components/google_workspace/tests', 'components/vtb_reports/tests', 'components/telegram/tests', 'components/bond_search/tests')
foreach ($suite in $suites) {
    $path = Join-Path $root $suite
    $restorePythonPath = $suite -eq 'components/bond_search/tests'
    $previousPythonPath = $null
    if ($restorePythonPath) {
        $previousPythonPath = [System.Environment]::GetEnvironmentVariable('PYTHONPATH', 'Process')
        $env:PYTHONPATH = Join-Path $root 'components/bond_search/src'
    }
    try {
        & $python -B -m unittest discover -s $path -p 'test_*.py' -v
    }
    finally {
        if ($restorePythonPath) {
            if ($null -eq $previousPythonPath) {
                [System.Environment]::SetEnvironmentVariable('PYTHONPATH', $null, 'Process')
            } else {
                [System.Environment]::SetEnvironmentVariable('PYTHONPATH', $previousPythonPath, 'Process')
            }
        }
    }
    if ($LASTEXITCODE -ne 0) { throw "Test suite failed: $suite" }
}

