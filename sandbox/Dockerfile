# agent-sandbox: a container that joins an agent-server's fleet as a machine and offers
# itself as a sandbox, where the server starts coding agents in /workspace only.
#
# It has both Go components (the client's agent and agent-proxy, and agent-machine) and
# the coding agents installed with npm. On start it signs in to AI_PROXY_SERVER (see
# entrypoint.sh), turns launches on for /workspace and stays in the fleet.
#
# Published as ghcr.io/joeyenfield/agent-sandbox (.github/workflows/images.yml), which
# compose.yaml's sandbox profile and the quickstart's machines run. To build it yourself,
# from the repository root (it needs client/ and machine/):
#   docker build -f sandbox/Dockerfile -t agent-sandbox .
#
# AGENTS picks the npm packages (the install lines of the shipped agents, server/agent_server/defaults.py):
#   docker build -f sandbox/Dockerfile --build-arg AGENTS="@openai/codex opencode-ai" -t agent-sandbox .

# The Go components, for whichever platform the image is built for.
FROM docker.io/library/golang:1.24-bookworm AS build
# The build's version (VERSION at the root; there's no git here): pass the server's,
# --build-arg AI_PROXY_VERSION=..., to match it exactly.
ARG AI_PROXY_VERSION=
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
WORKDIR /src
COPY client/go.mod client/go.sum client/
COPY machine/go.mod machine/go.sum machine/
RUN cd client && go mod download && cd ../machine && go mod download
COPY client/ client/
COPY machine/ machine/
COPY VERSION ./
RUN version="${AI_PROXY_VERSION:-$(cat VERSION)+unknown}" \
    && cd client && go build -trimpath -ldflags "-s -w -X main.version=$version" -o /out/ ./cmd/agent ./cmd/agent-proxy \
    && cd ../machine && go build -trimpath -ldflags "-s -w -X main.version=$version" -o /out/ ./cmd/agent-machine

FROM docker.io/library/node:22-bookworm-slim
ARG AGENTS="@anthropic-ai/claude-code @openai/codex opencode-ai @qwen-code/qwen-code @github/copilot"
# What coding agents expect to find: git, a shell, Python, search tools. tini reaps the
# processes agents leave behind, as agent-machine is the container's main process.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git openssh-client less procps jq unzip ripgrep python3 python3-venv tini \
    && rm -rf /var/lib/apt/lists/*
RUN if [ -n "$AGENTS" ]; then npm install -g --no-audit --no-fund $AGENTS && npm cache clean --force; fi
COPY --from=build /out/ /usr/local/bin/
COPY sandbox/entrypoint.sh /usr/local/bin/agent-sandbox-entrypoint
# Shell scripts must have LF endings, whatever the checkout used. node's image already
# has a user 1000 (node); the agents run as it, renamed agent.
RUN sed -i 's/\r$//' /usr/local/bin/agent-sandbox-entrypoint \
    && chmod 755 /usr/local/bin/agent-sandbox-entrypoint \
    && usermod --login agent --home /home/agent --move-home node \
    && groupmod --new-name agent node \
    && mkdir -p /workspace \
    && chown agent:agent /workspace
USER agent
WORKDIR /workspace
ENV HOME=/home/agent
# /home/agent: the sign-ins (agent's and agent-machine's), the machine's id and settings,
# and the agents' own logins. /workspace: the projects agents work on.
VOLUME ["/home/agent", "/workspace"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD agent-machine | grep -q "^Fleet *: connected"
ENTRYPOINT ["tini", "--", "agent-sandbox-entrypoint"]
