# Install and start a local instance from this checkout: the server and agent-auth on this
# computer, for you alone (docs\local.md). Needs Python 3.11+; npm and Go are used when present.
#   .\scripts\install-local.ps1 [-Port 19999] [-AuthPort 20000]
param([int]$Port, [int]$AuthPort)
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$python = if (Get-Command python -ErrorAction SilentlyContinue) { "python" } else { "py" }

Write-Output "Installing agent-server..."
& $python -m pip install --quiet -e (Join-Path $root "server")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if (Get-Command npm -ErrorAction SilentlyContinue) {
    Write-Output "Building the console..."
    Push-Location (Join-Path $root "ui")
    try {
        npm install --no-audit --no-fund --silent
        if ($LASTEXITCODE -eq 0) { npm run build --silent }
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    } finally { Pop-Location }
} elseif (-not (Test-Path (Join-Path $root "ui\dist\index.html"))) {
    Write-Warning "npm isn't installed and there's no ui\dist: the API works, the console won't until it's built."
}

# agent and agent-machine: the Go programs when Go is here, else the Python ones (the same commands).
if (Get-Command go -ErrorAction SilentlyContinue) {
    Write-Output "Building agent and agent-machine..."
    & (Join-Path $PSScriptRoot "build-client.ps1") | Out-Null
    & (Join-Path $PSScriptRoot "build-machine.ps1") | Out-Null
    $bins = "$root\client\bin;$root\machine\bin"
    if (-not ($env:Path -split ";" -contains "$root\client\bin")) {
        Write-Output "Add them to your PATH: `$env:Path = `"$bins;`$env:Path`" (or in Settings, for good)"
    }
} elseif (-not (Get-Command agent -ErrorAction SilentlyContinue)) {
    Write-Output "Installing agent and agent-machine (Python)..."
    & $python -m pip install --quiet (Join-Path $root "python")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$arguments = @("local", "start")
if ($Port) { $arguments += @("--port", $Port) }
if ($AuthPort) { $arguments += @("--auth-port", $AuthPort) }
agent-server @arguments
exit $LASTEXITCODE
