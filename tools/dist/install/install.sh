#!/bin/sh
# Installs the programs beside this script (agent, agent-proxy, agent-machine: whichever
# this archive has) on Linux, macOS and FreeBSD.
#
#   ./install.sh                     into /usr/local/bin as root, else ~/.local/bin
#   ./install.sh --prefix DIR        into DIR
#   ./install.sh --service           also start agent-machine at boot (systemd as root,
#                                    else a systemd user unit), at login (launchd) or as
#                                    an rc.d service (FreeBSD, root only)
#   ./install.sh --uninstall         remove them again, and the service (settings stay)
#
# The service runs as the user who ran the script (the sudo user when run with sudo),
# because agent-machine's sign-in and settings live in that user's home.
set -eu

PROGRAMS="agent agent-proxy agent-machine"
LABEL=com.ai-coding-agent-proxy.agent-machine

here=$(cd "$(dirname "$0")" && pwd)
prefix=""
service=0
uninstall=0
while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) [ $# -ge 2 ] || { echo "--prefix needs a folder" >&2; exit 2; }; prefix=$2; shift ;;
        --prefix=*) prefix=${1#--prefix=} ;;
        --service) service=1 ;;
        --uninstall) uninstall=1 ;;
        -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "install.sh: unknown option $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done

if [ "$(id -u)" -eq 0 ]; then root=1; else root=0; fi
if [ -z "$prefix" ]; then
    if [ $root -eq 1 ]; then prefix=/usr/local/bin; else prefix="$HOME/.local/bin"; fi
fi
os=$(uname -s)

# The service's user: whoever ran sudo, so it finds their agent-machine login.
user=$(id -un)
if [ $root -eq 1 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then user=$SUDO_USER; fi
user_home=$(eval echo "~$user")

as_user() {
    if [ "$user" = "$(id -un)" ]; then "$@"; else sudo -u "$user" -H "$@"; fi
}

# The service's PATH: agent-machine starts agent, docker or podman, ollama and strata from it.
service_path=/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin
if [ "$os" = Darwin ]; then service_path="$service_path:/opt/homebrew/bin"; fi
case ":$service_path:" in *":$prefix:"*) ;; *) service_path="$prefix:$service_path" ;; esac

systemd_unit() {
    cat <<EOF
[Unit]
Description=agent-machine: keeps this computer in its agent-server's fleet
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=$prefix/agent-machine run
Environment=PATH=$service_path
Restart=always
RestartSec=10
EOF
    if [ "$1" = system ]; then
        echo "User=$user"
        echo
        echo "[Install]"
        echo "WantedBy=multi-user.target"
    else
        echo
        echo "[Install]"
        echo "WantedBy=default.target"
    fi
}

launchd_plist() {
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key>
    <array><string>$prefix/agent-machine</string><string>run</string></array>
    <key>EnvironmentVariables</key>
    <dict><key>PATH</key><string>$service_path</string></dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
</dict>
</plist>
EOF
}

rcd_script() {
    cat <<EOF
#!/bin/sh
# PROVIDE: agent_machine
# REQUIRE: NETWORKING
# KEYWORD: shutdown
#
# agent-machine: keeps this computer in its agent-server's fleet.
# Enable with: sysrc agent_machine_enable=YES

. /etc/rc.subr

name=agent_machine
rcvar=agent_machine_enable
load_rc_config \$name
: \${agent_machine_enable:=NO}
: \${agent_machine_user:=$user}

pidfile=/var/run/agent_machine.pid
procname=/usr/sbin/daemon
command=/usr/sbin/daemon
command_args="-f -r -R 10 -P \$pidfile -u \$agent_machine_user /usr/bin/env HOME=$user_home PATH=$service_path $prefix/agent-machine run"

run_rc_command "\$1"
EOF
}

# stop_running stops this user's agent-machine if one is in the fleet (agent-machine start,
# or a run in a terminal), so the service's own can join, and says so.
stop_running() {
    [ -x "$prefix/agent-machine" ] || return 0
    stopped=$(as_user "$prefix/agent-machine" stop 2>&1) || true
    case "$stopped" in
        *"isn't running"*|"") ;;
        *) echo "$stopped" ;;
    esac
}

