"""Shared agent terminals: the launcher's connection and the console's viewers.

Every endpoint here checks remote.RemoteAccess: watching a terminal shows
everything the agent prints, and typing into it can run commands on this
computer.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable

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
# How often a viewer's credential is checked with the issuer (memberships and sharing: on every event).
IDENTITY_RECHECK_SECONDS = 5.0


def register(app: FastAPI, runtime: Runtime) -> None:
    access = runtime.remote
    hub = runtime.terminals

    def require_trusted(request: Request) -> None:
        if not access.trusted(request):
            raise HTTPException(status_code=401, detail="Sign in through the OIDC authentication service.")

    # The old access-key pairing is gone: these answer as they did once it was turned off, so an
    # old console or script gets a clear refusal. Only a signed-in identity is trusted now.
    @app.get("/api/remote")
    async def remote_status(request: Request) -> dict[str, Any]:
        return {"trusted": access.trusted(request), "local": False, "paired": False, "accounts": True}

    @app.post("/api/remote/code")
    async def pairing_code() -> None:
        raise HTTPException(status_code=403, detail="Access-key pairing was removed. Sign in through OIDC.")

    @app.post("/api/remote/rotate")
    async def rotate() -> None:
        raise HTTPException(status_code=403, detail="The access key was removed. Sign in through OIDC.")

    @app.post("/api/remote/pair")
    async def pair() -> None:
        raise HTTPException(status_code=410, detail="Access-key pairing was removed. Sign in through OIDC.")

    @app.post("/api/remote/unpair")
    async def unpair(request: Request) -> JSONResponse:
        if not remote.same_origin(request):
            raise HTTPException(status_code=403, detail="Unpair from the console's own page.")
        response = JSONResponse({"paired": False})
        response.delete_cookie(remote.COOKIE)
        return response

    @app.get("/api/terminals")
    async def list_terminals(request: Request, open: bool = False) -> dict[str, Any]:
        """Every shared terminal the caller may see, ended ones too. open=1: only those whose terminal and
        session haven't ended (the console's header and alerts, which poll this, need no history)."""
        require_trusted(request)
        in_flight: dict[str, int] = {}
        for record in runtime.active.values():
            in_flight[record["session_id"]] = in_flight.get(record["session_id"], 0) + 1
        identity = auth.request_identity(request)
        agents = runtime.agents(identity)
        terminals = []
        # Off the event loop: summaries flush the write queue and read the terminals table,
        # and the console's header polls this route.
        summaries = await asyncio.to_thread(hub.summaries)
        known = {summary["session_id"] for summary in summaries}
        # Launches on a linked server's machines (bridges.py): their terminals are there, viewed through the link.
        for session in runtime.sessions.list():
            if session.tags.get("peering") == "mirror" and session.tags.get("mirror_of") and session.session_id not in known:
                summaries.append({
                    "session_id": session.session_id, "cols": 80, "rows": 24, "command": None, "started_at": session.started_at,
                    "last_output_at": session.last_activity_at, "ended_at": session.ended_at, "exit_status": None,
                    "connected": session.ended_at is None, "viewers": 0, "bytes": 0, "remote": True,
                })
        for summary in summaries:
            session = runtime.sessions.get(summary["session_id"])
            if session is None or not sharing.can_view(identity, session):
                continue
            if open and (summary.get("ended_at") or session.ended_at):
                continue
            agent = agents.get(session.client)
            machine = runtime.fleet.machine_for(session.tags)
            terminal = hub.terminals.get(session.session_id)
            terminals.append({
                **summary,
                # The fleet machine it runs on, if any; machine_name is the computer's name either way.
                "machine": machine.id if machine else None,
                "machine_name": machine.name if machine else session.tags.get("machine_name") or session.tags.get("mirror_machine"),
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
                if terminal.host is not websocket:
                    # The launcher reconnected on another socket, which resumed from the server's count:
                    # anything still arriving here would be recorded twice.
                    break
                if message.get("bytes") is not None:
                    hub.output(terminal, message["bytes"])
                elif message.get("text"):
                    renewal = _renewal(message["text"])
                    if renewal is not None:
                        # The agent-proxy's next access token, before the one it connected with expires.
                        await auth.renew_socket(runtime, websocket, renewal)
                    else:
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
            await websocket.close(code=CLOSE_UNAUTHORIZED, reason="Sign in first")
            return
        identity = auth.request_identity(websocket)
        session = runtime.sessions.get(session_id)
        if session is not None and session.tags.get("peering") == "mirror" and sharing.can_view(identity, session):
            # A launch on a linked server's machine: its terminal is there, viewed through the link (bridges.py).
            await relay_mirror_terminal(runtime, websocket, identity, session)
            return
        terminal = hub.get(session) if session and sharing.can_view(identity, session) else None
        if terminal is None:
            await websocket.close(code=CLOSE_NOT_FOUND, reason="This session has no shared terminal")
            return

        checked: dict[str, Any] = {"identity": identity, "at": time.monotonic()}

        async def permitted(control: bool = False) -> int:
            # Re-read the credential (every few seconds: the issuer isn't asked on every byte) and the
            # memberships and sharing (every time, from memory), not the connection-time snapshot.
            if time.monotonic() - checked["at"] >= IDENTITY_RECHECK_SECONDS:
                try:
                    current = await asyncio.to_thread(runtime.auth.recheck, websocket)
                except auth.AuthUnavailable:
                    return 4410
                if current is None:
                    return 4410
                checked.update(identity=current, at=time.monotonic())
            current = runtime.auth.identity_for(checked["identity"].user, checked["identity"].via)
            if current is None:
                return 4410
            check = sharing.can_control if control else sharing.can_view
            return 0 if runtime.sessions.get(session_id) is session and check(current, session) else 4403

        # A viewer that reconnects says how much it has (?from=<offset>) and gets only what it missed.
        since = websocket.query_params.get("from")
        history, queue, start, resumed = hub.subscribe(terminal, identity, int(since) if since and since.isdigit() else None)
        sender = asyncio.create_task(_send_to_viewer(websocket, terminal, history, queue, permitted, start, resumed))
        try:
            while True:
                try:
                    message = json.loads(await websocket.receive_text())
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("type") == "token":
                    # The console renews its access token before it runs out and sends it here, so the
                    # viewer outlives the token it opened with (a refused one is ignored; 4410 follows).
                    if await auth.renew_socket(runtime, websocket, message.get("token")):
                        checked["at"] = 0.0
                    continue
                if isinstance(message, dict) and message.get("type") == "input":
                    denied = await permitted()
                    if denied:
                        await websocket.close(code=denied, reason="Renew your sign-in or check terminal access")
                        return
                    # Sharing can change while someone watches, so check on every keystroke.
                    allowed = await permitted(control=True) == 0
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


def _renewal(text: str) -> Any:
    """The token in a {"type": "token"} message, else None."""
    if '"token"' not in text:
        return None
    try:
        message = json.loads(text)
    except ValueError:
        return None
    return message.get("token") if isinstance(message, dict) and message.get("type") == "token" else None


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
    websocket: WebSocket, terminal: Terminal, history: bytes, queue: asyncio.Queue, still_allowed: Callable[[], Awaitable[int]],
    start: int = 0, resumed: bool = False,
) -> None:
    """The only task that writes to a viewer's socket, so messages never interleave."""
    async def authorized() -> bool:
        denied = await still_allowed()
        if not denied:
            return True
        await websocket.close(code=denied, reason="Renew your sign-in or check terminal access")
        return False

    try:
        if not await authorized():
            return
        await websocket.send_text(json.dumps({"type": "state", "terminal": terminal.public()}))
        # Where the bytes that follow start in the output, and whether they continue what the viewer has.
        await websocket.send_text(json.dumps({"type": "replay", "start": start, "resumed": resumed}))
        for offset in range(0, len(history), REPLAY_CHUNK):
            if not await authorized():
                return
            await websocket.send_bytes(history[offset:offset + REPLAY_CHUNK])
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


