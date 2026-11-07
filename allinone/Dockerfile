# ai-control-mesh: the whole system in one container, to try it with one command:
#
#   docker run -d --name mesh -p 127.0.0.1:19999:19999 -p 127.0.0.1:20000:20000 \
#     -v mesh-data:/data -v mesh-home:/home/agent -v mesh-workspace:/workspace ghcr.io/joeyenfield/ai-control-mesh
#   docker logs mesh      # the console's address and admin's password
#
# It runs agent-server and agent-auth (`agent-server local`, with everything in /data), and is a
# machine of its own fleet: agent-machine signed in as admin, with launches, files, terminals and
# model servers on for /workspace, the coding agents installed, and Ollama (CPU) for the console to
# start. See allinone/entrypoint.py and docs/local.md. The ports must stay 19999 and 20000 on the
# host too: the browser and the server inside both sign in at http://localhost:20000.
#
# Published as ghcr.io/joeyenfield/ai-control-mesh (.github/workflows/images.yml). To build it
# yourself, from the repository root:
#   docker build -f allinone/Dockerfile -t ai-control-mesh .
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
RUN mkdir -p /out/bin /out/lib \
    && cp /usr/bin/ollama /out/bin/ \
    && cp -a /usr/lib/ollama /out/lib/ \
    && find /out/lib/ollama -mindepth 1 -maxdepth 1 -type d -exec rm -rf {} +

# The console, built once here; the server only serves its files.
FROM docker.io/library/node:22-alpine AS console
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

FROM docker.io/library/node:22-bookworm-slim
ARG AGENTS="@anthropic-ai/claude-code @openai/codex opencode-ai @qwen-code/qwen-code @github/copilot"
ARG AI_PROXY_VERSION=
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git openssh-client less procps jq unzip ripgrep python3 python3-venv tini \
        socat libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN if [ -n "$AGENTS" ]; then npm install -g --no-audit --no-fund $AGENTS && npm cache clean --force; fi
COPY --from=build /out/ /usr/local/bin/
COPY --from=ollama /out/ /usr/local/
# The server, as the release bundle lays it out (scripts/release/release.py): installed editable, so
# /opt/mesh is its root, with VERSION, the built console, the docs and BUILD naming the version.
COPY VERSION LICENSE /opt/mesh/
COPY server/pyproject.toml server/README.md /opt/mesh/server/
COPY server/agent_server /opt/mesh/server/agent_server
COPY docs /opt/mesh/docs
COPY benchmarks/tests /opt/mesh/benchmarks/tests
COPY --from=console /ui/dist /opt/mesh/ui/dist
# quickstart/machine.py signs the machine in through the issuer's device flow, as a browser would.
COPY quickstart/machine.py /opt/mesh/quickstart_machine.py
COPY allinone/entrypoint.py /opt/mesh/entrypoint.py
RUN echo "${AI_PROXY_VERSION:-$(cat /opt/mesh/VERSION)+unknown}" > /opt/mesh/BUILD \
    && python3 -m venv /opt/mesh/venv \
    && /opt/mesh/venv/bin/pip install --no-cache-dir --disable-pip-version-check -e /opt/mesh/server \
    && ln -s /opt/mesh/venv/bin/agent-server /usr/local/bin/agent-server \
    && find /opt/mesh -name __pycache__ -prune -exec rm -rf {} +
# node's image has a user 1000 (node); everything runs as it, renamed agent.
RUN usermod --login agent --home /home/agent --move-home node \
    && groupmod --new-name agent node \
    && mkdir -p /workspace /data \
    && chown agent:agent /workspace /data
USER agent
WORKDIR /workspace
ENV HOME=/home/agent \
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
