# agent-sandbox: a container that joins an agent-server's fleet as a machine and offers
# itself as a sandbox, where the server starts coding agents in /workspace only.
#
# It's Ubuntu 24.04 with both Go components (the client's agent and agent-proxy, and
# agent-machine), the coding agents installed with npm, Ollama (CPU only) for the console to start,
# and the everyday tools agents use (git, gh, ssh/scp/sftp, ping, dig/nslookup, curl, build tools,
# Python; PACKAGES below). On start it signs in to AI_PROXY_SERVER (see entrypoint.sh), turns
# launches on for /workspace and stays in the fleet.
#
# Published as ghcr.io/joeyenfield/agent-sandbox (.github/workflows/images.yml), which
# deploy/compose/server's sandbox profile and the quickstart's machines run. To build it yourself,
# from the repository root (it needs client/ and machine/):
#   docker build -f sandbox/Dockerfile -t agent-sandbox .
#
# AGENTS picks the npm packages (the install lines of the shipped agents, server/agent_server/defaults.py),
# EXTRA_PACKAGES adds apt packages:
#   docker build -f sandbox/Dockerfile --build-arg AGENTS="@openai/codex opencode-ai" -t agent-sandbox .
#   docker build -f sandbox/Dockerfile --build-arg EXTRA_PACKAGES="postgresql-client nmap" -t agent-sandbox .

ARG OLLAMA_VERSION=0.40.14

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

# Ollama, for the console to start and pull models into here (servers are on unless
# AGENT_SERVERS=off). CPU only: the sandbox has no GPU of its own, and the CUDA, ROCm and
# Vulkan libraries are most of Ollama's 3 GB. For a GPU, relay an Ollama container that has
# one (AGENT_OLLAMA). --build-arg OLLAMA_VERSION=... picks another release (it's declared
# at the top, as an argument a FROM uses must be).
FROM docker.io/ollama/ollama:${OLLAMA_VERSION} AS ollama
# Its CPU backends are the files at the top of lib/ollama; each GPU backend is a folder.
RUN mkdir -p /out/bin /out/lib \
    && cp /usr/bin/ollama /out/bin/ \
    && cp -a /usr/lib/ollama /out/lib/ \
    && find /out/lib/ollama -mindepth 1 -maxdepth 1 -type d -exec rm -rf {} +

# Node.js for the agents (npm packages), from its official image: its /usr/local holds node, npm and npx.
FROM docker.io/library/node:22-bookworm-slim AS node

# Ubuntu, with the tools a person at a terminal would reach for, so agents can too: networking
# (ping, dig and nslookup, traceroute, ip, nc, telnet), ssh, scp and sftp, rsync, git and gh,
# build tools, Python with pip and pipx, editors, search and archive tools. tini reaps the
# processes agents leave behind, as agent-machine is the container's main process. socat relays
# model servers in other containers (AGENT_OLLAMA, AGENT_LLAMACPP) to the loopback ports
# agent-machine looks on; libgomp1 is what llama.cpp's Linux builds, which the console installs
# here, and Ollama's CPU backends need beyond the base image.
#
# More tools: --build-arg EXTRA_PACKAGES="..." bakes apt packages in; AGENT_APT_PACKAGES="..."
# installs them when the container starts (entrypoint.sh); and the agent user has sudo without a
# password (AGENT_SUDO=0 at build leaves it out), so an agent can `sudo apt-get install` what it
# needs. Keep PACKAGES in step with devhelpers/allinone/Dockerfile's (server/tests/test_allinone.py checks).
FROM docker.io/library/ubuntu:24.04
ARG AGENTS="@anthropic-ai/claude-code @openai/codex opencode-ai @qwen-code/qwen-code @github/copilot"
ARG PACKAGES="ca-certificates curl wget git git-lfs gh openssh-client rsync less nano vim-tiny procps psmisc htop lsof strace file tree jq ripgrep fd-find unzip zip xz-utils bzip2 zstd iputils-ping iputils-tracepath traceroute dnsutils net-tools iproute2 netcat-openbsd telnet socat build-essential pkg-config python3 python-is-python3 python3-pip python3-venv pipx sqlite3 openssl gnupg sudo tini libgomp1 tzdata bash-completion"
ARG EXTRA_PACKAGES=""
ARG AGENT_SUDO=1
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
# The package index is tried again when a mirror doesn't answer: a plain `apt-get update` goes on
# without it, and the install then fails with "Unable to locate package" (seen on GitHub's runners).
RUN for attempt in 1 2 3 4 5 6; do \
         apt-get update -o APT::Update::Error-Mode=any && break; \
         echo "apt-get update failed (attempt $attempt)"; sleep 20; \
       done \
    && apt-get install -y --no-install-recommends $PACKAGES $EXTRA_PACKAGES \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/bin/fdfind /usr/local/bin/fd
COPY --from=node /usr/local/ /usr/local/
RUN if [ -n "$AGENTS" ]; then npm install -g --no-audit --no-fund $AGENTS && npm cache clean --force; fi
COPY --from=build /out/ /usr/local/bin/
# ollama finds its backends in ../lib/ollama beside itself.
COPY --from=ollama /out/ /usr/local/
COPY sandbox/entrypoint.sh /usr/local/bin/agent-sandbox-entrypoint
# ping wherever the runtime allows it (sandbox/ping): cp drops the file capability, so the copy uses ICMP sockets.
COPY sandbox/ping /usr/local/bin/ping
# The quickstart's helpers: deploy/compose/quickstart/compose.yaml runs its machines with them, so
# that one file is all it needs. Stacks that mount their own copy over them (peer, testdata) still can.
COPY deploy/compose/quickstart/start.py deploy/compose/quickstart/machine.py /quickstart/
# Shell scripts must have LF endings, whatever the checkout used. Ubuntu's image already has a
# user 1000 (ubuntu); the agents run as it, renamed agent.
RUN sed -i 's/\r$//' /usr/local/bin/agent-sandbox-entrypoint /usr/local/bin/ping \
    && chmod 755 /usr/local/bin/agent-sandbox-entrypoint /usr/local/bin/ping \
    && cp /usr/bin/ping /usr/local/lib/ping-socket \
    && usermod --login agent --home /home/agent --move-home --shell /bin/bash ubuntu \
    && groupmod --new-name agent ubuntu \
    && if [ "$AGENT_SUDO" = 1 ]; then echo "agent ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/agent && chmod 440 /etc/sudoers.d/agent; fi \
    && mkdir -p /workspace \
    && chown agent:agent /workspace \
    && chgrp -R 0 /home/agent /workspace \
    && chmod -R g=u /home/agent /workspace \
    && chmod g+w /etc/passwd
# OpenShift/OKD's restricted policy runs the container as a random user in group 0: the home and
# workspace are group 0's to write too, and the entrypoint adds that user to /etc/passwd (which
# ssh and git need to know who they are).
USER agent
WORKDIR /workspace
# Color by default: a container started without -t has no TERM, and agents then print plain text.
ENV HOME=/home/agent TERM=xterm-256color COLORTERM=truecolor
# /home/agent: the sign-ins (agent's and agent-machine's), the machine's id and settings,
# and the agents' own logins. /workspace: the projects agents work on.
VOLUME ["/home/agent", "/workspace"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD agent-machine | grep -q "^Fleet *: connected"
ENTRYPOINT ["tini", "--", "agent-sandbox-entrypoint"]
