"""Shared MCP servers: the console's list, and launch-capability-protected agent traffic.

See mcp.py. An agent reaches a shared server at /session/<id>/mcp/<machine>/<name> with its
launch-only URL token; the server relays it to the machine's bridge through its tunnel, as
Streamable HTTP, unchanged. Every POST (a JSON-RPC message or a batch: initialize, tools/list,
tools/call, ...) is recorded in the session with what came back; the GET stream and DELETE are
passed on as they are.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .. import auth, mcp
from ..runtime import Runtime
from ..sessions import Session
from ..telemetry import RequestTelemetry, parse_json_line


LOGGER = logging.getLogger("agent-proxy")
# What goes to the machine's bridge from the agent, and back. The agent's own credentials don't:
# the machine adds what its MCP server needs.
REQUEST_HEADERS = ("content-type", "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id")
RESPONSE_HEADERS = ("mcp-session-id", "cache-control")
# The bridge's key (machine/internal/mcp KeyHeader).
KEY_HEADER = "X-AI-Proxy-MCP-Key"
# How much of a reply is kept for the trace.
TRACE_LIMIT = 2 * 1024 * 1024


def register(app: FastAPI, runtime: Runtime) -> None:
    fleet = runtime.fleet

    @app.get("/api/mcp")
    async def shared_servers(request: Request) -> list[dict[str, Any]]:
        """The MCP servers machines share that you may give the agents you start."""
        return mcp.listed(fleet, auth.request_identity(request))

    @app.api_route("/session/{session_id}/mcp/{machine_id}/{name}", methods=["GET", "POST", "DELETE"])
    async def relay(session_id: str, machine_id: str, name: str, request: Request) -> Response:
        session = runtime.sessions.get(session_id)
        if session is None or session.ended_at is not None:
            raise HTTPException(status_code=404, detail="No open session with that id")
        server_id = f"{machine_id}/{name}"
        if server_id not in (session.tags.get("mcp") or []):
            raise HTTPException(status_code=404, detail="This session wasn't given that MCP server: choose it when you start the agent (--mcp)")
        if not mcp.valid_token(session.mcp_token_hash, request.query_params.get("key")):
            raise HTTPException(status_code=404, detail="MCP access not found")
        machine = fleet.machines.get(machine_id)
        # Whoever started it (launched_by, for a launch from the console) has to be allowed still: grants change.
        user = session.tags.get("launched_by") or session.tags.get("owner")
        identity = runtime.auth.identity_for(str(user), "session") if runtime.auth.enabled and user else None
        if machine is None or (runtime.auth.enabled and identity is None) or not mcp.usable(fleet, identity, machine):
            raise HTTPException(status_code=403, detail="You may no longer use this machine's MCP servers")
        shared = machine.mcp or {}
        if not fleet.connected(machine.id) or not shared.get("port") or not machine.mcp_key:
            raise HTTPException(status_code=503, detail=f"{machine.name} isn't connected: agent-machine isn't running there")
        if not any(item["name"] == name for item in shared.get("servers") or []):
            raise HTTPException(status_code=404, detail=f"{machine.name} no longer shares an MCP server called {name}")
        # The bridge listens on the machine's loopback: through its tunnel, or directly when it's this computer.
        bridge = SimpleNamespace(name=f"{machine.name} MCP", machine=machine.id, url=f"http://127.0.0.1:{shared['port']}")
        if not runtime.tunnels.tunnelled(bridge) and not fleet.loopback(machine):
            raise HTTPException(status_code=502, detail=f"{machine.name}'s agent-machine can't carry MCP traffic: run agent-machine update there")
        target = await runtime.tunnels.base_url(bridge) + f"/mcp/{name}"
        headers = {key: value for key, value in request.headers.items() if key.lower() in REQUEST_HEADERS}
        headers[KEY_HEADER] = machine.mcp_key
        body = await request.body()
        if request.method != "POST":
            return await _pass(runtime, request.method, target, headers, body)
        return await _recorded(runtime, session, f"{machine.name}/{name}", target, headers, body)


async def _pass(runtime: Runtime, method: str, target: str, headers: dict[str, str], body: bytes) -> Response:
    """A GET stream or a DELETE, as it is: what the MCP server says outside a request's reply isn't recorded."""
    assert runtime.client
    try:
        upstream = await runtime.client.send(runtime.client.build_request(method, target, headers=headers, content=body or None), stream=True)
    except httpx.HTTPError as exc:
        return JSONResponse({"detail": f"Couldn't reach the machine's MCP server: {runtime.unavailable(exc)}"}, status_code=502)

    async def chunks() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(chunks(), status_code=upstream.status_code, headers=_headers(upstream), media_type=upstream.headers.get("content-type"))


async def _recorded(runtime: Runtime, session: Session, host: str, target: str, headers: dict[str, str], body: bytes) -> Response:
    """One POST to the MCP server, recorded with its reply: JSON, or an event stream that ends with the last response."""
    try:
        payload: Any = json.loads(body or b"null")
    except ValueError:
        payload = None
    request_id = await runtime.sessions.next_request_id(session)
    telemetry = RequestTelemetry(
        session_id=session.session_id, request_id=request_id, client=session.client, model="",
        backend=session.backend, endpoint=mcp.rpc_label(payload), request_bytes=len(body),
        kind="mcp", method="POST", host=host,
    )
    runtime.track(telemetry)
    runtime.attach(telemetry, payload)
    assert runtime.client
    try:
        upstream = await runtime.client.send(runtime.client.build_request("POST", target, headers=headers, content=body), stream=True)
    except httpx.HTTPError as exc:
        telemetry.status, telemetry.error_type = 502, type(exc).__name__
        message = runtime.unavailable(exc)
        runtime.complete(session, telemetry, payload, {"error": message})
        return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": f"Couldn't reach the machine's MCP server: {message}"}}, status_code=502)
    telemetry.status = upstream.status_code
    if upstream.status_code >= 400:
        telemetry.error_type = "upstream_error"

    async def chunks() -> AsyncIterator[bytes]:
        kept = bytearray()
        objects: list[dict[str, Any]] = []
        buffer = b""
        try:
            async for chunk in upstream.aiter_bytes():
                telemetry.saw_content()
                telemetry.response_bytes += len(chunk)
                if len(kept) < TRACE_LIMIT:
                    kept.extend(chunk[: TRACE_LIMIT - len(kept)])
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    _consume(runtime, telemetry, objects, line)
                runtime.progress(telemetry)
                yield chunk
            if buffer:
                _consume(runtime, telemetry, objects, buffer)
            if not objects and kept:
                # A JSON reply over several lines, or a batch's array.
                try:
                    whole = json.loads(bytes(kept))
                    objects = whole if isinstance(whole, list) else [whole]
                except ValueError:
                    pass
        except (asyncio.CancelledError, GeneratorExit):
            telemetry.error_type = "client_closed"
            raise
        finally:
            await upstream.aclose()
            if any(isinstance(item, dict) and item.get("error") for item in objects) and telemetry.error_type is None:
                telemetry.error_type = "mcp_error"
            response: Any = objects if len(objects) != 1 else objects[0]
            if not objects and kept:
                response = bytes(kept).decode("utf-8", "replace")
            runtime.complete(session, telemetry, payload, response)

    return StreamingResponse(chunks(), status_code=upstream.status_code, headers=_headers(upstream), media_type=upstream.headers.get("content-type"))


def _consume(runtime: Runtime, telemetry: RequestTelemetry, objects: list[dict[str, Any]], line: bytes) -> None:
    obj = parse_json_line(line)
    if obj is not None:
        objects.append(obj)
        runtime.stream_object(telemetry, obj)


def _headers(upstream: httpx.Response) -> dict[str, str]:
    return {key: value for key, value in upstream.headers.items() if key.lower() in RESPONSE_HEADERS}
