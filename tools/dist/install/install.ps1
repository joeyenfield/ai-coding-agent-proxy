# Installs the programs beside this script (agent.exe, agent-proxy.exe, agent-machine.exe:
# whichever this zip has) on Windows, for the current user, and puts them on its PATH.
#
#   powershell -ExecutionPolicy Bypass -File install.ps1              into %LOCALAPPDATA%\Programs\agent-proxy
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Prefix DIR  into DIR
#   ... install.ps1 -Service      also start agent-machine when you sign in to Windows (a scheduled task)
#   ... install.ps1 -Uninstall    remove them again, the task and the PATH entry (settings stay;
#                                 uninstall.ps1 beside it does it, and takes -Purge)
param([string]$Prefix = "", [switch]$Service, [switch]$Uninstall)
$ErrorActionPreference = "Stop"

$programs = @("agent.exe", "agent-proxy.exe", "agent-machine.exe")
$task = "agent-machine"
if (-not $Prefix) { $Prefix = Join-Path $env:LOCALAPPDATA "Programs\agent-proxy" }
$Prefix = [IO.Path]::GetFullPath($Prefix)

function Get-UserPath { $p = [Environment]::GetEnvironmentVariable("Path", "User"); if ($p) { $p } else { "" } }
function Test-OnPath([string]$path, [string]$dir) {
    foreach ($entry in $path -split ";") { if ($entry.TrimEnd("\") -ieq $dir.TrimEnd("\")) { return $true } }
    return $false
}

# Stop-Machine stops this user's agent-machine if one is in the fleet, so its file can be
# replaced and the task's own can join, and says so.
function Stop-Machine {
    $exe = Join-Path $Prefix "agent-machine.exe"
    if (-not (Test-Path $exe)) { return }
    $stopped = (& $exe stop 2>&1 | Out-String).Trim()
    if ($stopped -and $stopped -notmatch "isn't running") { Write-Output $stopped }
}

# uninstall.ps1 beside this script does the removing, the same one the server serves.
if ($Uninstall) {
    & (Join-Path $PSScriptRoot "uninstall.ps1") -Prefix $Prefix
    exit 0
}

$found = @($programs | Where-Object { Test-Path (Join-Path $PSScriptRoot $_) })
if (-not $found) { throw "No programs beside this script in $PSScriptRoot" }

New-Item -ItemType Directory -Force $Prefix | Out-Null
foreach ($program in $found) {
    $target = Join-Path $Prefix $program
    # A running agent-proxy can't be overwritten, but it can be renamed out of the way.
    Remove-Item -Force -ErrorAction SilentlyContinue "$target.old"
    if (Test-Path $target) {
        try { Remove-Item -Force $target } catch { Move-Item -Force $target "$target.old" }
    }
    Copy-Item (Join-Path $PSScriptRoot $program) $target
    # A downloaded zip marks its files as from the internet; SmartScreen would ask about each one.
    Unblock-File $target
    Write-Output "Installed $target"
}

$path = Get-UserPath
if (-not (Test-OnPath $path $Prefix)) {
    [Environment]::SetEnvironmentVariable("Path", (@($path.TrimEnd(";"), $Prefix) | Where-Object { $_ }) -join ";", "User")
    Write-Output "Added $Prefix to your PATH (open a new terminal to use it)."
}
if (-not (Test-OnPath $env:Path $Prefix)) { $env:Path = "$env:Path;$Prefix" }

if ($Service) {
    $exe = Join-Path $Prefix "agent-machine.exe"
    if (-not (Test-Path $exe)) { throw "-Service needs agent-machine.exe, which this zip doesn't have." }
    # `start` detaches a windowless `run` and exits, so no console stays open after sign-in.
    $action = New-ScheduledTaskAction -Execute $exe -Argument "start" -WorkingDirectory $Prefix
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings `
        -Description "agent-machine: keeps this computer in its agent-server's fleet" -Force | Out-Null
    Write-Output "Scheduled task ${task}: starts agent-machine when $env:USERNAME signs in."
    Stop-Machine
    & $exe start
    if ((& $exe) -match "not signed in") {
        if (-not $env:AI_PROXY_ONE_LINER) { Write-Output "If it isn't signed in yet: agent-machine login https://your-server:9999, then agent-machine start." }
    }
}

# The server's one-line installer (irm <server>/install.ps1 | iex) signs them in itself.
if ($env:AI_PROXY_ONE_LINER) { return }
if ($found -contains "agent-proxy.exe") { Write-Output "Next: agent login https://your-server:9999 (once), then agent --list" }
if (($found -contains "agent-machine.exe") -and -not $Service) {
    Write-Output "Next: agent-machine login https://your-server:9999 (once), then agent-machine start, or install.ps1 -Service to start it at sign-in"
}
