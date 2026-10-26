#!/bin/sh
# agent-server's container start: fill an empty config volume with the image's defaults
# (never overwriting what's there), then run the server.
set -eu
for file in /app/defaults/config/*.yaml; do
  target="/app/config/$(basename "$file")"
  if [ ! -e "$target" ]; then
    cp "$file" "$target"
    echo "agent-server: created $target from the image's defaults" >&2
  fi
done
exec agent-server "$@"
