#!/usr/bin/env sh
# Build agent-machine into machine/bin for this machine:
#   scripts/build-machine.sh
# For every platform (with the client too), into dist/<version>/<os>-<arch>: scripts/build-dist.sh
set -eu
cd "$(dirname "$0")/../machine"
# The checkout's version, as the server and tools/dist work it out (VERSION, plus the commit).
version="${VERSION:-$(cd ../tools && GOTOOLCHAIN="${GOTOOLCHAIN:-local}" go run ./dist -print-version)}"
export GOTOOLCHAIN="${GOTOOLCHAIN:-local}"
ext=""
[ "$(go env GOOS)" = windows ] && ext=.exe
go build -trimpath -ldflags "-s -w -X main.version=$version" -o "bin/agent-machine$ext" ./cmd/agent-machine
echo "machine/bin/agent-machine$ext"
