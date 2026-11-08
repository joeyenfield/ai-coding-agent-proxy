"""What one agent-server process serves: its roles (AI_PROXY_ROLES).

The server is one program. A small install runs every part of it in one process (`all`, the
default); a large one runs each role as its own Deployment, so each scales on what it carries:

  api        the console's API: settings, accounts, machines' pages, sessions, requests, downloads, the console
  gateway    agents' model calls (/v1, /anthropic, /session/<id>/...), and shared MCP servers unless mcp runs apart
  mcp        shared MCP servers' traffic only (/session/<id>/mcp/...), for a Deployment of its own
  intercept  account agents' HTTPS (WS /api/proxy/tunnel), decrypted and recorded
  fleet      machines' connections (WS /api/machines/connect) and the tunnels they dial back
  realtime   shared terminals (/api/terminals) and the live view (/api/live)
  worker     jobs one process runs at a time: retention, usage roll-ups, audit checkpoints and exports

Which role serves which route is decided here, once, by the module that registered it (and a
few paths inside them), so the ingress table in docs/scaling.md and this file say the same.
Every role also answers /health, /ready and its internal port.

Any role but `all`, or more than one uvicorn worker, needs the processes to share state:
AI_PROXY_REDIS_URL. Without it, a server on PostgreSQL also holds a lock for as long as it
runs, so a second single-node process on the same database refuses to start (check_single).
"""

from __future__ import annotations

import os
from typing import Any, Iterable


ROLES = ("api", "gateway", "mcp", "intercept", "fleet", "realtime", "worker")
EVERY = frozenset(ROLES)
# Routes every role answers.
ALWAYS = ("/health", "/ready")


class RoleError(ValueError):
    pass


def parse(value: str | None) -> frozenset[str]:
    text = (value or "all").strip().lower()
    if text in ("", "all"):
        return EVERY
    roles = {item.strip() for item in text.split(",") if item.strip()}
    unknown = roles - EVERY - {"all"}
    if unknown:
        raise RoleError(f"Unknown role {', '.join(sorted(unknown))}: AI_PROXY_ROLES takes all, or some of {', '.join(ROLES)}")
    return EVERY if "all" in roles else frozenset(roles)


def from_environment() -> frozenset[str]:
    return parse(os.getenv("AI_PROXY_ROLES"))


def workers_from_environment() -> int:
    try:
        return max(1, int(os.getenv("AI_PROXY_WORKERS") or 1))
    except ValueError as exc:
        raise RoleError("AI_PROXY_WORKERS is a number of processes") from exc


def check(roles: frozenset[str], workers: int, scaled: bool) -> None:
    """Refuse a split, or several workers, that would keep state in one process only."""
    if scaled:
        return
    if roles != EVERY:
        raise RoleError("Running some roles only (AI_PROXY_ROLES) needs AI_PROXY_REDIS_URL: the roles share state through it")
    if workers > 1:
        raise RoleError("More than one worker (AI_PROXY_WORKERS) needs AI_PROXY_REDIS_URL: workers are separate processes")


# The module each route came from, to its role. Paths inside a module that belong elsewhere first.
PATH_ROLES: tuple[tuple[str, str], ...] = (
    ("/api/machines/connect", "fleet"),
    ("/api/machines/tunnel/", "fleet"),
    ("/api/live", "realtime"),
    ("/api/proxy/tunnel", "intercept"),
    # A linked server's socket (links.py) is held like a machine's; its relayed model calls are the gateway's.
    ("/api/links/connect", "fleet"),
    ("/api/links/relay/provider/", "gateway"),
    ("/api/links/relay/mcp/", "gateway"),
)
MODULE_ROLES = {
    "compat": "gateway",
    "mcp": "mcp",
    "terminals": "realtime",
    "proxy": "intercept",
}


def role_of(path: str, module: str) -> str:
    if path in ALWAYS:
        return "*"
    if path.startswith("/internal/") or path == "/metrics":
        return "*"
    if module == "mcp" and path.startswith("/api/"):
        # The console's list of MCP servers (GET /api/mcp); agents' MCP traffic is under /session/.
        return "api"
    for prefix, role in PATH_ROLES:
        if path == prefix or path.startswith(prefix.rstrip("/") + "/") or path == prefix.rstrip("/"):
            return role
    return MODULE_ROLES.get(module, "api")


def keep(path: str, module: str, roles: Iterable[str]) -> bool:
    role = role_of(path, module)
    if role == "*":
        return True
    # compat's bare /api/{path} (agents' native Ollama calls) arrives with the console's /api/* at the api role.
    if module == "compat" and path.startswith("/api/") and "api" in roles:
        return True
    # MCP traffic is the gateway's too, so a gateway alone serves everything agents call.
    if role == "mcp" and "gateway" in roles:
        return True
    return role in roles


def filter_routes(app: Any, modules: list[tuple[str, int, int]], roles: frozenset[str]) -> None:
    """Drop the routes this process's roles don't serve. modules: (name, first route, end) as registered."""
    if roles == EVERY:
        return
    module_of: dict[int, str] = {}
    for name, start, end in modules:
        for index in range(start, end):
            module_of[index] = name
    kept = []
    for index, route in enumerate(app.router.routes):
        path = getattr(route, "path", "") or ""
        if keep(path, module_of.get(index, "api"), roles):
            kept.append(route)
    app.router.routes[:] = kept
