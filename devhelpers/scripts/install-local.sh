#!/usr/bin/env sh
# Install and start a local instance from this checkout: the server and agent-auth on this
# computer, for you alone (docs/local.md). Needs Python 3.11+; npm and Go are used when present.
#   devhelpers/scripts/install-local.sh [--port 19999] [--auth-port 20000]
set -eu
root="$(cd "$(dirname "$0")/../.." && pwd)"
python="$(command -v python3 || command -v python)"

echo "Installing agent-server..."
"$python" -m pip install --quiet -e "$root/server"

if command -v npm >/dev/null 2>&1; then
  echo "Building the console..."
  (cd "$root/ui" && npm install --no-audit --no-fund --silent && npm run build --silent)
elif [ ! -f "$root/ui/dist/index.html" ]; then
  echo "npm isn't installed and there's no ui/dist: the API works, the console won't until it's built." >&2
fi

# agent and agent-machine: the Go programs when Go is here, else the Python ones (the same commands).
if command -v go >/dev/null 2>&1; then
  echo "Building agent and agent-machine..."
  "$root/devhelpers/scripts/build-client.sh" >/dev/null
  "$root/devhelpers/scripts/build-machine.sh" >/dev/null
  case ":$PATH:" in
    *":$root/client/bin:"*) ;;
    *) echo "Add them to your PATH: export PATH=\"$root/client/bin:$root/machine/bin:\$PATH\"" ;;
  esac
elif ! command -v agent >/dev/null 2>&1; then
  echo "Installing agent and agent-machine (Python)..."
  "$python" -m pip install --quiet "$root/python"
fi

exec agent-server local start "$@"
