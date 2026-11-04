#!/bin/sh
# Joins the agent-server's fleet as a machine that takes launches in the workspace only.
#
#   AI_PROXY_SERVER          the API, for example https://api.aicontrolmesh.com (required)
#   On first start, approve both printed OIDC device codes in your browser.
#   Later starts reuse rotating credentials in the persistent home volume.
#   AI_PROXY_MACHINE_SETUP_CODE  a setup code from the console's Machines page: agent-machine
#                            login uses it instead of a device code (and AI_PROXY_MACHINE_ENABLE)
#   AI_PROXY_SETUP_CODE      the same for agent's own sign-in (the console sets both to one code,
#                            with a use for each); later starts don't spend it again
#   AI_PROXY_MACHINE_NAME    the name the console shows (default: the container's hostname)
#   AGENT_WORKSPACE          where agents may start (default /workspace)
#
# Local model servers, which agent-machine only looks for on its own loopback:
#   AGENT_OLLAMA             an Ollama in another container, host[:port] (default port 11434),
#                            relayed to 127.0.0.1:11434
#   AGENT_LLAMACPP           a llama-server in another container, host[:port] (default port
#                            8080), relayed to 127.0.0.1:$AI_PROXY_LLAMACPP_PORT (default 9931)
#   AGENT_SERVERS=on         let the console install llama.cpp here and start, stop and
#                            configure the model servers in this container (default off)
#
# Git, for agents that clone, pull and push:
#   /ssh (a read-only mount)  SSH keys (id_ed25519, ...) and optionally config and known_hosts,
#                            copied into ~/.ssh on every start with the permissions ssh wants
#   GIT_USER_NAME, GIT_USER_EMAIL   who commits
#   AGENT_REPOS              git URLs to clone into the workspace on start (space-separated;
#                            ones already there are left alone)
#
# Both components sign in separately: agent-machine joins the fleet, agent launches agents.
set -eu

server=${AI_PROXY_SERVER:-}
if [ -z "$server" ]; then
    echo "agent-sandbox: set AI_PROXY_SERVER to the API's URL, for example https://api.aicontrolmesh.com" >&2
    exit 2
fi
case "$server" in *://*) ;; *) server="https://$server" ;; esac
server=${server%/}
workspace=${AGENT_WORKSPACE:-/workspace}
config=${XDG_CONFIG_HOME:-$HOME/.config}

# sign_in signs one component in, unless its credentials already name this server. A login that
# fails (a code nobody approved, a used-up setup code, the sign-in service refusing) is tried again
# after a wait that doubles up to 15 minutes. Exiting instead would have the container's restart
# policy start it again at once, and every start asks the sign-in service for a new code: a
# container left like that sent the server thousands of requests an hour.
sign_in() {
    program=$1
    credentials=$2
    if grep -Fqs "\"url\": \"$server\"" "$credentials" && grep -qs '"refresh_token": "[^"]' "$credentials"; then
        echo "$program: already signed in to $server"
        return 0
    fi
    wait=30
    until "$program" login "$server" </dev/null; do
        echo "agent-sandbox: $program couldn't sign in to $server. Trying again in ${wait}s: approve its code when it shows one, or start the container with a setup code (AI_PROXY_MACHINE_SETUP_CODE)." >&2
        sleep "$wait"
        wait=$((wait * 2))
        if [ "$wait" -gt 900 ]; then wait=900; fi
    done
}

# Wait for the server: in compose it may still be starting.
tries=0
until curl -fsS -o /dev/null "$server/health"; do
    tries=$((tries + 1))
    if [ $tries -ge 60 ]; then
        echo "agent-sandbox: $server/health didn't answer in 2 minutes" >&2
        exit 1
    fi
    sleep 2
done

sign_in agent-machine "$config/agent-machine/credentials.json"
sign_in agent "$config/agent-proxy/credentials.json"

mkdir -p "$workspace"

