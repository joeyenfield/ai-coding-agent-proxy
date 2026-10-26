#!/bin/sh
# Installs agent (with agent-proxy) and agent-machine from the agent-server at {{SERVER}},
# for this computer's system, then signs them in to it. Linux, macOS and FreeBSD:
#
#   curl -fsSL {{SERVER}}/install.sh | sh
#   curl -fsSL {{SERVER}}/install.sh | sh -s -- --only agent-machine --service
#
#   --only agent|agent-machine   just one of them (default: both)
#   --service                    start agent-machine at boot or login (root: a system service)
#   --prefix DIR                 install into DIR (default: /usr/local/bin as root, else ~/.local/bin)
#   --no-login                   don't sign in; print the commands instead
#   --no-start                   don't start agent-machine after signing it in
#
# The agent-server writes this script for its own build ({{VERSION}}), so it always
# installs the programs that speak to it, checked against their SHA-256. It downloads the
# archives for this system, then runs the installer inside them (install.sh).
set -eu

SERVER='{{SERVER}}'
VERSION='{{VERSION}}'
# platform component sha256 path, one archive per line.
FILES='{{FILES}}'

only=""
pass=""
login=1
start=1
service=0
while [ $# -gt 0 ]; do
    case "$1" in
        --only) [ $# -ge 2 ] || { echo "--only needs agent or agent-machine" >&2; exit 2; }; only=$2; shift ;;
        --only=*) only=${1#--only=} ;;
        --service) service=1; pass="$pass --service" ;;
        --prefix) [ $# -ge 2 ] || { echo "--prefix needs a folder" >&2; exit 2; }; pass="$pass --prefix $2"; prefix=$2; shift ;;
        --prefix=*) pass="$pass --prefix ${1#--prefix=}"; prefix=${1#--prefix=} ;;
        --no-login) login=0 ;;
        --no-start) start=0 ;;
        -h|--help) sed -n '2,13p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//' || true; exit 0 ;;
        *) echo "install: unknown option $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done
case "$only" in ""|agent|agent-machine) ;; *) echo "install: --only takes agent or agent-machine" >&2; exit 2 ;; esac

say() { printf '%s\n' "$*"; }
fail() { printf 'install: %s\n' "$*" >&2; exit 1; }

# This system, named the way tools/dist names its builds (AGENT_PLATFORM overrides it).
platform=${AGENT_PLATFORM:-}
if [ -z "$platform" ]; then
    case "$(uname -s)" in
        Linux) os=linux ;;
        Darwin) os=darwin ;;
        FreeBSD) os=freebsd ;;
        MINGW*|MSYS*|CYGWIN*) fail "this is Windows: in PowerShell, run  irm $SERVER/install.ps1 | iex" ;;
        *) fail "no build for $(uname -s); see $SERVER/get" ;;
    esac
    case "$(uname -m)" in
        x86_64|amd64) arch=amd64 ;;
        aarch64|arm64) arch=arm64 ;;
        armv7*|armv8l|armhf) arch=armv7 ;;
        i386|i486|i586|i686) arch=386 ;;
        ppc64le) arch=ppc64le ;;
        riscv64) arch=riscv64 ;;
        s390x) arch=s390x ;;
        loongarch64) arch=loong64 ;;
        *) fail "no build for $(uname -m) processors; see $SERVER/get" ;;
    esac
    # Apple silicon under Rosetta reports x86_64.
    if [ "$os" = darwin ] && [ "$arch" = amd64 ] && [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" = 1 ]; then arch=arm64; fi
    platform=$os-$arch
fi

if [ -z "$FILES" ]; then
    fail "the agent-server at $SERVER has no builds to hand out yet (scripts/build-dist.sh on its computer)"
fi

if command -v curl >/dev/null 2>&1; then
    fetch() { curl -fsSL -o "$2" "$1"; }
elif command -v wget >/dev/null 2>&1; then
    fetch() { wget -q -O "$2" "$1"; }
else
    fail "needs curl or wget"
fi
if command -v sha256sum >/dev/null 2>&1; then
    digest() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then
    digest() { shasum -a 256 "$1" | cut -d' ' -f1; }
elif command -v sha256 >/dev/null 2>&1; then
    digest() { sha256 -q "$1"; }
else
    fail "needs sha256sum, shasum or sha256 to check the download"
fi

work=$(mktemp -d 2>/dev/null || mktemp -d -t agent-install)
trap 'rm -rf "$work"' EXIT INT TERM

found=0
components=""
for component in agent agent-machine; do
    if [ -n "$only" ] && [ "$only" != "$component" ]; then continue; fi
    line=$(printf '%s\n' "$FILES" | while read -r p c s u; do
        if [ "$p" = "$platform" ] && [ "$c" = "$component" ]; then printf '%s %s\n' "$s" "$u"; fi
    done)
    if [ -z "$line" ]; then
        say "No $component for $platform in this server's build; see $SERVER/get"
        continue
    fi
    sum=${line%% *}
    path=${line#* }
    archive="$work/${path##*/}"
    say "Downloading $component $VERSION for $platform"
    fetch "$SERVER$path" "$archive" || fail "couldn't download $SERVER$path"
    [ "$(digest "$archive")" = "$sum" ] || fail "$component's download doesn't match its SHA-256; not installing it"
    (cd "$work" && tar xzf "$archive") || fail "couldn't unpack $archive"
    found=1
    components="$components $component"
done
[ $found -eq 1 ] || fail "nothing to install for $platform"

# The archives' own installer puts every program beside it on the PATH (and sets up the service).
# shellcheck disable=SC2086
AI_PROXY_ONE_LINER=1 sh "$work/$platform/install.sh" $pass

# Find what was installed, even before a new shell has the folder on its PATH.
bin=${prefix:-}
if [ -z "$bin" ]; then
    if [ "$(id -u)" -eq 0 ]; then bin=/usr/local/bin; else bin="$HOME/.local/bin"; fi
fi
program() { if [ -x "$bin/$1" ]; then printf '%s\n' "$bin/$1"; else command -v "$1" 2>/dev/null || printf '%s\n' "$1"; fi; }

# Sign-ins ask for a user and password when the server has accounts: from the terminal,
# as this script's own input is the download.
if [ $login -eq 1 ] && ! (: </dev/tty) 2>/dev/null; then
    say "No terminal to sign in from; run the commands below yourself."
    login=0
fi
for component in $components; do
    if [ "$component" = agent ]; then
        if [ $login -eq 1 ]; then
            say ""
            say "Signing agent in to $SERVER"
            "$(program agent)" login "$SERVER" </dev/tty || say "Sign in later with: agent login $SERVER"
        else
            say "Next: agent login $SERVER"
        fi
    else
        if [ $login -eq 1 ]; then
            say ""
            say "Signing agent-machine in to $SERVER"
            if "$(program agent-machine)" login "$SERVER" </dev/tty; then
                # A service retries by itself; otherwise join the fleet now, in the background.
                if [ $service -eq 0 ] && [ $start -eq 1 ]; then "$(program agent-machine)" start || true; fi
            else
                say "Sign in later with: agent-machine login $SERVER, then agent-machine start"
            fi
        else
            say "Next: agent-machine login $SERVER, then agent-machine start"
        fi
    fi
done
say ""
say "Done: $VERSION from $SERVER. Open its console at $SERVER"
