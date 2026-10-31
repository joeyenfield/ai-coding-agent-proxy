#!/bin/sh
# Removes agent, agent-proxy and agent-machine from this computer, as install.sh or the
# server's one-line installer put them there. Linux, macOS and FreeBSD:
#
#   curl -fsSL <server>/uninstall.sh | sh
#   curl -fsSL <server>/uninstall.sh | sh -s -- --purge
#   ./uninstall.sh                   from an unpacked archive (install.sh --uninstall runs it)
#
#   --only agent|agent-machine   just one of them (default: both)
#   --prefix DIR                 the folder they're in (default: /usr/local/bin as root, else ~/.local/bin)
#   --purge                      also sign them out (revoking their tokens) and delete their
#                                settings, sign-ins, logs and caches
#
# It stops agent-machine and agent-proxy, removes agent-machine's service (systemd, launchd
# or rc.d) and, as root, what `sudo agent-machine servers service` set up. Programs a package
# manager installed are left to it. Containers agent-machine ran and models it pulled stay.
#
# The agent-server serves this file as it is (server/agent_server/install/uninstall.sh), and
# tools/dist puts the same file in every archive. The service names and paths below are the
# ones tools/dist/install/install.sh creates; keep them in step.
set -eu

LABEL=com.ai-coding-agent-proxy.agent-machine

prefix=""
only=""
purge=0
while [ $# -gt 0 ]; do
    case "$1" in
        --only) [ $# -ge 2 ] || { echo "--only needs agent or agent-machine" >&2; exit 2; }; only=$2; shift ;;
        --only=*) only=${1#--only=} ;;
        --prefix) [ $# -ge 2 ] || { echo "--prefix needs a folder" >&2; exit 2; }; prefix=$2; shift ;;
        --prefix=*) prefix=${1#--prefix=} ;;
        --purge) purge=1 ;;
        -h|--help) sed -n '2,16p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//' || true; exit 0 ;;
        *) echo "uninstall: unknown option $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done
case "$only" in ""|agent|agent-machine) ;; *) echo "uninstall: --only takes agent or agent-machine" >&2; exit 2 ;; esac

programs=""
if [ "$only" != agent-machine ]; then programs="agent agent-proxy"; fi
if [ "$only" != agent ]; then programs="$programs agent-machine"; fi
has() { case " $programs " in *" $1 "*) return 0 ;; esac; return 1; }

if [ "$(id -u)" -eq 0 ]; then root=1; else root=0; fi
if [ -z "$prefix" ]; then
    if [ $root -eq 1 ]; then prefix=/usr/local/bin; else prefix="$HOME/.local/bin"; fi
fi
prefix=${prefix%/}
os=$(uname -s)

# Whose sign-ins and settings: whoever ran sudo, as install.sh's service runs as them.
user=$(id -un)
if [ $root -eq 1 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then user=$SUDO_USER; fi
# Their home and config folder as the programs find them: $HOME and XDG_CONFIG_HOME for
# yourself, the account's own home for the sudo user.
if [ "$user" = "$(id -un)" ]; then
    user_home=$HOME
    config="${XDG_CONFIG_HOME:-$HOME/.config}"
else
    user_home=$(eval echo "~$user")
    config="$user_home/.config"
fi

as_user() {
    if [ "$user" = "$(id -un)" ]; then "$@"; else sudo -u "$user" -H "$@"; fi
}

remove_service() {
    case "$os" in
        Linux)
            if [ $root -eq 1 ] && [ -f /etc/systemd/system/agent-machine.service ]; then
                systemctl disable --now agent-machine.service >/dev/null 2>&1 || true
                rm -f /etc/systemd/system/agent-machine.service
                systemctl daemon-reload
                echo "Removed the agent-machine system service."
            fi
            units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
            if [ $root -eq 0 ] && [ -f "$units/agent-machine.service" ]; then
                systemctl --user disable --now agent-machine.service >/dev/null 2>&1 || true
                rm -f "$units/agent-machine.service"
                systemctl --user daemon-reload
                echo "Removed the agent-machine user service."
            fi
            ;;
        Darwin)
            plist="$user_home/Library/LaunchAgents/$LABEL.plist"
            if [ -f "$plist" ]; then
                launchctl bootout "gui/$(id -u "$user")/$LABEL" >/dev/null 2>&1 || true
                rm -f "$plist"
                echo "Removed $plist."
            fi
            ;;
        FreeBSD)
            if [ $root -eq 1 ] && [ -f /usr/local/etc/rc.d/agent_machine ]; then
                service agent_machine stop >/dev/null 2>&1 || true
                sysrc -x agent_machine_enable >/dev/null 2>&1 || true
                rm -f /usr/local/etc/rc.d/agent_machine
                echo "Removed the agent_machine rc.d service."
            fi
            ;;
    esac
}

# stop_machine stops an agent-machine started by hand (agent-machine start, or a run in a terminal).
stop_machine() {
    [ -x "$prefix/agent-machine" ] || return 0
    stopped=$(as_user "$prefix/agent-machine" stop 2>&1) || true
    case "$stopped" in
        *"isn't running"*|"") ;;
        *) echo "$stopped" ;;
    esac
}

