# ai-control-mesh: the whole system in one container, to try it with one command:
#
#   docker run -d --name mesh -p 127.0.0.1:19999:19999 -p 127.0.0.1:20000:20000 \
#     -v mesh-data:/data -v mesh-home:/home/agent -v mesh-workspace:/workspace ghcr.io/joeyenfield/ai-control-mesh
#   docker logs mesh      # the console's address and admin's password
#
# It runs agent-server and agent-auth (`agent-server local`, with everything in /data), and is a
# machine of its own fleet: agent-machine signed in as admin, with launches, files, terminals and
# model servers on for /workspace, the coding agents installed, and Ollama (CPU) for the console to
# start. See devhelpers/allinone/entrypoint.py and docs/local.md. The ports must stay 19999 and 20000 on the
# host too: the browser and the server inside both sign in at http://localhost:20000.
#
# Published as ghcr.io/joeyenfield/ai-control-mesh (.github/workflows/images.yml). To build it
# yourself, from the repository root:
#   docker build -f devhelpers/allinone/Dockerfile -t ai-control-mesh .
# or, with Compose, from devhelpers/allinone: podman compose up -d --build
#
# Its first stages are sandbox/Dockerfile's (the Go programs, Ollama's CPU build, the agents on
# node): keep OLLAMA_VERSION, AGENTS and the base images in step (server/tests/test_allinone.py checks).

ARG OLLAMA_VERSION=0.40.14

FROM docker.io/library/golang:1.24-bookworm AS build
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

FROM docker.io/ollama/ollama:${OLLAMA_VERSION} AS ollama
# Ollama's CPU backends are the files at the top of lib/ollama; each GPU backend is a folder, and
# they are most of its 3 GB. OLLAMA_GPU names the folders to keep (cuda_v12, cuda_v13, vulkan),
# for a container that is given a GPU. Empty is CPU only, as ai-control-mesh is published;
# ai-control-mesh-gpu is this file built with OLLAMA_GPU=cuda_v13 (images.yml, compose.gpu.yaml here).
ARG OLLAMA_GPU=
RUN mkdir -p /out/bin /out/lib \
    && cp /usr/bin/ollama /out/bin/ \
    && cp -a /usr/lib/ollama /out/lib/ \
    && for folder in /out/lib/ollama/*/; do \
         case " $OLLAMA_GPU " in *" $(basename "$folder") "*) ;; *) rm -rf "$folder" ;; esac; \
       done

# The console, built once here; the server only serves its files.
FROM docker.io/library/node:22-alpine AS console
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

FROM docker.io/library/node:22-bookworm-slim AS node

# Ubuntu with the everyday tools, as agent-sandbox (its PACKAGES, EXTRA_PACKAGES and sudo for the
# agent user, so agents can install more; AGENT_APT_PACKAGES at start, in entrypoint.py).
FROM docker.io/library/ubuntu:24.04
ARG AGENTS="@anthropic-ai/claude-code @openai/codex opencode-ai @qwen-code/qwen-code @github/copilot"
ARG PACKAGES="ca-certificates curl wget git git-lfs gh openssh-client rsync less nano vim-tiny procps psmisc htop lsof strace file tree jq ripgrep fd-find unzip zip xz-utils bzip2 zstd iputils-ping iputils-tracepath traceroute dnsutils net-tools iproute2 netcat-openbsd telnet socat build-essential pkg-config python3 python-is-python3 python3-pip python3-venv pipx sqlite3 openssl gnupg sudo tini libgomp1 tzdata bash-completion"
ARG EXTRA_PACKAGES=""
ARG AGENT_SUDO=1
ARG AI_PROXY_VERSION=
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8
# The package index is tried again when a mirror doesn't answer, as in sandbox/Dockerfile.
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
COPY --from=ollama /out/ /usr/local/
# The server, as the release bundle lays it out (devhelpers/scripts/release/release.py): installed editable, so
# /opt/mesh is its root, with VERSION, the built console, the docs and BUILD naming the version.
COPY VERSION LICENSE /opt/mesh/
COPY server/pyproject.toml server/README.md /opt/mesh/server/
COPY server/agent_server /opt/mesh/server/agent_server
COPY docs /opt/mesh/docs
COPY benchmarks/tests /opt/mesh/benchmarks/tests
COPY --from=console /ui/dist /opt/mesh/ui/dist
# deploy/compose/quickstart/machine.py signs the machine in through the issuer's device flow, as a browser would.
COPY deploy/compose/quickstart/machine.py /opt/mesh/quickstart_machine.py
COPY devhelpers/allinone/entrypoint.py /opt/mesh/entrypoint.py
# ping wherever the runtime allows it, as in agent-sandbox (sandbox/ping).
COPY sandbox/ping /usr/local/bin/ping
RUN sed -i 's/\r$//' /usr/local/bin/ping && chmod 755 /usr/local/bin/ping \
    && cp /usr/bin/ping /usr/local/lib/ping-socket \
    && echo "${AI_PROXY_VERSION:-$(cat /opt/mesh/VERSION)+unknown}" > /opt/mesh/BUILD \
    && python3 -m venv /opt/mesh/venv \
    && /opt/mesh/venv/bin/pip install --no-cache-dir --disable-pip-version-check -e /opt/mesh/server \
    && ln -s /opt/mesh/venv/bin/agent-server /usr/local/bin/agent-server \
    && find /opt/mesh -name __pycache__ -prune -exec rm -rf {} +
# Ubuntu's image has a user 1000 (ubuntu); everything runs as it, renamed agent.
RUN usermod --login agent --home /home/agent --move-home --shell /bin/bash ubuntu \
    && groupmod --new-name agent ubuntu \
    && if [ "$AGENT_SUDO" = 1 ]; then echo "agent ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/agent && chmod 440 /etc/sudoers.d/agent; fi \
    && mkdir -p /workspace /data \
    && chown agent:agent /workspace /data
USER agent
WORKDIR /workspace
# Color by default, as in agent-sandbox: a container started without -t has no TERM.
ENV HOME=/home/agent \
    TERM=xterm-256color \
    COLORTERM=truecolor \
    AI_PROXY_LOCAL_HOME=/data/server \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
# /data: the server's database, sign-in state and secrets. /home/agent: this machine's sign-ins and
# settings, and the agents' own logins. /workspace: the projects agents work on.
VOLUME ["/data", "/home/agent", "/workspace"]
EXPOSE 19999 20000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
    CMD curl -fs http://127.0.0.1:19999/health >/dev/null && agent-machine | grep -q "^Fleet *: connected"
ENTRYPOINT ["tini", "--", "/opt/mesh/venv/bin/python", "/opt/mesh/entrypoint.py"]
