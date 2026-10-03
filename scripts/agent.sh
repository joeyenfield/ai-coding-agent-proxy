#!/usr/bin/env sh
# Launch a coding agent through the proxy: scripts/agent.sh qwen . --model qwen3.6:35b
set -eu
exec python3 -m agent_proxy.launcher "$@"
