#!/usr/bin/env sh
# Get Podman ready to build ARM64 images on an x64 computer: start the Podman machine if it is
# stopped and register its ARM64 emulator (QEMU). Run it before
#   devhelpers/release_scripts/release.sh services --platform linux/arm64
# The registration is lost when the Podman machine restarts, so this is safe to run every time.
# On Windows the machine's own service can't register it: /proc/sys/fs/binfmt_misc is unreadable
# under WSL, so this mounts the registry at /run/binfmt and registers there.
set -eu

fail() { echo "$*" >&2; exit 1; }
command -v podman >/dev/null 2>&1 || fail "Podman isn't installed."

register='
set -e
[ -f /usr/lib/binfmt.d/qemu-aarch64-static.conf ] || { echo "QEMU is missing: install qemu-user-static" >&2; exit 3; }
mkdir -p /run/binfmt
mountpoint -q /run/binfmt || mount -t binfmt_misc binfmt_misc /run/binfmt
[ -e /run/binfmt/qemu-aarch64 ] || cat /usr/lib/binfmt.d/qemu-aarch64-static.conf > /run/binfmt/register
'

case "$(uname -s)" in
    Linux)
        # Podman runs here directly: there is no machine to start.
        if [ ! -e /proc/sys/fs/binfmt_misc/qemu-aarch64 ]; then
            echo "==> Registering the ARM64 emulator (sudo)"
            sudo sh -c "$register" || fail "Couldn't register QEMU. Install qemu-user-static and run this again."
        fi
        ;;
    *)
        state=$(podman machine inspect --format '{{.State}}' 2>/dev/null || true)
        [ -n "$state" ] || fail "There is no Podman machine: make one with podman machine init."
        if [ "$state" != running ]; then
            echo "==> Starting the Podman machine"
            podman machine start
        fi
        echo "==> Registering the ARM64 emulator in the Podman machine"
        # Git Bash would turn /run/... into a Windows path, and leaves a file named NUL behind.
        MSYS_NO_PATHCONV=1 podman machine ssh "sudo sh -c '$register'" </dev/null \
            || fail "Couldn't register QEMU. In the machine: sudo dnf install qemu-user-static"
        rm -f NUL
        ;;
esac
echo "Podman can build linux/arm64 images."
