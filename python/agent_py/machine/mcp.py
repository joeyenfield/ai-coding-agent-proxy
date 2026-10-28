"""MCP servers this machine shares with the server's agents: the ones its owner lists with
`agent-machine mcp add` (mcp.json in agent-machine's folder, the Go agent-machine's format).

Every one of them is reached through the bridge: an HTTP server on a loopback port, in a thread
of its own, that speaks MCP's Streamable HTTP at /mcp/<name>, behind a key only this run and the
server know. The server reaches that port through the fleet's tunnel, like a model server. A
stdio server (a command) gets a process of its own for each MCP session; an HTTP one (a URL) is
proxied. Commands, environments, folders and headers stay here: the server only learns names.
The Go agent-machine's is machine/internal/mcp.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import httpx
import uvicorn
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from .. import common
from . import settings

KEY_HEADER = "x-ai-proxy-mcp-key"
SESSION_HEADER = "mcp-session-id"
# A stdio session nobody has used for this long is ended (agents rarely say goodbye).
IDLE_TIMEOUT = 6 * 3600
MAX_SESSIONS = 32
MAX_BODY = 16 << 20
PING_EVERY = 30
BACKLOG = 100
# What Session.next gives when nothing came in time.
WAITED = object()
NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")

log = logging.getLogger("agent-machine")


@dataclass
class Server:
    name: str
    description: str = ""
    command: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    dir: str = ""
    url: str = ""
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def transport(self) -> str:
        return "http" if self.url else "stdio"

    def check(self) -> None:
        if not NAME.fullmatch(self.name):
            raise ValueError(f"name an MCP server with up to 40 lowercase letters, numbers, '-' and '_' (not {self.name!r})")
        if bool(self.url) == bool(self.command):
            raise ValueError("give an MCP server either a command (after --) or --url")
        if self.url:
            parts = urlsplit(self.url)
            if parts.scheme not in ("http", "https") or not parts.netloc:
                raise ValueError(f"{self.url} isn't an http:// or https:// URL")

    def found(self) -> bool:
        return bool(self.url) or common.look_path(self.command[0]) is not None

    def stored(self) -> Dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value}


def path() -> Path:
    return settings.home() / "mcp.json"


def load() -> List[Server]:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    servers = []
    for item in (data or {}).get("servers") or []:
        try:
            server = Server(**{key: value for key, value in item.items() if key in Server.__dataclass_fields__})
            server.check()
        except (TypeError, ValueError):
            continue
        servers.append(server)
    return sorted(servers, key=lambda server: server.name)


def save(servers: List[Server]) -> None:
    # Readable by this user only: environments and headers often hold tokens.
    common.write_json(path(), {"servers": [server.stored() for server in servers]})


def add(server: Server) -> None:
    server.check()
    save([item for item in load() if item.name != server.name] + [server])


def remove(name: str) -> bool:
    servers = load()
    kept = [item for item in servers if item.name != name]
    if len(kept) == len(servers):
        return False
    save(kept)
    return True


class Bridge:
    """The loopback HTTP server the fleet's tunnel reaches this machine's MCP servers on."""

    def __init__(self, loader=load) -> None:
        self.loader = loader
        self.port = 0
        self.key = ""
        self.sessions: Dict[str, "Session"] = {}
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.server: Optional[uvicorn.Server] = None
        self.lock = threading.Lock()

    def describe(self) -> Optional[Dict[str, Any]]:
        """The bridge for the hello: its port and key, and the servers by name (never how they run).
        It starts the bridge the first time there are servers to share."""
        servers = self.loader()
        if not servers:
            return None
        self.start()
        return {"port": self.port, "key": self.key, "servers": [
            {"name": server.name, "description": server.description, "transport": server.transport, "found": server.found()}
            for server in servers
        ]}

    def start(self) -> None:
        with self.lock:
            if self.port:
                return
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            self.key = secrets.token_hex(24)
            self.server = uvicorn.Server(uvicorn.Config(self.app, interface="asgi3", log_level="warning", lifespan="off", access_log=False, timeout_graceful_shutdown=1))
            ready = threading.Event()

            def serve() -> None:
                loop = asyncio.new_event_loop()
                self.loop = loop
                asyncio.set_event_loop(loop)
                loop.call_soon(ready.set)
                loop.create_task(self.reap())
                loop.run_until_complete(self.server.serve(sockets=[listener]))

            threading.Thread(target=serve, name="mcp-bridge", daemon=True).start()
            ready.wait(10)
            self.port = listener.getsockname()[1]

    def close(self) -> None:
        if self.loop and self.server:
            for session in list(self.sessions.values()):
                asyncio.run_coroutine_threadsafe(session.close(), self.loop)
            self.server.should_exit = True

    async def reap(self) -> None:
        while True:
            await asyncio.sleep(60)
            for session in list(self.sessions.values()):
                if session.idle(IDLE_TIMEOUT):
                    self.sessions.pop(session.id, None)
                    await session.close()

    async def app(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return
        request = Request(scope, receive)
        response = await self.handle(request)
        await response(scope, receive, send)

    async def handle(self, request: Request) -> Response:
        if not self.key or not hmac.compare_digest(request.headers.get(KEY_HEADER, ""), self.key):
            return error(401, "not the server's key")
        if not request.url.path.startswith("/mcp/"):
            return error(404, "not found")
        name = request.url.path[len("/mcp/"):]
        server = next((item for item in await asyncio.to_thread(self.loader) if item.name == name), None)
        if server is None:
            return error(404, f"this machine shares no MCP server called {name}")
        if server.url:
            return await forward(request, server)
        return await self.stdio(request, server)

    async def stdio(self, request: Request, server: Server) -> Response:
        session_id = request.headers.get(SESSION_HEADER, "")
        session = None
        if session_id:
            session = self.sessions.get(session_id)
            if session is None or session.server != server.name:
                return error(404, "no such MCP session: initialize again")
            session.used = time.monotonic()
        if request.method == "POST":
            body = await request.body()
            if len(body) > MAX_BODY:
                return error(413, "too big")
            try:
                messages = split(body)
            except ValueError as problem:
                return error(400, str(problem))
            if session is None:
                if not any(item.get("method") == "initialize" for item in messages):
                    return error(400, "start with initialize: there's no Mcp-Session-Id")
                try:
                    session = await self.open(server)
                except (OSError, ValueError) as problem:
                    log.info("mcp %s: %s", server.name, problem)
                    return error(502, str(problem))
            return await session.post(request, messages)
        if request.method == "GET":
            if session is None:
                return error(400, "name the session in Mcp-Session-Id")
            if "text/event-stream" not in request.headers.get("accept", ""):
                return error(406, "accept text/event-stream")
            return session.listen()
        if request.method == "DELETE":
            if session is None:
                return error(400, "name the session in Mcp-Session-Id")
            self.sessions.pop(session.id, None)
            await session.close()
            return Response(status_code=200)
        return error(405, "POST, GET or DELETE")

    async def open(self, server: Server) -> "Session":
        if len(self.sessions) >= MAX_SESSIONS:
            raise ValueError(f"this machine already runs {MAX_SESSIONS} MCP sessions; end some agents first")
        program = common.look_path(server.command[0])
        if not program:
            raise ValueError(f"{server.command[0]} isn't installed here")
        flags: Dict[str, Any] = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {"start_new_session": True}
        process = await asyncio.create_subprocess_exec(
            program, *server.command[1:], cwd=server.dir or None, env={**os.environ, **server.env},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=MAX_BODY, **flags,
        )
        session = Session(secrets.token_hex(16), server.name, process)
        self.sessions[session.id] = session
        session.tasks = [asyncio.ensure_future(session.read()), asyncio.ensure_future(session.errors())]

        async def ended() -> None:
            code = await process.wait()
            log.info("mcp %s: session %s ended (exit %s)", server.name, session.id[:8], code)
            self.sessions.pop(session.id, None)
            session.done.set()

        session.tasks.append(asyncio.ensure_future(ended()))
        log.info("mcp %s: session %s started", server.name, session.id[:8])
        return session


class Stream:
    def __init__(self, events: bool) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.events = events
        self.closed = False


class Session:
    """One MCP session with a stdio server: its process, and the streams waiting on it."""

    def __init__(self, session_id: str, server: str, process: asyncio.subprocess.Process) -> None:
        self.id = session_id
        self.server = server
        self.process = process
        self.waiting: Dict[str, Stream] = {}
        self.posts: List[Stream] = []
        self.listening: Optional[Stream] = None
        self.backlog: List[bytes] = []
        self.used = time.monotonic()
        self.done = asyncio.Event()
        self.tasks: List[asyncio.Future] = []

    def idle(self, limit: float) -> bool:
        return not self.posts and self.listening is None and time.monotonic() - self.used > limit

    async def write(self, messages: List[Dict[str, Any]]) -> None:
        assert self.process.stdin
        for message in messages:
            self.process.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
        await self.process.stdin.drain()

    async def post(self, request: Request, messages: List[Dict[str, Any]]) -> Response:
        ids = [key(item["id"]) for item in messages if item.get("method") and item.get("id") is not None]
        if not ids:
            try:
                await self.write(messages)
            except (OSError, RuntimeError):
                return error(502, "the MCP server has stopped")
            return Response(status_code=202)
        events = "text/event-stream" in request.headers.get("accept", "")
        out = Stream(events)
        for item in ids:
            self.waiting[item] = out
        self.posts.append(out)
        try:
            await self.write(messages)
        except (OSError, RuntimeError):
            self.forget(out, ids)
            return error(502, "the MCP server has stopped")
        headers = {"Mcp-Session-Id": self.id}

        async def replies():
            remaining = len(ids)
            try:
                while remaining:
                    message = await self.next(out)
                    if message is None:
                        return
                    if is_response(message):
                        remaining -= 1
                    yield message
            finally:
                self.forget(out, ids)

        if events:
            async def stream():
                async for message in replies():
                    yield b"event: message\ndata: " + message + b"\n\n"

            return StreamingResponse(stream(), media_type="text/event-stream", headers={**headers, "Cache-Control": "no-cache"})
        collected = [json.loads(message) async for message in replies()]
        if len(collected) < len(ids):
            return error(502, "the MCP server stopped before answering")
        return JSONResponse(collected[0] if len(messages) == 1 and len(collected) == 1 else collected, headers=headers)

    async def next(self, out: Stream, timeout: Optional[float] = None) -> Any:
        """The stream's next message; None once the process has ended (or the stream was replaced), and
        WAITED after timeout. Nothing that has taken a message is cancelled, so none is lost."""
        getter = asyncio.ensure_future(out.queue.get())
        ended = asyncio.ensure_future(self.done.wait())
        done, _ = await asyncio.wait([getter, ended], timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        ended.cancel()
        if getter in done:
            return getter.result()
        getter.cancel()
        return None if done else WAITED

    def forget(self, out: Stream, ids: List[str]) -> None:
        out.closed = True
        for item in ids:
            if self.waiting.get(item) is out:
                del self.waiting[item]
        if out in self.posts:
            self.posts.remove(out)
        self.used = time.monotonic()

    def listen(self) -> Response:
        """The session's GET stream: what the server says outside any request's reply."""
        if self.listening is not None:
            self.listening.closed = True
            self.listening.queue.put_nowait(None)
        out = Stream(True)
        self.listening = out
        backlog, self.backlog = self.backlog, []

        async def stream():
            try:
                for message in backlog:
                    yield b"event: message\ndata: " + message + b"\n\n"
                while not out.closed:
                    message = await self.next(out, PING_EVERY)
                    if message is WAITED:
                        # A comment: clients ignore it, and proxies on the way don't time the stream out.
                        yield b": ping\n\n"
                        continue
                    if message is None:
                        return
                    yield b"event: message\ndata: " + message + b"\n\n"
            finally:
                if self.listening is out:
                    self.listening = None
                self.used = time.monotonic()

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    async def read(self) -> None:
        """The server's messages, one JSON value per line, to the stream each belongs on."""
        assert self.process.stdout
        while True:
            try:
                line = await self.process.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue
            if not line:
                return
            line = line.strip()
            if not line:
                continue
            try:
                json.loads(line)
            except ValueError:
                log.info("mcp %s: not JSON on stdout: %.200s", self.server, line)
                continue
            self.route(line)

    async def errors(self) -> None:
        assert self.process.stderr
        while True:
            line = await self.process.stderr.readline()
            if not line:
                return
            log.info("mcp %s: %s", self.server, line.decode("utf-8", "replace").rstrip())

    def route(self, message: bytes) -> None:
        """A response to its request's stream; anything else to the newest open event stream, the GET
        stream, or the backlog for the next GET."""
        out = None
        if is_response(message):
            out = self.waiting.get(key(json.loads(message).get("id")))
        if out is None:
            out = next((item for item in reversed(self.posts) if item.events), None) or self.listening
        if out is None or out.closed:
            if len(self.backlog) < BACKLOG:
                self.backlog.append(message)
            return
        out.queue.put_nowait(message)

    async def close(self) -> None:
        """Its stdin first, so it can stop by itself, then the process and what it started."""
        if self.process.stdin:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        try:
            await asyncio.wait_for(self.process.wait(), 2)
            return
        except asyncio.TimeoutError:
            pass
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.process.pid)], capture_output=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                os.killpg(self.process.pid, signal.SIGTERM)
        except OSError:
            self.process.kill()