# stop_proxy stops this install's agent-proxy, which keeps its port, and the agents' traffic,
# until it exits. Agents running through it lose their connection to the server. `agent proxy
# stop` asks the one on its port to exit; one on another port, or too old to be asked, is ended
# by its path (the launcher starts it as `<prefix>/agent proxy`).
stop_proxy() {
    stopped=0
    if [ -x "$prefix/agent" ]; then
        case "$(as_user "$prefix/agent" proxy stop 2>&1 || true)" in
            *"agent-proxy stopped"*) stopped=1 ;;
        esac
    fi
    if ! command -v pkill >/dev/null 2>&1; then
        if [ $stopped -eq 1 ]; then echo "Stopped agent-proxy."; fi
        return 0
    fi
    if [ -x "$prefix/agent" ] && pkill -f "^$prefix/agent proxy" 2>/dev/null; then stopped=1; fi
    if [ -x "$prefix/agent-proxy" ] && pkill -f "^$prefix/agent-proxy( |\$)" 2>/dev/null; then stopped=1; fi
    if [ $stopped -eq 1 ]; then echo "Stopped agent-proxy."; fi
}

# sign_out signs one program out of every server it's signed in to; logout takes one at a time.
sign_out() {
    [ -x "$prefix/$1" ] || return 0
    tries=0
    while [ $tries -lt 20 ]; do
        out=$(as_user "$prefix/$1" logout 2>&1) || true
        case "$out" in
            *"Signed out"*) printf '%s\n' "$out" ;;
            *"Not signed in"*) return 0 ;;
            *) printf '%s\n' "$out"; return 0 ;;
        esac
        tries=$((tries + 1))
    done
}

# Ollama's systemd service, as `sudo agent-machine servers service` opened it to this user's
# agent-machine: a sudoers rule, a drop-in and the settings file it reads.
remove_ollama_service_rule() {
    [ "$os" = Linux ] || return 0
    dropin=/etc/systemd/system/ollama.service.d/agent-machine.conf
    if [ $root -eq 0 ]; then
        if [ -f "$dropin" ]; then echo "agent-machine may still restart ollama.service; sudo $prefix/agent-machine servers service off (before uninstalling) takes that back."; fi
        return 0
    fi
    removed=0
    for file in /etc/sudoers.d/agent-machine-ollama "$dropin" /etc/agent-machine/ollama.env; do
        if [ -f "$file" ]; then rm -f "$file"; removed=1; fi
    done
    rmdir /etc/agent-machine 2>/dev/null || true
    if [ $removed -eq 1 ]; then
        if command -v systemctl >/dev/null 2>&1; then systemctl daemon-reload || true; fi
        echo "agent-machine may no longer restart ollama.service (removed its sudoers rule and settings)."
    fi
}

delete() {
    if [ -e "$1" ]; then rm -rf "$1" && echo "Deleted $1"; fi
}

if has agent-machine; then
    remove_service
    stop_machine
fi
if has agent; then stop_proxy; fi
if [ $purge -eq 1 ]; then
    for program in agent agent-machine; do
        if has "$program"; then sign_out "$program"; fi
    done
fi
if has agent-machine; then remove_ollama_service_rule; fi

removed=0
for program in $programs; do
    if [ -f "$prefix/$program" ]; then
        rm -f "$prefix/$program" "$prefix/.$program.new"
        echo "Removed $prefix/$program"
        removed=1
    fi
done
if [ $removed -eq 0 ]; then echo "Nothing to remove in $prefix."; fi

if [ $purge -eq 1 ]; then
    if has agent; then
        if [ -n "${AI_PROXY_CREDENTIALS:-}" ]; then
            delete "$AI_PROXY_CREDENTIALS"
            delete "$AI_PROXY_CREDENTIALS.lock"
        fi
        delete "$config/agent-proxy"
        delete "$user_home/.cache/agent-proxy"
    fi
    if has agent-machine; then
        # A folder named by hand may hold more than agent-machine's files.
        if [ -n "${AI_PROXY_MACHINE_HOME:-}" ]; then
            echo "Left $AI_PROXY_MACHINE_HOME (AI_PROXY_MACHINE_HOME); delete it yourself if it only held agent-machine's files."
        else
            delete "$config/agent-machine"
        fi
    fi
else
    echo "Sign-ins and settings are left in place; --purge signs out and deletes them too."
fi

# Copies installed some other way: say how to remove them rather than guessing.
for program in $programs; do
    found=$(command -v "$program" 2>/dev/null || true)
    case "$found" in
        ""|"$prefix/$program") continue ;;
    esac
    if head -n 1 "$found" 2>/dev/null | grep -q python; then
        echo "$found is the Python version: pip uninstall ai-proxy-agent-py"
    elif [ "${found%/*}" = /usr/bin ]; then
        echo "$found came from a package: remove it with your package manager (packages agent-proxy and agent-machine)"
    else
        echo "$found is still there: run this again with --prefix ${found%/*}"
    fi
done
