# Public site and agent-server HTTPS on Windows: nginx with a Let's Encrypt certificate, renewed by itself.
#
# Run once from an elevated PowerShell (it opens firewall ports and registers startup and
# renewal tasks); rerun it any time, it picks up where things are:
#
#   powershell -ExecutionPolicy Bypass -File scripts\https\setup-https.ps1 -HostName aicontrolmesh.com
#
# What it sets up, in -Root (default C:\ProgramData\agent-proxy-https):
#   nginx\      nginx for Windows (the current stable release from nginx.org), serving
#               HTTP on -HttpPort (9980) for Let's Encrypt's check and HTTPS on -HttpsPorts
#               (9443, 9998), routing the apex/www to -SiteBackend, console to
#               -ConsoleBackend, and api to -Backend
#   win-acme\   win-acme, the Let's Encrypt client: it proves the name by putting a file
#               where nginx serves /.well-known/acme-challenge/, writes the certificate to
#               certs\, and renews it from its own scheduled task, reloading nginx
#   tasks       "agent-proxy-https nginx" starts nginx at boot; firewall rules open the ports
#
# The apex, www, console and api names have to reach this computer from the internet on port 80 for Let's Encrypt
# (the router sends 80 to -HttpPort), and on 443 for people (to the first -HttpsPorts).
#
# Testing: -Staging uses Let's Encrypt's staging service (a certificate browsers don't
# trust, but no rate limits); -CertFile/-KeyFile use your own certificate instead;
# -NoTasks skips the firewall and tasks (and the need for admin).
param(
    [string]$HostName = "aicontrolmesh.com",
    [string]$ConsoleHostName = "",
    [string]$ApiHostName = "",
    [string]$AuthHostName = "",
    [string]$AdditionalHostNames = "aiproxy.hopto.me",
    [string]$Email = "",
    [string]$Root = (Join-Path $env:ProgramData "agent-proxy-https"),
    [string]$Backend = "http://127.0.0.1:9999",
    [string]$AuthBackend = "http://127.0.0.1:10000",
    [string]$ConsoleBackend = "http://127.0.0.1:8081",
    [string]$SiteBackend = "http://127.0.0.1:8080",
    [int]$HttpPort = 9980,
    # Comma-separated: powershell -File passes "9443,9998" as one string.
    [string]$HttpsPorts = "9443,9998",
    [switch]$Staging,
    [string]$CertFile = "",
    [string]$KeyFile = "",
    [switch]$NoTasks
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[int[]]$HttpsPorts = @($HttpsPorts -split "[,\s]+" | Where-Object { $_ } | ForEach-Object { [int]$_ })
if (-not $HttpsPorts) { throw "-HttpsPorts needs at least one port" }
$HostNames = @($HostName, "www.$HostName") + @($AdditionalHostNames -split "[,\s]+" | Where-Object { $_ })
if (-not $ConsoleHostName) { $ConsoleHostName = "console.$HostName" }
$HostNames += $ConsoleHostName
if (-not $ApiHostName) { $ApiHostName = "api.$HostName" }
$HostNames += $ApiHostName
if (-not $AuthHostName) { $AuthHostName = "auth.$HostName" }
$HostNames += $AuthHostName
$HostNames = @($HostNames | Select-Object -Unique)
$wacsHostNames = $HostNames -join ","
$siteHostNames = @($HostName, "www.$HostName") -join " "
$consoleHostNames = $ConsoleHostName
$apiHostNames = @($ApiHostName) + @($AdditionalHostNames -split "[,\s]+" | Where-Object { $_ })
$apiHostNames = @($apiHostNames | Select-Object -Unique) -join " "
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$here = $PSScriptRoot
$Root = [IO.Path]::GetFullPath($Root)
$nginxDir = Join-Path $Root "nginx"
$wacsDir = Join-Path $Root "win-acme"
$acmeDir = Join-Path $Root "acme"
$certDir = Join-Path $Root "certs"
$nginx = Join-Path $nginxDir "nginx.exe"
$wacs = Join-Path $wacsDir "wacs.exe"
$task = "agent-proxy-https nginx"
$cert = Join-Path $certDir "$HostName-chain.pem"
$key = Join-Path $certDir "$HostName-key.pem"
# nginx wants forward slashes, and a prefix ending in one.
function Slash([string]$path) { $path.Replace("\", "/") }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin -and -not $NoTasks) {
    throw "Run this from an elevated PowerShell (Run as administrator): it opens firewall ports and registers the startup and renewal tasks. -NoTasks skips those."
}
foreach ($name in $HostNames) {
    if ($name -notmatch '^[A-Za-z0-9.-]+$') { throw "Host names must be DNS names, such as aicontrolmesh.com" }
}
New-Item -ItemType Directory -Force $Root, $acmeDir, $certDir | Out-Null

# Native programs' output, stderr included: PowerShell 5.1 turns redirected stderr into errors under Stop.
function Invoke-Native([string]$program, [string[]]$arguments) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $output = & $program @arguments 2>&1 | ForEach-Object { "$_" } } finally { $ErrorActionPreference = $previous }
    return @{ Code = $LASTEXITCODE; Text = ($output -join "`n") }
}

