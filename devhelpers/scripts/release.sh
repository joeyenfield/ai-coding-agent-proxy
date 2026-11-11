#!/bin/sh
# Build locally. Pass website, services or installers, followed by --help for options.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "${PYTHON:-python3}" "$script_dir/release/local.py" "$@"
