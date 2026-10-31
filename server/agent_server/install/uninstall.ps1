# Removes agent.exe, agent-proxy.exe and agent-machine.exe from this computer, as install.ps1
# or the server's one-line installer put them there, with their PATH entry and scheduled task:
#
#   irm <server>/uninstall.ps1 | iex
#   & ([scriptblock]::Create((irm <server>/uninstall.ps1))) -Purge
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1    from an unpacked zip (install.ps1 -Uninstall runs it)
#
#   -Only agent|agent-machine   just one of them (default: both)
#   -Prefix DIR                 the folder they're in (default: %LOCALAPPDATA%\Programs\agent-proxy)
#   -Purge                      also sign them out (revoking their tokens) and delete their
#                               settings, sign-ins, logs and caches
#
# It stops agent-machine and agent-proxy first, as Windows can't delete a running program.
# An .msi install is removed from Settings > Apps instead. Containers agent-machine ran and
# models it pulled stay.
#
# The agent-server serves this file as it is (server/agent_server/install/uninstall.ps1), and
# tools/dist puts the same file in every zip. The task's name is the one
# tools/dist/install/install.ps1 registers; keep them in step.
param([string]$Only = "", [string]$Prefix = "", [switch]$Purge)
$ErrorActionPreference = "Stop"

