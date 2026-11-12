#!/bin/sh
# Installs AI Control Mesh {{VERSION}} on this computer, for you alone, and starts it: the server
# and its sign-in service on localhost, then agent and agent-machine signed in to it
# (docs/local.md). Linux, macOS and FreeBSD:
#
#   curl -fsSL {{BASE}}/install-local.sh | sh
#   curl -fsSL {{BASE}}/install-local.sh | sh -s -- --port 29999 --auth-port 30000
#
#   --port N, --auth-port N   the console's and agent-auth's ports (default 19999 and 20000; kept)
#   --prefix DIR              where agent-server, agent and agent-machine go (default
#                             ~/.local/bin, or /usr/local/bin as root)
#   --no-client               only the server: no agent or agent-machine
#   --no-login                don't sign agent and agent-machine in; print the commands instead
#   --python                  the Python agent and agent-machine rather than the Go builds (used
#                             anyway when there's no Go build for this system)
#   --no-tray                 no tray icon (agent-tray, put in on a desktop: it shows what runs,
#                             starts and stops it, opens the console, and starts at sign-in)
#
# Needs Python 3.11 or later (with venv) and curl or wget. Run it again to update. Every
# download is checked against the SHA-256 written into this script when the release was made.
# devhelpers/release_scripts/release/release.py fills it in.
set -eu

BASE='{{BASE}}'
VERSION='{{VERSION}}'
# The server bundle: name sha256.
BUNDLE='{{BUNDLE}}'
# platform component sha256 name, one archive per line.
FILES='{{FILES}}'
# The Python agent and agent-machine, for any system: name sha256.
WHEEL='{{WHEEL}}'

# usage: the options, from text in the script itself. Piped into sh there's no file to read them from.
usage() {
    cat <<'USAGE'
Installs AI Control Mesh on this computer, for you alone, and starts it (docs/local.md).

  --port N, --auth-port N   the console's and agent-auth's ports (default 19999 and 20000; kept)
  --prefix DIR              where agent-server, agent and agent-machine go (default
                            ~/.local/bin, or /usr/local/bin as root)
  --no-client               only the server: no agent or agent-machine
  --no-login                don't sign agent and agent-machine in; print the commands instead
  --python                  the Python agent and agent-machine rather than the Go builds (used
                            anyway when there's no Go build for this system)
  --no-tray                 no tray icon (agent-tray, put in on a desktop: it shows what runs,
                            starts and stops it, opens the console, and starts at sign-in)

Needs Python 3.11 or later (with venv) and curl or wget. Run it again to update.
Pass options through the pipe with: curl -fsSL <url>/install-local.sh | sh -s -- --port 29999
USAGE
}

ports=""
prefix=""
client=1
login=1
use_python=0
tray=1
while [ $# -gt 0 ]; do
    case "$1" in
        --python) use_python=1 ;;
        --no-tray) tray=0 ;;
        --port|--auth-port) [ $# -ge 2 ] || { echo "$1 needs a number" >&2; exit 2; }; ports="$ports $1 $2"; shift ;;
        --port=*|--auth-port=*) ports="$ports $1" ;;
        --prefix) [ $# -ge 2 ] || { echo "--prefix needs a folder" >&2; exit 2; }; prefix=$2; shift ;;
        --prefix=*) prefix=${1#--prefix=} ;;
        --no-client) client=0 ;;
        --no-login) login=0 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install-local: unknown option $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done

say() { printf '%s\n' "$*"; }
fail() { printf 'install-local: %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
    Linux) os=linux ;;
    Darwin) os=darwin ;;
    FreeBSD) os=freebsd ;;
    MINGW*|MSYS*|CYGWIN*) fail "this is Windows: in PowerShell, run  irm $BASE/install-local.ps1 | iex" ;;
    # No Go build for it: the Python agent and agent-machine, below.
    *) os=$(uname -s | tr '[:upper:]' '[:lower:]') ;;
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
    *) arch=$(uname -m) ;;
