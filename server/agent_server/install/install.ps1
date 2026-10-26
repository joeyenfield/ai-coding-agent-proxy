# Installs agent (with agent-proxy) and agent-machine from the agent-server at {{SERVER}}
# on Windows, for this computer's processor, then signs them in to it:
#
#   irm {{SERVER}}/install.ps1 | iex
#   & ([scriptblock]::Create((irm {{SERVER}}/install.ps1))) -Only agent-machine -Service
#
#   -Only agent|agent-machine   just one of them (default: both)
#   -Service                    start agent-machine when you sign in to Windows
#   -Prefix DIR                 install into DIR (default: %LOCALAPPDATA%\Programs\agent-proxy)
#   -NoLogin                    don't sign in; print the commands instead
#   -NoStart                    don't start agent-machine after signing it in
#
# The agent-server writes this script for its own build ({{VERSION}}), so it always
# installs the programs that speak to it, checked against their SHA-256. It downloads the
# zips for this computer, then runs the installer inside them (install.ps1).
param([string]$Only = "", [switch]$Service, [string]$Prefix = "", [switch]$NoLogin, [switch]$NoStart)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Server = '{{SERVER}}'
$Version = '{{VERSION}}'
# platform component sha256 path, one zip per line.
$Files = @'
{{FILES}}
'@ -split "`n" | Where-Object { $_.Trim() }

if ($Only -and $Only -notin @("agent", "agent-machine")) { throw "-Only takes agent or agent-machine" }
if (-not $Files) { throw "The agent-server at $Server has no builds to hand out yet (scripts/build-dist.ps1 on its computer)." }

# This computer, named the way tools/dist names its builds ($env:AGENT_PLATFORM overrides it).
$platform = $env:AGENT_PLATFORM
if (-not $platform) {
    $arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    $platform = switch ($arch) {
        "AMD64" { "windows-amd64" }
        "ARM64" { "windows-arm64" }
        "x86" { "windows-386" }
        default { throw "No build for $arch processors; see $Server/get" }
    }
}

$work = Join-Path ([IO.Path]::GetTempPath()) ("agent-install-" + [guid]::NewGuid().ToString("n"))
New-Item -ItemType Directory -Force $work | Out-Null
try {
    $components = @()
    foreach ($component in @("agent", "agent-machine")) {
        if ($Only -and $Only -ne $component) { continue }
        $entry = $Files | ForEach-Object { $parts = $_.Trim() -split "\s+"; if ($parts[0] -eq $platform -and $parts[1] -eq $component) { , $parts } } | Select-Object -First 1
        if (-not $entry) { Write-Output "No $component for $platform in this server's build; see $Server/get"; continue }
        $zip = Join-Path $work ($entry[3] -split "/")[-1]
        Write-Output "Downloading $component $Version for $platform"
        Invoke-WebRequest -UseBasicParsing -Uri "$Server$($entry[3])" -OutFile $zip
        if ((Get-FileHash -Algorithm SHA256 $zip).Hash.ToLower() -ne $entry[2]) { throw "$component's download doesn't match its SHA-256; not installing it." }
        Expand-Archive -Force $zip $work
        $components += $component
    }
    if (-not $components) { throw "Nothing to install for $platform." }

    # The zips' own installer puts every program beside it on the PATH (and sets up -Service).
    $installer = @{}
    if ($Prefix) { $installer.Prefix = $Prefix }
    if ($Service) { $installer.Service = $true }
    # It leaves signing in to this script.
    $env:AI_PROXY_ONE_LINER = "1"
    try { & (Join-Path $work "$platform\install.ps1") @installer } finally { Remove-Item Env:AI_PROXY_ONE_LINER -ErrorAction SilentlyContinue }
} finally {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $work
}

$bin = if ($Prefix) { [IO.Path]::GetFullPath($Prefix) } else { Join-Path $env:LOCALAPPDATA "Programs\agent-proxy" }
foreach ($component in $components) {
    $exe = Join-Path $bin "$component.exe"
    if ($component -eq "agent") {
        if ($NoLogin) { Write-Output "Next: agent login $Server"; continue }
        Write-Output ""
        Write-Output "Signing agent in to $Server"
        & $exe login $Server
        if ($LASTEXITCODE -ne 0) { Write-Output "Sign in later with: agent login $Server" }
    } else {
        if ($NoLogin) { Write-Output "Next: agent-machine login $Server, then agent-machine start"; continue }
        Write-Output ""
        Write-Output "Signing agent-machine in to $Server"
        & $exe login $Server
        if ($LASTEXITCODE -ne 0) {
            Write-Output "Sign in later with: agent-machine login $Server, then agent-machine start"
        } elseif (-not $NoStart) {
            # Started (or restarted) now, signed in; with -Service it also starts at every sign-in.
            & $exe stop | Out-Null
            & $exe start
        }
    }
}
Write-Output ""
Write-Output "Done: $Version from $Server. Open its console at $Server"
