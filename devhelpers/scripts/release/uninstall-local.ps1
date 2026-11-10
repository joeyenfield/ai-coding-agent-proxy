# Removes the AI Control Mesh that install-local.ps1 put on this computer (docs/local.md): the tray
# icon, agent and agent-machine, the local server and its programs. Windows, in PowerShell:
#
#   irm <release>/uninstall-local.ps1 | iex
#   & ([scriptblock]::Create((irm <release>/uninstall-local.ps1))) -Purge
#
#   -Prefix DIR    where agent-server, agent and agent-machine are (default
#                  %LOCALAPPDATA%\Programs\agent-proxy)
#   -KeepClient    leave agent and agent-machine (and their sign-ins) as they are
#   -Purge         also delete the server's data (its database, recordings and keys), and sign
#                  agent and agent-machine out and delete their settings
#
# Without -Purge the server's data stays in its folder, so installing again finds it.
# devhelpers/scripts/release/release.py puts this file in every release as it is. The folders
# are the ones install-local.ps1 uses; keep them in step.
param([string]$Prefix = "", [switch]$KeepClient, [switch]$Purge)
$ErrorActionPreference = "Stop"

# Runs a program for what it does: a warning it prints on stderr mustn't stop the removal.
function Invoke-Program([scriptblock]$command) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $command } finally { $ErrorActionPreference = $previous }
}

# The same folder agent-server local uses (server/agent_server/local.py, instance_home).
$home_ = if ($env:AI_PROXY_LOCAL_HOME) { $env:AI_PROXY_LOCAL_HOME } else { Join-Path $env:LOCALAPPDATA "AI Control Mesh" }
if (-not $Prefix) { $Prefix = Join-Path $env:LOCALAPPDATA "Programs\agent-proxy" }
$Prefix = [IO.Path]::GetFullPath($Prefix).TrimEnd("\")
$venv = Join-Path $home_ "venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
$agentServer = Join-Path $venv "Scripts\agent-server.exe"

# Run through iex this is the user's own PowerShell, so it never calls exit.
if (-not ((Test-Path $venv) -or (Test-Path (Join-Path $home_ "local.json")))) {
    Write-Output "There's no local install in $home_."
} else {
    # The tray icon first: it would start the server again.
    if (Test-Path (Join-Path $venv "Scripts\agent-tray.exe")) {
        Invoke-Program { & $venvPython -m agent_py.tray --autostart off } | Out-Null
        Write-Output "Removed the tray icon."
    }

    # agent and agent-machine, with the uninstaller the server's own files carry. Before the server
    # stops: with -Purge it signs them out there. It's a file on disk, which Windows' default
    # execution policy refuses to run, so it runs in a PowerShell of its own that is told to allow it.
    if (-not $KeepClient) {
        $uninstaller = Get-ChildItem -ErrorAction SilentlyContinue (Join-Path $home_ "app\*\server\agent_server\install\uninstall.ps1") | Select-Object -Last 1
        if ($uninstaller) {
            $shell = (Get-Process -Id $PID).Path
            if ((Split-Path -Leaf "$shell") -notmatch '^(powershell|pwsh)\.exe$') { $shell = "powershell.exe" }
            $arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $uninstaller.FullName, "-Prefix", $Prefix)
            if ($Purge) { $arguments += "-Purge" }
            Invoke-Program { & $shell @arguments }
            if ($LASTEXITCODE -ne 0) { Write-Output "agent and agent-machine weren't fully removed; see above." }
        } else {
            Write-Output "agent and agent-machine are left in $Prefix`: this install has no uninstaller for them."
        }
    }

    if (Test-Path $agentServer) { Invoke-Program { & $agentServer local stop } | Out-Null }
    $link = Join-Path $Prefix "agent-server.cmd"
    if (Test-Path $link) { Remove-Item -Force $link }
    # The folder goes with its last file, and then its PATH entry: nothing is left to find there.
    if ((Test-Path $Prefix) -and -not (Get-ChildItem -Force $Prefix)) {
        Remove-Item -Force $Prefix
        $path = [Environment]::GetEnvironmentVariable("Path", "User")
        if ($path -and (($path -split ";") | Where-Object { $_ -and $_.TrimEnd("\") -ieq $Prefix })) {
            $kept = ($path -split ";") | Where-Object { $_ -and $_.TrimEnd("\") -ine $Prefix }
            [Environment]::SetEnvironmentVariable("Path", $kept -join ";", "User")
        }
    }
    # Windows can't delete a running program: whatever still runs from the environment (the tray
    # icon, a Python agent-machine) is stopped first.
    $left = @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith("$venv\", [StringComparison]::OrdinalIgnoreCase) })
    if ($left) {
        $left | Stop-Process -Force -ErrorAction SilentlyContinue
        $left | Wait-Process -Timeout 10 -ErrorAction SilentlyContinue
    }
    foreach ($folder in @((Join-Path $home_ "app"), $venv)) {
        if (Test-Path $folder) { Remove-Item -Recurse -Force $folder }
    }
    Write-Output "Removed the local server's programs."

    if ($Purge) {
        if (Test-Path $home_) { Remove-Item -Recurse -Force $home_ }
        Write-Output "Deleted its data in $home_."
    } else {
        Write-Output "Its data is still in $home_`: delete that folder too, or run this with -Purge, to remove it."
    }
}
