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
#   --python                     the Python versions (any system with Python 3.9 or later), in
#                                their own environment, linked into the prefix
#   --no-tray                    no tray icon (agent-tray, put in on a desktop with Python 3.9 or
#                                later: what runs here, its start and stop, at every sign-in)
#
# The agent-server writes this script for its own build ({{VERSION}}), so it always
# installs the programs that speak to it, checked against their SHA-256. It downloads the
# archives for this system, then runs the installer inside them (install.sh). With --python,
# or when there's no build for this system, it installs the Python wheel instead.
set -eu

SERVER='{{SERVER}}'
VERSION='{{VERSION}}'
# platform component sha256 path, one archive per line; "python python" is the wheel.
FILES='{{FILES}}'

# usage: the options, from text in the script itself. Piped into sh there's no file to read them from.
usage() {
    cat <<'USAGE'
Installs agent (with agent-proxy) and agent-machine from this agent-server, then signs them in.

  --only agent|agent-machine   just one of them (default: both)
  --service                    start agent-machine at boot or login (root: a system service)
  --prefix DIR                 install into DIR (default: /usr/local/bin as root, else ~/.local/bin)
  --no-login                   don't sign in; print the commands instead
  --no-start                   don't start agent-machine after signing it in
  --python                     the Python versions (any system with Python 3.9 or later), in
                               their own environment, linked into the prefix
  --no-tray                    no tray icon (agent-tray, put in on a desktop with Python 3.9 or
                               later: what runs here, its start and stop, at every sign-in)

Pass options through the pipe with: curl -fsSL <server>/install.sh | sh -s -- --only agent
USAGE
}

only=""
prefix=""
login=1
start=1
service=0
python=0
tray=1
while [ $# -gt 0 ]; do
    case "$1" in
        --python) python=1 ;;
        --no-tray) tray=0 ;;
        --only) [ $# -ge 2 ] || { echo "--only needs agent or agent-machine" >&2; exit 2; }; only=$2; shift ;;
        --only=*) only=${1#--only=} ;;
        --service) service=1 ;;
        --prefix) [ $# -ge 2 ] || { echo "--prefix needs a folder" >&2; exit 2; }; prefix=$2; shift ;;
        --prefix=*) prefix=${1#--prefix=} ;;
        --no-login) login=0 ;;
        --no-start) start=0 ;;
        -h|--help) usage; exit 0 ;;
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
        # No Go build for it: the Python versions, below.
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
    platform=$os-$arch
fi

if [ -z "$FILES" ]; then
    fail "the agent-server at $SERVER has no builds to hand out yet. On its computer, run: agent-server downloads fetch (or devhelpers/scripts/build-dist.sh in a checkout)"
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

# Where the programs go, and where the Python versions' environment is.
bin=$prefix
if [ "$(id -u)" -eq 0 ]; then
    [ -n "$bin" ] || bin=/usr/local/bin
    pyhome=/usr/local/lib/agent-py
else
    [ -n "$bin" ] || bin="$HOME/.local/bin"
    pyhome="${XDG_DATA_HOME:-$HOME/.local/share}/agent-py"
fi

# lookup <platform> <component>: "sha256 path" from FILES, or nothing.
lookup() {
    printf '%s\n' "$FILES" | while read -r p c s u; do
        if [ "$p" = "$1" ] && [ "$c" = "$2" ]; then printf '%s %s\n' "$s" "$u"; fi
    done | head -n 1
}
wanted=""
for component in agent agent-machine; do
    if [ -z "$only" ] || [ "$only" = "$component" ]; then wanted="$wanted $component"; fi
done
if [ $python -eq 0 ]; then
    for component in $wanted; do
        if [ -z "$(lookup "$platform" "$component")" ] && [ -n "$(lookup python python)" ]; then
            say "No $component build for $platform: installing the Python versions instead."
            python=1
        fi
    done
fi