# Run through iex this is the user's own PowerShell, so it never calls exit: it throws.
if ($Only -and $Only -notin @("agent", "agent-machine")) { throw "-Only takes agent or agent-machine" }
$programs = @()
if ($Only -ne "agent-machine") { $programs += @("agent.exe", "agent-proxy.exe") }
if ($Only -ne "agent") { $programs += "agent-machine.exe" }
# AI_PROXY_SCHEDULED_TASK names another task, for tests on a computer that has the real one.
$task = if ($env:AI_PROXY_SCHEDULED_TASK) { $env:AI_PROXY_SCHEDULED_TASK } else { "agent-machine" }
if (-not $Prefix) { $Prefix = Join-Path $env:LOCALAPPDATA "Programs\agent-proxy" }
$Prefix = [IO.Path]::GetFullPath($Prefix).TrimEnd("\")
# Where the Go programs keep them: %APPDATA%, and the home folder as Go's os.UserHomeDir finds it.
$config = if ($env:APPDATA) { $env:APPDATA } else { Join-Path $env:USERPROFILE "AppData\Roaming" }

function Get-UserPath { $p = [Environment]::GetEnvironmentVariable("Path", "User"); if ($p) { $p } else { "" } }
function Test-OnPath([string]$path, [string]$dir) {
    foreach ($entry in $path -split ";") { if ($entry.TrimEnd("\") -ieq $dir) { return $true } }
    return $false
}

# Invoke-Program runs one of the programs and returns what it printed, its errors included:
# with "Stop", Windows PowerShell would throw at the first line it writes to stderr.
function Invoke-Program([string]$exe, [string[]]$arguments) {
    $ErrorActionPreference = "Continue"
    ((& $exe @arguments 2>&1 | ForEach-Object { "$_" }) -join "`n").Trim()
}

# Stop-Proxy stops this install's agent-proxy; Windows can't delete its program while it runs.
# Agents running through it lose their connection to the server. `agent proxy stop` asks the one
# on its port to exit; one on another port, or too old to be asked, is ended by its path (the
# launcher starts it as `agent.exe proxy`).
function Stop-Proxy {
    $agent = Join-Path $Prefix "agent.exe"
    if ((Test-Path $agent) -and ((Invoke-Program $agent @("proxy", "stop")) -match "agent-proxy stopped")) {
        Write-Output "Stopped agent-proxy."
    }
    foreach ($name in @("agent", "agent-proxy")) {
        $exe = Join-Path $Prefix "$name.exe"
        Get-CimInstance Win32_Process -Filter "Name = '$name.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.ExecutablePath -ieq $exe -and ($name -eq "agent-proxy" -or $_.CommandLine -match '\sproxy(\s|$)') } |
            ForEach-Object {
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
                Write-Output "Stopped agent-proxy (process $($_.ProcessId))."
            }
    }
}

# Remove-SignIns signs one program out of every server it's signed in to; logout takes one at a time.
function Remove-SignIns([string]$program) {
    $exe = Join-Path $Prefix $program
    if (-not (Test-Path $exe)) { return }
    for ($i = 0; $i -lt 20; $i++) {
        $out = Invoke-Program $exe "logout"
        if ($out -match "Not signed in") { return }
        Write-Output $out
        if ($out -notmatch "Signed out") { return }
    }
}

function Remove-Folder([string]$path) {
    if ($path -and (Test-Path $path)) { Remove-Item -Recurse -Force $path; Write-Output "Deleted $path" }
}

if ($programs -contains "agent-machine.exe") {
    if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $task -Confirm:$false
        Write-Output "Removed the scheduled task $task."
    }
    $machine = Join-Path $Prefix "agent-machine.exe"
    if (Test-Path $machine) {
        $stopped = Invoke-Program $machine "stop"
        if ($stopped -and $stopped -notmatch "isn't running") { Write-Output $stopped }
    }
}
if ($programs -contains "agent.exe") { Stop-Proxy }
if ($Purge) {
    foreach ($program in @("agent.exe", "agent-machine.exe")) { if ($programs -contains $program) { Remove-SignIns $program } }
}

$busy = @()
$removed = $false
foreach ($program in $programs) {
    $target = Join-Path $Prefix $program
    Remove-Item -Force -ErrorAction SilentlyContinue "$target.old"
    if (-not (Test-Path $target)) { continue }
    try {
        Remove-Item -Force $target
        Write-Output "Removed $target"
        $removed = $true
    } catch {
        $busy += $target
    }
}
if (-not $removed -and -not $busy) { Write-Output "Nothing to remove in $Prefix." }

# The PATH entry and the folder go once none of the programs are left in it.
if (-not (@("agent.exe", "agent-proxy.exe", "agent-machine.exe") | Where-Object { Test-Path (Join-Path $Prefix $_) })) {
    $path = Get-UserPath
    if (Test-OnPath $path $Prefix) {
        $kept = ($path -split ";" | Where-Object { $_ -and ($_.TrimEnd("\") -ine $Prefix) }) -join ";"
        [Environment]::SetEnvironmentVariable("Path", $kept, "User")
        Write-Output "Removed $Prefix from your PATH."
    }
    if ((Test-Path $Prefix) -and -not (Get-ChildItem $Prefix -Force)) { Remove-Item $Prefix }
}

if ($Purge) {
    if ($programs -contains "agent.exe") {
        if ($env:AI_PROXY_CREDENTIALS) {
            Remove-Folder $env:AI_PROXY_CREDENTIALS
            Remove-Folder "$($env:AI_PROXY_CREDENTIALS).lock"
        }
        Remove-Folder (Join-Path $config "agent-proxy")
        Remove-Folder (Join-Path $env:USERPROFILE ".cache\agent-proxy")
    }
    if ($programs -contains "agent-machine.exe") {
        # A folder named by hand may hold more than agent-machine's files.
        if ($env:AI_PROXY_MACHINE_HOME) {
            Write-Output "Left $($env:AI_PROXY_MACHINE_HOME) (AI_PROXY_MACHINE_HOME); delete it yourself if it only held agent-machine's files."
        } else {
            Remove-Folder (Join-Path $config "agent-machine")
        }
    }
} else {
    Write-Output "Sign-ins and settings are left in place; -Purge signs out and deletes them too."
}

# Copies installed some other way: say how to remove them rather than guessing.
foreach ($program in $programs) {
    $found = Get-Command $program -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $found -or ($found.Source -ieq (Join-Path $Prefix $program))) { continue }
    $folder = Split-Path $found.Source
    if ($found.Source -match "\\Python[^\\]*\\|\\Scripts\\") {
        Write-Output "$($found.Source) is the Python version: pip uninstall ai-proxy-agent-py"
    } elseif ($env:ProgramFiles -and $found.Source.StartsWith($env:ProgramFiles, [StringComparison]::OrdinalIgnoreCase)) {
        Write-Output "$($found.Source) came from an .msi: remove it in Settings > Apps"
    } else {
        Write-Output "$($found.Source) is still there: run this again with -Prefix `"$folder`""
    }
}

if ($busy) { throw "Couldn't remove $($busy -join ', '): in use, most likely by an agent running through it. Close it and run this again." }
