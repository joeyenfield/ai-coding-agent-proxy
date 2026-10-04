"""HTTPS forward proxy that records everything an agent sends to hosted services.

Agents signed in to their own accounts (Claude Code with a claude.ai login,
GitHub Copilot CLI with a GitHub login) talk to their vendors directly, so the
reverse-proxy endpoints can't see them. Launched with HTTPS_PROXY pointing here
and NODE_EXTRA_CA_CERTS set to the local CA, the agent opens a CONNECT tunnel
per host; the proxy terminates TLS with a certificate from that CA, forwards each
request unchanged over a normally verified TLS connection, and records the
exchange as a request in the agent's session.

The session comes from the proxy URL's user name
(http://<session_id>:x@127.0.0.1:8183), which clients send as
Proxy-Authorization. Traffic without one goes to a shared "unattributed"
session. Credentials in headers and token endpoints are redacted before
anything is shown or saved.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import h11
import httpx

from .sessions import Session
from .telemetry import RequestTelemetry, has_content, parse_json_line
from .translate import estimate_tokens
from .websocket_tap import CLOSE, TEXT, WebSocketTap

if TYPE_CHECKING:
    from .runtime import Runtime


LOGGER = logging.getLogger("agent-proxy")
MAX_HEAD = 256 * 1024
# Bodies larger than this are forwarded in full but only partly kept for display.
CAPTURE_LIMIT = 4 * 1024 * 1024
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authorization", "proxy-authenticate", "proxy-connection",
    "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding",
}
# Events that end one Responses API turn sent over a WebSocket.
TURN_END_EVENTS = {"response.completed", "response.failed", "response.incomplete", "response.cancelled", "error"}
# After one side of a relayed connection closes, how long the other side gets to finish.
CLOSE_GRACE = 2.0
RESPONSE_DROP = {"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding"}
SECRET_HEADER_PARTS = ("auth", "cookie", "token", "secret", "api-key", "apikey", "session")
SECRET_FIELDS = {
    "access_token", "refresh_token", "id_token", "token", "client_secret", "password",
    "api_key", "apikey", "code", "code_verifier", "device_code", "secret",
}
SENSITIVE_PATH_PARTS = ("oauth", "token", "login", "auth", "device")
REDACTED = "[redacted]"


class InterceptProxy:
    def __init__(self, runtime: "Runtime"):
        self.runtime = runtime
        self.settings = runtime.settings.intercept
        self.server: asyncio.base_events.Server | None = None
        self.error: str | None = None
        self._unattributed: str | None = None
        self._connections: set[asyncio.Task[Any]] = set()

    @property
    def running(self) -> bool:
        return self.server is not None

    async def start(self) -> None:
        try:
            self.server = await asyncio.start_server(
                self._accept, self.settings.listen_host, self.settings.listen_port, limit=MAX_HEAD,
            )
            LOGGER.info("intercept proxy listening on %s:%s", self.settings.listen_host, self.settings.listen_port)
        except OSError as exc:
            self.error = f"Couldn't listen on {self.settings.listen_host}:{self.settings.listen_port}: {exc.strerror or exc}"
            LOGGER.warning("intercept proxy disabled: %s", self.error)

    async def stop(self) -> None:
        if self.server is None:
            return
        self.server.close()
        for task in list(self._connections):
            task.cancel()
        await asyncio.gather(*self._connections, return_exceptions=True)
        await self.server.wait_closed()
        self.server = None

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task
        self._connections.add(task)
        try:
            await self._connection(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("intercept connection failed")
        finally:
            self._connections.discard(task)
            writer.close()

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        request_line, _, header_block = head.decode("latin-1").partition("\r\n")
        method, target, _version = (request_line.split(" ", 2) + ["", ""])[:3]
        headers = _parse_headers(header_block)
        session = self._session(headers.get("proxy-authorization"))
        if method.upper() != "CONNECT":
            # A plain http:// request sent to the proxy with an absolute URL.
            await self._serve(reader, writer, session, None, head)
            return
        host, port = _split_host(target)
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        if self.settings.tunnelled(host):
            await self._tunnel(reader, writer, session, host, port)
            return
        await writer.start_tls(self.runtime.ca.server_context(host))
        await self._serve(reader, writer, session, (host, port), b"")

    async def _serve(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        session: Session,
        origin: tuple[str, int] | None,
        initial: bytes,
    ) -> None:
        """Run HTTP/1.1 request/response cycles on one client connection."""
        connection = h11.Connection(h11.SERVER, max_incomplete_event_size=MAX_HEAD)
        if initial:
            # An empty receive_data() call means end of stream to h11.
            connection.receive_data(initial)
        while True:
            request: h11.Request | None = None
            body = bytearray()
            while True:
                event = connection.next_event()
                if event is h11.NEED_DATA:
                    if connection.they_are_waiting_for_100_continue:
                        writer.write(connection.send(h11.InformationalResponse(status_code=100, headers=[])))
                    data = await reader.read(65536)
                    connection.receive_data(data)
                    if not data and request is None:
                        return
                    continue
                if isinstance(event, h11.Request):
                    request = event
                elif isinstance(event, h11.Data):
                    body += event.data
                elif isinstance(event, h11.EndOfMessage):
                    break
                elif isinstance(event, h11.ConnectionClosed):
                    return
            assert request is not None
            headers = [(name.decode("latin-1"), value.decode("latin-1")) for name, value in request.headers]
            url = _url(origin, request.target.decode("latin-1"), headers)
            if any(name.lower() == "upgrade" for name, _ in headers):
                await self._upgrade(reader, writer, connection, session, request, bytes(body), url)
                return
            await self._exchange(writer, connection, session, request.method.decode(), url, headers, bytes(body))
            if connection.our_state is h11.MUST_CLOSE or connection.their_state is h11.MUST_CLOSE:
                return
            try:
                connection.start_next_cycle()
            except h11.LocalProtocolError:
                return

    async def _exchange(
        self,
        writer: asyncio.StreamWriter,
        connection: h11.Connection,
        session: Session,
        method: str,
        url: str,
        headers: list[tuple[str, str]],
        body: bytes,
    ) -> None:
        runtime = self.runtime
        parts = urlsplit(url)
        host = parts.hostname or ""
        path = parts.path or "/"
        request_type = _header(headers, "content-type")
        payload = _body_value(body, request_type, path)
        model = payload.get("model", "") if isinstance(payload, dict) else ""
        telemetry = RequestTelemetry(
            session_id=session.session_id, request_id=await runtime.sessions.next_request_id(session),
            client=session.client, model=str(model or ""), backend=host,
            endpoint=path, request_bytes=len(body), kind="intercept", method=method, host=host,
            max_output_tokens=payload.get("max_tokens") if isinstance(payload, dict) else None,
            temperature=payload.get("temperature") if isinstance(payload, dict) else None,
            estimated_input_tokens=_estimate(payload),
        )
        http: dict[str, Any] = {
            "method": method, "url": _redact_query(url), "request_headers": redact_headers(headers),
            "request_content_type": request_type,
        }
        runtime.track(telemetry)
        runtime.attach(telemetry, payload)
        runtime.live.http(_key(telemetry), http)
        upstream_headers = [(name, value) for name, value in headers if name.lower() not in HOP_HEADERS]
        assert runtime.client
        try:
            upstream = await runtime.client.send(
                runtime.client.build_request(method, url, headers=upstream_headers, content=body or None),
                stream=True, follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            telemetry.status = 502
            telemetry.error_type = type(exc).__name__
            message = f"Proxy couldn't reach {host}: {exc}".encode()
            writer.write(connection.send(h11.Response(status_code=502, headers=[("content-type", "text/plain"), ("content-length", str(len(message)))])))
            writer.write(connection.send(h11.Data(data=message)))
            writer.write(connection.send(h11.EndOfMessage()))
            runtime.complete(session, telemetry, payload, {"error": str(exc)}, http=http)
            return
        telemetry.status = upstream.status_code
        if upstream.status_code >= 400:
            telemetry.error_type = "upstream_error"
        response_type = upstream.headers.get("content-type", "")
        http.update({
            "status": upstream.status_code, "reason": upstream.reason_phrase,
            "response_headers": redact_headers(upstream.headers.multi_items()),
            "response_content_type": response_type,
        })
        runtime.live.http(_key(telemetry), http)
        streamed = "event-stream" in response_type or "ndjson" in response_type
        telemetry.streaming = streamed and upstream.status_code < 400
        response_headers = [(name, value) for name, value in upstream.headers.multi_items() if name.lower() not in RESPONSE_DROP]
        captured = bytearray()
        objects: list[dict[str, Any]] = []
        buffer = b""
        try:
            writer.write(connection.send(h11.Response(
                status_code=upstream.status_code, headers=response_headers,
                reason=upstream.reason_phrase.encode("latin-1", "replace"),
            )))
            async for chunk in upstream.aiter_bytes():
                telemetry.response_bytes += len(chunk)
                if streamed:
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        self._consume(telemetry, objects, line)
                    runtime.progress(telemetry)
                elif len(captured) < CAPTURE_LIMIT:
                    captured += chunk[: CAPTURE_LIMIT - len(captured)]
                writer.write(connection.send(h11.Data(data=chunk)))
                await writer.drain()
            if buffer:
                self._consume(telemetry, objects, buffer)
            writer.write(connection.send(h11.EndOfMessage()))
            await writer.drain()
        except httpx.HTTPError as exc:
            # The upstream broke mid-response; drop the client connection so it notices.
            telemetry.error_type = type(exc).__name__
            raise ConnectionAbortedError(str(exc)) from exc
        except (ConnectionError, asyncio.CancelledError):
            telemetry.error_type = "client_closed"
            raise
        finally:
            await upstream.aclose()
            if streamed:
                if not telemetry.streamed_chunks:
                    # Only model output has an end marker worth checking for.
                    telemetry.completed = True
                response: Any = objects
            else:
                response = _body_value(bytes(captured), response_type, path, truncated=telemetry.response_bytes > len(captured))
                if isinstance(response, dict):
                    telemetry.apply_ollama_stats(response)
                    if has_content(response):
                        telemetry.saw_content()
            runtime.complete(session, telemetry, payload, response, http=http)

    def _consume(self, telemetry: RequestTelemetry, objects: list[dict[str, Any]], line: bytes) -> None:
        obj = parse_json_line(line)
        if not obj:
            return
        if len(objects) < 20_000:
            objects.append(obj)
        telemetry.apply_ollama_stats(obj)
        if has_content(obj):
            telemetry.saw_content()
        self.runtime.stream_object(telemetry, obj)

    async def _upgrade(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        connection: h11.Connection,
        session: Session,
        request: h11.Request,
        body: bytes,
        url: str,
    ) -> None:
        """Relay a WebSocket (or other upgraded) connection byte for byte.

        The connection is recorded as one "upgrade" request. Text messages are
        decoded on the side; Responses API turns (response.create followed by
        streamed events) are each recorded as their own model call.
        """
        parts = urlsplit(url)
        host = parts.hostname or ""
        secure = parts.scheme == "https"
        port = parts.port or (443 if secure else 80)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        lines = [f"{request.method.decode()} {target} HTTP/1.1", f"Host: {parts.netloc}"]
        lines += [
            f"{name.decode('latin-1')}: {value.decode('latin-1')}" for name, value in request.headers
            # Dropping extensions keeps frames uncompressed so the tap can read them.
            if name.lower() not in {b"host", b"proxy-authorization", b"proxy-connection", b"sec-websocket-extensions"}
        ]
        telemetry = RequestTelemetry(
            session_id=session.session_id, request_id=await self.runtime.sessions.next_request_id(session),
            client=session.client, model="", backend=host, endpoint=parts.path or "/", request_bytes=len(body),
            kind="upgrade", method=request.method.decode(), host=host,
        )
        http = {"method": telemetry.method, "url": _redact_query(url), "request_headers": redact_headers(
            [(name.decode("latin-1"), value.decode("latin-1")) for name, value in request.headers]
        )}
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(
                host, port, ssl=secure or None, server_hostname=host if secure else None, limit=MAX_HEAD,
            )
        except OSError as exc:
            telemetry.status = 502
            telemetry.error_type = type(exc).__name__
            self.runtime.complete(session, telemetry, None, {"error": str(exc)}, http=http)
            return
        upstream_writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
        trailing, _ = connection.trailing_data
        if trailing:
            upstream_writer.write(trailing)
        telemetry.status = 101
        telemetry.streaming = False
        self.runtime.track(telemetry)
        self.runtime.live.http(_key(telemetry), http)
        turns = SocketTurns(self.runtime, session, host, parts.path or "/", http)
        taps = {True: WebSocketTap(), False: WebSocketTap(skip_http_head=True)}

        async def observe(outbound: bool, data: bytes) -> None:
            for opcode, payload in taps[outbound].feed(data):
                if opcode == TEXT:
                    await turns.message(outbound, payload)
                elif opcode == CLOSE:
                    turns.closed()
            if not outbound and taps[False].status is not None:
                telemetry.status = taps[False].status

        try:
            await _pipe_both(reader, writer, upstream_reader, upstream_writer, telemetry, observe)
        finally:
            turns.closed()
            self.runtime.complete(session, telemetry, None, {"messages": turns.counts}, http=http)

    async def _tunnel(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        session: Session,
        host: str,
        port: int,
    ) -> None:
        """Pass encrypted bytes through for hosts listed under intercept.passthrough."""
        telemetry = RequestTelemetry(
            session_id=session.session_id, request_id=await self.runtime.sessions.next_request_id(session),
            client=session.client, model="", backend=host, endpoint=f"{host}:{port}", request_bytes=0,
            kind="tunnel", method="CONNECT", host=host,
        )
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(host, port, limit=MAX_HEAD)
        except OSError as exc:
            telemetry.status = 502
            telemetry.error_type = type(exc).__name__
            self.runtime.complete(session, telemetry, None, {"error": str(exc)})
            return
        self.runtime.track(telemetry)
        try:
            await _pipe_both(reader, writer, upstream_reader, upstream_writer, telemetry)
        finally:
            self.runtime.complete(session, telemetry, None, None)

    def _session(self, authorization: str | None) -> Session:
        session_id = _proxy_user(authorization)
        session = self.runtime.sessions.get(session_id) if session_id else None
        if session:
            return session
        existing = self.runtime.sessions.get(self._unattributed or "")
        if existing and existing.ended_at is None:
            return existing
        session = self.runtime.sessions.create({
            "client": "unattributed", "backend": "direct",
            "tags": {"implicit": True, "intercept": True},
        })
        self._unattributed = session.session_id
        return session


class SocketTurns:
    """Turn Responses API traffic on a WebSocket into model-call records.

    Codex sends {"type": "response.create", ...} and the server streams the
    usual Responses events back, ending with response.completed.
    """

    def __init__(self, runtime: "Runtime", session: Session, host: str, path: str, http: dict[str, Any]):
        self.runtime = runtime
        self.session = session
        self.host = host
        self.path = path
        self.http = http
        self.current: tuple[RequestTelemetry, Any, list[dict[str, Any]]] | None = None
        self.counts = {"sent": 0, "received": 0, "turns": 0}

    async def message(self, outbound: bool, payload: bytes) -> None:
        self.counts["sent" if outbound else "received"] += 1
        try:
            obj = json.loads(payload)
        except ValueError:
            return
        if not isinstance(obj, dict):
            return
        if outbound:
            if obj.get("type") == "response.create":
                await self._start(obj, len(payload))
            return
        if self.current is None:
            return
        telemetry, _, events = self.current
        telemetry.response_bytes += len(payload)
        if len(events) < 20_000:
            events.append(obj)
        telemetry.apply_ollama_stats(obj)
        if has_content(obj):
            telemetry.saw_content()
        self.runtime.stream_object(telemetry, obj)
        self.runtime.progress(telemetry)
        if obj.get("type") in TURN_END_EVENTS:
            if obj.get("type") != "response.completed":
                telemetry.error_type = str(obj.get("type"))
            self._finish()

    def closed(self) -> None:
        if self.current is not None:
            self._finish()

    async def _start(self, message: dict[str, Any], size: int) -> None:
        self.closed()
        # Some clients nest the request body under "response".
        body = message.get("response") if isinstance(message.get("response"), dict) else message
        telemetry = RequestTelemetry(
            session_id=self.session.session_id,
            request_id=await self.runtime.sessions.next_request_id(self.session),
            client=self.session.client, model=str(body.get("model") or ""), backend=self.host,
            endpoint=self.path, request_bytes=size, kind="intercept", method="WS", host=self.host,
            max_output_tokens=body.get("max_output_tokens"), temperature=body.get("temperature"),
            estimated_input_tokens=_estimate(body),
        )
        telemetry.status = 101
        telemetry.streaming = True
        self.counts["turns"] += 1
        self.runtime.track(telemetry)
        self.runtime.attach(telemetry, body)
        self.runtime.live.http(_key(telemetry), {**self.http, "method": "WS", "status": 101, "reason": "WebSocket message"})
        self.current = (telemetry, body, [])

    def _finish(self) -> None:
        assert self.current is not None
        telemetry, body, events = self.current
        self.current = None
        self.runtime.complete(self.session, telemetry, body, events, http={**self.http, "method": "WS"})


async def _pipe_both(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
    telemetry: RequestTelemetry,
    observe: Any = None,
) -> None:
    async def pipe(source: asyncio.StreamReader, target: asyncio.StreamWriter, outbound: bool) -> None:
        try:
            while data := await source.read(65536):
                if outbound:
                    telemetry.request_bytes += len(data)
                else:
                    telemetry.response_bytes += len(data)
                target.write(data)
                await target.drain()
                if observe is not None:
                    try:
                        await observe(outbound, data)
                    except Exception:
                        # Recording is best effort; never let it break the relay.
                        LOGGER.exception("couldn't decode relayed WebSocket traffic")
        except (ConnectionError, OSError):
            pass
        finally:
            if target.can_write_eof():
                try:
                    target.write_eof()
                except (OSError, RuntimeError):
                    pass

    tasks = {
        asyncio.ensure_future(pipe(client_reader, upstream_writer, True)),
        asyncio.ensure_future(pipe(upstream_reader, client_writer, False)),
    }
    try:
        # Servers often hold a socket open after the client leaves; don't wait on them forever.
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if pending:
            _, pending = await asyncio.wait(pending, timeout=CLOSE_GRACE)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for task in tasks:
            task.cancel()
        upstream_writer.close()
        telemetry.completed = True


def _estimate(payload: Any) -> int | None:
    if isinstance(payload, dict) and any(key in payload for key in ("messages", "input", "prompt")):
        return estimate_tokens(payload)
    return None


def redact_headers(headers: Any) -> list[list[str]]:
    redacted = []
    for name, value in headers:
        lowered = name.lower()
        if any(part in lowered for part in SECRET_HEADER_PARTS):
            # Keep only an auth scheme word ("Bearer", "Basic") so the kind of credential stays visible.
            scheme = value.split(" ", 1)[0] if " " in value and "cookie" not in lowered else ""
            value = f"{scheme} {REDACTED}" if re.fullmatch(r"[A-Za-z][A-Za-z-]{1,19}", scheme) else REDACTED
        redacted.append([name, value])
    return redacted


def redact_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED if str(key).lower() in SECRET_FIELDS and isinstance(item, (str, int)) else redact_fields(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_fields(item) for item in value]
    return value


def _body_value(body: bytes, content_type: str | None, path: str, truncated: bool = False) -> Any:
    """Decode a body for display: JSON when possible, else text, else a size note."""
    if not body:
        return None
    sensitive = any(part in path.lower() for part in SENSITIVE_PATH_PARTS)
    content_type = (content_type or "").lower()
    if not truncated and ("json" in content_type or body[:1] in {b"{", b"["}):
        try:
            value = json.loads(body)
            return redact_fields(value) if sensitive else value
        except ValueError:
            pass
    if sensitive and "x-www-form-urlencoded" in content_type:
        return {"form": REDACTED, "bytes": len(body)}
    if content_type.startswith(("text/", "application/x-www-form-urlencoded")) or "json" in content_type or "xml" in content_type:
        text = body.decode("utf-8", "replace")
        return text + ("\n… truncated" if truncated else "")
    return {"binary": True, "content_type": content_type or None, "captured_bytes": len(body), "truncated": truncated}


def _url(origin: tuple[str, int] | None, target: str, headers: list[tuple[str, str]]) -> str:
    if origin is None:
        if target.startswith(("http://", "https://")):
            return target
        return f"http://{_header(headers, 'host') or 'localhost'}{target}"
    host, port = origin
    authority = host if port == 443 else f"{host}:{port}"
    if target.startswith(("http://", "https://")):
        return target
    return f"https://{authority}{target}"


def _redact_query(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    pairs = []
    for pair in parts.query.split("&"):
        name, equals, _ = pair.partition("=")
        pairs.append(f"{name}={REDACTED}" if equals and any(part in name.lower() for part in SECRET_HEADER_PARTS + ("key", "code", "sig")) else pair)
    return parts._replace(query="&".join(pairs)).geturl()


def _header(headers: list[tuple[str, str]], name: str) -> str | None:
    return next((value for key, value in headers if key.lower() == name), None)


def _parse_headers(block: str) -> dict[str, str]:
    headers = {}
    for line in block.split("\r\n"):
        name, colon, value = line.partition(":")
        if colon:
            headers[name.strip().lower()] = value.strip()
    return headers


def _split_host(target: str) -> tuple[str, int]:
    if target.startswith("["):
        host, _, rest = target[1:].partition("]")
        return host, int(rest.lstrip(":") or 443)
    host, _, port = target.rpartition(":")
    if not host:
        return port, 443
    return host, int(port or 443)


def _proxy_user(authorization: str | None) -> str | None:
    if not authorization or not authorization.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(authorization[6:].strip()).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    return decoded.partition(":")[0] or None


def _key(telemetry: RequestTelemetry) -> str:
    return f"{telemetry.session_id}:{telemetry.request_id}"
