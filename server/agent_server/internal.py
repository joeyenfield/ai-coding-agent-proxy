"""The internal port: how the server's processes reach each other (AI_PROXY_INTERNAL_PORT).

With several processes (cluster.py), some things can only be done by the one holding a connection:
asking a machine something over its fleet connection, handing it a tunnel, a shared terminal's
viewers. The others call that process here:

  POST /internal/fleet/<machine>/ask      a Fleet.ask (with ?progress=1, its progress lines then the answer)
  POST /internal/fleet/<machine>/launch   a launch, for the user named (checked here again with their permissions now)
  POST /internal/fleet/<machine>/shell    a terminal, the same way
  POST /internal/fleet/<machine>/tunnel   ask the machine to dial back for a tunnel another process asked for
  WS   /internal/tunnels/<id>             a tunnel the machine dialled back to another process, passed on
  GET  /internal/live?key=                one live request's payloads (the live view's details)
  GET  /metrics                           Prometheus metrics (the only path here without the token)

A console viewer of a terminal hosted elsewhere is passed on whole (TerminalForward): its WebSocket is
proxied to the hosting process's ordinary /api/terminals/<id>/view route on this port, with the
viewer's own credentials, so that process checks them as it would anyone's.

Every call carries AI_PROXY_INTERNAL_TOKEN (x-ai-proxy-internal); the port refuses anything without it,
and the public port refuses /internal/* outright (InternalGate). Inside the cluster is never a
credential for anything else: launches and shells name the user, whose permissions are checked here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from .cluster import FORWARDED_FOR, INTERNAL_HEADER
from .fleet import FleetError, MachineGone


LOGGER = logging.getLogger("agent-proxy")
FORWARDED_HOST = "x-ai-proxy-forwarded-host"
# Headers a proxied WebSocket doesn't pass on (the next hop makes its own).
HOP_HEADERS = {
    "host", "connection", "upgrade", "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
    "sec-websocket-accept", "content-length", INTERNAL_HEADER, FORWARDED_FOR, FORWARDED_HOST,
}


class InternalEntry:
    """The internal listener's app: checks the token, then serves the same app as the public port."""

    def __init__(self, app: Any, runtime: Any):
        self.app = app
        self.runtime = runtime

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            # The public server runs the app's lifespan; this one only answers.
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
                message = await receive()
            if message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
            return
        if scope["type"] not in ("http", "websocket"):
            return
        if scope["path"] == "/metrics" and scope["type"] == "http":
            # Off the event loop: the audit backlog gauge reads the database.
            body = (await asyncio.to_thread(metrics, self.runtime)).encode()
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain; version=0.0.4")]})
            await send({"type": "http.response.body", "body": body})
            return
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers") or []}
        if not self.runtime.cluster.authentic(headers.get(INTERNAL_HEADER)):
            await _refuse(scope, receive, send, 403, "Not one of this server's processes")
            return
        scope = dict(scope)
        state = dict(scope.get("state") or {})
        state["internal"] = True
        scope["state"] = state
        rewritten = []
        for key, value in scope.get("headers") or []:
            name = key.decode("latin-1").lower()
            if name in (INTERNAL_HEADER, FORWARDED_FOR, FORWARDED_HOST):
                continue
            if name == "host" and headers.get(FORWARDED_HOST):
                continue
            rewritten.append((key, value))
        if headers.get(FORWARDED_HOST):
            rewritten.append((b"host", headers[FORWARDED_HOST].encode("latin-1")))
        scope["headers"] = rewritten
        if headers.get(FORWARDED_FOR):
            scope["client"] = (headers[FORWARDED_FOR], 0)
        await self.app(scope, receive, send)


