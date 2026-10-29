from __future__ import annotations

import argparse
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
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import installer, openapi, remote, version
from .auth import AuthMiddleware, UserStore
from .config import Settings
from .downloads import Downloads
from .intercept import InterceptProxy, InterceptTransport
from .routes import auth, compat, containers, machines, management, mcp, proxy, terminals
from .runtime import LOGGER, Runtime, upstream_client
from .ui_build import ensure_built


UI_MISSING = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>AI Control Mesh</title>
<style>body{font-family:system-ui,sans-serif;max-width:640px;margin:80px auto;padding:0 24px;line-height:1.6}
code{background:#eef2f1;padding:2px 6px;border-radius:4px}</style></head><body>
<h1>AI Control Mesh is running</h1><p>The web UI has not been built yet. Build it once with</p>
<p><code>cd ui &amp;&amp; npm install &amp;&amp; npm run build</code></p>
<p>or run the dev server with <code>npm run dev</code> and open <code>http://localhost:5173</code>.</p>
<p>The API is available at <a href="/docs">/docs</a>.</p></body></html>"""


def create_app(settings: Settings | None = None, restart: Any = None, address_pinned: bool = False) -> FastAPI:
    """The proxy's app. restart, from the supervisor, lets the console restart it with fresh settings."""
    settings = settings or Settings.load()
    runtime = Runtime(settings)
    runtime.restart_hook = restart
    runtime.address_pinned = address_pinned

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        settings.log_dir.joinpath("proxy").mkdir(parents=True, exist_ok=True)
        _configure_logging(settings.log_dir / "proxy" / "proxy.log")
        runtime.client = upstream_client(timeout=httpx.Timeout(600, connect=10))
        if settings.intercept.enabled:
            runtime.intercept_client = upstream_client(
                timeout=httpx.Timeout(600, connect=10), transport=InterceptTransport(), trust_env=False,
            )
        LOGGER.info("proxy started instance=%s", runtime.proxy_instance_id)
        if settings.intercept.enabled:
            runtime.intercept = InterceptProxy(runtime)
            await runtime.intercept.start()
        yield
        if runtime.intercept:
            await runtime.intercept.stop()
        assert runtime.client
        await runtime.client.aclose()
        if runtime.intercept_client:
            await runtime.intercept_client.aclose()
        LOGGER.info("proxy stopped instance=%s", runtime.proxy_instance_id)

    # /docs (Swagger UI), /redoc and /openapi.json; openapi.install shapes the document.
    app = FastAPI(
        title="agent-server API", version=version.current(), lifespan=lifespan,
        swagger_ui_parameters={"persistAuthorization": True, "docExpansion": "none", "filter": True},
    )
    openapi.install(app, version.current())
    app.state.runtime = runtime
    # Sign-in, once accounts exist. Added first so CORS (added next) runs outside it.
    app.add_middleware(AuthMiddleware, runtime=runtime)
    # Before sign-in: an agent or agent-machine too old (or new) for this server hears how to update.
    app.add_middleware(version.VersionMiddleware)
    # Cross-origin consoles keep their session cookie on the API host, not on the parent domain.
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.console_origins,
        allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
    )
    auth.register(app, runtime)
    # Every console route before compat: its /api/{path} catch-all forwards agents' native Ollama calls.
    management.register(app, runtime)
    terminals.register(app, runtime)
    proxy.register(app, runtime)
    containers.register(app, runtime)
    machines.register(app, runtime)
    mcp.register(app, runtime)
    compat.register(app, runtime)
    _register_downloads(app, Downloads(settings.dist_dir))
    _register_help(app, settings.docs_dir)
    if settings.serve_ui:
        _register_ui(app, settings.ui_dir)
    return app


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
    if (ui_dir / "assets").is_dir():
        app.mount("/assets", _Assets(directory=ui_dir / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    async def ui_root() -> Any:
        return FileResponse(index, headers=NO_CACHE) if index.exists() else HTMLResponse(UI_MISSING)

    # Registered last so API and compatibility routes take precedence; anything
    # else is a client-side route in the single-page app.
    @app.get("/{path:path}", include_in_schema=False)
    async def ui_fallback(path: str) -> Any:
        candidate = (ui_dir / path).resolve()
        if candidate.is_file() and candidate.is_relative_to(ui_dir.resolve()):
            return FileResponse(candidate, headers=NO_CACHE)
        if path.startswith(("api/", "v1/", "session/", "anthropic/", "downloads/", "help-files/")) or not index.exists():
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(index, headers=NO_CACHE)


def _configure_logging(path: Path) -> None:
    if LOGGER.handlers:
        return
    LOGGER.setLevel(logging.INFO)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(handler)


def main() -> None:
    if sys.argv[1:2] and sys.argv[1] in {"user", "team", "org"}:
        raise SystemExit(account_command(sys.argv[1], sys.argv[2:]))
    parser = argparse.ArgumentParser(description="Run the agent-server: the console, accounts and routing for every agent machine's agent-proxy (users and organizations: agent-server user|org|team --help)")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--reload", action="store_true", help="Restart on code changes (development)")
    parser.add_argument("--no-build", action="store_true", help="Serve ui/dist as is, without rebuilding it when the UI sources changed")
    args = parser.parse_args()
    build = not args.no_build and os.getenv("AI_PROXY_UI_BUILD", "1") != "0"
    if args.reload:
        # Development: uvicorn restarts on code changes, so the console's restart is off.
        settings = Settings.load()
        settings.host = args.host or settings.host
        if build and settings.serve_ui:
            ensure_built(settings.ui_dir)
        ensure_first_user(settings)
        uvicorn.run(
            "agent_server.app:create_app", factory=True, host=settings.host, port=args.port or settings.port,
            reload=True, **_tls(settings),
        )
        return
    Supervisor().serve(Settings.load, build=build, host=args.host, port=args.port)


FIRST_USER = "admin"


def ensure_first_user(settings: Settings) -> Path | None:
    """Create an admin on the first start that listens beyond this computer, so sign-in is on.

    Without a user, anyone who reaches the port can read the recorded traffic and drive the
    console. It only happens while users.json doesn't exist, so removing every user later
    keeps sign-in off. The password is saved beside users.json as well as printed, because
    a launcher that starts the proxy discards its output. Returns the saved file.
    """
    environment_user = os.getenv("AI_PROXY_USERNAME") and os.getenv("AI_PROXY_PASSWORD")
    if settings.host in remote.LOOPBACK_NAMES or environment_user or settings.users_path.exists():
        return None
    password = secrets.token_urlsafe(12)
    UserStore(settings.users_path).add_user(FIRST_USER, password)
    saved = settings.users_path.with_name("first-user-password")
    remote.write_secret(saved, password)
    print(
        f"agent-server: {settings.host} accepts other machines, so sign-in is now on.\n"
        f"  Created the user '{FIRST_USER}', an admin, with the password {password} (saved in {saved}).\n"
        "  Sign in to the console, change the password on Server › Organizations, then delete that file.\n"
        f"  Sign each agent machine in once with: agent login {settings.public_url or settings.proxy_url}\n"
        f"  Machines that supply models join with: agent-machine login {settings.public_url or settings.proxy_url}, then agent-machine start",
        file=sys.stderr, flush=True,
    )
    return saved


def _tls(settings: Settings) -> dict[str, str]:
    return {"ssl_certfile": str(settings.tls_cert), "ssl_keyfile": str(settings.tls_key)} if settings.tls_cert and settings.tls_key else {}


class Supervisor:
    """Runs the proxy and, when the console asks, serves it again with the configuration on disk.

    The restart happens in this process, so it keeps its console window and PID on
    every platform. Each round reloads config/*.yaml, rebuilds the console if its
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
            # Every round, so switching the listen host to 0.0.0.0 and restarting turns sign-in on too.
            ensure_first_user(settings)
            app = create_app(settings, restart=self.restart, address_pinned=bool(host or port))
            # Connections still open after this long are closed, so a restart can't hang.
            config = uvicorn.Config(app, host=settings.host, port=settings.port, timeout_graceful_shutdown=5, **_tls(settings))
            self.server = uvicorn.Server(config)
            self.restart_requested = False
            self.server.run()
            if not self.restart_requested:
                return
            print(f"agent-server: restarting with the latest configuration from {settings.backends_file.parent}", file=sys.stderr, flush=True)


def account_command(kind: str, argv: list[str]) -> int:
    """agent-server user|team|org ...: users, organizations and their teams, from the server's shell."""
    parser = argparse.ArgumentParser(
        prog=f"agent-server {kind}",
        description="Users, organizations and their teams. With at least one user, everyone signs in.",
    )
    commands = parser.add_subparsers(dest="action", required=True)
    if kind == "user":
        add = commands.add_parser("add", help="Add a user (the first one is an admin)")
        add.add_argument("name")
        add.add_argument("--admin", action="store_true", help="Can manage the server, organizations and users")
        add.add_argument("--org", action="append", default=[], help="An organization to join (created if new); repeat for more")
        add.add_argument("--team", action="append", default=[], help="A team to join, as <org>/<team> or just <team> with one --org (created if new); repeat for more")
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
        remove = commands.add_parser("remove", help="Remove an organization (its members keep their accounts)")
        remove.add_argument("id")
        commands.add_parser("list", help="List organizations and their members")
    args = parser.parse_args(argv)
    store = UserStore(Settings.load().users_path)
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
            print("\n".join(f"{user['name']} ({user['role']}) {' '.join(user['orgs'] + user['teams'])}".rstrip() for user in users) if users else "No users: sign-in is off.")
        elif args.action == "remove":
            if not store.remove_user(args.name):
                print(f"No user called {args.name}", file=sys.stderr)
                return 1
            print(f"Removed {args.name}." + ("" if store.users() else " No users are left, so sign-in is off."))
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
                print(f"Added {user['name']} ({user['role']})." + (" Sign-in is on: everyone now needs a user to use this proxy." if first else ""))
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
