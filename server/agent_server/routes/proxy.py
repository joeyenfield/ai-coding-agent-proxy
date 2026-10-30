"""The tunnel the agent-proxy on each agent machine uses for account agents' HTTPS.

Account agents send their HTTPS through the agent-proxy on their own machine. It
carries each connection here over a WebSocket on the server's own port, so the
server's intercept handler decrypts, records and forwards it as if the agent were
connected directly, and the intercept listener never has to face the network.
"""

from __future__ import annotations

from fastapi import FastAPI, WebSocket

from .. import remote
from ..runtime import Runtime
from .terminals import CLOSE_NOT_FOUND, CLOSE_UNAUTHORIZED


def register(app: FastAPI, runtime: Runtime) -> None:
    @app.websocket("/api/proxy/tunnel")
    async def tunnel(websocket: WebSocket) -> None:
        await websocket.accept()
        if not (remote.same_origin(websocket) and runtime.remote.trusted(websocket)):
            await websocket.close(code=CLOSE_UNAUTHORIZED, reason="Sign this machine in first: agent login <server>")
            return
        if runtime.intercept is None:
            await websocket.close(code=CLOSE_NOT_FOUND, reason="HTTPS interception is off on this server")
            return
        await runtime.intercept.serve_tunnel(websocket)
