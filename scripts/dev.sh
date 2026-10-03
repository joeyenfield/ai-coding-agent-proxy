#!/usr/bin/env sh
# Run the API with auto-reload and the Vite dev server together. Ctrl+C stops both.
set -eu
cd "$(dirname "$0")/.."
agent-proxy --reload &
API=$!
trap 'kill $API' EXIT INT TERM
cd ui && npm run dev
