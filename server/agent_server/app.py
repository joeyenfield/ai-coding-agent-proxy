from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import installer, internal, openapi, remote, roles, version
from .access_log import AccessLogMiddleware
from .auth import ROOT_TENANT, AuthMiddleware, UserStore
from .config import Settings
from .downloads import Downloads
from .api_providers import ProviderTransport
from .intercept import InterceptProxy, InterceptTransport
from .audit import AuditMiddleware
from .routes import api_providers as api_provider_routes
from .routes import auth, compat, containers, governance, machines, management, mcp, provider_accounts, providers, proxy, terminals
from .runtime import LOGGER, Runtime, upstream_client
from .ui_build import ensure_built
from .usage_limits import UsageLimits


UI_MISSING = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>AI Control Mesh</title>
<style>body{font-family:system-ui,sans-serif;max-width:640px;margin:80px auto;padding:0 24px;line-height:1.6}
code{background:#eef2f1;padding:2px 6px;border-radius:4px}</style></head><body>
<h1>AI Control Mesh is running</h1><p>The web UI has not been built yet. Build it once with</p>
<p><code>cd ui &amp;&amp; npm install &amp;&amp; npm run build</code></p>
<p>or run the dev server with <code>npm run dev</code> and open <code>http://localhost:5173</code>.</p>
<p>The API is available at <a href="/docs">/docs</a>.</p></body></html>"""


def create_app(
    settings: Settings | None = None, restart: Any = None, address_pinned: bool = False,
    cluster: Any = None, internal_port: int | None = None,
) -> FastAPI:
    """The proxy's app. restart, from the supervisor, lets the console restart it with fresh settings.

    cluster and internal_port are for tests running several processes' apps in one (cluster.py, internal.py);
    otherwise they come from AI_PROXY_REDIS_URL and AI_PROXY_INTERNAL_PORT.
    """
    settings = settings or Settings.load()
    runtime = Runtime(settings, cluster)
    roles.check(runtime.roles, roles.workers_from_environment(), runtime.cluster.scaled)
    runtime.restart_hook = restart
    runtime.address_pinned = address_pinned
    if internal_port is None and os.getenv("AI_PROXY_INTERNAL_PORT"):
        internal_port = int(os.environ["AI_PROXY_INTERNAL_PORT"])
    if roles.workers_from_environment() > 1 and internal_port is not None:
        # Several workers in one pod can't share one internal port: each takes one of its own and says which.
        internal_port = 0

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        settings.log_dir.joinpath("proxy").mkdir(parents=True, exist_ok=True)
        _configure_logging(settings.log_dir / "proxy" / "proxy.log")
        listener = await _start_internal(app, runtime, internal_port) if internal_port is not None else None
        await runtime.cluster.start()
        # A long reply may be silent for minutes (a model loading), but a full pool shouldn't make requests
        # queue silently for that long. Connections are kept 30 s, so an agent's next call after a tool
        # runs reuses its tunnel through the machine instead of asking for a new one.
        runtime.client = upstream_client(
            timeout=httpx.Timeout(600, connect=10, pool=30),
            limits=httpx.Limits(max_connections=400, max_keepalive_connections=100, keepalive_expiry=30),
        )
        # Hosted and gateway API providers (api_providers.py): public addresses only, pinned; one client verifying
        # TLS and one for the providers a site admin set not to (verify_tls: false).
        runtime.api_clients = {
            verify: upstream_client(transport=ProviderTransport(verify=verify), trust_env=False, timeout=httpx.Timeout(600, connect=10, pool=30))
            for verify in (True, False)
        }
        if settings.intercept.enabled:
            runtime.intercept_client = upstream_client(
                timeout=httpx.Timeout(600, connect=10), transport=InterceptTransport(), trust_env=False,
            )
        LOGGER.info("proxy started instance=%s", runtime.proxy_instance_id)
        if settings.intercept.enabled and "intercept" in runtime.roles:
            runtime.intercept = InterceptProxy(runtime)
            await runtime.intercept.start()
        tasks = [asyncio.ensure_future(_flush_terminals(runtime)), asyncio.ensure_future(runtime.live.run())]
        if "worker" in runtime.roles:
            # Jobs one process runs at a time (each takes a lease first): retention, audit checkpoints and exports.
            tasks.append(asyncio.ensure_future(runtime.prune_daily()))
            tasks += runtime.jobs()
        yield
        runtime.draining = True
        for task in tasks:
            task.cancel()
        if runtime.intercept:
            await runtime.intercept.stop()
        assert runtime.client
        await runtime.client.aclose()
        if runtime.intercept_client:
            await runtime.intercept_client.aclose()
        for client in runtime.api_clients.values():
            await client.aclose()
        runtime.auth.client.close()
        runtime.audit.close()
        await runtime.cluster.close()
        if listener is not None:
            await listener.stop()
        runtime.sessions.close()
        # Writes still queued (the last requests' records) go out before the database closes.
        runtime.db.close()
        LOGGER.info("proxy stopped instance=%s", runtime.proxy_instance_id)

    # /docs (Swagger UI), /redoc and /openapi.json; openapi.install shapes the document.
    app = FastAPI(
        title="agent-server API", version=version.current(), lifespan=lifespan,
        swagger_ui_parameters={"persistAuthorization": True, "docExpansion": "none", "filter": True},
    )
    openapi.install(app, version.current())
    app.state.runtime = runtime
    # Which routes each register() added, so the roles this process doesn't serve can be dropped (roles.py).
    modules: list[tuple[str, int, int]] = []

    def registered(name: str, register: Any, *args: Any) -> None:
        start = len(app.router.routes)
        register(app, *args)
        modules.append((name, start, len(app.router.routes)))

    # Sign-in, once accounts exist. Added first so CORS (added next) runs outside it.
    app.add_middleware(AuthMiddleware, runtime=runtime)
    # Before sign-in: an agent or agent-machine too old (or new) for this server hears how to update.
    app.add_middleware(version.VersionMiddleware)
    # Cross-origin consoles keep their session cookie on the API host, not on the parent domain.
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.console_origins,
        allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
        # Ask a model follows a reply whose connection dropped by its request's id.
        expose_headers=["X-Request-ID", "X-Session-ID"],
    )
    registered("auth", auth.register, runtime)
    registered("internal", internal.register, runtime)
    _register_ready(app, runtime)
    # Every console route before compat: its /api/{path} catch-all forwards agents' native Ollama calls.
    # Before management: /api/sessions/{id}/provider-credential sits beside its session routes.
    registered("provider_accounts", provider_accounts.register, runtime)
    registered("providers", providers.register, runtime)
    registered("governance", governance.register, runtime)
    registered("api_providers", api_provider_routes.register, runtime)
    registered("management", management.register, runtime)
    registered("terminals", terminals.register, runtime)
    registered("proxy", proxy.register, runtime)
    registered("containers", containers.register, runtime)
    registered("machines", machines.register, runtime)
    registered("mcp", mcp.register, runtime)
    registered("compat", compat.register, runtime)
    _register_downloads(app, Downloads(settings.dist_dir))
    _register_help(app, settings.docs_dir)
    if settings.serve_ui and "api" in runtime.roles:
        _register_ui(app, settings.ui_dir)
    roles.filter_routes(app, modules, runtime.roles)
    # A viewer of a terminal another process hosts goes to that process (before sign-in: it signs in there).
    app.add_middleware(internal.TerminalForward, runtime=runtime)
    # What requests did that the audit log keeps (audit.py): outside sign-in, so it sees who and what was refused.
    app.add_middleware(AuditMiddleware, runtime=runtime)
    # /internal/* only through the internal port.
    app.add_middleware(internal.InternalGate)
    # Added last, so outermost (outside the SessionMiddleware auth.register adds): it records what every layer answered.
    app.add_middleware(AccessLogMiddleware, log=runtime.access_log)
    return app


def _register_ready(app: FastAPI, runtime: Runtime) -> None:
    @app.get("/ready", include_in_schema=False)
    async def ready() -> Any:
        """Whether this process should be sent traffic: its database, shared store and blob storage answer,
        and it isn't shutting down. /health only says it runs."""
        checks = {"database": await asyncio.to_thread(runtime.db.ping), "cluster": runtime.cluster.ready()}
        if runtime.blobs is not None:
            checks["blobs"] = await asyncio.to_thread(runtime.blobs.ready)
        ok = all(item[0] for item in checks.values()) and not getattr(runtime, "draining", False)
        body = {"ready": ok, "pod": runtime.cluster.pod_id, "roles": sorted(runtime.roles), "checks": {name: {"ok": item[0], "detail": item[1]} for name, item in checks.items()}}
        return JSONResponse(body, status_code=200 if ok else 503)


class _Listener:
    """The internal port's server, run beside the public one in the same event loop."""

    def __init__(self, server: Any, task: Any):
        self.server = server
        self.task = task

    async def stop(self) -> None:
        self.server.should_exit = True
        try:
            await asyncio.wait_for(self.task, 10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self.task.cancel()
        await self.server.shutdown()


async def _start_internal(app: FastAPI, runtime: Runtime, port: int) -> _Listener:
    """Listen on the internal port (internal.py) and tell the other processes where (its heartbeat)."""
    host = os.getenv("AI_PROXY_INTERNAL_HOST") or "0.0.0.0"
    config = uvicorn.Config(internal.InternalEntry(app, runtime), host=host, port=port, lifespan="off", access_log=False, log_level="warning")
    config.load()
    server = uvicorn.Server(config)
    server.lifespan = config.lifespan_class(config)
    await server.startup()
    bound = server.servers[0].sockets[0].getsockname()[1] if server.servers else port
    address = os.getenv("AI_PROXY_POD_IP") or ("127.0.0.1" if host in ("0.0.0.0", "127.0.0.1", "::") else host)
    runtime.cluster.internal_url = f"http://{address}:{bound}"
    return _Listener(server, asyncio.ensure_future(server.main_loop()))


async def _flush_terminals(runtime: Runtime) -> None:
    """Shared terminals' quiet output goes to the database within a few seconds, not only when more comes."""
    while True:
        await asyncio.sleep(2)
        runtime.terminals.flush_quiet()


def _register_downloads(app: FastAPI, downloads: Downloads) -> None:
    """The builds of agent, agent-machine and the Android app for this server's version: public, like /health."""

    @app.get("/downloads/index.json")
    async def download_index() -> dict[str, Any]:
        """Every file of the build the server hands out, with its platform, kind, size and SHA-256."""
        return downloads.index()

    @app.get("/install.sh", response_class=PlainTextResponse)
    @app.get("/install.ps1", response_class=PlainTextResponse)
    async def install_script(request: Request) -> PlainTextResponse:
        """The one-line installer: curl -fsSL <server>/install.sh | sh, or irm <server>/install.ps1 | iex in PowerShell."""
        kind = "ps1" if request.url.path.endswith(".ps1") else "sh"
        try:
            text = installer.script(kind, str(request.base_url), downloads)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return PlainTextResponse(text, media_type="text/x-shellscript" if kind == "sh" else "text/plain")

    @app.get("/uninstall.sh", response_class=PlainTextResponse)
    @app.get("/uninstall.ps1", response_class=PlainTextResponse)
    async def uninstall_script(request: Request) -> PlainTextResponse:
        """The uninstaller: curl -fsSL <server>/uninstall.sh | sh, or irm <server>/uninstall.ps1 | iex in PowerShell. Needs no build."""
        kind = "ps1" if request.url.path.endswith(".ps1") else "sh"
        return PlainTextResponse(installer.uninstaller(kind), media_type="text/x-shellscript" if kind == "sh" else "text/plain")

    @app.get("/android", include_in_schema=False)
    @app.get("/downloads/android.apk")
    async def android_app() -> RedirectResponse:
        """The Android app of the build the server hands out: a short address to type on a phone."""
        app_file = next((item for item in downloads.index()["files"] if item["kind"] == "android"), None)
        if app_file is None:
            raise HTTPException(status_code=404, detail="This server has no Android app to hand out. Build it with scripts/build-dist.sh (it needs the Android SDK and a JDK).")
        return RedirectResponse(app_file["url"], status_code=302)

    @app.get("/downloads/{wanted}/{path:path}")
    async def download(wanted: str, path: str) -> FileResponse:
        target = downloads.file(wanted, path)
        if target is None:
            raise HTTPException(status_code=404, detail="Not one of this server's downloads. See /downloads/index.json.")
        return FileResponse(target, filename=target.name, media_type="application/octet-stream")


# The documentation's files the console's Docs page reads: its Markdown pages, and the images they show.
HELP_FILES = {".md": "text/markdown; charset=utf-8", ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml"}


def _register_help(app: FastAPI, docs_dir: Path) -> None:
    """docs/ for the console's Docs page (/help): public, like the downloads, as it holds nothing about this server."""

    @app.get("/help-files/{path:path}")
    async def help_file(path: str) -> FileResponse:
        root = docs_dir.resolve()
        candidate = (root / path).resolve()
        # Only the documentation's own pages and pictures, and nothing outside docs/ (no "..", no links out).
        if candidate.suffix.lower() not in HELP_FILES or not candidate.is_relative_to(root) or not candidate.is_file():
            raise HTTPException(status_code=404, detail="Not one of the documentation's pages. See /help.")
        # Checked again every time, so a page changed by a deploy shows at once.
        return FileResponse(candidate, media_type=HELP_FILES[candidate.suffix.lower()], headers=NO_CACHE)


# index.html names the build's assets, so a browser holding on to an old one runs the old console, and
# its lazy chunks (mermaid.js, the terminal) are gone after a rebuild. It's checked again every time
# (an ETag makes that cheap); the assets have the build in their names and never change.
NO_CACHE = {"Cache-Control": "no-cache"}


class _Assets(StaticFiles):
    def file_response(self, *args: Any, **kwargs: Any) -> Any:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


def _register_ui(app: FastAPI, ui_dir: Path) -> None:
    index = ui_dir / "index.html"
    headers = {**NO_CACHE, "Content-Security-Policy": "frame-ancestors 'none'; object-src 'none'; base-uri 'self'",
               "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin"}
    if (ui_dir / "assets").is_dir():
        app.mount("/assets", _Assets(directory=ui_dir / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    async def ui_root() -> Any:
        return FileResponse(index, headers=headers) if index.exists() else HTMLResponse(UI_MISSING, headers=headers)

    # Registered last so API and compatibility routes take precedence; anything
    # else is a client-side route in the single-page app.
    @app.get("/{path:path}", include_in_schema=False)
    async def ui_fallback(path: str) -> Any:
        candidate = (ui_dir / path).resolve()
        if candidate.is_file() and candidate.is_relative_to(ui_dir.resolve()):
            return FileResponse(candidate, headers=headers)
        if path.startswith(("api/", "v1/", "session/", "anthropic/", "downloads/", "help-files/")) or not index.exists():
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(index, headers=headers)


class _JsonFormatter(logging.Formatter):
    """One JSON object a line (AI_PROXY_LOG_FORMAT=json), for log collectors."""

    def format(self, record: logging.LogRecord) -> str:
        import json

        entry = {
            "time": _utc(record.created),
            "level": record.levelname.lower(), "logger": record.name, "message": record.getMessage(),
            "pod": os.getenv("AI_PROXY_POD_ID") or None,
        }
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def _utc(seconds: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _configure_logging(path: Path) -> None:
    """The server's log: logs/proxy/proxy.log, or standard output (AI_PROXY_LOG_STDOUT=1, as in containers);
    plain lines, or JSON ones (AI_PROXY_LOG_FORMAT=json)."""
    if LOGGER.handlers:
        return
    LOGGER.setLevel(logging.INFO)
    handler: logging.Handler = logging.StreamHandler(sys.stdout) if os.getenv("AI_PROXY_LOG_STDOUT") == "1" else logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(_JsonFormatter() if os.getenv("AI_PROXY_LOG_FORMAT") == "json" else logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(handler)


def main() -> None:
    if sys.argv[1:2] and sys.argv[1] in {"user", "team", "org"}:
        raise SystemExit(account_command(sys.argv[1], sys.argv[2:]))
    if sys.argv[1:2] and sys.argv[1] in {"config", "db", "vault", "audit"}:
        from . import commands

        raise SystemExit(commands.main(sys.argv[1], sys.argv[2:]))
    if sys.argv[1:2] == ["local"]:
        from . import local

        raise SystemExit(local.main(sys.argv[2:]))
    parser = argparse.ArgumentParser(description="Run the agent-server: the console, accounts and routing for every agent machine's agent-proxy (users and organizations: agent-server user|org|team --help; its settings and database: agent-server config|db --help; a whole server on this computer: agent-server local --help)")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--reload", action="store_true", help="Restart on code changes (development)")
    parser.add_argument("--no-build", action="store_true", help="Serve ui/dist as is, without rebuilding it when the UI sources changed")
    args = parser.parse_args()
    build = not args.no_build and os.getenv("AI_PROXY_UI_BUILD", "1") != "0"
    workers = roles.workers_from_environment()
    if workers > 1 and not args.reload:
        # Several worker processes (AI_PROXY_WORKERS, with AI_PROXY_REDIS_URL): each builds its own app. No
        # console restarts: settings apply in every process as they change, and a new listen address needs a rollout.
        settings = Settings.load()
        roles.check(roles.from_environment(), workers, bool(os.getenv("AI_PROXY_REDIS_URL")))
        if "worker" in roles.from_environment():
            ensure_first_user(settings)
        settings.db.close()
        uvicorn.run(
            "agent_server.app:create_app", factory=True, host=args.host or settings.host, port=args.port or settings.port,
            workers=workers, access_log=False, timeout_graceful_shutdown=_graceful(), **_tls(settings),
        )
        return
    if args.reload:
        # Development: uvicorn restarts on code changes, so the console's restart is off.
        settings = Settings.load()
        settings.host = args.host or settings.host
        if build and settings.serve_ui:
            ensure_built(settings.ui_dir)
        ensure_first_user(settings)
        uvicorn.run(
            "agent_server.app:create_app", factory=True, host=settings.host, port=args.port or settings.port,
            reload=True, access_log=False, **_tls(settings),
        )
        return
    Supervisor().serve(Settings.load, build=build, host=args.host, port=args.port)


FIRST_USER = "admin"
# The lock a single-node server on PostgreSQL holds while it runs (db.Database.hold_lock): a second one refuses to start.
SINGLE_NODE_LOCK = 7_406_100


def single_node(settings: Settings) -> None:
    """Without AI_PROXY_REDIS_URL, state is kept in this process alone: refuse to be the second such process on
    one PostgreSQL database (SQLite is one file on one computer anyway)."""
    if os.getenv("AI_PROXY_REDIS_URL"):
        return
    db = settings.open_database()
    if not db.hold_lock(SINGLE_NODE_LOCK):
        raise SystemExit(
            "agent-server: another agent-server is running on this database without AI_PROXY_REDIS_URL.\n"
            "  One process keeps its sessions, machines and terminals in memory, so a second one would serve them wrong.\n"
            "  To run several, give every one the same AI_PROXY_REDIS_URL and AI_PROXY_INTERNAL_TOKEN (docs/scaling.md)."
        )


def _graceful() -> int:
    """How long open connections (streaming replies, machines' sockets) get to finish on shutdown, in seconds."""
    try:
        return max(1, int(os.getenv("AI_PROXY_GRACEFUL_SECONDS") or 5))
    except ValueError:
        return 5


def ensure_first_user(settings: Settings) -> Path | None:
    """Bootstrap an account on first start; OIDC remains mandatory even with no users."""
    environment_user = os.getenv("AI_PROXY_USERNAME") and os.getenv("AI_PROXY_PASSWORD")
    store = UserStore(settings.open_database())
    if environment_user:
        return None
    if store.initialized():
        if not any(user["role"] == "admin" and user["tenant"] == ROOT_TENANT for user in store.user_list()):
            print(
                "agent-server: no site admin (an admin of the root organization) is left, so nobody can change the server's settings.\n"
                "  Make one with: agent-server user add <name> --admin",
                file=sys.stderr, flush=True,
            )
        return None
    password = secrets.token_urlsafe(12)
    store.add_user(FIRST_USER, password)
    # The one thing kept as a file: for the operator to read once, then delete.
    saved = settings.users_path.with_name("first-user-password")
    remote.write_secret(saved, password)
    print(
        "agent-server: OIDC sign-in is required on every interface.\n"
        f"  Created the user '{FIRST_USER}', an admin, with the password {password} (saved in {saved}).\n"
        "  Start agent-auth, sign in through the console, reset this password with agent-server user passwd admin, then delete that file.\n"
        f"  Sign each agent machine in once with: agent login {settings.public_url or settings.proxy_url}\n"
        f"  Machines that supply models join with: agent-machine login {settings.public_url or settings.proxy_url}, then agent-machine start",
        file=sys.stderr, flush=True,
    )
    return saved


def _tls(settings: Settings) -> dict[str, str]:
    return {"ssl_certfile": str(settings.tls_cert), "ssl_keyfile": str(settings.tls_key)} if settings.tls_cert and settings.tls_key else {}


class Supervisor:
    """Runs the proxy and, when the console asks, serves it again with the stored configuration.

    The restart happens in this process, so it keeps its console window and PID on
    every platform. Each round reloads the stored settings, rebuilds the console if its
    sources changed, and binds the listen address from the config (unless --host or
    --port fix it). Python code changes still need a full restart of agent-server.
    """

    def __init__(self) -> None:
        self.server: uvicorn.Server | None = None
        self.restart_requested = False
        self.stopped = False

    def restart(self) -> None:
        self.restart_requested = True
        if self.server:
            self.server.should_exit = True

    def stop(self) -> None:
        self.stopped = True
        if self.server:
            self.server.should_exit = True

    def serve(self, load: Any, build: bool = True, host: str | None = None, port: int | None = None) -> None:
        while not self.stopped:
            settings = load()
            if build and settings.serve_ui:
                ensure_built(settings.ui_dir)
            settings.host = host or settings.host
            settings.port = port or settings.port
            single_node(settings)
            # Bootstrap is independent of the listen address; authentication is always required. With roles, the
            # worker does it (one of the processes, whichever starts first: it's done once).
            if "worker" in roles.from_environment():
                ensure_first_user(settings)
            app = create_app(settings, restart=self.restart, address_pinned=bool(host or port))
            # Connections still open after this long are closed, so a restart can't hang.
            config = uvicorn.Config(app, host=settings.host, port=settings.port, access_log=False, timeout_graceful_shutdown=_graceful(), **_tls(settings))
            self.server = uvicorn.Server(config)
            self.restart_requested = False
            self.server.run()
            if not self.restart_requested:
                return
            print("agent-server: restarting with the latest stored configuration", file=sys.stderr, flush=True)


def account_command(kind: str, argv: list[str]) -> int:
    """agent-server user|team|org ...: users, organizations and their teams, from the server's shell."""
    parser = argparse.ArgumentParser(
        prog=f"agent-server {kind}",
        description="Users, organizations and their teams. Everyone signs in; the first start made an admin.",
    )
    commands = parser.add_subparsers(dest="action", required=True)
    if kind == "user":
        add = commands.add_parser("add", help="Add a user (the first one is an admin)")
        add.add_argument("name")
        add.add_argument("--admin", action="store_true", help="An admin of their organization; without --org, a site admin (the root organization), who runs the server and its organizations")
        add.add_argument("--org", action="append", default=[], help="The one organization (tenant) they belong to, for good (created if new); members need one")
        add.add_argument("--team", action="append", default=[], help="A team of their organization to join, as <org>/<team> or just <team> (created if new); repeat for more")
        passwd = commands.add_parser("passwd", help="Set a user's password (signs them out everywhere)")
        passwd.add_argument("name")
        remove = commands.add_parser("remove", help="Remove a user")
        remove.add_argument("name")
        commands.add_parser("list", help="List users")
    elif kind == "team":
        add = commands.add_parser("add", help="Add a team to an organization")
        add.add_argument("org")
        add.add_argument("id")
        add.add_argument("--name", help="Display name")
        remove = commands.add_parser("remove", help="Remove a team")
        remove.add_argument("org")
        remove.add_argument("id")
        listing = commands.add_parser("list", help="List teams and their members")
        listing.add_argument("org", nargs="?")
    else:
        add = commands.add_parser("add", help="Add an organization")
        add.add_argument("id")
        add.add_argument("--name", help="Display name")
        rename = commands.add_parser("rename", help="Rename an organization")
        rename.add_argument("id")
        rename.add_argument("name")
        remove = commands.add_parser("remove", help="Remove an organization that has no users left")
        remove.add_argument("id")
        commands.add_parser("list", help="List organizations and their members")
    args = parser.parse_args(argv)
    settings = Settings.load()
    store = UserStore(settings.open_database())
    try:
        if kind == "org" and args.action == "list":
            orgs = store.orgs()
            print("\n".join(f"{org['id']} ({org['name']}): {', '.join(org['members']) or 'no members'}" for org in orgs) if orgs else "No organizations.")
        elif kind == "org" and args.action == "add":
            print(f"Added organization {store.add_org(args.id, args.name)}.")
        elif kind == "org" and args.action == "rename":
            store.rename_org(args.id, args.name)
            print(f"{args.id} is now called {store.org(args.id)['name']}.")
        elif kind == "org":
            store.remove_org(args.id)
            print(f"Removed organization {args.id}.")
        elif kind == "team" and args.action == "list":
            teams = [team for org in store.orgs() if args.org in (None, org["id"]) for team in org["teams"]]
            print("\n".join(f"{team['key']}: {', '.join(team['members']) or 'no members'}" for team in teams) if teams else "No teams.")
        elif kind == "team" and args.action == "add":
            print(f"Added team {store.add_team(args.org, args.id, args.name)}.")
        elif kind == "team":
            store.remove_team(args.org, args.id)
            print(f"Removed team {args.org}/{args.id}.")
        elif args.action == "list":
            users = store.user_list()
            print("\n".join(f"{user['name']} ({user['role']}) {' '.join(user['orgs'] + user['teams'])}".rstrip() for user in users) if users else "No users yet: the server makes an admin on its first start, or add one.")
        elif args.action == "remove":
            record = store.user(args.name)
            if record is None or not store.remove_user(args.name):
                print(f"No user called {args.name}", file=sys.stderr)
                return 1
            # Limits are keyed by name in the database, so they go here too; machines, provider
            # accounts and session ownership need the running server (remove in the console).
            UsageLimits(store.db).remove_user(record["tenant"], args.name)
            print(f"Removed {args.name}." + ("" if store.users() else " No users are left: nobody can sign in until you add one."))
        else:
            if args.action == "passwd" and args.name not in store.users():
                print(f"No user called {args.name}", file=sys.stderr)
                return 1
            if args.action == "add":
                teams = []
                for team in args.team:
                    if "/" not in team and len(args.org) != 1:
                        raise ValueError(f"Name the team as <organization>/{team}, or give one --org")
                    teams.append(team if "/" in team else f"{args.org[0]}/{team}")
            password = _new_password(args.name)
            if args.action == "passwd":
                store.set_password(args.name, password)
                print(f"Changed the password for {args.name}; they're signed out everywhere.")
            else:
                for org in {*args.org, *(team.split("/", 1)[0] for team in teams)}:
                    if store.org(org) is None:
                        store.add_org(org)
                known = {team["key"] for org in store.orgs() for team in org["teams"]}
                for team in teams:
                    if team not in known:
                        store.add_team(*team.split("/", 1))
                first = not store.users()
                user = store.add_user(args.name, password, "admin" if args.admin else None, teams, args.org)
                print(f"Added {user['name']} ({user['role']})." + (" As the first user, they're an admin." if first else ""))
    except (KeyError, ValueError) as exc:
        print(str(exc).strip("'"), file=sys.stderr)
        return 1
    return 0


def _new_password(name: str) -> str:
    # AI_PROXY_NEW_PASSWORD lets scripts set one without a prompt.
    password = os.getenv("AI_PROXY_NEW_PASSWORD")
    if password:
        return password
    password = getpass.getpass(f"Password for {name}: ")
    if getpass.getpass("Again: ") != password:
        raise ValueError("The passwords don't match.")
    return password


if __name__ == "__main__":
    main()
