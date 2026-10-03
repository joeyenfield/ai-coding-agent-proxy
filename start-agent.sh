#!/usr/bin/env sh
set -eu
exec python3 -m local_agent.launcher "$@"
