"""Authenticated remote access and browser-origin checks; legacy key storage helpers."""

from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
import socket
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import psutil
from starlette.requests import HTTPConnection


COOKIE = "ai_proxy_remote"
HEADER = "x-ai-proxy-token"
# About as long as browsers keep a cookie; rotating the key signs every device out sooner.
COOKIE_MAX_AGE = 400 * 24 * 3600
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}
TAILSCALE = ipaddress.ip_network("100.64.0.0/10")
VIRTUAL_ADAPTERS = ("vethernet", "virtualbox", "vmware", "hyper-v", "docker", "podman", "wsl", "veth", "br-", "virbr", "loopback")
# A short code to type where the QR code can't be scanned. It stands in for the key,
# so it's single-use, expires, and a few wrong guesses cancel it.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I to misread
CODE_LENGTH = 8
CODE_TTL = 10 * 60
CODE_ATTEMPTS = 5


class RemoteAccess:
    """OAuth trust checks alongside compatibility helpers for obsolete pairing state."""

    def __init__(self, path: Path):
        self.path = path
        self._token: str | None = None
        self._code: tuple[str, float] | None = None
        self._failures = 0
        # Set by the runtime; authentication is always enabled.
        self.auth: Any = None

    @property
    def accounts(self) -> bool:
        return bool(self.auth and self.auth.enabled)

    @property
    def token(self) -> str:
        if self._token is None:
            self._token = os.getenv("AI_PROXY_TOKEN") or read_token(self.path, create=True) or ""
        return self._token

    def rotate(self) -> str:
        """Replace the key, which signs out every paired device."""
        self._token = secrets.token_urlsafe(32)
        self._code = None
        write_secret(self.path, self._token)
        return self._token

    def new_code(self) -> str:
        """A one-time pairing code, replacing any earlier one."""
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
        self._code, self._failures = (code, time.time() + CODE_TTL), 0
        return code

    def redeem(self, supplied: str) -> bool:
        """Whether supplied is the access key or the current pairing code, which is then spent."""
        if hmac.compare_digest(supplied.encode(), self.token.encode()):
            return True
        code, expires = self._code or ("", 0.0)
        typed = supplied.replace("-", "").replace(" ", "").upper()
        if code and time.time() < expires and hmac.compare_digest(typed.encode(), code.encode()):
            self._code = None
            return True
        self._failures += 1
        if self._failures >= CODE_ATTEMPTS:
            self._code = None
        return False

    def has_key(self, connection: HTTPConnection) -> bool:
        supplied = connection.headers.get(HEADER) or connection.cookies.get(COOKIE) or ""
        return bool(supplied) and hmac.compare_digest(supplied.encode(), self.token.encode())

    def local(self, connection: HTTPConnection) -> bool:
        if self.accounts:
            return False
        client = connection.client.host if connection.client else ""
        return _is_loopback(client) and _hostname(connection.headers.get("host", "")) in LOOPBACK_NAMES

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


def addresses(bound_host: str) -> list[dict[str, Any]]:
    """Addresses another device could use to reach a proxy bound to bound_host, likeliest first."""
    if _is_loopback(bound_host) or bound_host == "localhost":
        return []
    if bound_host not in {"0.0.0.0", "::", ""}:
        return [{"address": bound_host, "interface": None, "kind": _kind(bound_host, "")}]
    found = []
    try:
        interfaces = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
    except (OSError, RuntimeError):
        return []
    for name, entries in interfaces.items():
        if name in stats and not stats[name].isup:
            continue
        for entry in entries:
            if entry.family != socket.AF_INET:
                continue
            ip = ipaddress.ip_address(entry.address)
            if ip.is_loopback or ip.is_link_local:
                continue
            found.append({"address": entry.address, "interface": name, "kind": _kind(entry.address, name)})
    order = {"lan": 0, "tailscale": 1, "public": 2, "virtual": 3}
    return sorted(found, key=lambda item: (order[item["kind"]], item["interface"] or ""))


def _kind(address: str, interface: str) -> str:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "lan"
    if ip in TAILSCALE or "tailscale" in interface.lower():
        return "tailscale"
    if any(marker in interface.lower() for marker in VIRTUAL_ADAPTERS):
        return "virtual"
    return "lan" if ip.is_private else "public"


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _hostname(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host
