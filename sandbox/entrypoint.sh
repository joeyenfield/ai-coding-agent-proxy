#!/bin/sh
# Joins the agent-server's fleet as a machine that takes launches in the workspace only.
#
#   AI_PROXY_SERVER          the API, for example https://api.aicontrolmesh.com (required)
#   On first start, approve both printed OIDC device codes in your browser.
#   Later starts reuse rotating credentials in the persistent home volume.
#   AI_PROXY_MACHINE_NAME    the name the console shows (default: the container's hostname)
#   AGENT_WORKSPACE          where agents may start (default /workspace)
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

# sign_in signs one component in, unless its credentials already name this server.
sign_in() {
    program=$1
    credentials=$2
    if grep -Fqs "\"url\": \"$server\"" "$credentials" && grep -qs '"refresh_token": "[^"]' "$credentials"; then
        echo "$program: already signed in to $server"
        return 0
    fi
    "$program" login "$server" </dev/null
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

if [ -n "${AI_PROXY_MACHINE_NAME:-}" ]; then agent-machine name "$AI_PROXY_MACHINE_NAME" >/dev/null; fi
agent-machine launches on >/dev/null
agent-machine roots add "$workspace" >/dev/null
# The sandbox only runs agents: no containers or model servers of its own.
agent-machine containers off >/dev/null
agent-machine servers off >/dev/null

# Agents started here inherit this environment: keep the secrets out of it.
unset AI_PROXY_PASSWORD AI_PROXY_PASSWORD_FILE AI_PROXY_KEY
export AI_PROXY_URL="$server"
agent-machine
exec agent-machine run
