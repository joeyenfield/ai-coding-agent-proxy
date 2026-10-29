"""agent-proxy: the proxy on each agent machine, which the agents there register with.

It listens on this machine's loopback only and carries everything to the agent-server this
machine signed in to with `agent login <server>`:

- Its HTTP port (8184) takes the launcher's API calls (sessions, the server's CA, ...), signed
  with the machine's sign-in, and agents' model endpoints (/session/<id>/...), streamed
  through unchanged. Terminals shared with `agent --control` are relayed too.
- Its HTTPS proxy port (8183) is where account agents' HTTPS_PROXY points. Each connection
  is tunnelled to the server over a WebSocket, and the server decrypts, records and forwards it.

The same proxy as the Go client's (client/internal/localproxy), as an ASGI app served by
uvicorn, with the HTTPS proxy port on the same event loop.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import socket
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import httpx
import uvicorn
import websockets
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect as ws_connect

from .. import common
from . import settings

# The server API the launcher uses, and nothing more: any program on this machine can reach
# the proxy, and what it forwards here is signed as you.
LAUNCHER_API: List[Tuple[str, "re.Pattern[str]"]] = [
    ("GET", re.compile(r"^/api/auth/status$")),
    ("GET", re.compile(r"^/api/config$")),
    ("GET", re.compile(r"^/api/agents$")),
    ("POST", re.compile(r"^/api/agents/[^/]+/sessions$")),
    ("GET", re.compile(r"^/api/intercept(/ca\.pem|/bundle\.pem)?$")),
    ("GET", re.compile(r"^/api/remote$")),
    ("POST", re.compile(r"^/api/sessions$")),
    ("PATCH", re.compile(r"^/api/sessions/[^/]+$")),
    ("GET", re.compile(r"^/api/containers/[^/]+$")),
    ("POST", re.compile(r"^/api/containers/[^/]+/start$")),
]

TERMINAL_HOST = re.compile(r"^/api/terminals/([^/]+)/host$")

# Not passed on: hop-by-hop headers, and a browser's own credentials and origin (the proxy signs instead).
SKIPPED = {
    "host", "connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "content-length",
    "proxy-authorization", "proxy-connection", "cookie", "origin",
}

# Also dropped from agents' requests, as Go's ReverseProxy drops them: this hop's forwarding headers.
FORWARDING = {"forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto"}

# WebSocket close codes a peer may not send itself.
RESERVED_CLOSE = {1005, 1006, 1015}

log = logging.getLogger("agent-proxy")


def allowed(method: str, path: str) -> bool:
    return any(method == candidate and pattern.match(path) for candidate, pattern in LAUNCHER_API)


def local(headers: Any) -> bool:
    """A program on this computer naming it in Host, not a web page (DNS rebinding, or another site's page)."""
    host = headers.get("host", "")
    if not common.loopback_host(host):
        return False
    origin = headers.get("origin", "")
    if not origin:
        return True
    # Browsers send Origin with WebSockets and cross-site requests; programs don't.
    return (urlparse(origin).netloc or "").lower() == host.lower()


def forwarded(headers: Any) -> Dict[str, str]:
    copied: Dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() not in SKIPPED:
            copied[name] = value
    return copied


def refuse() -> JSONResponse:
    return JSONResponse({"detail": "agent-proxy only serves programs on this computer."}, status_code=403)


async def dial(upstream: common.Upstream, server: str, path: str):
    """A WebSocket to path on server, signed with the machine's sign-in, never through an environment proxy."""
    headers = common.identify({}, settings.PROGRAM)
    token = upstream.token(server)
    if token:
        headers[common.TOKEN_HEADER] = token
    return await ws_connect(common.websocket_url(server, path), additional_headers=headers, proxy=None, open_timeout=10, max_size=None, compression=None, user_agent_header=None)


def refused_status(error: BaseException) -> Optional[int]:
    response = getattr(error, "response", None)
    return getattr(response, "status_code", None)


class AgentProxy:
    """The agent-proxy's HTTP side, an ASGI app. intercept_port is the HTTPS proxy's actual port."""

    def __init__(self, upstream: common.Upstream, intercept_port: int) -> None:
        self.upstream = upstream
        self.intercept_port = intercept_port
        self.http = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(None, connect=30))
        # Only what the request itself carries: no User-Agent or Accept-Encoding of httpx's own.
        self.http.headers.clear()
        self.shutdown: Optional[Any] = None

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "websocket":
            await self.websocket(WebSocket(scope, receive, send))
            return
        if scope["type"] != "http":
            return
        request = Request(scope, receive)
        response = await self.handle(request)
        await response(scope, receive, send)

    async def handle(self, request: Request) -> Response:
        path = request.url.path
        if path == "/health" and request.method == "GET":
            server = self.upstream.server()
            return JSONResponse({
                "status": "ok", "role": "agent-proxy", "server": server, "user": self.upstream.user(server) or None,
                "intercept_port": self.intercept_port, "version": common.VERSION, "protocol": common.PROTOCOL,
            })
        if path == "/shutdown":
            return self.stop(request)
        if path.startswith("/api/"):
            return await self.launcher_api(request)
        if path.startswith(("/session/", "/v1/", "/anthropic/")):
            if not local(request.headers):
                return refuse()
            return await self.agent(request)
        return JSONResponse({"detail": "Not found"}, status_code=404)

    def stop(self, request: Request) -> Response:
        if request.method != "POST":
            return JSONResponse({"detail": "POST to stop agent-proxy"}, status_code=405)
        # A cross-site page can POST without preflight; Origin and Host keep it out like everything else.
        if not local(request.headers):
            return refuse()
        if self.shutdown is None:
            return JSONResponse({"detail": "Not found"}, status_code=404)
        log.info("agent-proxy stopping, as asked from this computer")
        asyncio.get_running_loop().call_later(0.1, self.shutdown)
        return JSONResponse({"stopping": True, "version": common.VERSION})

    async def launcher_api(self, request: Request) -> Response:
        """The launcher's calls, forwarded to the server signed with this machine's sign-in."""
        if not local(request.headers):
            return refuse()
        path = request.url.path
        if not allowed(request.method, path):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        server = self.upstream.server()
        headers = common.identify(forwarded(request.headers))
        # Uncompressed, so /api/intercept can be read and rewritten below.
        headers.pop("accept-encoding", None)
        headers.pop("Accept-Encoding", None)
        token = self.upstream.token(server)
        if token:
            headers[common.TOKEN_HEADER] = token
        target = server + path + (f"?{request.url.query}" if request.url.query else "")
        try:
            answer = await self.http.request(request.method, target, content=await request.body(), headers=headers)
        except httpx.HTTPError as error:
            return JSONResponse({"detail": f"agent-proxy couldn't reach the agent-server at {server}: {error}"}, status_code=502)
        if path == "/api/intercept" and answer.status_code == 200:
            try:
                info = answer.json()
                # Agents on this machine reach the intercept through this proxy's own port.
                info["url"] = f"http://127.0.0.1:{self.intercept_port}"
                return JSONResponse(info)
            except ValueError:
                pass
        kept = {name: value for name, value in answer.headers.items() if name.lower() not in SKIPPED and name.lower() != "content-encoding"}
        return Response(answer.content, status_code=answer.status_code, headers=kept)

    async def agent(self, request: Request) -> Response:
        """An agent's request, to the same path on the server, streamed both ways. The session id is the credential: no token."""
        server = self.upstream.server()
        target = server + request.url.path + (f"?{request.url.query}" if request.url.query else "")
        headers = common.identify({name: value for name, value in forwarded(request.headers).items() if name.lower() not in FORWARDING})
        length = request.headers.get("content-length")
        if length is not None:
            headers["Content-Length"] = length
        if "accept-encoding" not in request.headers:
            # As the agent asked: what it reads raw, not compressed for a client that never asked.
            headers["Accept-Encoding"] = "identity"
        # The body streams through as it arrives, like the response: an upload is never held here whole.
        body = request.stream() if length not in (None, "0") or "chunked" in request.headers.get("transfer-encoding", "").lower() else None
        try:
            outgoing = self.http.build_request(request.method, target, content=body, headers=headers)
            answer = await self.http.send(outgoing, stream=True)
        except httpx.HTTPError as error:
            message = f"agent-proxy couldn't reach the agent-server at {server}: {error}"
            return JSONResponse({"error": {"message": message, "type": "proxy_error"}}, status_code=502)
        kept = {name: value for name, value in answer.headers.items() if name.lower() not in SKIPPED}

        async def chunks():
            try:
                # Every chunk at once: agents read server-sent events as they arrive.
                async for chunk in answer.aiter_raw():
                    yield chunk
            finally:
                await answer.aclose()

        return StreamingResponse(chunks(), status_code=answer.status_code, headers=kept)

    async def websocket(self, launcher: WebSocket) -> None:
        """A terminal shared with --control, relayed to the server signed as this machine."""
        match = TERMINAL_HOST.match(launcher.url.path)
        # Host and Origin are checked after accepting, so a refusal reaches the launcher as a close code it can show.
        await launcher.accept()
        if not match:
            await launcher.close(4404, "Not found")
            return
        if not local(launcher.headers):
            await launcher.close(4403, "agent-proxy only serves programs on this computer")
            return
        server = self.upstream.server()
        try:
            remote = await dial(self.upstream, server, f"/api/terminals/{quote(match.group(1), safe='')}/host")
        except Exception as error:
            status = refused_status(error)
            if status in (401, 403):
                await launcher.close(4400 + status % 100, "the agent-server refused this machine: run `agent login`")
            elif status:
                # Anything else (a reverse proxy's 502/503/504 while the server restarts) passes: the
                # launcher gives up on 4403 and 4404, so they mustn't stand for "try again later".
                await launcher.close(1011, f"the agent-server at {server} answered HTTP {status}"[:120])
            else:
                await launcher.close(1011, f"couldn't reach the agent-server at {server}: {error}"[:120])
            return
        code, reason = await relay(launcher, remote)
        await remote.close()
        if code in RESERVED_CLOSE:
            code = 1011
        try:
            # Pass the server's close on, so the launcher sees a refusal (4401, 4404) for what it is.
            await launcher.close(code or 1000, (reason or "")[:120])
        except RuntimeError:
            pass

    async def aclose(self) -> None:
        await self.http.aclose()


