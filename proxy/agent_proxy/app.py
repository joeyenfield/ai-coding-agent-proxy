from __future__ import annotations

import argparse
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import Settings
from .routes import compat, management
from .runtime import LOGGER, Runtime


UI_MISSING = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>AI Proxy</title>
<style>body{font-family:system-ui,sans-serif;max-width:640px;margin:80px auto;padding:0 24px;line-height:1.6}
code{background:#eef2f1;padding:2px 6px;border-radius:4px}</style></head><body>
<h1>AI Proxy is running</h1><p>The web UI has not been built yet. Build it once with</p>
<p><code>cd ui &amp;&amp; npm install &amp;&amp; npm run build</code></p>
<p>or run the dev server with <code>npm run dev</code> and open <code>http://localhost:5173</code>.</p>
<p>The API is available at <a href="/docs">/docs</a>.</p></body></html>"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    runtime = Runtime(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        settings.log_dir.joinpath("proxy").mkdir(parents=True, exist_ok=True)
        _configure_logging(settings.log_dir / "proxy" / "proxy.log")
        runtime.client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10))
        LOGGER.info("proxy started instance=%s", runtime.proxy_instance_id)
        yield
        assert runtime.client
        await runtime.client.aclose()
        LOGGER.info("proxy stopped instance=%s", runtime.proxy_instance_id)

    app = FastAPI(title="AI Coding Agent Proxy", version=__version__, lifespan=lifespan)
    app.state.runtime = runtime
    # The Vite dev server proxies /api itself; this only matters for a UI served elsewhere.
    app.add_middleware(
        CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"], allow_headers=["*"],
    )
    management.register(app, runtime)
    compat.register(app, runtime)
    _register_ui(app, settings.ui_dir)
    return app


def _register_ui(app: FastAPI, ui_dir: Path) -> None:
    index = ui_dir / "index.html"
    if (ui_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=ui_dir / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    async def ui_root() -> Any:
        return FileResponse(index) if index.exists() else HTMLResponse(UI_MISSING)

    # Registered last so API and compatibility routes take precedence; anything
    # else is a client-side route in the single-page app.
    @app.get("/{path:path}", include_in_schema=False)
    async def ui_fallback(path: str) -> Any:
        candidate = (ui_dir / path).resolve()
        if candidate.is_file() and candidate.is_relative_to(ui_dir.resolve()):
            return FileResponse(candidate)
        if path.startswith(("api/", "v1/", "session/", "anthropic/")) or not index.exists():
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(index)


def _configure_logging(path: Path) -> None:
    if LOGGER.handlers:
        return
    LOGGER.setLevel(logging.INFO)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the AI coding agent proxy")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--reload", action="store_true", help="Restart on code changes (development)")
    args = parser.parse_args()
    settings = Settings.load()
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    if args.reload:
        uvicorn.run("agent_proxy.app:create_app", factory=True, host=settings.host, port=settings.port, reload=True)
        return
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