# python_env: Python 3.9 or later, the wheel (downloaded once and checked), and an environment with
# pip in $pyhome. It says why and returns 1 when it can't: --python stops then, the tray is left out.
wheel=""
python_env() {
    line=$(lookup python python)
    if [ -z "$line" ]; then say "This server's build has no Python wheel; see $SERVER/get"; return 1; fi
    interpreter=""
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
            interpreter=$(command -v "$candidate")
            break
        fi
    done
    if [ -z "$interpreter" ]; then say "The Python agent, agent-machine and agent-tray need Python 3.9 or later (python3)."; return 1; fi
    sum=${line%% *}
    path=${line#* }
    if [ -z "$wheel" ]; then
        wheel="$work/${path##*/}"
        say "Downloading the Python agent and agent-machine $VERSION"
        fetch "$SERVER$path" "$wheel" || { wheel=""; say "Couldn't download $SERVER$path."; return 1; }
        if [ "$(digest "$wheel")" != "$sum" ]; then
            rm -f "$wheel"; wheel=""
            say "The wheel doesn't match its SHA-256; not installing it."
            return 1
        fi
    fi
    if ! "$pyhome/venv/bin/python" -m pip --version >/dev/null 2>&1; then
        rm -rf "$pyhome/venv"
        mkdir -p "$pyhome"
        "$interpreter" -m venv "$pyhome/venv" >/dev/null 2>&1 && "$pyhome/venv/bin/python" -m pip --version >/dev/null 2>&1 || {
            rm -rf "$pyhome/venv"
            say "Couldn't make a Python environment with pip in $pyhome (on Debian and Ubuntu: sudo apt install python3-venv)."
            return 1
        }
    fi
}

components=""
if [ $python -eq 1 ]; then
    python_env || fail "the Python versions can't be installed here"
    "$pyhome/venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade "$wheel" || fail "pip couldn't install $wheel"
    mkdir -p "$bin"
    for component in $wanted; do
        programs=$component
        if [ "$component" = agent ]; then programs="agent agent-proxy"; fi
        for program in $programs; do
            ln -sf "$pyhome/venv/bin/$program" "$bin/$program"
            say "Installed $bin/$program (Python, in $pyhome)"
        done
        components="$components $component"
    done
    if [ $service -eq 1 ]; then say "--service sets up the Go agent-machine only; start this one with: agent-machine start"; service=0; fi
    case ":$PATH:" in
        *":$bin:"*) ;;
        *) say "$bin isn't on your PATH. Add it, for example in ~/.profile: export PATH=\"$bin:\$PATH\"" ;;
    esac
else
    for component in $wanted; do
        line=$(lookup "$platform" "$component")
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
        components="$components $component"
    done
    [ -n "$components" ] || fail "nothing to install for $platform"

    # The archives' own installer puts every program beside it on the PATH (and sets up the service).
    # Its options go in "$@", so a prefix with spaces in it stays one argument.
    set --
    if [ $service -eq 1 ]; then set -- --service; fi
    if [ -n "$prefix" ]; then set -- "$@" --prefix "$prefix"; fi
    AI_PROXY_ONE_LINER=1 sh "$work/$platform/install.sh" "$@"
fi

# The tray icon (agent-tray), for you on a desktop (macOS, or a display on Linux and BSD), never as
# root. It comes from the wheel, in its own environment; only agent-tray is linked, so the Go
# programs stay the ones you run. Without it everything else works the same.
if [ $tray -eq 1 ] && [ "$(id -u)" -ne 0 ] && { [ "$(uname -s)" = Darwin ] || [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; }; then
    say "Installing the tray icon (agent-tray)"
    if python_env && "$pyhome/venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade "$wheel[tray]"; then
        ln -sf "$pyhome/venv/bin/agent-tray" "$bin/agent-tray"
        "$pyhome/venv/bin/agent-tray" --autostart on >/dev/null 2>&1 || true
    else
        say "The tray icon couldn't be installed; everything else works without it."
        tray=0
    fi
else
    tray=0
fi

# Find what was installed, even before a new shell has the folder on its PATH.
program() { if [ -x "$bin/$1" ]; then printf '%s\n' "$bin/$1"; else command -v "$1" 2>/dev/null || printf '%s\n' "$1"; fi; }

# Sign-ins show a code to approve and wait: from the terminal, as this script's own input is the
# download. A setup code from the console (AI_PROXY_MACHINE_SETUP_CODE for agent-machine,
# AI_PROXY_SETUP_CODE for agent) signs in with no one there, so then no terminal is needed.
signin() { if (: </dev/tty) 2>/dev/null; then "$@" </dev/tty; else "$@" </dev/null; fi; }
if [ $login -eq 1 ] && [ -z "${AI_PROXY_MACHINE_SETUP_CODE:-}${AI_PROXY_SETUP_CODE:-}" ] && ! (: </dev/tty) 2>/dev/null; then
    say "No terminal to sign in from; run the commands below yourself."
    login=0
fi
for component in $components; do
    if [ "$component" = agent ]; then
        if [ $login -eq 1 ]; then
            say ""
            say "Signing agent in to $SERVER"
            signin "$(program agent)" login "$SERVER" || say "Sign in later with: agent login $SERVER"
        else
            say "Next: agent login $SERVER"
        fi
    else
        if [ $login -eq 1 ]; then
            say ""
            say "Signing agent-machine in to $SERVER"
            if signin "$(program agent-machine)" login "$SERVER"; then
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
if [ $tray -eq 1 ]; then
    # In the background, apart from this shell, so it stays when the terminal closes.
    if command -v setsid >/dev/null 2>&1; then
        setsid "$pyhome/venv/bin/agent-tray" </dev/null >/dev/null 2>&1 &
    else
        nohup "$pyhome/venv/bin/agent-tray" </dev/null >/dev/null 2>&1 &
    fi
    say "Its icon is in the system tray (agent-tray), and starts when you sign in (agent-tray --autostart off to stop that)."
fi
