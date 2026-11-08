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
#   -Python                     the Python versions (Python 3.9 or later), in their own
#                               environment (%LOCALAPPDATA%\agent-py), copied into the prefix
#   -NoTray                     no tray icon (agent-tray, put in when Python 3.9 or later is here:
#                               what runs, its start and stop, at every sign-in to Windows)
#
# The agent-server writes this script for its own build ({{VERSION}}), so it always
# installs the programs that speak to it, checked against their SHA-256. It downloads the
# zips for this computer, then runs the installer inside them (install.ps1). With -Python,
# or when there's no build for this processor, it installs the Python wheel instead.
param([string]$Only = "", [switch]$Service, [string]$Prefix = "", [switch]$NoLogin, [switch]$NoStart, [switch]$Python, [switch]$NoTray)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Server = '{{SERVER}}'
$Version = '{{VERSION}}'
# platform component sha256 path, one zip per line; "python python" is the wheel.
$Files = @'
{{FILES}}
'@ -split "`n" | Where-Object { $_.Trim() }

if ($Only -and $Only -notin @("agent", "agent-machine")) { throw "-Only takes agent or agent-machine" }
if (-not $Files) { throw "The agent-server at $Server has no builds to hand out yet (devhelpers/scripts/build-dist.ps1 on its computer)." }

# This computer, named the way tools/dist names its builds ($env:AGENT_PLATFORM overrides it).
$platform = $env:AGENT_PLATFORM
if (-not $platform) {
    $arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    $platform = switch ($arch) {
        "AMD64" { "windows-amd64" }
        "ARM64" { "windows-arm64" }
        "x86" { "windows-386" }
        # No Go build for it: the Python versions, below.
        default { "windows-$arch".ToLower() }
    }
}

function Find-Entry([string]$platform, [string]$component) {
    $Files | ForEach-Object { $parts = $_.Trim() -split "\s+"; if ($parts[0] -eq $platform -and $parts[1] -eq $component) { , $parts } } | Select-Object -First 1
}
# Runs a command for its output alone. PowerShell 5.1 turns a native program's redirected stderr
# into errors, which "Stop" would throw, so they're let through here and dropped.
function Invoke-Probe([scriptblock]$command) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $output = & $command 2>$null; if ($LASTEXITCODE -eq 0) { $output } } catch { $null } finally { $ErrorActionPreference = $previous }
}
# Runs a program for what it does, its exit code in $LASTEXITCODE: a warning it prints on stderr
# mustn't stop the install when this script's output is redirected.
function Invoke-Native([scriptblock]$command) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $command } finally { $ErrorActionPreference = $previous }
}

$wanted = @("agent", "agent-machine") | Where-Object { -not $Only -or $Only -eq $_ }
if (-not $Python -and (Find-Entry "python" "python")) {
    foreach ($component in $wanted) {
        if (-not (Find-Entry $platform $component)) {
            Write-Output "No $component build for $platform`: installing the Python versions instead."
            $Python = [switch]$true
        }
    }
}
$bin = if ($Prefix) { [IO.Path]::GetFullPath($Prefix) } else { Join-Path $env:LOCALAPPDATA "Programs\agent-proxy" }
$pyhome = Join-Path $env:LOCALAPPDATA "agent-py"
$venvPython = Join-Path $pyhome "venv\Scripts\python.exe"
# The downloaded wheel, in a table: Initialize-PythonEnv fills it in, and a function can only change
# a variable of its caller's through a reference (irm | iex runs this as a scriptblock, not a script).
$py = @{ wheel = $null }

