"""Terminal trust and browser-origin checks, and the owner-only secret files the server keeps.

Only a middleware-authenticated OAuth identity is trusted. Loopback and the old
access key grant nothing; pairing devices was removed with them.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path
from urllib.parse import urlparse

from starlette.requests import HTTPConnection


# The old pairing cookie, which /api/remote/unpair still clears.
COOKIE = "ai_proxy_remote"
HEADER = "x-ai-proxy-token"


class RemoteAccess:
    """Whether a connection may use the terminal endpoints: only with a signed-in identity."""

    def trusted(self, connection: HTTPConnection) -> bool:
        return connection.scope.get("state", {}).get("identity") is not None


def same_origin(connection: HTTPConnection) -> bool:
    """Browsers send Origin with WebSockets and cross-site POSTs; refuse other sites' pages.

    A separately hosted console must be explicitly trusted for both HTTP and WebSockets.
    """
    origin = connection.headers.get("origin")
    if not origin:
        return True
    app = connection.scope.get("app")
    runtime = getattr(app.state, "runtime", None) if app else None
    if runtime and origin in runtime.settings.console_origins:
        return True
    parsed = urlparse(origin)
    scheme = "https" if connection.url.scheme in {"https", "wss"} else "http"
    return (
        parsed.scheme == scheme and parsed.netloc.lower() == connection.headers.get("host", "").lower()
        and not parsed.path and not parsed.query and not parsed.fragment and not parsed.username
    )


def read_token(path: Path, create: bool = False) -> str | None:
    try:
        token = path.read_text(encoding="ascii").strip()
    except OSError:
        token = ""
    if token or not create:
        return token or None
    token = secrets.token_urlsafe(32)
    write_secret(path, token)
    return token


def write_secret(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    # Owner-only where the platform supports it; on Windows the user's profile ACLs apply.
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(token + "\n")
    temporary.replace(path)
