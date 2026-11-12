# Get Podman ready to build ARM64 images on an x64 computer: start the Podman machine if it is
# stopped and register its ARM64 emulator (QEMU). Run it before
#   .\devhelpers\release_scripts\release.ps1 services --platform linux/arm64
# The registration is lost when the Podman machine restarts, so this is safe to run every time.
# The machine's own service can't register it: /proc/sys/fs/binfmt_misc is unreadable under WSL,
# so this mounts the registry at /run/binfmt and registers there.
$ErrorActionPreference = 'Stop'

if (-not (Get-Command podman -ErrorAction SilentlyContinue)) { throw "Podman isn't installed." }

$state = podman machine inspect --format '{{.State}}' 2>$null
if (-not $state) { throw "There is no Podman machine: make one with podman machine init." }
if ($state -ne 'running') {
    Write-Host "==> Starting the Podman machine"
    podman machine start
    if ($LASTEXITCODE -ne 0) { throw "The Podman machine didn't start (exit $LASTEXITCODE)." }
}

Write-Host "==> Registering the ARM64 emulator in the Podman machine"
# One line with no double quotes: PowerShell 5.1 mangles them in arguments to native programs.
$register = 'test -f /usr/lib/binfmt.d/qemu-aarch64-static.conf || exit 3; ' +
    'mkdir -p /run/binfmt; ' +
    'mountpoint -q /run/binfmt || mount -t binfmt_misc binfmt_misc /run/binfmt; ' +
    'test -e /run/binfmt/qemu-aarch64 || cat /usr/lib/binfmt.d/qemu-aarch64-static.conf > /run/binfmt/register'
podman machine ssh "sudo sh -c '$register'"
if ($LASTEXITCODE -eq 3) { throw "QEMU is missing. In the machine: sudo dnf install qemu-user-static" }
if ($LASTEXITCODE -ne 0) { throw "Couldn't register QEMU (exit $LASTEXITCODE)." }
Write-Host "Podman can build linux/arm64 images."
