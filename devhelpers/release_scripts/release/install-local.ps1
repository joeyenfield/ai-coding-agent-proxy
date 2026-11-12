# Installs AI Control Mesh {{VERSION}} on this computer, for you alone, and starts it: the server
# and its sign-in service on localhost, then agent and agent-machine signed in to it
# (docs/local.md). Windows, in PowerShell:
#
#   irm {{BASE}}/install-local.ps1 | iex
#   & ([scriptblock]::Create((irm {{BASE}}/install-local.ps1))) -Port 29999 -AuthPort 30000
#
#   -Port N, -AuthPort N   the console's and agent-auth's ports (default 19999 and 20000; kept)
#   -Prefix DIR            where agent-server, agent and agent-machine go (default
#                          %LOCALAPPDATA%\Programs\agent-proxy, put on your PATH)
#   -NoClient              only the server: no agent or agent-machine
#   -NoLogin               don't sign agent and agent-machine in; print the commands instead
#   -Python                the Python agent and agent-machine rather than the Go builds (used
#                          anyway when there's no Go build for this processor)
#   -NoTray                no tray icon (agent-tray: it shows what runs, starts and stops it,
#                          opens the console, and starts when you sign in to Windows)
#
# Needs Python 3.11 or later (python.org's, or py). Run it again to update. Every download is
# checked against the SHA-256 written into this script when the release was made.
# devhelpers/release_scripts/release/release.py fills it in.
param([int]$Port = 0, [int]$AuthPort = 0, [string]$Prefix = "", [switch]$NoClient, [switch]$NoLogin, [switch]$Python, [switch]$NoTray)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Base = '{{BASE}}'
$Version = '{{VERSION}}'
# The server bundle: name sha256.
$Bundle = '{{BUNDLE}}' -split " "
# The Python agent and agent-machine, for any system: name sha256.
$Wheel = '{{WHEEL}}' -split " "
# platform component sha256 name, one zip per line.
$Files = @'
{{FILES}}
'@ -split "`n" | Where-Object { $_.Trim() }

$arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
$platform = if ($env:AGENT_PLATFORM) { $env:AGENT_PLATFORM } else {
    switch ($arch) { "AMD64" { "windows-amd64" } "ARM64" { "windows-arm64" } "x86" { "windows-386" } default { "windows-$arch" } }
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
function Invoke-Program([scriptblock]$command) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $command } finally { $ErrorActionPreference = $previous }
}

# Python 3.11 or later: the py launcher first, as "python" may be the Store's placeholder.
$interpreter = $null
foreach ($candidate in @(@("py", "-3"), @("python"), @("python3"))) {
    if (-not (Get-Command $candidate[0] -ErrorAction SilentlyContinue)) { continue }
    $arguments = @($candidate | Select-Object -Skip 1)
    $found = Invoke-Probe { & $candidate[0] @arguments -c "import sys; print(sys.executable if sys.version_info >= (3, 11) else '')" }
    if ($found) { $interpreter = "$found".Trim(); break }
}
if (-not $interpreter) { throw "Needs Python 3.11 or later: install it from https://www.python.org/downloads/ and run this again." }

