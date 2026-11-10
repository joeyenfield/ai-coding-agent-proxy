#!/bin/sh
# Removes the AI Control Mesh that install-local.sh put on this computer (docs/local.md): the tray
# icon, agent and agent-machine, the local server and its programs. Linux, macOS and FreeBSD:
#
#   curl -fsSL <release>/uninstall-local.sh | sh
#   curl -fsSL <release>/uninstall-local.sh | sh -s -- --purge
#
#   --prefix DIR     where agent-server, agent and agent-machine are (default ~/.local/bin, or
#                    /usr/local/bin as root)
#   --keep-client    leave agent and agent-machine (and their sign-ins) as they are
#   --purge          also delete the server's data (its database, recordings and keys), and sign
#                    agent and agent-machine out and delete their settings
#
# Without --purge the server's data stays in its folder, so installing again finds it.
# devhelpers/scripts/release/release.py puts this file in every release as it is. The folders
# are the ones install-local.sh uses; keep them in step.
set -eu

# usage: the options, from text in the script itself. Piped into sh there's no file to read them from.
usage() {
    cat <<'USAGE'
Removes the AI Control Mesh that install-local.sh put on this computer.

  --prefix DIR     where agent-server, agent and agent-machine are (default ~/.local/bin, or
                   /usr/local/bin as root)
  --keep-client    leave agent and agent-machine (and their sign-ins) as they are
  --purge          also delete the server's data (its database, recordings and keys), and sign
                   agent and agent-machine out and delete their settings

Pass options through the pipe with: curl -fsSL <url>/uninstall-local.sh | sh -s -- --purge
USAGE
}

prefix=""
client=1
purge=0
while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) [ $# -ge 2 ] || { echo "--prefix needs a folder" >&2; exit 2; }; prefix=$2; shift ;;
        --prefix=*) prefix=${1#--prefix=} ;;
        --keep-client) client=0 ;;
        --purge) purge=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "uninstall-local: unknown option $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done

say() { printf '%s\n' "$*"; }

# The same folder agent-server local uses (server/agent_server/local.py, instance_home).
if [ -n "${AI_PROXY_LOCAL_HOME:-}" ]; then
    home=$AI_PROXY_LOCAL_HOME
elif [ "$(uname -s)" = Darwin ]; then
    home="$HOME/Library/Application Support/AI Control Mesh"
else
    home="${XDG_DATA_HOME:-$HOME/.local/share}/ai-control-mesh"
fi
if [ -z "$prefix" ]; then
    if [ "$(id -u)" -eq 0 ]; then prefix=/usr/local/bin; else prefix="$HOME/.local/bin"; fi
fi
venv="$home/venv"
if [ ! -d "$venv" ] && [ ! -f "$home/local.json" ]; then
    say "There's no local install in $home."
    exit 0
fi

# The tray icon first: it would start the server again.
if [ -x "$venv/bin/agent-tray" ]; then
    "$venv/bin/agent-tray" --autostart off >/dev/null 2>&1 || true
    if command -v pkill >/dev/null 2>&1; then pkill -f "$venv/bin/agent-tray" >/dev/null 2>&1 || true; fi
    say "Removed the tray icon."
fi

# agent and agent-machine, with the uninstaller the server's own files carry. Before the server
# stops: with --purge it signs them out there.
if [ $client -eq 1 ]; then
    uninstaller=""
    for candidate in "$home"/app/*/server/agent_server/install/uninstall.sh; do
        if [ -f "$candidate" ]; then uninstaller=$candidate; fi
    done
    if [ -n "$uninstaller" ]; then
        if [ $purge -eq 1 ]; then
            sh "$uninstaller" --prefix "$prefix" --purge || say "agent and agent-machine weren't fully removed; see above."
        else
            sh "$uninstaller" --prefix "$prefix" || say "agent and agent-machine weren't fully removed; see above."
        fi
    else
        say "agent and agent-machine are left in $prefix: this install has no uninstaller for them."
    fi
fi

if [ -x "$venv/bin/agent-server" ]; then "$venv/bin/agent-server" local stop >/dev/null 2>&1 || true; fi
# Only the links this install made: one that points into its environment.
for name in agent-server agent-tray agent agent-proxy agent-machine; do
    if [ -L "$prefix/$name" ]; then
        case "$(readlink "$prefix/$name")" in "$venv"/*) rm -f "$prefix/$name" ;; esac
    fi
done
rm -rf "$home/app" "$venv"
say "Removed the local server's programs."

if [ $purge -eq 1 ]; then
    rm -rf "$home"
    say "Deleted its data in $home."
else
    say "Its data is still in $home: delete that folder too, or run this with --purge, to remove it."
fi
