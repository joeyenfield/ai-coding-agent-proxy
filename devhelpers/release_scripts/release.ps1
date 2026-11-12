# Build locally. Pass website, services or installers, followed by --help for options.
$ErrorActionPreference = 'Stop'
& python (Join-Path $PSScriptRoot 'release/local.py') @args
if ($LASTEXITCODE -ne 0) { throw "Local release failed (exit $LASTEXITCODE)." }
