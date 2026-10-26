#!/usr/bin/env sh
# Launch a coding agent with the Go client, built first from this checkout:
#   scripts/agent.sh qwen . --model qwen3.6:35b
set -eu
root="$(cd "$(dirname "$0")/.." && pwd)"
"$root/scripts/build-client.sh" >/dev/null
ext=""
[ "$(cd "$root/client" && go env GOOS)" = windows ] && ext=.exe
exec "$root/client/bin/agent$ext" "$@"