function Get-Release([string]$name, [string]$sum, [string]$file) {
    Invoke-WebRequest -UseBasicParsing -Uri "$Base/$name" -OutFile $file
    if ((Get-FileHash -Algorithm SHA256 $file).Hash.ToLower() -ne $sum) { throw "$name doesn't match its SHA-256; not installing it." }
}
function Get-UserPath { $p = [Environment]::GetEnvironmentVariable("Path", "User"); if ($p) { $p } else { "" } }
function Test-OnPath([string]$path, [string]$dir) {
    $dir = $dir.TrimEnd("\")
    foreach ($item in $path -split ";") { if ($item -and $item.TrimEnd("\") -ieq $dir) { return $true } }
    return $false
}

# The same folder agent-server local uses (server/agent_server/local.py, instance_home).
$home_ = if ($env:AI_PROXY_LOCAL_HOME) { $env:AI_PROXY_LOCAL_HOME } else { Join-Path $env:LOCALAPPDATA "AI Control Mesh" }
if (-not $Prefix) { $Prefix = Join-Path $env:LOCALAPPDATA "Programs\agent-proxy" }
$Prefix = [IO.Path]::GetFullPath($Prefix)
$venv = Join-Path $home_ "venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
$agentServer = Join-Path $venv "Scripts\agent-server.exe"
$apps = Join-Path $home_ "app"
$app = Join-Path $apps $Version

$work = Join-Path ([IO.Path]::GetTempPath()) ("agent-local-" + [guid]::NewGuid().ToString("n"))
New-Item -ItemType Directory -Force $work | Out-Null
$installed = @()
try {
    Write-Output "Downloading AI Control Mesh $Version"
    $zip = Join-Path $work $Bundle[0]
    Get-Release $Bundle[0] $Bundle[1] $zip

    # A running instance is stopped first, so its files can be replaced; its data stays.
    if (Test-Path $agentServer) { Invoke-Program { & $agentServer local stop } | Out-Null }
    New-Item -ItemType Directory -Force $apps | Out-Null
    if (Test-Path "$app.new") { Remove-Item -Recurse -Force "$app.new" }
    Invoke-Program { & $interpreter -m zipfile -e $zip "$app.new" }
    if ($LASTEXITCODE -ne 0) { throw "Couldn't unpack $($Bundle[0])." }
    if (Test-Path $app) { Remove-Item -Recurse -Force $app }
    Move-Item "$app.new" $app

    $hasPip = $false
    if (Test-Path $venvPython) { $hasPip = [bool](Invoke-Probe { & $venvPython -m pip --version }) }
    if (-not $hasPip) {
        Write-Output "Making a Python environment in $venv"
        if (Test-Path $venv) { Remove-Item -Recurse -Force $venv }
        Invoke-Program { & $interpreter -m venv $venv }
        if ($LASTEXITCODE -ne 0) { throw "Couldn't make a Python environment in $venv." }
    }
    Write-Output "Installing agent-server (a minute or two the first time)"
    Invoke-Program { & $venvPython -m pip install --quiet --disable-pip-version-check -e (Join-Path $app "server") }
    if ($LASTEXITCODE -ne 0) { throw "pip couldn't install agent-server." }
    # Earlier versions' folders: the environment now points at this one.
    Get-ChildItem -Directory $apps | Where-Object { $_.FullName -ne $app } | Remove-Item -Recurse -Force

    # agent-server on the PATH, beside agent and agent-machine.
    New-Item -ItemType Directory -Force $Prefix | Out-Null
    Set-Content -Encoding ascii (Join-Path $Prefix "agent-server.cmd") "@`"$agentServer`" %*"
    $path = Get-UserPath
    if (-not (Test-OnPath $path $Prefix)) {
        [Environment]::SetEnvironmentVariable("Path", (@($path.TrimEnd(";"), $Prefix) | Where-Object { $_ }) -join ";", "User")
        Write-Output "Added $Prefix to your PATH (open a new terminal to use it)."
    }
    if (-not (Test-OnPath $env:Path $Prefix)) { $env:Path = "$env:Path;$Prefix" }

    $start = @("local", "start")
    if ($Port) { $start += @("--port", "$Port") }
    if ($AuthPort) { $start += @("--auth-port", "$AuthPort") }
    Invoke-Program { & $agentServer @start }
    if ($LASTEXITCODE -ne 0) { throw "The local server didn't start; see the logs in $home_\logs." }
    $state = Get-Content -Raw (Join-Path $home_ "local.json") | ConvertFrom-Json
    $server = "http://localhost:$(if ($state.port) { $state.port } else { 19999 })"

    # The wheel: the Python agent and agent-machine, and agent-tray (its tray extra).
    $wheelFile = Join-Path $work $Wheel[0]
    $wheelFetched = $false
    $entries = @{}
    foreach ($component in @("agent", "agent-machine")) {
        $entries[$component] = $Files | ForEach-Object { $parts = $_.Trim() -split "\s+"; if ($parts[0] -eq $platform -and $parts[1] -eq $component) { , $parts } } | Select-Object -First 1
    }
    if (-not $NoClient -and -not $Python -and -not ($entries["agent"] -and $entries["agent-machine"])) {
        Write-Output "There's no Go build for $platform in this release: installing the Python agent and agent-machine instead."
        $Python = [switch]$true
    }
    if (-not $NoClient -and $Python) {
        # Into the server's environment (Python 3.11 or later already). pip's launchers name that
        # environment's python.exe by its full path, so copies of them work in the prefix.
        Write-Output "Downloading the Python agent and agent-machine $Version"
        Get-Release $Wheel[0] $Wheel[1] $wheelFile
        $wheelFetched = $true
        $extra = if ($NoTray) { "" } else { "[tray]" }
        Invoke-Program { & $venvPython -m pip install --quiet --disable-pip-version-check --upgrade "$wheelFile$extra" }
        if ($LASTEXITCODE -ne 0) { throw "pip couldn't install $($Wheel[0])." }
        foreach ($program in @("agent", "agent-proxy", "agent-machine")) {
            $target = Join-Path $Prefix "$program.exe"
            if (Test-Path $target) {
                # A running program can't be replaced, but it can be renamed.
                try { Remove-Item -Force $target } catch { Move-Item -Force $target "$target.old-$([DateTime]::UtcNow.Ticks)" }
            }
            Copy-Item (Join-Path $venv "Scripts\$program.exe") $target
            Write-Output "Installed $target (Python)"
        }
        $installed = @("agent", "agent-machine")
    } elseif (-not $NoClient) {
        foreach ($component in @("agent", "agent-machine")) {
            $entry = $entries[$component]
            if (-not $entry) { Write-Output "There's no $component for $platform in this release."; continue }
            Write-Output "Downloading $component $Version for $platform"
            $archive = Join-Path $work $entry[3]
            Get-Release $entry[3] $entry[2] $archive
            Expand-Archive -Force $archive $work
            $installed += $component
        }
        if ($installed) {
            # The zips' own installer puts the programs in the prefix; signing in is done below.
            # It's a file on disk, which Windows' default execution policy refuses to run (irm | iex
            # isn't one), so it runs in a PowerShell of its own that is told to allow it.
            $shell = (Get-Process -Id $PID).Path
            # Another host (the ISE, say) can't run a file this way: Windows' own PowerShell can.
            if ((Split-Path -Leaf "$shell") -notmatch '^(powershell|pwsh)\.exe$') { $shell = "powershell.exe" }
            $env:AI_PROXY_ONE_LINER = "1"
            try {
                Invoke-Program { & $shell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $work "$platform\install.ps1") -Prefix $Prefix.TrimEnd("\") }
            } finally { Remove-Item Env:AI_PROXY_ONE_LINER -ErrorAction SilentlyContinue }
            if ($LASTEXITCODE -ne 0) { throw "The installer in the download didn't finish (exit code $LASTEXITCODE)." }
        }
    }
    $tray = $null
    if (-not $NoTray) {
        if (-not $wheelFetched) {
            # Only agent-tray comes from it: the Go agent and agent-machine above stay the ones you run.
            Write-Output "Installing the tray icon"
            Get-Release $Wheel[0] $Wheel[1] $wheelFile
            Invoke-Program { & $venvPython -m pip install --quiet --disable-pip-version-check --upgrade "$wheelFile[tray]" }
        }
        if ($LASTEXITCODE -eq 0 -and (Test-Path (Join-Path $venv "Scripts\agent-tray.exe"))) {
            $tray = Join-Path $venv "Scripts\agent-tray.exe"
            Invoke-Program { & $venvPython -m agent_py.tray --autostart on } | Out-Null
        } else {
            Write-Output "The tray icon couldn't be installed (pip install $($Wheel[0])[tray] failed); everything else works without it."
        }
    }
} finally {
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $work
}

foreach ($component in $installed) {
    $exe = Join-Path $Prefix "$component.exe"
    if ($component -eq "agent") {
        if ($NoLogin) { Write-Output "Next: agent login $server"; continue }
        Write-Output ""
        Write-Output "Signing agent in to $server (sign in as admin in the browser)"
        Invoke-Program { & $exe login $server }
        if ($LASTEXITCODE -ne 0) { Write-Output "Sign in later with: agent login $server" }
    } else {
        if ($NoLogin) { Write-Output "Next: agent-machine login $server --enable launches,files, then agent-machine start"; continue }
        Write-Output ""
        Write-Output "Signing agent-machine in to $server"
        Invoke-Program { & $exe login $server --enable launches,files }
        if ($LASTEXITCODE -ne 0) {
            Write-Output "Sign in later with: agent-machine login $server --enable launches,files, then agent-machine start"
        } else {
            Invoke-Program { & $exe stop } | Out-Null
            Invoke-Program { & $exe start }
        }
    }
}
Write-Output ""
Write-Output "Done: AI Control Mesh $Version runs at $server"
# Said again here: the first start printed it before the sign-ins scrolled it away.
$passwordFile = Join-Path $home_ "private\first-user-password"
if (Test-Path $passwordFile) {
    Write-Output "Open $server and sign in as admin. The password is in $passwordFile"
} else {
    Write-Output "Open $server and sign in with your account."
}
if ($installed -contains "agent-machine") { Write-Output "Let the console start agents in a folder of yours: agent-machine roots add <folder>" }
Write-Output "Stop it with agent-server local stop; start it again with agent-server local."
Write-Output "Remove it with: irm $Base/uninstall-local.ps1 | iex"
if ($tray) {
    # A windowed program: it stays when this PowerShell closes. One runs at a time; a second leaves.
    Start-Process -FilePath $tray
    Write-Output "Its icon is in the system tray (behind ^ until you drag it out), and starts when you sign in to Windows."
}
