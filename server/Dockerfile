# agent-server: the API and the console it serves, in one image.
#
# Build from the repository root (the console's sources are in ui/):
#   docker build -f server/Dockerfile -t agent-server .
# or use compose.yaml at the root: docker compose up -d
#
# The agent machines don't use this image: they run the Go client (client/).

# The console (ui/), built once here; the server only serves its files.
FROM node:22-alpine AS console
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

FROM python:3.13-slim
# The build's version (VERSION, below; there's no git here): --build-arg AI_PROXY_VERSION=...
# names it exactly, such as the dist/<version>/ mounted for downloads.
ARG AI_PROXY_VERSION=
ENV AI_PROXY_VERSION=${AI_PROXY_VERSION}
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AI_PROXY_HOME=/app \
    AI_PROXY_HOST=0.0.0.0 \
    AI_PROXY_UI_BUILD=0
WORKDIR /app
COPY server/pyproject.toml server/README.md /src/server/
COPY server/agent_server /src/server/agent_server
RUN pip install /src/server && rm -rf /src
COPY --from=console /ui/dist /app/ui/dist
COPY VERSION /app/VERSION
# The documentation the console shows under /help.
COPY docs /app/docs
# Defaults for an empty config volume: the container's backends, and the repository's
# models and agents (agents.yaml is what every launcher's `agent` gets from the server).
COPY server/docker/backends.yaml /app/defaults/config/backends.yaml
COPY config/models.yaml config/agents.yaml /app/defaults/config/
COPY benchmarks/tests /app/benchmarks/tests
COPY server/docker/entrypoint.sh /usr/local/bin/agent-server-entrypoint
# Shell scripts must have LF endings, whatever the checkout used.
RUN sed -i 's/\r$//' /usr/local/bin/agent-server-entrypoint \
    && chmod 755 /usr/local/bin/agent-server-entrypoint \
    && useradd --system --uid 10001 --home-dir /app agent \
    && mkdir -p /app/config /app/logs /app/certs \
    && chown -R agent:agent /app/config /app/logs /app/certs
USER agent
# config/: backends, models, agents (the console's Settings page rewrites backends.yaml).
# logs/: sessions, traces, terminals. certs/: the HTTPS intercept CA, users and keys.
# Builds to hand out (scripts/build-dist.sh's dist/) are read from /app/dist: mount them there.
VOLUME ["/app/config", "/app/logs", "/app/certs"]
EXPOSE 9999
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9999/health', timeout=3)"
ENTRYPOINT ["agent-server-entrypoint"]