function Expand-Download([string]$url) {
    $zip = Join-Path ([IO.Path]::GetTempPath()) ([IO.Path]::GetRandomFileName() + ".zip")
    Write-Host "Downloading $url"
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $zip
    $unpacked = Join-Path ([IO.Path]::GetTempPath()) ([IO.Path]::GetRandomFileName())
    Expand-Archive -Force $zip $unpacked
    Remove-Item -Force $zip
    return $unpacked
}

# nginx for Windows: the current stable release.
if (-not (Test-Path $nginx)) {
    $page = (Invoke-WebRequest -UseBasicParsing -Uri "https://nginx.org/en/download.html").Content
    $stable = [regex]::Match($page, '(?s)Stable version.*?nginx-(\d+\.\d+\.\d+)\.zip')
    if (-not $stable.Success) { throw "Couldn't find nginx's stable release on nginx.org/en/download.html" }
    $unpacked = Expand-Download "https://nginx.org/download/nginx-$($stable.Groups[1].Value).zip"
    Move-Item (Join-Path $unpacked "nginx-$($stable.Groups[1].Value)") $nginxDir
    Remove-Item -Recurse -Force $unpacked
}
Write-Output "nginx: $((Invoke-Native $nginx @('-v')).Text)"

# win-acme: the latest release, unless a certificate is given.
if (-not $CertFile -and -not (Test-Path $wacs)) {
    $release = Invoke-RestMethod -UseBasicParsing -Uri "https://api.github.com/repos/win-acme/win-acme/releases/latest" -Headers @{ "User-Agent" = "agent-proxy-setup" }
    $asset = $release.assets | Where-Object { $_.name -match '\.x64\.pluggable\.zip$' } | Select-Object -First 1
    if (-not $asset) { throw "Couldn't find win-acme's x64 build in its latest release ($($release.tag_name))" }
    $unpacked = Expand-Download $asset.browser_download_url
    New-Item -ItemType Directory -Force $wacsDir | Out-Null
    Get-ChildItem $unpacked | Move-Item -Destination $wacsDir -Force
    Remove-Item -Recurse -Force $unpacked
}

# The reload win-acme runs after each renewal, and nginx's own commands.
$reload = Join-Path $Root "reload-nginx.ps1"
Set-Content -Encoding UTF8 -Path $reload -Value @"
# Written by setup-https.ps1: win-acme runs it after renewing the certificate.
& "$nginx" -p "$(Slash $nginxDir)/" -s reload
"@

function Write-Config([bool]$https) {
    $listen = ($HttpsPorts | ForEach-Object { "        listen       $_ ssl;" }) -join "`n"
    $listenDefault = ($HttpsPorts | ForEach-Object { "        listen       $_ ssl default_server;" }) -join "`n"
    $block = ""
    if ($https) {
        $block = (Get-Content -Raw (Join-Path $here "nginx-https.conf.template")).Replace("`r`n", "`n")
        $block = $block.Replace("{{HTTPS_LISTEN_DEFAULT}}", $listenDefault).Replace("{{HTTPS_LISTEN}}", $listen)
        $block = $block.Replace("{{SITE_HOSTS}}", $siteHostNames).Replace("{{CONSOLE_HOSTS}}", $consoleHostNames)
        $block = $block.Replace("{{API_HOSTS}}", $apiHostNames).Replace("{{CONSOLE_HOSTNAME}}", $ConsoleHostName)
        $block = $block.Replace("{{AUTH_HOSTNAME}}", $AuthHostName).Replace("{{AUTH_BACKEND}}", $AuthBackend.TrimEnd("/"))
        $block = $block.Replace("{{CERT}}", (Slash $cert)).Replace("{{KEY}}", (Slash $key))
        $block = $block.Replace("{{BACKEND}}", $Backend.TrimEnd("/")).Replace("{{SITE_BACKEND}}", $SiteBackend.TrimEnd("/")).Replace("{{CONSOLE_BACKEND}}", $ConsoleBackend.TrimEnd("/"))
    }
    $text = (Get-Content -Raw (Join-Path $here "nginx.conf.template")).Replace("`r`n", "`n")
    $text = $text.Replace("{{HTTPS_BLOCK}}", $block.TrimEnd()).Replace("{{HTTP_PORT}}", "$HttpPort").Replace("{{ACME_ROOT}}", (Slash $acmeDir))
    $redirects = ($HostNames | ForEach-Object { "        $_    $_;" }) -join "`n"
    $text = $text.Replace("{{HOST}}", $HostName).Replace("{{HOST_REDIRECTS}}", $redirects)
    $text = $text.Replace("{{BACKEND}}", $Backend.TrimEnd("/")).Replace("{{HTTPS_LISTEN}}", ($HttpsPorts -join ", "))
    [IO.File]::WriteAllText((Join-Path $nginxDir "conf\nginx.conf"), $text)
}

