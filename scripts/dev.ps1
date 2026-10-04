# Run the API with auto-reload and the Vite dev server together. Ctrl+C stops both.
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$api = Start-Process agent-proxy -ArgumentList "--reload", "--no-build" -NoNewWindow -PassThru
try {
    Set-Location ui
    npm run dev
} finally {
    Stop-Process -Id $api.Id -ErrorAction SilentlyContinue
}
