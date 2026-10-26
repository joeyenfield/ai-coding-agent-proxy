#!/usr/bin/env sh
# Build agent (agent, agent-proxy), agent-machine and the Android app for every standard platform:
#   scripts/build-dist.sh                            every target (tools/dist lists them)
#   scripts/build-dist.sh linux/arm64 windows/amd64  only these
#   ONLY=agent-machine scripts/build-dist.sh         only one component
#   PACKAGES=1 scripts/build-dist.sh                 native packages too: .deb, .rpm, .apk,
#                                                    Arch; .msi with wix on PATH; .pkg on a Mac
#   ANDROID=0 scripts/build-dist.sh                  without the Android app (built when a
#                                                    JDK and the Android SDK are here)
# Everything goes into dist/<version>/ (only that version's folder is replaced): per target
# dist/<version>/<os>-<arch>/ has an archive per component (.zip for Windows, .tar.gz
# elsewhere, each with its installer: install.sh or install.ps1) and the packages, and
# artifact/ there has the binaries; dist/<version>/android/ the app; dist/<version>/SHA256SUMS
# covers every file. The server hands out the folder of its own version.
# The version is the checkout's: VERSION at the root, plus the commit unless HEAD is its tag
# (tools/dist/version.go). VERSION=... in the environment names another.
set -eu
cd "$(dirname "$0")/../tools"
export GOTOOLCHAIN="${GOTOOLCHAIN:-local}"
packages=false
case "${PACKAGES:-}" in 1|true|yes) packages=true ;; esac
android=true
case "${ANDROID:-}" in 0|false|no) android=false ;; esac
go run ./dist "-version=${VERSION:-}" "-only=${ONLY:-}" "-packages=$packages" "-android=$android" "$@"
