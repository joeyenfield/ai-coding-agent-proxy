# Build agent-machine into machine\bin for this machine:
#   .\scripts\build-machine.ps1
# For every platform (with the client too), into dist\<version>\<os>-<arch>: .\scripts\build-dist.ps1
$ErrorActionPreference = "Stop"
# Push, not Set: a script run with & shares the caller's location.
Push-Location (Join-Path $PSScriptRoot "..\machine")
try {
    if (-not $env:GOTOOLCHAIN) { $env:GOTOOLCHAIN = "local" }
    $version = $env:VERSION
    if (-not $version) {
        # The checkout's version, as the server and tools\dist work it out (VERSION, plus the commit).
        Push-Location (Join-Path $PSScriptRoot "..\tools")
        try { $version = go run ./dist -print-version } finally { Pop-Location }
        if (-not $version) { $version = "dev" }
    }
    go build -trimpath -ldflags "-s -w -X main.version=$version" -o "bin/agent-machine.exe" ./cmd/agent-machine
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Output "machine\bin\agent-machine.exe"
} finally {
    Pop-Location
}