function Start-Nginx {
    $prefix = "$(Slash $nginxDir)/"
    $check = Invoke-Native $nginx @("-p", $prefix, "-t")
    if ($check.Code -ne 0) { throw "nginx's configuration doesn't load:`n$($check.Text)" }
    $running = Get-Process nginx -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $nginx }
    if ($running) {
        $reloaded = Invoke-Native $nginx @("-p", $prefix, "-s", "reload")
        if ($reloaded.Code -ne 0) { throw "nginx didn't reload:`n$($reloaded.Text)" }
        Write-Output "nginx reloaded"
    } else {
        Start-Process -FilePath $nginx -ArgumentList "-p", $prefix -WorkingDirectory $nginxDir -WindowStyle Hidden
        Start-Sleep -Seconds 2
        if (-not (Get-Process nginx -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $nginx })) {
            throw "nginx didn't start; see $(Join-Path $nginxDir 'logs\error.log')"
        }
        Write-Output "nginx started"
    }
}

if (-not $NoTasks) {
    $ports = @($HttpPort) + $HttpsPorts
    Get-NetFirewallRule -DisplayName "agent-proxy-https" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName "agent-proxy-https" -Direction Inbound -Protocol TCP -LocalPort $ports -Action Allow -Program $nginx | Out-Null
    Write-Output "Firewall: TCP $($ports -join ', ') open to nginx"
}

# First the plain HTTP side, so Let's Encrypt can check the name; HTTPS once there's a certificate.
$haveCert = (Test-Path $cert) -and (Test-Path $key)
Write-Config $haveCert
Start-Nginx

if ($CertFile) {
    Copy-Item -Force $CertFile $cert
    Copy-Item -Force $KeyFile $key
} else {
    # Let's Encrypt fetches http://<name>/.well-known/acme-challenge/<token>: the router's
    # port 80 has to reach nginx on $HttpPort. A quick look first, which only works where
    # the router lets this network reach its own public address.
    $probe = Join-Path $acmeDir ".well-known\acme-challenge\agent-proxy-check"
    New-Item -ItemType Directory -Force (Split-Path $probe) | Out-Null
    # Not Set-Content -NoNewline <path> <value>: Windows PowerShell 5.1 binds those the other way round.
    [IO.File]::WriteAllText($probe, "ok")
    try {
        $seen = (Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 -Uri "http://$HostName/.well-known/acme-challenge/agent-proxy-check").Content
        if ($seen -eq "ok") { Write-Output "http://$HostName reaches nginx: good." }
    } catch {
        Write-Output "Couldn't reach http://$HostName from here ($($_.Exception.Message)). That's normal when the router doesn't loop back; Let's Encrypt checks from outside."
    }
    Remove-Item -Force -ErrorAction SilentlyContinue $probe

    $arguments = @(
        "--source", "manual", "--host", $wacsHostNames,
        "--validation", "filesystem", "--webroot", $acmeDir,
        "--store", "pemfiles", "--pemfilespath", $certDir, "--pemfilesname", $HostName,
        "--installation", "script", "--script", $reload,
        "--accepttos"
    )
    if ($Email) { $arguments += @("--emailaddress", $Email) }
    if ($Staging) { $arguments += @("--baseuri", "https://acme-staging-v02.api.letsencrypt.org/") }
    Write-Output "Asking Let's Encrypt for a certificate for $HostName"
    & $wacs @arguments
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $cert)) {
        throw "win-acme didn't get a certificate (its log is in $env:ProgramData\win-acme). Check that port 80 on $HostName reaches this computer's $HttpPort."
    }
}

Write-Config $true
Start-Nginx

if (-not $NoTasks) {
    # nginx at every boot; win-acme added its own daily renewal task with the certificate.
    $action = New-ScheduledTaskAction -Execute $nginx -Argument "-p `"$(Slash $nginxDir)/`"" -WorkingDirectory $nginxDir
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
        -Description "nginx: HTTPS in front of agent-server (scripts/https/setup-https.ps1)" -Force | Out-Null
    Write-Output "Scheduled task ${task}: starts nginx at boot."
    if (-not $CertFile) {
        $renewal = Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -like "win-acme*" }
        if ($renewal) { Write-Output "Renewal: win-acme's task $($renewal.TaskName -join ', ')" } else { Write-Output "Warning: win-acme's renewal task isn't there; run $wacs --setuptaskscheduler" }
    }
}

Write-Output ""
Write-Output "HTTPS is up: https://$HostName and https://www.$HostName serve the website; https://$ConsoleHostName serves the console; https://$ApiHostName serves the API."
Write-Output "The router's 443 goes to $($HttpsPorts[0]); nginx listens on $($HttpsPorts -join ', ') here."
Write-Output "Set proxy.public_url to https://$ConsoleHostName, AI_PROXY_SERVE_UI=0 and AI_PROXY_CONSOLE_ORIGINS=https://$ConsoleHostName on agent-server."
Write-Output "Build the standalone console with VITE_API_URL=https://$ApiHostName and serve it at $ConsoleBackend."