async def relay(launcher: WebSocket, remote) -> Tuple[int, str]:
    """Copies messages both ways until either side ends. Returns how the server's side closed, when it did."""
    closed: Dict[str, Any] = {"code": 0, "reason": ""}

    async def up() -> None:
        try:
            while True:
                message = await launcher.receive()
                if message["type"] == "websocket.disconnect":
                    return
                data = message.get("bytes")
                await remote.send(data if data is not None else message.get("text") or "")
        except (WebSocketDisconnect, websockets.ConnectionClosed, RuntimeError):
            return

    async def down() -> None:
        try:
            async for message in remote:
                if isinstance(message, bytes):
                    await launcher.send_bytes(message)
                else:
                    await launcher.send_text(message)
        except websockets.ConnectionClosed:
            pass
        except (WebSocketDisconnect, RuntimeError):
            return
        if remote.close_code is not None:
            closed["code"], closed["reason"] = remote.close_code, remote.close_reason or ""

    tasks = [asyncio.ensure_future(up()), asyncio.ensure_future(down())]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    # Give the other direction a moment to record a close that's already on its way.
    await asyncio.wait(pending, timeout=0.05)
    for task in pending:
        task.cancel()
    return closed["code"], closed["reason"]


class TunnelServer:
    """Account agents' connections to the HTTPS proxy port, each carried to the server's intercept over its own WebSocket."""

    def __init__(self, upstream: common.Upstream) -> None:
        self.upstream = upstream

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self.tunnel(reader, writer)
        except Exception as error:  # one agent's connection never takes the proxy down
            log.warning("HTTPS proxy connection failed: %s", error)
        finally:
            writer.close()

    async def tunnel(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        server = self.upstream.server()
        try:
            remote = await dial(self.upstream, server, "/api/proxy/tunnel")
        except Exception as error:
            status = refused_status(error)
            await read_head(reader)
            if status:
                await refuse_tunnel(reader, writer, 407 if status in (401, 403) else 502, f"the agent-server answered {status}: run `agent login {server}`")
            else:
                await refuse_tunnel(reader, writer, 502, f"couldn't reach the agent-server at {server}: {error}")
            return
        state = {"answered": False}

        async def up() -> None:
            while True:
                data = await reader.read(65536)
                if not data:
                    return
                await remote.send(data)

        async def down() -> None:
            async for message in remote:
                if isinstance(message, bytes):
                    state["answered"] = True
                    writer.write(message)
                    await writer.drain()

        tasks = [asyncio.ensure_future(up()), asyncio.ensure_future(down())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        await asyncio.wait(pending, timeout=0.05)
        for task in pending:
            task.cancel()
        for task in done:
            if not task.cancelled():
                task.exception()
        code, reason = remote.close_code or 0, remote.close_reason or ""
        await remote.close()
        if not state["answered"] and code >= 4000:
            await refuse_tunnel(reader, writer, 407 if code in (4401, 4403) else 502, reason or "the agent-server refused")


async def read_head(reader: asyncio.StreamReader) -> None:
    """Reads the agent's request head (its CONNECT) before a refusal: closing a socket with
    unread data resets it on Windows, and the agent would never see the answer."""
    head = b""
    deadline = time.monotonic() + 0.5
    while b"\r\n\r\n" not in head and len(head) < 256 * 1024:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            chunk = await asyncio.wait_for(reader.read(4096), remaining)
        except asyncio.TimeoutError:
            return
        if not chunk:
            return
        head += chunk


async def refuse_tunnel(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, status: int, reason: str) -> None:
    log.info("HTTPS proxy connection refused (%d): %s", status, reason)
    phrase = "Proxy Authentication Required" if status == 407 else "Bad Gateway"
    reason = " ".join(reason.split())
    writer.write(f"HTTP/1.1 {status} {phrase}\r\nX-Agent-Proxy-Error: {reason}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode("utf-8", "replace"))
    try:
        await writer.drain()
        # Close our side only, so the answer arrives before the connection ends.
        if writer.can_write_eof():
            writer.write_eof()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if not await asyncio.wait_for(reader.read(65536), deadline - time.monotonic()):
                break
    except (OSError, asyncio.TimeoutError):
        pass


def listen(port: int) -> socket.socket:
    """A loopback port; 0 picks a free one."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform != "win32":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(128)
    sock.setblocking(False)
    return sock


def check_ports(port: int, intercept_port: int) -> str:
    """A plain message when the ports are taken. Usually that's the agent-proxy `agent` started in the background."""
    busy = 0
    for candidate in (port, intercept_port):
        if not candidate:
            continue
        try:
            listen(candidate).close()
        except OSError:
            busy = candidate
            break
    if not busy:
        return ""
    running = common.health(f"http://127.0.0.1:{port}")
    if running and running.get("role") == "agent-proxy":
        return (f"agent-proxy is already running on 127.0.0.1:{port}, carrying traffic to {running.get('server')}. "
                "`agent` starts it in the background when needed; stop that process to run agent-proxy in the foreground.")
    return (f"127.0.0.1:{busy} is in use by another program. Choose free ports with --port and --intercept-port "
            "(or AI_PROXY_LOCAL_PORT and AI_PROXY_LOCAL_INTERCEPT_PORT).")


def open_log() -> None:
    path = settings.log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        handler = logging.NullHandler()
    # The Go agent-proxy's format (log.LstdFlags), so one log reads the same whichever wrote it.
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y/%m/%d %H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def stop(base: str) -> bool:
    """Asks the agent-proxy at base to exit and waits until it has; False when none runs there."""
    running = common.health(base)
    if running is None:
        return False
    if running.get("role") != "agent-proxy":
        raise RuntimeError(f"something other than agent-proxy answers on {base}")
    with common.client(5) as http:
        response = http.post(base + "/shutdown")
    if response.status_code != 200:
        raise RuntimeError(f"the agent-proxy on {base} (version {running.get('version')}) can't be stopped this way; end its process")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if common.health(base) is None:
            return True
        time.sleep(0.1)
    raise RuntimeError(f"agent-proxy on {base} is still answering")


def main(name: str, args: List[str]) -> int:
    """agent-proxy in the foreground: `agent proxy`. `agent proxy stop` asks the running one to exit."""
    if args and args[0] == "stop":
        try:
            stopped = stop(settings.proxy_url())
        except (RuntimeError, httpx.HTTPError) as error:
            print(f"{name} stop: {error}", file=sys.stderr)
            return 1
        print("agent-proxy stopped; `agent` starts it again when needed." if stopped else "agent-proxy isn't running.")
        return 0
    parser = argparse.ArgumentParser(prog=name, description=(
        "The proxy on this agent machine. Agents started with `agent` register with it, and it carries their "
        "traffic to the agent-server this machine signed in to with `agent login <server>`."))
    parser.add_argument("--server", default="", help=f"The agent-server to use (default: AI_PROXY_URL, then the one from `agent login`, then {common.DEFAULT_SERVER})")
    parser.add_argument("--port", type=int, default=settings.proxy_port(), help="HTTP port on 127.0.0.1")
    parser.add_argument("--intercept-port", type=int, default=settings.intercept_port(), help="HTTPS proxy port on 127.0.0.1 for account agents")
    options = parser.parse_args(args)
    problem = check_ports(options.port, options.intercept_port)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    open_log()
    try:
        http_socket, tunnel_socket = listen(options.port), listen(options.intercept_port)
    except OSError as error:
        print(f"agent-proxy: {error}", file=sys.stderr)
        return 1
    try:
        asyncio.run(serve(settings.upstream(options.server), http_socket, tunnel_socket))
    except KeyboardInterrupt:
        pass
    return 0


async def serve(upstream: common.Upstream, http_socket: socket.socket, tunnel_socket: socket.socket) -> None:
    actual = tunnel_socket.getsockname()[1]
    proxy = AgentProxy(upstream, actual)
    tunnels = await asyncio.start_server(TunnelServer(upstream).handle, sock=tunnel_socket)
    server = uvicorn.Server(uvicorn.Config(proxy, lifespan="off", log_level="warning", access_log=False, ws_max_size=64 * 1024 * 1024, timeout_graceful_shutdown=1))
    print(f"agent-proxy: 127.0.0.1:{http_socket.getsockname()[1]}, HTTPS proxy 127.0.0.1:{actual}, carrying traffic to {upstream.server()}", file=sys.stderr)
    log.info("agent-proxy carrying traffic to %s; HTTPS proxy on 127.0.0.1:%d", upstream.server(), actual)

    def shutdown() -> None:
        server.should_exit = True
        tunnels.close()

    proxy.shutdown = shutdown
    try:
        await server.serve(sockets=[http_socket])
    finally:
        tunnels.close()
        await proxy.aclose()
