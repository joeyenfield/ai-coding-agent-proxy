"""The server's OpenAPI document (/openapi.json) and the docs pages that render it (/docs, /redoc).

FastAPI generates the document from the routes; this groups them by area (from the module
that registered each route), gives every operation a unique id (the agent catch-alls
answer several methods with one function, which FastAPI would name the same), declares
how to sign in so Swagger UI's "Authorize" works, and lists the WebSockets, which OpenAPI
can't describe. The pages and the document need a sign-in like the rest of the API once
accounts exist (auth.PROTECTED_PREFIXES).
"""

from __future__ import annotations

import re
import warnings
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute

from .auth import PROTECTED_PREFIXES, PUBLIC_PATHS
# The tag for each route module, in the order Swagger UI shows them.
TAGS = [
    ("Accounts", "agent_server.routes.auth", "OIDC browser sign-in, refresh and logout; organizations, their teams, and users (`/api/orgs`, `/api/users`)."),
    ("Agents and sessions", "agent_server.routes.management", "Agents, sessions, requests, traces, live traffic, the models agents can use, configuration and the server itself."),
    ("Machines", "agent_server.routes.machines", "The fleet: machines, launches on them, and their LLM providers: adopting model servers, their models, status, logs and load, Ollama's settings, starting and stopping Ollama and Strata, and model catalogue search. The server asks the machine for all of it."),
    ("MCP", "agent_server.routes.mcp",
     "MCP servers machines share (`agent-machine mcp add`), and agents' MCP traffic to them under "
     "`/session/{session_id}/mcp/{machine}/{name}` (Streamable HTTP; launch-only URL capability)."),
    ("Containers", "agent_server.routes.containers", "Strata and Ollama containers that machines run for the server."),
    ("Terminals", "agent_server.routes.terminals", "Shared agent terminals. OAuth authentication and session permissions required."),
    ("Downloads", "agent_server.app", "The builds of agent, agent-machine and the Android app this server hands out, and the documentation the console shows (`/help-files/...`); public, like /health."),
    ("Agent APIs", "agent_server.routes.compat",
     "What agents call: OpenAI (`/v1`), Anthropic (`/anthropic`) and native Ollama (`/api/...`), unscoped or under "
     "`/session/{session_id}/`. OAuth access tokens are required, including scoped sessions; MCP also needs its launch-only URL capability."),
]

WEBSOCKETS = """
## WebSockets

OpenAPI can't describe these, so they're listed here:

| Path | Who connects | What it carries |
|---|---|---|
| `/api/machines/connect` | `agent-machine run`, with its token | The fleet: the machine's hello and updates, container, server, provider and launch requests |
| `/api/proxy/tunnel` | `agent-proxy`, with the machine's token | One account agent's HTTPS connection, for the server to capture |
| `/api/machines/tunnel/{id}` | `agent-machine`, with its token, when the server asks | One connection of agents' traffic to a model server on that machine (tunnels.py) |
| `/api/terminals/{session_id}/host` | The launcher (`--control`) | The agent's terminal output, and viewers' input back to it |
| `/api/terminals/{session_id}/view` | The console's terminal page | The terminal's history and output, and what the viewer types |
"""

DESCRIPTION = """
The API of agent-server: the console uses it, and so do the launcher (`agent`), `agent-proxy` and `agent-machine`.

## Signing in

Authentication is mandatory on every interface, including loopback. Sign in through the
OIDC issuer (by default https://auth.aicontrolmesh.com). API requests need one of:

- **An OAuth access token** in `Authorization: Bearer <access_token>`. ID tokens are not API credentials.
- **An OAuth access token** in `X-AI-Proxy-Token` for agent-proxy traffic, preserving the agent's own vendor Authorization.
- **The console's sign-in cookie** (`ai_proxy_session`): signed in to the console in this browser, this page already uses it.

`agent login` and `agent-machine login` use device authorization at the issuer. Session ids
identify sessions but do not authenticate API requests. MCP also needs its launch-only URL
capability. Existing sharing and machine grants still apply. Changes to the server need an admin.
""" + WEBSOCKETS


def install(app: FastAPI, version: str) -> None:
    """Replaces app.openapi with one that builds the document above, once."""

    def openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        with warnings.catch_warnings():
            # FastAPI names every method of a route the same; _tag_and_name renames them.
            warnings.filterwarnings("ignore", message="Duplicate Operation ID")
            schema = get_openapi(
                title="agent-server API", version=version, description=DESCRIPTION.strip(),
                routes=app.routes, tags=[{"name": name, "description": text} for name, _, text in TAGS],
            )
        _tag_and_name(app, schema)
        components = schema.setdefault("components", {})
        components["securitySchemes"] = {
            "accessToken": {"type": "http", "scheme": "bearer", "description": "An OAuth access token for this API, not an OIDC ID token."},
            "proxyAccessToken": {"type": "apiKey", "in": "header", "name": "X-AI-Proxy-Token",
                                 "description": "An OAuth access token; the alternate header preserves coding agents' vendor Authorization."},
            "consoleCookie": {"type": "apiKey", "in": "cookie", "name": "ai_proxy_session",
                              "description": "Set by signing in to the console."},
        }
        schema["security"] = [{"accessToken": []}, {"proxyAccessToken": []}, {"consoleCookie": []}]
        for path, methods in schema.get("paths", {}).items():
            if path in PUBLIC_PATHS or not path.startswith(PROTECTED_PREFIXES):
                for operation in methods.values():
                    if isinstance(operation, dict):
                        operation["security"] = []
        app.openapi_schema = schema
        return schema

    app.openapi = openapi  # type: ignore[method-assign]


def _tag_and_name(app: FastAPI, schema: dict[str, Any]) -> None:
    tags = {module: name for name, module, _ in TAGS}
    routes: dict[tuple[str, str], APIRoute] = {}
    for route in app.routes:
        if isinstance(route, APIRoute) and route.include_in_schema:
            for method in route.methods:
                routes[(route.path_format, method.lower())] = route
    used: set[str] = set()
    for path, operations in schema.get("paths", {}).items():
        for method, operation in operations.items():
            route = routes.get((path, method))
            if route is None:
                continue
            operation["tags"] = [tags.get(route.endpoint.__module__, "Server")]
            # name_method_path, made unique: one function serving several methods or paths.
            base = re.sub(r"\W+", "_", f"{route.name}_{method}_{path}").strip("_")
            operation_id, n = base, 2
            while operation_id in used:
                operation_id, n = f"{base}_{n}", n + 1
            used.add(operation_id)
            operation["operationId"] = operation_id
