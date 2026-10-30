"""The project's version, and the protocol level the server and the Go programs agree on.

VERSION at the repository root holds X.Y.Z; it's the only place a version is written
by hand. Every component's build version comes from it the same way (tools/dist
computes it for the Go programs and the Android app, this module for the server):

  - X.Y.Z when HEAD is tagged exactly vX.Y.Z and the tree is clean;
  - X.Y.Z+<short commit> otherwise, with .dirty when tracked files have uncommitted changes;
  - X.Y.Z+unknown without git;
  - AI_PROXY_VERSION, when set, wins (Docker images, CI).

PROTOCOL is the API level, raised only for a change older programs can't follow.
`agent` and `agent-machine` send theirs with every request (X-AI-Proxy-Protocol); the
server accepts MIN_PROTOCOL..PROTOCOL and answers 426 to anything else, with where to
get a build that fits (VersionMiddleware). Keep it equal to settings.Protocol in
client/ and machine/ (test_client.py checks).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import ROOT


PROTOCOL = 2
MIN_PROTOCOL = 2
VERSION_HEADER = "x-ai-proxy-version"
PROTOCOL_HEADER = "x-ai-proxy-protocol"
CLOSE_UPGRADE = 4426
BASE = re.compile(r"\d+\.\d+\.\d+")


def base_version(root: Path = ROOT) -> str:
    try:
        text = (root / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"
    return text if BASE.fullmatch(text) else "0.0.0"


def build_version(root: Path = ROOT, environ: dict[str, str] | None = None) -> str:
    """This build's version, as tools/dist names the Go programs' (keep the two in step)."""
    environ = os.environ if environ is None else environ
    if environ.get("AI_PROXY_VERSION"):
        return environ["AI_PROXY_VERSION"].strip()
    base = base_version(root)
    describe = _git(root, "describe", "--tags", "--always", "--abbrev=7")
    if describe is None:
        return f"{base}+unknown"
    dirty = is_dirty(root)
    if describe == f"v{base}" and not dirty:
        return base
    commit = _git(root, "rev-parse", "--short=7", "HEAD") or "unknown"
    return f"{base}+{commit}" + (".dirty" if dirty else "")


def is_dirty(root: Path = ROOT) -> bool:
    """Whether tracked files have uncommitted changes (untracked ones don't count). The server's
    settings are in its database, so nothing in the checkout is exempt; tools/dist/version.go agrees."""
    return _git(root, "status", "--porcelain", "--untracked-files=no", "--", ".") is not None


@lru_cache(maxsize=1)
def current() -> str:
    return build_version()


def compatible(protocol: str | int | None) -> bool | None:
    """Whether a program's protocol is one this server speaks; None when it didn't say (browsers, older tools)."""
    if protocol in (None, ""):
        return None
    try:
        level = int(protocol)
    except (TypeError, ValueError):
        return False
    return MIN_PROTOCOL <= level <= PROTOCOL


def upgrade_message(program: str, version: str, protocol: str | int | None) -> str:
    return (
        f"{program or 'This program'} {version or '(unknown version)'} speaks protocol {protocol}; "
        f"this agent-server ({current()}) needs {MIN_PROTOCOL}..{PROTOCOL}. Update it: agent update "
        "(or agent-machine update), or download it from the server's Downloads page (/get)."
    )


def public() -> dict[str, Any]:
    return {"version": current(), "protocol": PROTOCOL, "min_protocol": MIN_PROTOCOL}


class VersionMiddleware:
    """Refuses agent and agent-machine builds whose protocol this server doesn't speak, with how to update."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers") or []}
        protocol = headers.get(PROTOCOL_HEADER)
        if compatible(protocol) is not False:
            await self.app(scope, receive, send)
            return
        program = headers.get("user-agent", "").split("/", 1)[0]
        message = upgrade_message(program, headers.get(VERSION_HEADER, ""), protocol)
        if scope["type"] == "websocket":
            await receive()  # websocket.connect
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.close", "code": CLOSE_UPGRADE, "reason": message.encode()[:120].decode("utf-8", "ignore")})
            return
        body = json.dumps({"detail": message, **public(), "downloads": "/downloads/index.json"}).encode()
        await send({"type": "http.response.start", "status": 426, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def _git(root: Path, *args: str) -> str | None:
    if not (root / ".git").exists():
        return None
    try:
        done = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else None
