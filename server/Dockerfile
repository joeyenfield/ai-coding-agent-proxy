# agent-server: the API and the console it serves, in one image.
#
# Published as ghcr.io/joeyenfield/agent-server (.github/workflows/images.yml), which
# compose.yaml and the quickstart run. To build it yourself, from the repository root (the
# console's sources are in ui/):
#   docker build -f server/Dockerfile -t agent-server .
#
# The agent machines don't use this image: they run the Go client (client/).

# The console (ui/), built once here; the server only serves its files.
FROM docker.io/library/node:22-alpine AS console
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

FROM docker.io/library/python:3.13-slim
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
COPY benchmarks/tests /app/benchmarks/tests
RUN useradd --system --uid 10001 --home-dir /app agent \
    && mkdir -p /app/config /app/logs /app/certs /app/auth-state \
    && chown -R agent:agent /app/config /app/logs /app/certs /app/auth-state
USER agent
# Everything the server keeps is in its database (AI_PROXY_DATABASE_URL; compose.yaml runs
# PostgreSQL). Without one it's a SQLite file in logs/. logs/ also holds the server's own log,
# certs/ the first admin's password once; config/ is read only to import an older install.
# Builds to hand out (scripts/build-dist.sh's dist/) are read from /app/dist: mount them there.
VOLUME ["/app/logs", "/app/certs"]
EXPOSE 9999
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9999/health', timeout=3)"
ENTRYPOINT ["agent-server"]