async def serve_guest_viewer(
    runtime: Runtime, websocket: WebSocket, session: Any, guest: Any, still_allowed: Callable[[], bool], typing_allowed: Callable[[], bool],
) -> None:
    """A guest of a linked server watching a launch it started on this server's machine (routes/links.py, which
    checked the link, the bridge and the launch): the same history and live output a console viewer gets, and
    input only while the bridge lets that machine's terminals out."""
    hub = runtime.terminals
    terminal = hub.get(session)
    if terminal is None:
        await websocket.close(code=CLOSE_NOT_FOUND, reason="This session has no shared terminal")
        return

    async def permitted(control: bool = False) -> int:
        if not still_allowed():
            return 4403
        return 4403 if control and not typing_allowed() else 0

    since = websocket.query_params.get("from")
    history, queue, start, resumed = hub.subscribe(terminal, guest, int(since) if since and since.isdigit() else None)
    sender = asyncio.create_task(_send_to_viewer(websocket, terminal, history, queue, permitted, start, resumed))
    try:
        while True:
            try:
                message = json.loads(await websocket.receive_text())
            except ValueError:
                continue
            if isinstance(message, dict) and message.get("type") == "input":
                if await permitted():
                    await websocket.close(code=4403, reason="The bridge no longer lets this out")
                    return
                error = await hub.send_input(terminal, message) if await permitted(control=True) == 0 else "This machine's terminals aren't shared to type into over this bridge."
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