# SSH keys from /ssh. A mount from a Windows or macOS host usually arrives with modes ssh
# refuses (and owned by someone else), so copy rather than use it in place.
ssh_dir=${AGENT_SSH_DIR:-/ssh}
if [ -d "$ssh_dir" ] && [ -n "$(ls -A "$ssh_dir" 2>/dev/null)" ]; then
    mkdir -p "$HOME/.ssh"
    chmod 700 "$HOME/.ssh"
    for file in "$ssh_dir"/*; do
        [ -f "$file" ] || continue
        name=$(basename "$file")
        cp "$file" "$HOME/.ssh/$name"
        case "$name" in
            *.pub|known_hosts*|config) chmod 644 "$HOME/.ssh/$name" ;;
            *) chmod 600 "$HOME/.ssh/$name" ;;
        esac
    done
    # Without a config of its own, trust a host's key the first time it's seen, so a
    # clone in an agent's terminal doesn't stop at a yes/no question nobody answers.
    if [ ! -f "$ssh_dir/config" ]; then
        printf 'Host *\n    StrictHostKeyChecking accept-new\n' > "$HOME/.ssh/config"
        chmod 644 "$HOME/.ssh/config"
    fi
    echo "SSH keys from $ssh_dir: $(cd "$HOME/.ssh" && ls | grep -v -e '\.pub$' -e '^config$' -e '^known_hosts' | tr '\n' ' ')"
fi
if [ -n "${GIT_USER_NAME:-}" ]; then git config --global user.name "$GIT_USER_NAME"; fi
if [ -n "${GIT_USER_EMAIL:-}" ]; then git config --global user.email "$GIT_USER_EMAIL"; fi
for repo in ${AGENT_REPOS:-}; do
    folder="$workspace/$(basename "$repo" .git)"
    if [ -e "$folder" ]; then
        echo "$folder is already there; not cloning $repo"
    # No terminal to answer a password or passphrase prompt: fail instead of hanging.
    elif ! GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND="ssh -o BatchMode=yes" git clone "$repo" "$folder"; then
        echo "agent-sandbox: couldn't clone $repo (carrying on without it)" >&2
    fi
done

# relay HOST[:PORT] DEFAULT_PORT LOCAL_PORT NAME keeps 127.0.0.1:LOCAL_PORT forwarded to a
# model server in another container, where agent-machine finds it and tunnels to it.
relay() {
    target=$1
    target=${target#*://}
    target=${target%%/*}
    case "$target" in
        "") echo "agent-sandbox: $4 is empty" >&2; exit 2 ;;
        *:*) ;;
        *) target="$target:$2" ;;
    esac
    host=${target%:*}
    port=${target##*:}
    case "$port" in ''|*[!0-9]*) echo "agent-sandbox: $4=$1 has no valid port" >&2; exit 2 ;; esac
    echo "$4: relaying 127.0.0.1:$3 to $host:$port"
    # socat forks one child per connection; the loop brings the listener back if it ever exits.
    (while :; do
        socat "TCP-LISTEN:$3,bind=127.0.0.1,reuseaddr,fork" "TCP:$host:$port" || true
        sleep 2
    done) &
}
llamacpp_port=${AI_PROXY_LLAMACPP_PORT:-9931}
if [ -n "${AGENT_OLLAMA:-}" ]; then relay "$AGENT_OLLAMA" 11434 11434 AGENT_OLLAMA; fi
if [ -n "${AGENT_LLAMACPP:-}" ]; then relay "$AGENT_LLAMACPP" 8080 "$llamacpp_port" AGENT_LLAMACPP; fi

if [ -n "${AI_PROXY_MACHINE_NAME:-}" ]; then agent-machine name "$AI_PROXY_MACHINE_NAME" >/dev/null; fi
agent-machine launches on >/dev/null
agent-machine roots add "$workspace" >/dev/null
# docker run is root on the computer the sandbox runs on: never from inside it.
agent-machine containers off >/dev/null
case "${AGENT_SERVERS:-off}" in
    on|1|true|yes)
        if [ -n "${AGENT_LLAMACPP:-}" ]; then
            echo "agent-sandbox: AGENT_LLAMACPP holds 127.0.0.1:$llamacpp_port, so a llama.cpp started here from the console can't listen there" >&2
        fi
        agent-machine servers on >/dev/null ;;
    off|0|false|no|"") agent-machine servers off >/dev/null ;;
    *) echo "agent-sandbox: AGENT_SERVERS must be on or off, not $AGENT_SERVERS" >&2; exit 2 ;;
esac

# Agents started here inherit this environment: keep the secrets out of it.
unset AI_PROXY_PASSWORD AI_PROXY_PASSWORD_FILE AI_PROXY_KEY AI_PROXY_MACHINE_SETUP_CODE AI_PROXY_SETUP_CODE
export AI_PROXY_URL="$server"
agent-machine
exec agent-machine run