# Initialize-PythonEnv: Python 3.9 or later, the wheel (downloaded once and checked), and an
# environment with pip in $pyhome. It returns why it can't, or nothing: -Python stops then, the tray
# is left out.
function Initialize-PythonEnv {
    $entry = Find-Entry "python" "python"
    if (-not $entry) { return "This server's build has no Python wheel; see $Server/get" }
    # The py launcher first, as "python" may be the Store's placeholder.
    $interpreter = $null
    foreach ($candidate in @(@("py", "-3"), @("python"), @("python3"))) {
        if (-not (Get-Command $candidate[0] -ErrorAction SilentlyContinue)) { continue }
        $arguments = @($candidate | Select-Object -Skip 1)
        $found = Invoke-Probe { & $candidate[0] @arguments -c "import sys; print(sys.executable if sys.version_info >= (3, 9) else '')" }
        if ($found) { $interpreter = "$found".Trim(); break }
    }
    if (-not $interpreter) { return "The Python agent, agent-machine and agent-tray need Python 3.9 or later: install it from https://www.python.org/downloads/" }
    if (-not $py.wheel) {
        $file = Join-Path $work ($entry[3] -split "/")[-1]
        Write-Output "Downloading the Python agent and agent-machine $Version" | Out-Host
        try { Invoke-WebRequest -UseBasicParsing -Uri "$Server$($entry[3])" -OutFile $file } catch { return "Couldn't download $Server$($entry[3]): $_" }
        if ((Get-FileHash -Algorithm SHA256 $file).Hash.ToLower() -ne $entry[2]) { Remove-Item -Force $file; return "The wheel doesn't match its SHA-256; not installing it." }
        $py.wheel = $file
    }
    if (-not ((Test-Path $venvPython) -and (Invoke-Probe { & $venvPython -m pip --version }))) {
        if (Test-Path "$pyhome\venv") { Remove-Item -Recurse -Force "$pyhome\venv" }
        New-Item -ItemType Directory -Force $pyhome | Out-Null
        Invoke-Native { & $interpreter -m venv "$pyhome\venv" } | Out-Host
        if ($LASTEXITCODE -ne 0) { return "Couldn't make a Python environment in $pyhome." }
    }
}

$work = Join-Path ([IO.Path]::GetTempPath()) ("agent-install-" + [guid]::NewGuid().ToString("n"))
New-Item -ItemType Directory -Force $work | Out-Null
try {
    $components = @()
    if ($Python) {
        $problem = Initialize-PythonEnv
        if ($problem) { throw $problem }
        Invoke-Native { & $venvPython -m pip install --quiet --disable-pip-version-check --upgrade $py.wheel }
        if ($LASTEXITCODE -ne 0) { throw "pip couldn't install $($py.wheel)." }
        # pip's launchers name their environment's python.exe by its full path, so copies work anywhere.
        New-Item -ItemType Directory -Force $bin | Out-Null
        foreach ($component in $wanted) {
            $programs = if ($component -eq "agent") { @("agent", "agent-proxy") } else { @("agent-machine") }
            foreach ($program in $programs) {
                $target = Join-Path $bin "$program.exe"
                if (Test-Path $target) {
                    # A running program can't be replaced, but it can be renamed.
                    try { Remove-Item -Force $target } catch { Move-Item -Force $target "$target.old-$([DateTime]::UtcNow.Ticks)" }
                }
                Copy-Item (Join-Path $pyhome "venv\Scripts\$program.exe") $target
                Write-Output "Installed $target (Python, in $pyhome)"
            }
            $components += $component
        }
        if ($Service) { Write-Output "-Service sets up the Go agent-machine only; start this one with: agent-machine start" }
        $path = [Environment]::GetEnvironmentVariable("Path", "User"); if (-not $path) { $path = "" }
        if (-not (($path -split ";") | Where-Object { $_ -and $_.TrimEnd("\") -ieq $bin.TrimEnd("\") })) {
            [Environment]::SetEnvironmentVariable("Path", (@($path.TrimEnd(";"), $bin) | Where-Object { $_ }) -join ";", "User")
            Write-Output "Added $bin to your PATH (open a new terminal to use it)."
        }
        if (-not (($env:Path -split ";") | Where-Object { $_ -and $_.TrimEnd("\") -ieq $bin.TrimEnd("\") })) { $env:Path = "$env:Path;$bin" }
    } else {
    foreach ($component in $wanted) {
        $entry = Find-Entry $platform $component
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
    }
    # The tray icon (agent-tray), from the wheel in its own environment. Only agent-tray comes from
    # it: the Go programs stay the ones you run. Without it everything else works the same.
    $tray = $null
    if (-not $NoTray) {
        Write-Output "Installing the tray icon (agent-tray)"
        $problem = Initialize-PythonEnv
        if (-not $problem) {
            Invoke-Native { & $venvPython -m pip install --quiet --disable-pip-version-check --upgrade "$($py.wheel)[tray]" }
            if ($LASTEXITCODE -ne 0) { $problem = "pip couldn't install its tray extra" }
        }
        if ($problem) {
            Write-Output "The tray icon couldn't be installed ($problem); everything else works without it."
        } else {
            $tray = Join-Path $pyhome "venv\Scripts\agent-tray.exe"
            Invoke-Native { & $venvPython -m agent_py.tray --autostart on } | Out-Null
        }
    }
} finally {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $work
}

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
if ($tray -and (Test-Path $tray)) {
    # A windowed program: it stays when this PowerShell closes. One runs at a time; a second leaves.
    Start-Process -FilePath $tray
    Write-Output "Its icon is in the system tray (behind ^ until you drag it out), and starts when you sign in to Windows."
}
