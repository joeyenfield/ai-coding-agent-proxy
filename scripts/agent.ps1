# Launch a coding agent with the Go client, built first from this checkout:
#   .\scripts\agent.ps1 qwen . --model qwen3.6:35b
$ErrorActionPreference = "Stop"
& (Join-Path $PSScriptRoot "build-client.ps1") | Out-Null
if ($LASTEXITCODE) { exit $LASTEXITCODE }
& (Join-Path $PSScriptRoot "..\client\bin\agent.exe") @args
exit $LASTEXITCODE
