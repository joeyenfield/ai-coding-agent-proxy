#!/usr/bin/env sh
# Install and start a local instance from this checkout: the server and agent-auth on this
# computer, for you alone (docs/local.md). Needs Python 3.11+; npm and Go are used when present.
#   devhelpers/scripts/install-local.sh [--port 19999] [--auth-port 20000]
set -eu
root="$(cd "$(dirname "$0")/../.." && pwd)"

python=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    python="$(command -v "$candidate")"
    break
  fi
done
if [ -z "$python" ]; then
  echo "install-local: needs Python 3.11 or later (python3); install it and run this again." >&2
  exit 1
fi
# A system Python that refuses pip installs (Debian, Ubuntu, Homebrew: PEP 668) gets an
# environment in the checkout instead. One you made and activated yourself is used as it is.
if "$python" -c 'import os, sys, sysconfig; sys.exit(not (sys.prefix == sys.base_prefix and os.path.exists(os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED"))))'; then
  venv="$root/.venv"
  if [ ! -x "$venv/bin/python" ]; then
    echo "This system's Python doesn't take pip installs: making an environment in $venv"
    "$python" -m venv "$venv" || {
      echo "install-local: couldn't make a Python environment (on Debian and Ubuntu: sudo apt install python3-venv)." >&2
      exit 1
    }
  fi
  python="$venv/bin/python"
fi

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

# Where pip put agent-server, which may not be on the PATH (an environment, or a --user install).
scripts="$("$python" -c 'import sysconfig; print(sysconfig.get_path("scripts"))')"
if [ -x "$scripts/agent-server" ]; then
  case ":$PATH:" in
    *":$scripts:"*) ;;
    *) echo "agent-server is in $scripts, which isn't on your PATH. Add it: export PATH=\"$scripts:\$PATH\"" ;;
  esac
  exec "$scripts/agent-server" local start "$@"
fi
exec agent-server local start "$@"
