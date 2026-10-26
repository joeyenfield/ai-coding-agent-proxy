# Build agent (agent, agent-proxy), agent-machine and the Android app for every standard platform:
#   .\scripts\build-dist.ps1                                     every target (tools\dist lists them)
#   .\scripts\build-dist.ps1 -Targets linux/arm64,windows/amd64  only these
#   .\scripts\build-dist.ps1 -Only agent-machine                 only one component
#   .\scripts\build-dist.ps1 -Packages                           native packages too: .deb, .rpm, .apk,
#                                                                Arch; .msi with wix on PATH; .pkg on a Mac
#   .\scripts\build-dist.ps1 -NoAndroid                          without the Android app (built when a
#                                                                JDK and the Android SDK are here)
# Everything goes into dist\<version>\ (only that version's folder is replaced): per target
# dist\<version>\<os>-<arch>\ has an archive per component (.zip for Windows, .tar.gz
# elsewhere, each with its installer: install.ps1 or install.sh) and the packages, and
# artifact\ there has the binaries; dist\<version>\android\ the app; dist\<version>\SHA256SUMS
# covers every file. The server hands out the folder of its own version.
# The version is the checkout's: VERSION at the root, plus the commit unless HEAD is its tag
# (tools\dist\version.go). $env:VERSION names another.
param([string[]]$Targets = @(), [string]$Only = "", [switch]$Packages, [switch]$NoAndroid)
$ErrorActionPreference = "Stop"
# Push, not Set: a script run with & shares the caller's location.
Push-Location (Join-Path $PSScriptRoot "..\tools")
try {
    if (-not $env:GOTOOLCHAIN) { $env:GOTOOLCHAIN = "local" }
    $android = (-not $NoAndroid.IsPresent).ToString().ToLower()
    go run ./dist "-version=$env:VERSION" "-only=$Only" "-packages=$($Packages.IsPresent.ToString().ToLower())" "-android=$android" @Targets
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}
