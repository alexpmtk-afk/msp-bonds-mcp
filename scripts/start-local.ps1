# PowerShell 5.1+, stdio MCP, no service or scheduled job.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Install isolated Python environment first.' }
& $python -B (Join-Path $root 'server.py')
exit $LASTEXITCODE