class InternalGate:
    """The public port never serves /internal/*: only the internal listener (InternalEntry) marks a request internal."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket") and scope["path"].startswith("/internal/") and not (scope.get("state") or {}).get("internal"):
            await _refuse(scope, receive, send, 404, "Not found")
            return
        await self.app(scope, receive, send)


class TerminalForward:
    """A viewer of a terminal another process hosts is passed on to that process, credentials and all."""

    def __init__(self, app: Any, runtime: Any):
        self.app = app
        self.runtime = runtime

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "websocket" and self.runtime.cluster.scaled and not (scope.get("state") or {}).get("internal"):
            parts = scope["path"].split("/")
            # /api/terminals/<id>/view
            if len(parts) == 5 and parts[1:3] == ["api", "terminals"] and parts[4] == "view":
                pod = self.runtime.terminals.host_of(parts[3])
                if pod:
                    await forward_websocket(self.runtime.cluster, pod, scope, receive, send)
                    return
        await self.app(scope, receive, send)


async def forward_websocket(cluster: Any, pod: str, scope: dict[str, Any], receive: Any, send: Any) -> None:
    """Proxy one WebSocket to the same path on another process's internal port."""
    import websockets
    from websockets.exceptions import ConnectionClosed, InvalidStatus

    first = await receive()
    if first["type"] != "websocket.connect":
        return
    query = scope.get("query_string", b"").decode("latin-1")
    url = cluster.url_of(pod).replace("http://", "ws://", 1).replace("https://", "wss://", 1) + scope["path"] + (f"?{query}" if query else "")
    headers: list[tuple[str, str]] = []
    host = ""
    for key, value in scope.get("headers") or []:
        name = key.decode("latin-1").lower()
        if name == "host":
            host = value.decode("latin-1")
        if name not in HOP_HEADERS:
            headers.append((name, value.decode("latin-1")))
    client = scope.get("client") or ("", 0)
    headers += [(INTERNAL_HEADER, cluster.internal_token), (FORWARDED_HOST, host), (FORWARDED_FOR, str(client[0]))]
    try:
        onward = await websockets.connect(url, additional_headers=headers, max_size=None, open_timeout=10, proxy=None)
    except InvalidStatus as exc:
        status = exc.response.status_code
        await send({"type": "websocket.close", "code": 4400 + status % 100 if status < 500 else 1011})
        return
    except Exception as exc:  # noqa: BLE001 - the hosting process went
        LOGGER.warning("couldn't pass a terminal viewer on to %s: %s", pod, exc or type(exc).__name__)
        await send({"type": "websocket.close", "code": 1013, "reason": "Try again"})
        return
    await send({"type": "websocket.accept"})

    async def outward() -> None:
        while True:
            message = await receive()
            if message["type"] == "websocket.disconnect":
                await onward.close(code=int(message.get("code") or 1000))
                return
            if message.get("text") is not None:
                await onward.send(message["text"])
            elif message.get("bytes") is not None:
                await onward.send(message["bytes"])

    async def inward() -> None:
        try:
            async for data in onward:
                if isinstance(data, bytes):
                    await send({"type": "websocket.send", "bytes": data})
                else:
                    await send({"type": "websocket.send", "text": data})
        except ConnectionClosed:
            pass
        code = onward.close_code or 1000
        await send({"type": "websocket.close", "code": code, "reason": onward.close_reason or ""})

    tasks = [asyncio.ensure_future(outward()), asyncio.ensure_future(inward())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await onward.close()


def register(app: FastAPI, runtime: Any) -> None:
    fleet = runtime.fleet

    def require_internal(request: Any) -> None:
        if not (request.scope.get("state") or {}).get("internal"):
            raise HTTPException(status_code=404, detail="Not found")

    def error(status: int, detail: str, gone: bool = False) -> JSONResponse:
        return JSONResponse({"error": {"status": status, "detail": detail, "gone": gone}}, status_code=status if status < 600 else 502)

    def here(machine_id: str) -> Any:
        machine = fleet.machines.get(machine_id)
        if machine is None or machine_id not in fleet.sockets:
            return None
        return machine

    @app.post("/internal/fleet/{machine_id}/{action}", include_in_schema=False)
    async def fleet_action(machine_id: str, action: str, request: Request, progress: int = 0) -> Any:
        require_internal(request)
        machine = here(machine_id)
        if machine is None:
            # It moved, or went: the caller looks again (a read is asked once more).
            return error(502, f"{machine_id} isn't connected to {runtime.cluster.pod_id} any more", gone=True)
        body = await request.json()
        if not isinstance(body, dict):
            return error(400, "Send a JSON object")
        try:
            if action == "ask":
                fields = body.get("fields") if isinstance(body.get("fields"), dict) else {}
                op, channel, timeout = str(body.get("op") or ""), str(body.get("channel") or "container"), body.get("timeout")
                if progress:
                    return StreamingResponse(_asked_with_progress(fleet, machine, op, timeout, channel, fields), media_type="application/x-ndjson")
                return {"answer": await fleet._ask(machine, op, timeout, channel, None, **fields)}
            if action in ("launch", "shell"):
                identity = runtime.auth.identity_for(str(body.get("user") or ""), "internal") if body.get("user") else None
                request_body = body.get("request") if isinstance(body.get("request"), dict) else {}
                if action == "launch":
                    # A guest's launch (bridges.py) was checked by the process that took the peer's request.
                    guest = body.get("guest") if isinstance(body.get("guest"), dict) else None
                    return await fleet.launch(machine, identity, request_body, str(body.get("launch") or "") or None, guest=guest)
                return await fleet.shell(machine, identity, request_body)
            if action == "tunnel":
                await fleet.send_tunnel(machine, str(body.get("tunnel") or ""), int(body.get("port") or 0))
                return {"ok": True}
        except MachineGone as exc:
            return error(exc.status, exc.detail, gone=True)
        except FleetError as exc:
            return error(exc.status, exc.detail)
        except ConnectionError as exc:
            return error(502, str(exc), gone=True)
        return error(404, "No such action")

    @app.websocket("/internal/tunnels/{tunnel_id}")
    async def tunnel(websocket: WebSocket, tunnel_id: str) -> None:
        await websocket.accept()
        if not (websocket.scope.get("state") or {}).get("internal"):
            await websocket.close(code=4404)
            return
        identity = SimpleNamespace(user=websocket.headers.get("x-ai-proxy-user") or None, tenant=websocket.headers.get("x-ai-proxy-tenant") or None)
        if not await runtime.tunnels.attach(tunnel_id, websocket, identity):
            await websocket.close(code=4404, reason="No such tunnel")

    @app.get("/internal/live", include_in_schema=False)
    async def live_details(request: Request, key: str) -> Any:
        require_internal(request)
        details = runtime.live.details(key, local=True)
        if details is None:
            return JSONResponse({"detail": "gone"}, status_code=404)
        return details


async def _asked_with_progress(fleet: Any, machine: Any, op: str, timeout: Any, channel: str, fields: dict[str, Any]) -> Any:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    task = asyncio.ensure_future(fleet._ask(machine, op, timeout, channel, queue, **fields))
    while True:
        getter = asyncio.ensure_future(queue.get())
        done, _ = await asyncio.wait({task, getter}, return_when=asyncio.FIRST_COMPLETED)
        if getter in done:
            yield json.dumps({"progress": getter.result()}) + "\n"
            continue
        getter.cancel()
        while not queue.empty():
            yield json.dumps({"progress": queue.get_nowait()}) + "\n"
        try:
            yield json.dumps({"answer": task.result()}) + "\n"
        except MachineGone as exc:
            yield json.dumps({"error": {"status": exc.status, "detail": exc.detail, "gone": True}}) + "\n"
        except FleetError as exc:
            yield json.dumps({"error": {"status": exc.status, "detail": exc.detail}}) + "\n"
        return


async def _refuse(scope: dict[str, Any], receive: Any, send: Any, status: int, message: str) -> None:
    if scope["type"] == "websocket":
        await receive()
        await send({"type": "websocket.close", "code": 4400 + status % 100})
        return
    body = json.dumps({"detail": message}).encode()
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": body})


def metrics(runtime: Any) -> str:
    """Prometheus text: what this process carries now (for the autoscalers and dashboards)."""
    cluster = runtime.cluster
    labels = f'pod="{cluster.pod_id}"'
    streams = sum(1 for record in runtime.active.values() if record.get("kind") in (None, "api", "intercept", "upgrade", "mcp"))
    values = [
        ("agent_server_up", "gauge", "1 while this process runs", 1),
        ("agent_server_open_streams", "gauge", "Agents' requests in flight here", streams),
        ("agent_server_requests_in_flight", "gauge", "Every request in flight here, tunnels included", len(runtime.active)),
        ("agent_server_fleet_sockets", "gauge", "Machines' connections held here", len(runtime.fleet.sockets)),
        ("agent_server_fleet_machines", "gauge", "Machines known to this process", len(runtime.fleet.machines)),
        ("agent_server_tunnels_open", "gauge", "Tunnels to machines' model servers open here", len(runtime.tunnels._sockets)),
        ("agent_server_terminals_hosted", "gauge", "Shared terminals whose launcher is connected here", sum(1 for item in runtime.terminals.terminals.values() if item.host is not None)),
        ("agent_server_terminal_viewers", "gauge", "Console viewers of terminals here", sum(len(item.viewers) for item in runtime.terminals.terminals.values())),
        ("agent_server_live_subscribers", "gauge", "Live views watching here", len(runtime.live.subscribers)),
        ("agent_server_sessions_cached", "gauge", "Sessions held in memory here", len(runtime.sessions._sessions)),
        ("agent_server_db_write_queue", "gauge", "Writes waiting for the database", runtime.db.writer.queue.qsize()),
        ("agent_server_cluster_processes", "gauge", "Processes of this server alive", len(cluster.pods())),
        ("agent_server_uptime_seconds", "counter", "Seconds since this process started", round(time.time() - runtime.started_at, 1)),
        ("agent_server_audit_backlog", "gauge", "Audit events not yet sent to every export", runtime.audit.backlog() if getattr(runtime, "audit", None) else 0),
    ]
    lines = []
    for name, kind, text, value in values:
        lines += [f"# HELP {name} {text}", f"# TYPE {name} {kind}", f"{name}{{{labels}}} {value}"]
    lines.append(f'agent_server_info{{{labels},version="{_version()}",roles="{",".join(sorted(getattr(runtime, "roles", ())))}"}} 1')
    return "\n".join(lines) + "\n"


def _version() -> str:
    from . import version

    return version.current()
