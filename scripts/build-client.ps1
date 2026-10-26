# Build the Go client (agent, agent-proxy) into client\bin for this machine:
#   .\scripts\build-client.ps1
# or for every platform (with agent-machine), into dist\<version>\<os>-<arch>, to hand to other machines:
#   .\scripts\build-client.ps1 -All      (the same as .\scripts\build-dist.ps1)
param([switch]$All)
$ErrorActionPreference = "Stop"
if ($All) {
    & (Join-Path $PSScriptRoot "build-dist.ps1")
    exit $LASTEXITCODE
}
# Push, not Set: a script run with & shares the caller's location.
Push-Location (Join-Path $PSScriptRoot "..\client")
if (-not $env:GOTOOLCHAIN) { $env:GOTOOLCHAIN = "local" }
$version = $env:VERSION
if (-not $version) {
    # The checkout's version, as the server and tools\dist work it out (VERSION, plus the commit).
    Push-Location (Join-Path $PSScriptRoot "..\tools")
    try { $version = go run ./dist -print-version } finally { Pop-Location }
    if (-not $version) { $version = "dev" }
}
$flags = "-s -w -X main.version=$version"
function Build($os, $arch, $out) {
    $ext = if ($os -eq "windows") { ".exe" } else { "" }
    foreach ($program in "agent", "agent-proxy") {
        $env:GOOS = $os; $env:GOARCH = $arch; $env:CGO_ENABLED = "0"
        go build -trimpath -ldflags $flags -o "$out/$program$ext" "./cmd/$program"
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
}
try {
    Build (go env GOHOSTOS) (go env GOHOSTARCH) "bin"
    Write-Output "client\bin\agent.exe"
} finally {
    Remove-Item Env:GOOS, Env:GOARCH, Env:CGO_ENABLED -ErrorAction SilentlyContinue
    Pop-Location
}