esac
# Apple silicon under Rosetta reports x86_64.
if [ "$os" = darwin ] && [ "$arch" = amd64 ] && [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" = 1 ]; then arch=arm64; fi
platform=${AGENT_PLATFORM:-$os-$arch}

python=""
for candidate in python3 python3.13 python3.12 python3.11 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        python=$(command -v "$candidate")
        break
    fi
done
[ -n "$python" ] || fail "needs Python 3.11 or later (python3); install it and run this again"

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
    fail "needs sha256sum, shasum or sha256 to check the downloads"
fi
# download <name> <sha256> <file>: from the release, refused unless it matches.
download() {
    fetch "$BASE/$1" "$3" || fail "couldn't download $BASE/$1"
    [ "$(digest "$3")" = "$2" ] || fail "$1 doesn't match its SHA-256; not installing it"
}

# The same folder agent-server local uses (server/agent_server/local.py, instance_home).
if [ -n "${AI_PROXY_LOCAL_HOME:-}" ]; then
    home=$AI_PROXY_LOCAL_HOME
elif [ "$os" = darwin ]; then
    home="$HOME/Library/Application Support/AI Control Mesh"
else
    home="${XDG_DATA_HOME:-$HOME/.local/share}/ai-control-mesh"
fi
if [ -z "$prefix" ]; then
    if [ "$(id -u)" -eq 0 ]; then prefix=/usr/local/bin; else prefix="$HOME/.local/bin"; fi
fi
venv="$home/venv"
app="$home/app/$VERSION"

work=$(mktemp -d 2>/dev/null || mktemp -d -t agent-local)
trap 'rm -rf "$work"' EXIT INT TERM

bundle=${BUNDLE%% *}
say "Downloading AI Control Mesh $VERSION"
download "$bundle" "${BUNDLE#* }" "$work/$bundle"

# A running instance is stopped first, so its files can be replaced; its data stays.
if [ -x "$venv/bin/agent-server" ]; then "$venv/bin/agent-server" local stop >/dev/null 2>&1 || true; fi
mkdir -p "$home/app"
rm -rf "$app.new"
"$python" -m zipfile -e "$work/$bundle" "$app.new" || fail "couldn't unpack $bundle"
rm -rf "$app"
mv "$app.new" "$app"

if ! "$venv/bin/python" -m pip --version >/dev/null 2>&1; then
    say "Making a Python environment in $venv"
    rm -rf "$venv"
    "$python" -m venv "$venv" >/dev/null 2>&1 && "$venv/bin/python" -m pip --version >/dev/null 2>&1 || {
        rm -rf "$venv"
        fail "couldn't make a Python environment with pip (on Debian and Ubuntu: sudo apt install python3-venv)"
    }
fi
say "Installing agent-server (a minute or two the first time)"
"$venv/bin/python" -m pip install --quiet --disable-pip-version-check -e "$app/server" || fail "pip couldn't install agent-server"
# Earlier versions' folders: the environment now points at this one.
for old in "$home/app"/*; do
    if [ -d "$old" ] && [ "$old" != "$app" ]; then rm -rf "$old"; fi
done
mkdir -p "$prefix"
ln -sf "$venv/bin/agent-server" "$prefix/agent-server"

# shellcheck disable=SC2086
"$venv/bin/agent-server" local start $ports || fail "the local server didn't start; see the logs in $home/logs"
port=$("$venv/bin/python" -c 'import json, sys; print(json.load(open(sys.argv[1])).get("port") or 19999)' "$home/local.json")
server="http://localhost:$port"

# lookup <component>: "sha256 name" of its archive for this system, or nothing.
lookup() {
    printf '%s\n' "$FILES" | while read -r p c s n; do
        if [ "$p" = "$platform" ] && [ "$c" = "$1" ]; then printf '%s %s\n' "$s" "$n"; fi
    done | head -n 1
}
if [ $client -eq 1 ] && [ $use_python -eq 0 ] && { [ -z "$(lookup agent)" ] || [ -z "$(lookup agent-machine)" ]; }; then
    say "There's no Go build for $platform in this release: installing the Python agent and agent-machine instead."
    use_python=1
fi

# The tray icon goes in where there's a desktop to show it: macOS, or a display on Linux and BSD.
if [ $tray -eq 1 ] && [ "$os" != darwin ] && [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then tray=0; fi
# The wheel: the Python agent and agent-machine, and agent-tray (its tray extra).
wheel=${WHEEL%% *}
fetched=0
fetch_wheel() {
    if [ $fetched -eq 0 ]; then
        download "$wheel" "${WHEEL#* }" "$work/$wheel"
        fetched=1
    fi
}
extra=""
if [ $tray -eq 1 ]; then extra="[tray]"; fi

installed=""
if [ $client -eq 1 ] && [ $use_python -eq 1 ]; then
    # Into the server's environment (Python 3.11 or later already), linked beside agent-server.
    say "Downloading the Python agent and agent-machine $VERSION"
    fetch_wheel
    "$venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade "$work/$wheel$extra" || fail "pip couldn't install $wheel"
    for program in agent agent-proxy agent-machine; do
        ln -sf "$venv/bin/$program" "$prefix/$program"
        say "Installed $prefix/$program (Python)"
    done
    installed="agent agent-machine"
elif [ $client -eq 1 ]; then
    for component in agent agent-machine; do
        line=$(lookup "$component")
        if [ -z "$line" ]; then
            say "There's no $component for $platform in this release."
            continue
        fi
        name=${line#* }
        say "Downloading $component $VERSION for $platform"
        download "$name" "${line%% *}" "$work/$name"
        (cd "$work" && tar xzf "$name") || fail "couldn't unpack $name"
        installed="$installed $component"
    done
    if [ -n "$installed" ]; then
        # The archives' own installer puts the programs in the prefix; signing in is done below.
        AI_PROXY_ONE_LINER=1 sh "$work/$platform/install.sh" --prefix "$prefix"
    fi
fi
if [ $tray -eq 1 ] && [ $use_python -eq 0 ]; then
    # Only agent-tray is linked: the Go agent and agent-machine above stay the ones you run.
    say "Installing the tray icon"
    fetch_wheel
    "$venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade "$work/$wheel[tray]" || {
        say "The tray icon couldn't be installed (pip install $wheel[tray] failed); everything else works without it."
        tray=0
    }
fi
if [ $tray -eq 1 ]; then
    ln -sf "$venv/bin/agent-tray" "$prefix/agent-tray"
    "$venv/bin/agent-tray" --autostart on >/dev/null 2>&1 || true
fi

# Sign-ins show a code to approve in the browser and wait: from the terminal, as this script's
# own input is the download.
if [ $login -eq 1 ] && [ -n "$installed" ] && ! (: </dev/tty) 2>/dev/null; then
    say "No terminal to sign in from; run the commands below yourself."
    login=0
fi
for component in $installed; do
    if [ "$component" = agent ]; then
        if [ $login -eq 1 ]; then
            say ""
            say "Signing agent in to $server (sign in as admin in the browser)"
            "$prefix/agent" login "$server" </dev/tty || say "Sign in later with: agent login $server"
        else
            say "Next: agent login $server"
        fi
    elif [ $login -eq 1 ]; then
        say ""
        say "Signing agent-machine in to $server"
        if "$prefix/agent-machine" login "$server" --enable launches,files </dev/tty; then
            "$prefix/agent-machine" start || true
        else
            say "Sign in later with: agent-machine login $server --enable launches,files, then agent-machine start"
        fi
    else
        say "Next: agent-machine login $server --enable launches,files, then agent-machine start"
    fi
done

say ""
say "Done: AI Control Mesh $VERSION runs at $server"
# Said again here: the first start printed it before the sign-ins scrolled it away.
if [ -f "$home/private/first-user-password" ]; then
    say "Open $server and sign in as admin. The password is in $home/private/first-user-password"
else
    say "Open $server and sign in with your account."
fi
case "$installed" in *agent-machine*) say "Let the console start agents in a folder of yours: agent-machine roots add <folder>" ;; esac
# The archives' installer has said so already when it ran.
case ":$PATH:" in
    *":$prefix:"*) ;;
    *) [ -n "$installed" ] && [ $use_python -eq 0 ] || say "$prefix isn't on your PATH. Add it, for example in ~/.profile: export PATH=\"$prefix:\$PATH\"" ;;
esac
say "Stop it with agent-server local stop; start it again with agent-server local."
say "Remove it with: curl -fsSL $BASE/uninstall-local.sh | sh"
if [ $tray -eq 1 ]; then
    # In the background, apart from this shell, so it stays when the terminal closes.
    if command -v setsid >/dev/null 2>&1; then
        setsid "$venv/bin/agent-tray" </dev/null >/dev/null 2>&1 &
    else
        nohup "$venv/bin/agent-tray" </dev/null >/dev/null 2>&1 &
    fi
    say "Its icon is in the system tray (agent-tray), and starts when you sign in (agent-tray --autostart off to stop that)."
fi