install_service() {
    if [ ! -x "$prefix/agent-machine" ]; then
        echo "--service needs agent-machine, which this archive doesn't have." >&2
        exit 1
    fi
    case "$os" in
        Linux)
            command -v systemctl >/dev/null 2>&1 || { echo "--service needs systemd on Linux; run \`agent-machine start\` from your own startup instead." >&2; exit 1; }
            if [ $root -eq 1 ]; then
                systemd_unit system > /etc/systemd/system/agent-machine.service
                stop_running
                systemctl daemon-reload
                systemctl enable --now agent-machine.service
                echo "Service: /etc/systemd/system/agent-machine.service, running as $user (systemctl status agent-machine)"
            else
                units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
                mkdir -p "$units"
                systemd_unit user > "$units/agent-machine.service"
                stop_running
                systemctl --user daemon-reload
                systemctl --user enable --now agent-machine.service
                echo "Service: $units/agent-machine.service (systemctl --user status agent-machine)"
                echo "It runs while you're signed in. To run it from boot instead: sudo loginctl enable-linger $user"
            fi
            ;;
        Darwin)
            agents="$user_home/Library/LaunchAgents"
            plist="$agents/$LABEL.plist"
            uid=$(id -u "$user")
            mkdir -p "$agents"
            launchctl bootout "gui/$uid/$LABEL" >/dev/null 2>&1 || true
            launchd_plist > "$plist"
            if [ $root -eq 1 ]; then chown "$user" "$plist"; fi
            stop_running
            launchctl bootstrap "gui/$uid" "$plist"
            echo "Service: $plist, starts when $user logs in (launchctl print gui/$uid/$LABEL)"
            ;;
        FreeBSD)
            [ $root -eq 1 ] || { echo "--service on FreeBSD needs root: sudo ./install.sh --service" >&2; exit 1; }
            rcd_script > /usr/local/etc/rc.d/agent_machine
            chmod 0755 /usr/local/etc/rc.d/agent_machine
            stop_running
            sysrc agent_machine_enable=YES >/dev/null
            service agent_machine restart >/dev/null 2>&1 || service agent_machine start
            echo "Service: /usr/local/etc/rc.d/agent_machine, running as $user (service agent_machine status)"
            ;;
        *)
            echo "--service isn't supported on $os; run \`agent-machine start\` from your own startup instead." >&2
            exit 1
            ;;
    esac
    if as_user "$prefix/agent-machine" 2>/dev/null | grep -q "not signed in"; then
        echo "If it isn't signed in yet: agent-machine login https://your-server:9999 (as $user). The service keeps retrying until then."
    fi
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

if [ $uninstall -eq 1 ]; then
    remove_service
    stop_running
    for program in $PROGRAMS; do
        if [ -f "$prefix/$program" ]; then rm -f "$prefix/$program" && echo "Removed $prefix/$program"; fi
    done
    echo "Settings and sign-ins are left in place; agent logout and agent-machine logout revoke them."
    exit 0
fi

found=""
for program in $PROGRAMS; do
    if [ -f "$here/$program" ]; then found="$found $program"; fi
done
[ -n "$found" ] || { echo "install.sh: no programs beside this script in $here" >&2; exit 1; }

mkdir -p "$prefix"
for program in $found; do
    # Replace, not overwrite: a running agent-proxy or agent-machine keeps its old file.
    cp "$here/$program" "$prefix/.$program.new"
    chmod 0755 "$prefix/.$program.new"
    mv -f "$prefix/.$program.new" "$prefix/$program"
    # Downloaded on a Mac, the unsigned binary is quarantined and Gatekeeper refuses it.
    if [ "$os" = Darwin ]; then xattr -d com.apple.quarantine "$prefix/$program" >/dev/null 2>&1 || true; fi
    echo "Installed $prefix/$program"
done

case ":$PATH:" in
    *":$prefix:"*) ;;
    *) echo "$prefix isn't on your PATH. Add it, for example in ~/.profile: export PATH=\"$prefix:\$PATH\"" ;;
esac

if [ $service -eq 1 ]; then install_service; fi

# The server's one-line installer (curl <server>/install.sh | sh) signs them in itself.
if [ -z "${AI_PROXY_ONE_LINER:-}" ]; then
    case "$found" in
        *agent-proxy*) echo "Next: agent login https://your-server:9999 (once), then agent --list" ;;
    esac
    case "$found" in
        *agent-machine*) if [ $service -eq 0 ]; then echo "Next: agent-machine login https://your-server:9999 (once), then agent-machine start, or ./install.sh --service to start it at boot"; fi ;;
    esac
fi