async def forward(request: Request, server: Server) -> Response:
    """A request to a URL server as it is, with its own headers added."""
    target = server.url + (("&" if "?" in server.url else "?") + request.url.query if request.url.query else "")
    headers = {name: value for name, value in request.headers.items() if name.lower() not in {"host", "content-length", KEY_HEADER, "connection"}}
    headers.update(server.headers)
    client = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(600, connect=10))
    try:
        answer = await client.send(client.build_request(request.method, target, headers=headers, content=await request.body()), stream=True)
    except httpx.HTTPError as problem:
        await client.aclose()
        return error(502, f"couldn't reach {server.name}: {problem}")
    kept = {name: value for name, value in answer.headers.items() if name.lower() not in {"content-length", "transfer-encoding", "connection", "content-encoding"}}

    async def body():
        try:
            async for chunk in answer.aiter_bytes():
                yield chunk
        finally:
            await answer.aclose()
            await client.aclose()

    return StreamingResponse(body(), status_code=answer.status_code, headers=kept)


def split(body: bytes) -> List[Dict[str, Any]]:
    try:
        value = json.loads(body)
    except ValueError:
        value = None
    messages = value if isinstance(value, list) else [value]
    if not messages or not all(isinstance(item, dict) for item in messages):
        raise ValueError("send a JSON-RPC message or a batch of them")
    return messages


def is_response(message: bytes) -> bool:
    try:
        value = json.loads(message)
    except ValueError:
        return False
    return isinstance(value, dict) and not value.get("method") and value.get("id") is not None and ("result" in value or "error" in value)


def key(value: Any) -> str:
    """An id written the same way whichever way it came (1 and 1.0)."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return json.dumps(value)


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": message}}, status_code=status)