async def relay_mirror_terminal(runtime: Runtime, websocket: WebSocket, identity: Any, session: Any) -> None:
    """The console viewing a launch on a linked server's machine: piped to that server's terminal over the link,
    with the viewer's own sign-in and the session's sharing re-checked here as for any terminal."""
    from .. import routing
    from ..links import PEER_PREFIX, LinkDown

    bridges = runtime.bridges
    bridge = bridges.bridges.get(str(session.tags.get("mirror_bridge") or ""))
    link = bridges.link_of(bridge) if bridge else None
    source = session.tags.get("mirror_of")
    try:
        machine = routing.parse(str(session.tags.get("mirror_machine") or ""), "machine")
    except ValueError:
        machine = None
    if bridge is None or link is None or not source or machine is None:
        await websocket.close(code=CLOSE_NOT_FOUND, reason="This launch's terminal isn't reachable")
        return
    since = websocket.query_params.get("from")
    target = f"{PEER_PREFIX}session/{source}/terminal" + (f"?from={since}" if since and since.isdigit() else "")
    assertion = bridges.assertion(bridge, bridges.who(identity), {"kind": "launch", "org": bridge.peer_org, "machine": machine.name}, "terminal", session.session_id)
    try:
        remote = await runtime.links.websocket(link, target, assertion)
    except LinkDown as exc:
        await websocket.close(code=4502, reason=str(exc)[:120])
        return
    checked = {"at": time.monotonic()}

    async def allowed() -> bool:
        if time.monotonic() - checked["at"] < IDENTITY_RECHECK_SECONDS:
            return True
        checked["at"] = time.monotonic()
        try:
            current = await asyncio.to_thread(runtime.auth.recheck, websocket)
        except auth.AuthUnavailable:
            return False
        return current is not None and sharing.can_view(current, runtime.sessions.get(session.session_id) or session)

    async def inward() -> None:
        while True:
            kind, data = await remote.receive()
            if kind == "close":
                return
            if not await allowed():
                return
            if kind == "bytes":
                await websocket.send_bytes(data)
            else:
                await websocket.send_text(data)

    async def outward() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            text = message.get("text")
            if not text:
                continue
            renewal = _renewal(text)
            if renewal is not None:
                await auth.renew_socket(runtime, websocket, renewal)
                checked["at"] = 0.0
                continue
            current = auth.request_identity(websocket)
            if '"input"' in text and not (current is not None and sharing.can_control(current, runtime.sessions.get(session.session_id) or session)):
                continue
            await remote.send_text(text)

    tasks = [asyncio.ensure_future(inward()), asyncio.ensure_future(outward())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await remote.close()
        try:
            await websocket.close(code=1000)
        except Exception:  # noqa: BLE001 - already closed
            pass
