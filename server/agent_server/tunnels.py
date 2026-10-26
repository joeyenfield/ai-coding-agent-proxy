"""Agents' requests to a machine's model servers, carried by the machine itself.

The server can't always reach a machine's model server: the machine may be behind NAT,
in WSL, or on another network, with only its fleet connection out to the server. So for
a machine that can tunnel (its hello says "tunnel"), the server sends a backend's traffic
through it instead of to its address:

  - The server listens on a loopback port of its own for each (machine, model server),
    and the routes send that backend's requests there (base_url).
  - Each connection to it asks the machine, over the fleet connection, to open a tunnel
    ({"type": "tunnel", "tunnel": id, "port": port}).
  - The machine dials WS /api/machines/tunnel/<id> back, connects to 127.0.0.1:<port> on
    its side, and passes the bytes both ways (machine/internal/presence/tunnel.go).

It's plain TCP inside, so HTTP, streaming and keep-alive all work unchanged, and the
shared HTTP client reuses a tunnel like any other connection. A machine on this computer
is reached directly, and so are backends whose machine isn't connected or can't tunnel.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any
from urllib.parse import urlsplit

from .fleet import Fleet


LOGGER = logging.getLogger("agent-proxy")
# How long a machine may take to dial back for a tunnel.
OPEN_TIMEOUT = 10.0
CHUNK = 64 * 1024


class TunnelHub:
    def __init__(self, fleet: Fleet):
        self.fleet = fleet
        self._listeners: dict[tuple[str, int], asyncio.base_events.Server] = {}
        self._starting = asyncio.Lock()
        # Tunnels asked for and not yet dialled back: id -> (machine id, future for the WebSocket).
        self._pending: dict[str, tuple[str, asyncio.Future[Any]]] = {}
        self._open: set[asyncio.Task[Any]] = set()
        self._sockets: set[Any] = set()

    def tunnelled(self, backend: Any) -> bool:
        """Whether this backend's traffic goes through its machine rather than to its address."""
        machine = self.fleet.machines.get(backend.machine or "")
        return bool(
            machine and machine.tunnel and self.fleet.connected(machine.id) and not self.fleet.loopback(machine)
            and urlsplit(backend.url).port
        )

    async def base_url(self, backend: Any) -> str:
        """Where to send this backend's requests: a tunnel through its machine, or its own address."""
        if not self.tunnelled(backend):
            return backend.url.rstrip("/")
        parts = urlsplit(backend.url)
        key = (backend.machine, int(parts.port))
        async with self._starting:
            server = self._listeners.get(key)
            if server is None:
                machine_id, port = key
                server = await asyncio.start_server(
                    lambda reader, writer: self._serve(machine_id, port, reader, writer), "127.0.0.1", 0,
                )
                self._listeners[key] = server
        local_port = server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{local_port}{parts.path.rstrip('/')}"

    async def _serve(self, machine_id: str, port: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One connection from the HTTP client: ask the machine for a tunnel and pipe through it."""
        task = asyncio.current_task()
        if task:
            self._open.add(task)
        tunnel_id = secrets.token_urlsafe(16)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[tunnel_id] = (machine_id, future)
        try:
            socket = self.fleet.sockets.get(machine_id)
            if socket is None:
                return
            await socket.send_text(json.dumps({"type": "tunnel", "tunnel": tunnel_id, "port": port}))
            websocket, done = await asyncio.wait_for(future, OPEN_TIMEOUT)
            try:
                await _pipe(reader, writer, websocket)
            finally:
                done.set()
        except (asyncio.TimeoutError, ConnectionError, RuntimeError) as exc:
            LOGGER.warning("tunnel to %s port %s: %s", machine_id, port, exc or type(exc).__name__)
        finally:
            self._pending.pop(tunnel_id, None)
            writer.close()
            if task:
                self._open.discard(task)

    async def attach(self, tunnel_id: str, websocket: Any, identity: Any) -> bool:
        """A machine dialled back for a tunnel: hand its WebSocket over, and hold it until the pipe ends."""
        entry = self._pending.pop(tunnel_id, None)
        if entry is None:
            return False
        machine_id, future = entry
        machine = self.fleet.machines.get(machine_id)
        # Only the machine that was asked (signed in as its owner) can take its tunnel.
        if machine is None or (identity is not None and machine.owner not in (None, identity.user)) or future.done():
            if not future.done():
                future.set_exception(ConnectionError("the wrong machine dialled back"))
            return False
        done = asyncio.Event()
        self._sockets.add(websocket)
        future.set_result((websocket, done))
        try:
            await done.wait()
        finally:
            self._sockets.discard(websocket)
        return True

    async def close_all(self) -> None:
        """Before a restart: stop listening and end the tunnels; agents' next requests open new ones."""
        for server in self._listeners.values():
            server.close()
        self._listeners.clear()
        for websocket in list(self._sockets):
            try:
                await websocket.close(code=1012)
            except Exception:  # noqa: BLE001 - already closed
                pass
        for task in list(self._open):
            task.cancel()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, websocket: Any) -> None:
    async def outward() -> None:
        while True:
            data = await reader.read(CHUNK)
            if not data:
                return
            await websocket.send_bytes(data)

    async def inward() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data:
                writer.write(data)
                await writer.drain()

    tasks = [asyncio.ensure_future(outward()), asyncio.ensure_future(inward())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 - already closed
            pass
