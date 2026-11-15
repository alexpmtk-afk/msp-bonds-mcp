# Run common MCP, Google connector, and VTB tests in the same .venv.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Create the project .venv first.' }
$suites = @('tests', 'components/google_workspace/tests', 'components/vtb_reports/tests')
foreach ($suite in $suites) {
    $path = Join-Path $root $suite
    & $python -B -m unittest discover -s $path -p 'test_*.py' -v
    if ($LASTEXITCODE -ne 0) { throw "Test suite failed: $suite" }
}
