"""Shared agent terminals: the launcher's connection, the console's viewers, and pairing devices.

Every endpoint here checks remote.RemoteAccess: watching a terminal shows
everything the agent prints, and typing into it can run commands on this
computer.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .. import access as sharing, auth, remote
from ..fleet import SHELL_CLIENT
from ..runtime import Runtime
from ..terminals import Terminal


# WebSocket close codes the launcher and the console act on.
CLOSE_UNAUTHORIZED = 4401
CLOSE_NOT_FOUND = 4404
CLOSE_BEHIND = 4408
REPLAY_CHUNK = 256 * 1024
# Also revoke an idle viewer after a credential or membership changes outside this process.
ACCESS_POLL_SECONDS = 1.0


def register(app: FastAPI, runtime: Runtime) -> None:
    access = runtime.remote
    hub = runtime.terminals

    def require_trusted(request: Request) -> None:
        if not access.trusted(request):
            raise HTTPException(status_code=401, detail="Pair this device first: scan the code shown on the computer running the proxy.")

    def require_local(request: Request) -> None:
        if not (access.local(request) and remote.same_origin(request)):
            raise HTTPException(status_code=403, detail="Only the computer running the proxy can do this.")

    @app.get("/api/remote")
    async def remote_status(request: Request) -> dict[str, Any]:
        local = access.local(request)
        value: dict[str, Any] = {
            "trusted": access.trusted(request), "local": local, "paired": access.has_key(request) and not access.accounts,
            # With accounts, phones sign in instead of pairing.
            "accounts": access.accounts,
        }
        if local:
            # Only this computer sees the key, to show it as a QR code for a phone.
            value.update(
                key=access.token, listen_host=runtime.bound_host, port=runtime.bound_port,
                addresses=_addresses(runtime),
            )
        return value

    @app.post("/api/remote/code")
    async def pairing_code(request: Request) -> dict[str, Any]:
        require_local(request)
        code = access.new_code()
        return {"code": f"{code[:4]}-{code[4:]}", "expires_in": remote.CODE_TTL}

    @app.post("/api/remote/pair")
    async def pair(request: Request) -> JSONResponse:
        if not remote.same_origin(request):
            raise HTTPException(status_code=403, detail="Pair from the console's own page.")
        data = await request.json()
        key = str(data.get("key") or "").strip() if isinstance(data, dict) else ""
        if not key or not access.redeem(key):
            raise HTTPException(
                status_code=403,
                detail="That code doesn't match, or it has expired. Get a new one on the computer running the proxy, or scan its QR code again.",
            )
        response = JSONResponse({"paired": True})
        response.set_cookie(
            remote.COOKIE, access.token, max_age=remote.COOKIE_MAX_AGE, httponly=True, samesite="strict",
            secure=request.url.scheme == "https",
        )
        return response

    @app.post("/api/remote/unpair")
    async def unpair(request: Request) -> JSONResponse:
        if not remote.same_origin(request):
            raise HTTPException(status_code=403, detail="Unpair from the console's own page.")
        response = JSONResponse({"paired": False})
        response.delete_cookie(remote.COOKIE)
        return response

    @app.post("/api/remote/rotate")
    async def rotate(request: Request) -> dict[str, Any]:
        require_local(request)
        return {"key": access.rotate()}

    @app.get("/api/terminals")
    async def list_terminals(request: Request) -> dict[str, Any]:
        require_trusted(request)
        in_flight: dict[str, int] = {}
        for record in runtime.active.values():
            in_flight[record["session_id"]] = in_flight.get(record["session_id"], 0) + 1
        agents = runtime.agents()
        identity = auth.request_identity(request)
        terminals = []
        for summary in hub.summaries():
            session = runtime.sessions.get(summary["session_id"])
            if session is None or not sharing.can_view(identity, session):
                continue
            agent = agents.get(session.client)
            machine = runtime.fleet.machine_for(session.tags)
            terminal = hub.terminals.get(session.session_id)
            terminals.append({
                **summary,
                # The fleet machine it runs on, if any; machine_name is the computer's name either way.
                "machine": machine.id if machine else None,
                "machine_name": machine.name if machine else session.tags.get("machine_name"),
                "attention": hub.attention(terminal, in_flight.get(session.session_id, 0)) if terminal else None,
                "agent": session.client, "agent_name": agent.name if agent else ("Terminal" if session.client == SHELL_CLIENT else session.client),
                "project": session.project, "project_path": session.tags.get("project_path"),
                "model": session.model, "backend": session.backend,
                "session_ended_at": session.ended_at, "request_count": session.request_count,
                "last_activity_at": session.last_activity_at, "in_flight": in_flight.get(session.session_id, 0),
                "you": sharing.viewer(identity, session),
            })
        return {"terminals": terminals}

    @app.websocket("/api/terminals/{session_id}/host")
    async def terminal_host(websocket: WebSocket, session_id: str) -> None:
        """The launcher: sends what the agent prints, receives what to type into it."""
        await websocket.accept()
        if not (remote.same_origin(websocket) and access.trusted(websocket)):
            await websocket.close(code=CLOSE_UNAUTHORIZED, reason="Not allowed to share a terminal with this proxy")
            return
        session = runtime.sessions.get(session_id)
        if session is None:
            await websocket.close(code=CLOSE_NOT_FOUND, reason="Unknown session")
            return
        if not sharing.can_control(auth.request_identity(websocket), session):
            await websocket.close(code=CLOSE_UNAUTHORIZED, reason="Only the session's owner can share its terminal")
            return
        try:
            hello = json.loads(await websocket.receive_text())
        except (WebSocketDisconnect, ValueError):
            return
        terminal = hub.attach(session, websocket, hello if isinstance(hello, dict) else {})
        try:
            async with terminal.host_lock:
                await websocket.send_text(json.dumps({"type": "resume", "from": terminal.total}))
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    hub.output(terminal, message["bytes"])
                elif message.get("text"):
                    _control(hub, terminal, message["text"])
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.detach(terminal, websocket)

    @app.websocket("/api/terminals/{session_id}/view")
    async def terminal_view(websocket: WebSocket, session_id: str) -> None:
        """A console viewer: gets the history then live output, and may send input."""
        await websocket.accept()
        if not (remote.same_origin(websocket) and access.trusted(websocket)):
            await websocket.close(code=CLOSE_UNAUTHORIZED, reason="Pair this device first")
            return
        identity = auth.request_identity(websocket)
        session = runtime.sessions.get(session_id)
        terminal = hub.get(session) if session and sharing.can_view(identity, session) else None
        if terminal is None:
            await websocket.close(code=CLOSE_NOT_FOUND, reason="This session has no shared terminal")
            return
        accounts_required = identity is not None

        def permitted(control: bool = False) -> bool:
            # Re-read the credential and memberships, not the middleware's connection-time snapshot.
            if accounts_required or runtime.auth.enabled:
                current = runtime.auth.identify(websocket)
                trusted = current is not None
            else:
                current = None
                trusted = access.trusted(websocket)
            check = sharing.can_control if control else sharing.can_view
            return trusted and runtime.sessions.get(session_id) is session and check(current, session)

        history, queue = hub.subscribe(terminal, identity)
        sender = asyncio.create_task(_send_to_viewer(websocket, terminal, history, queue, permitted))
        try:
            while True:
                try:
                    message = json.loads(await websocket.receive_text())
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("type") == "input":
                    if not permitted():
                        await websocket.close(code=4403, reason="Terminal access revoked")
                        return
                    # Sharing can change while someone watches, so check on every keystroke.
                    allowed = permitted(control=True)
                    error = await hub.send_input(terminal, message) if allowed else "This session is shared with you to watch, not to type into."
                    if error:
                        try:
                            queue.put_nowait({"type": "error", "message": error})
                        except asyncio.QueueFull:
                            pass
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.unsubscribe(terminal, queue)
            sender.cancel()


def _addresses(runtime: Runtime) -> list[dict[str, Any]]:
    """Where a phone could open the console, each with its URL; a configured public URL comes first."""
    scheme = "https" if runtime.settings.tls_cert else "http"
    found = [{**item, "url": f"{scheme}://{item['address']}:{runtime.bound_port}"} for item in remote.addresses(runtime.bound_host)]
    public = runtime.settings.public_url
    if public:
        found.insert(0, {"address": urlparse(public).hostname or public, "interface": None, "kind": "configured", "url": public})
    return found


def _control(hub: Any, terminal: Terminal, text: str) -> None:
    try:
        message = json.loads(text)
    except ValueError:
        return
    if not isinstance(message, dict):
        return
    kind = message.get("type")
    if kind == "resize":
        hub.resize(terminal, message)
    elif kind == "gap" and isinstance(message.get("from"), int):
        hub.gap(terminal, message["from"])
    elif kind == "exit":
        hub.exited(terminal, message.get("status"))


async def _send_to_viewer(
    websocket: WebSocket, terminal: Terminal, history: bytes, queue: asyncio.Queue, still_allowed: Callable[[], bool],
) -> None:
    """The only task that writes to a viewer's socket, so messages never interleave."""
    async def authorized() -> bool:
        if still_allowed():
            return True
        await websocket.close(code=4403, reason="Terminal access revoked")
        return False

    try:
        if not await authorized():
            return
        await websocket.send_text(json.dumps({"type": "state", "terminal": terminal.public()}))
        for start in range(0, len(history), REPLAY_CHUNK):
            if not await authorized():
                return
            await websocket.send_bytes(history[start:start + REPLAY_CHUNK])
        if not await authorized():
            return
        await websocket.send_text(json.dumps({"type": "replayed"}))
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=ACCESS_POLL_SECONDS)
            except asyncio.TimeoutError:
                if not await authorized():
                    return
                continue
            if not await authorized():
                return
            if item is None:
                await websocket.close(code=CLOSE_BEHIND, reason="Fell behind; reconnect to catch up")
                return
            if isinstance(item, (bytes, bytearray)):
                await websocket.send_bytes(bytes(item))
            else:
                await websocket.send_text(json.dumps(item))
    except (WebSocketDisconnect, RuntimeError, asyncio.CancelledError):
        pass
    except Exception:  # noqa: BLE001 - a closed socket; the receive loop ends the viewer
        pass
