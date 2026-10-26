#!/usr/bin/env sh
# Build the Go client (agent, agent-proxy) into client/bin for this machine:
#   scripts/build-client.sh
# or for every platform (with agent-machine), into dist/<version>/<os>-<arch>, to hand to other machines:
#   scripts/build-client.sh --all      (the same as scripts/build-dist.sh)
set -eu
if [ "${1:-}" = "--all" ]; then
  exec "$(dirname "$0")/build-dist.sh"
fi
cd "$(dirname "$0")/../client"
# The checkout's version, as the server and tools/dist work it out (VERSION, plus the commit).
version="${VERSION:-$(cd ../tools && GOTOOLCHAIN="${GOTOOLCHAIN:-local}" go run ./dist -print-version)}"
flags="-s -w -X main.version=$version"
export GOTOOLCHAIN="${GOTOOLCHAIN:-local}"
ext=""
[ "$(go env GOOS)" = windows ] && ext=.exe
for program in agent agent-proxy; do
  go build -trimpath -ldflags "$flags" -o "bin/$program$ext" "./cmd/$program"
done
echo "client/bin/agent$ext"
