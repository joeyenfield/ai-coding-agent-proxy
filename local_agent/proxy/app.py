from __future__ import annotations

import argparse
import asyncio
import json
import logging
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

from local_agent.config import Settings
from .dashboard import UI_HTML
from .sessions import Session, SessionStore
from .telemetry import RequestTelemetry, has_content, parse_json_line


LOGGER = logging.getLogger("local-agent-proxy")


class Runtime:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.bound_host = settings.host
        self.bound_port = settings.port
        self.proxy_instance_id = str(uuid.uuid4())
        self.sessions = SessionStore(settings.log_dir)
        self.client: httpx.AsyncClient | None = None
        self.recent: deque[dict[str, Any]] = deque(maxlen=100)
        self.active: dict[str, dict[str, Any]] = {}
        self.config_lock = asyncio.Lock()
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.reload_history()

    def reload_history(self) -> None:
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        existing: list[dict[str, Any]] = []
        for session in self.sessions.list():
            for line in self.sessions.telemetry(session).splitlines():
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        continue
                    existing.append(record)
                    self.total_input_tokens += int(record.get("input_tokens") or 0)
                    self.total_output_tokens += int(record.get("output_tokens") or 0)
                except (ValueError, TypeError):
                    continue
        self.recent.clear()
        self.recent.extend(sorted(existing, key=lambda item: item.get("timestamp", ""))[-100:])

    def status(self) -> dict[str, Any]:
        records = list(self.recent)
        sessions = self.sessions.list()
        return {
            "proxy_instance_id": self.proxy_instance_id,
            "status": "ok",
            "proxy_url": self.settings.proxy_url,
            "listen_address": f"{self.bound_host}:{self.bound_port}",
            "configured_listen_address": f"{self.settings.host}:{self.settings.port}",
            "restart_required": (
                self.bound_host != self.settings.host
                or self.bound_port != self.settings.port
            ),
            "default_backend": self.settings.default_backend,
            "backends": {
                name: {"type": backend.type, "url": backend.url}
                for name, backend in self.settings.backends.items()
            },
            "active_sessions": sum(item.ended_at is None for item in sessions),
            "total_requests": sum(item.request_count for item in sessions),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "current_requests": list(self.active.values()),
            "recent_requests": records[::-1],
        }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    runtime = Runtime(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        settings.log_dir.joinpath("proxy").mkdir(parents=True, exist_ok=True)
        _configure_logging(settings.log_dir / "proxy" / "proxy.log")
        runtime.client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10))
        LOGGER.info("proxy started instance=%s", runtime.proxy_instance_id)
        yield
        assert runtime.client
        await runtime.client.aclose()
        LOGGER.info("proxy stopped instance=%s", runtime.proxy_instance_id)

    app = FastAPI(title="Local AI Agent Proxy", version="0.1.0", lifespan=lifespan)
    app.state.runtime = runtime

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard() -> str:
        return UI_HTML

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "proxy_instance_id": runtime.proxy_instance_id}

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return runtime.status()

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        return {
            "config": settings.network_config(),
            "restart_required": runtime.status()["restart_required"],
        }

    @app.put("/api/config")
    async def update_config(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
            async with runtime.config_lock:
                config = settings.update_network_config(data)
        except (ValueError, TypeError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        LOGGER.info("network configuration updated")
        return {
            "config": config,
            "restart_required": runtime.status()["restart_required"],
        }

    @app.post("/api/sessions", status_code=201)
    async def create_session(request: Request) -> dict[str, Any]:
        data = await request.json()
        try:
            settings.backend(data.get("backend"))
            return runtime.sessions.create(data).public()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/sessions")
    async def list_sessions() -> list[dict[str, Any]]:
        return [item.public() for item in runtime.sessions.list()]

    @app.get("/api/requests")
    async def list_requests(session_id: str | None = None) -> list[dict[str, Any]]:
        sessions = [_require_session(runtime, session_id)] if session_id else runtime.sessions.list()
        records = []
        for session in sessions:
            traces = set(runtime.sessions.traces(session))
            for line in runtime.sessions.telemetry(session).splitlines():
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        continue
                except ValueError:
                    continue
                records.append({
                    **record,
                    "session_id": session.session_id,
                    "trace_available": f"{record.get('request_id')}.json" in traces,
                })
        return sorted(records, key=lambda item: item.get("timestamp", ""), reverse=True)

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        return _require_session(runtime, session_id).public()

    @app.delete("/api/sessions/{session_id}/data")
    async def clear_session_data(session_id: str) -> dict[str, Any]:
        session = _require_session(runtime, session_id)
        _require_idle_session(runtime, session)
        try:
            runtime.sessions.clear_data(session)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        runtime.reload_history()
        return session.public()

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, Any]:
        session = _require_session(runtime, session_id)
        _require_idle_session(runtime, session)
        try:
            runtime.sessions.delete(session)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        runtime.reload_history()
        return {"deleted": session_id}

    @app.patch("/api/sessions/{session_id}")
    async def end_session(session_id: str, request: Request) -> dict[str, Any]:
        session = _require_session(runtime, session_id)
        data = await request.json()
        if data.get("ended", True):
            runtime.sessions.end(session, data.get("exit_status"))
        if "trace" in data:
            session.trace = bool(data["trace"])
            runtime.sessions._write_session(session)
        return session.public()

    @app.get("/api/sessions/{session_id}/telemetry")
    async def download_telemetry(session_id: str) -> PlainTextResponse:
        session = _require_session(runtime, session_id)
        return PlainTextResponse(
            runtime.sessions.telemetry(session),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="{session_id}-requests.jsonl"'},
        )

    @app.get("/api/sessions/{session_id}/traces")
    async def list_traces(session_id: str) -> list[str]:
        return runtime.sessions.traces(_require_session(runtime, session_id))

    @app.get("/api/sessions/{session_id}/traces/{request_id}")
    async def get_trace(session_id: str, request_id: str) -> dict[str, Any]:
        trace = runtime.sessions.read_trace(_require_session(runtime, session_id), request_id)
        if trace is None:
            raise HTTPException(status_code=404, detail="Trace not found")
        return trace

    @app.api_route("/v1/{path:path}", methods=["GET", "POST"])
    @app.api_route("/session/{session_id}/v1/{path:path}", methods=["GET", "POST"])
    async def openai_proxy(
        request: Request,
        path: str,
        session_id: str | None = None,
        x_ai_proxy_session: str | None = Header(default=None),
        x_ai_proxy_backend: str | None = Header(default=None),
    ):
        if path == "models" and request.method == "GET":
            selected_session = runtime.sessions.get(session_id or x_ai_proxy_session or "")
            return await _simple_forward(runtime, request, selected_session, "/api/tags", x_ai_proxy_backend)
        return await _forward(
            runtime, request, session_id or x_ai_proxy_session, f"/v1/{path}", x_ai_proxy_backend
        )

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "DELETE"])
    @app.api_route("/session/{session_id}/api/{path:path}", methods=["GET", "POST", "DELETE"])
    async def ollama_proxy(
        request: Request,
        path: str,
        session_id: str | None = None,
        x_ai_proxy_session: str | None = Header(default=None),
        x_ai_proxy_backend: str | None = Header(default=None),
    ):
        return await _forward(
            runtime, request, session_id or x_ai_proxy_session, f"/api/{path}", x_ai_proxy_backend
        )

    @app.post("/anthropic/v1/messages")
    @app.post("/session/{session_id}/anthropic/v1/messages")
    async def anthropic_messages(
        request: Request,
        session_id: str | None = None,
        x_ai_proxy_session: str | None = Header(default=None),
        x_ai_proxy_backend: str | None = Header(default=None),
    ):
        return await _anthropic(
            runtime, request, session_id or x_ai_proxy_session, x_ai_proxy_backend
        )

    return app


async def _forward(
    runtime: Runtime,
    request: Request,
    session_id: str | None,
    upstream_path: str,
    backend_override: str | None,
):
    body = await request.body()
    payload = _json(body)
    original_payload = json.loads(json.dumps(payload))
    session = _resolve_session(runtime, session_id, payload, backend_override)
    backend = runtime.settings.backend(backend_override or session.backend)
    request_id = await runtime.sessions.next_request_id(session)
    model = payload.get("model") or session.model or ""
    if model:
        payload["model"] = runtime.settings.model_name(model)
        body = json.dumps(payload).encode()
    telemetry = _telemetry(session, request_id, request.url.path, body, payload, model)
    telemetry.backend = backend.name
    headers = _upstream_headers(request.headers)
    runtime.active[f"{session.session_id}:{request_id}"] = _active_record(telemetry)
    assert runtime.client
    try:
        upstream_request = runtime.client.build_request(
            request.method, backend.url.rstrip("/") + upstream_path,
            content=body or None, headers=headers, params=request.query_params,
        )
        upstream = await runtime.client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        return _proxy_error(runtime, session, telemetry, original_payload, exc)

    telemetry.status = upstream.status_code
    content_type = upstream.headers.get("content-type", "application/json")
    is_stream = bool(payload.get("stream")) or "ndjson" in content_type or "event-stream" in content_type
    if not is_stream:
        response_body = await upstream.aread()
        await upstream.aclose()
        telemetry.response_bytes = len(response_body)
        obj = _json(response_body)
        telemetry.apply_ollama_stats(obj)
        record = _complete(runtime, session, telemetry, original_payload, obj)
        return JSONResponse(obj, status_code=upstream.status_code, headers=_telemetry_headers(record))

    runtime.active[f"{session.session_id}:{request_id}"] = _active_record(telemetry)
    return StreamingResponse(
        _stream_upstream(runtime, session, telemetry, original_payload, upstream),
        status_code=upstream.status_code,
        media_type=content_type,
        headers={"X-Request-ID": request_id, "X-Session-ID": session.session_id},
    )


async def _stream_upstream(
    runtime: Runtime,
    session: Session,
    telemetry: RequestTelemetry,
    request_payload: dict[str, Any],
    upstream: httpx.Response,
) -> AsyncIterator[bytes]:
    response_objects: list[dict[str, Any]] = []
    buffer = b""
    try:
        async for chunk in upstream.aiter_bytes():
            telemetry.response_bytes += len(chunk)
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                obj = parse_json_line(line)
                if obj:
                    response_objects.append(obj)
                    telemetry.apply_ollama_stats(obj)
                    if has_content(obj):
                        telemetry.saw_content()
            active = runtime.active.get(f"{session.session_id}:{telemetry.request_id}")
            if active:
                active.update({"response_bytes": telemetry.response_bytes, "ttft_ms": _live_ttft(telemetry)})
            yield chunk
        if buffer:
            obj = parse_json_line(buffer)
            if obj:
                response_objects.append(obj)
                telemetry.apply_ollama_stats(obj)
                if has_content(obj):
                    telemetry.saw_content()
    except Exception as exc:
        telemetry.error_type = type(exc).__name__
        LOGGER.exception("stream failed session=%s request=%s", session.session_id, telemetry.request_id)
        raise
    finally:
        await upstream.aclose()
        runtime.active.pop(f"{session.session_id}:{telemetry.request_id}", None)
        _complete(runtime, session, telemetry, request_payload, response_objects)


async def _anthropic(
    runtime: Runtime,
    request: Request,
    session_id: str | None,
    backend_override: str | None,
):
    raw = await request.body()
    original = _json(raw)
    session = _resolve_session(runtime, session_id, original, backend_override)
    backend = runtime.settings.backend(backend_override or session.backend)
    request_id = await runtime.sessions.next_request_id(session)
    ollama_payload = _anthropic_to_ollama(original, runtime.settings)
    telemetry = _telemetry(session, request_id, request.url.path, raw, original, original.get("model", ""))
    telemetry.backend = backend.name
    runtime.active[f"{session.session_id}:{request_id}"] = _active_record(telemetry)
    assert runtime.client
    try:
        upstream_request = runtime.client.build_request(
            "POST", backend.url.rstrip("/") + "/api/chat", json=ollama_payload
        )
        upstream = await runtime.client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        return _proxy_error(runtime, session, telemetry, original, exc)
    telemetry.status = upstream.status_code
    if upstream.status_code >= 400:
        body = await upstream.aread()
        await upstream.aclose()
        telemetry.response_bytes = len(body)
        telemetry.error_type = "upstream_error"
        _complete(runtime, session, telemetry, original, _json(body))
        return JSONResponse({"type": "error", "error": _json(body)}, status_code=upstream.status_code)
    if original.get("stream", False):
        runtime.active[f"{session.session_id}:{request_id}"] = _active_record(telemetry)
        return StreamingResponse(
            _anthropic_stream(runtime, session, telemetry, original, upstream),
            media_type="text/event-stream",
            headers={"X-Request-ID": request_id, "X-Session-ID": session.session_id},
        )
    data = _json(await upstream.aread())
    await upstream.aclose()
    telemetry.apply_ollama_stats(data)
    text = data.get("message", {}).get("content", "")
    if text:
        telemetry.saw_content()
    response = _ollama_to_anthropic(data, original.get("model", ""))
    telemetry.response_bytes = len(json.dumps(response).encode())
    _complete(runtime, session, telemetry, original, response)
    return JSONResponse(response)


async def _anthropic_stream(
    runtime: Runtime,
    session: Session,
    telemetry: RequestTelemetry,
    original: dict[str, Any],
    upstream: httpx.Response,
) -> AsyncIterator[bytes]:
    message_id = f"msg_{uuid.uuid4().hex}"
    collected: list[dict[str, Any]] = []
    used_tool = False
    start = {"type": "message_start", "message": {"id": message_id, "type": "message", "role": "assistant", "model": original.get("model", ""), "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}}
    event = _sse("message_start", start)
    telemetry.response_bytes += len(event)
    yield event
    event = _sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
    telemetry.response_bytes += len(event)
    yield event
    try:
        async for line in upstream.aiter_lines():
            obj = parse_json_line(line.encode())
            if not obj:
                continue
            collected.append(obj)
            telemetry.apply_ollama_stats(obj)
            text = obj.get("message", {}).get("content", "")
            if text:
                telemetry.saw_content()
                event = _sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}})
                telemetry.response_bytes += len(event)
                yield event
            previous_tool_count = sum(len(item.get("message", {}).get("tool_calls") or []) for item in collected[:-1])
            for call_offset, call in enumerate(obj.get("message", {}).get("tool_calls") or []):
                used_tool = True
                index = 1 + previous_tool_count + call_offset
                function = call.get("function", {})
                arguments = function.get("arguments", {})
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, separators=(",", ":"))
                events = [
                    _sse("content_block_start", {"type": "content_block_start", "index": index, "content_block": {"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex}", "name": function.get("name", ""), "input": {}}}),
                    _sse("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": arguments}}),
                    _sse("content_block_stop", {"type": "content_block_stop", "index": index}),
                ]
                for event in events:
                    telemetry.response_bytes += len(event)
                    yield event
        events = [
            _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use" if used_tool else "end_turn", "stop_sequence": None}, "usage": {"output_tokens": telemetry.output_tokens}}),
            _sse("message_stop", {"type": "message_stop"}),
        ]
        for event in events:
            telemetry.response_bytes += len(event)
            yield event
    except Exception as exc:
        telemetry.error_type = type(exc).__name__
        raise
    finally:
        await upstream.aclose()
        runtime.active.pop(f"{session.session_id}:{telemetry.request_id}", None)
        _complete(runtime, session, telemetry, original, collected)


def _anthropic_to_ollama(payload: dict[str, Any], settings: Settings) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    system = payload.get("system")
    if system:
        if isinstance(system, list):
            system = "".join(part.get("text", "") for part in system if isinstance(part, dict))
        messages.append({"role": "system", "content": system})
    for message in payload.get("messages", []):
        content = message.get("content", "")
        if isinstance(content, list):
            text = "".join(part.get("text", "") for part in content if part.get("type") == "text")
            tool_uses = [part for part in content if part.get("type") == "tool_use"]
            tool_results = [part for part in content if part.get("type") == "tool_result"]
            if text or tool_uses:
                converted: dict[str, Any] = {"role": message.get("role", "user"), "content": text}
                if tool_uses:
                    converted["tool_calls"] = [{"function": {"name": part.get("name", ""), "arguments": part.get("input", {})}} for part in tool_uses]
                messages.append(converted)
            for part in tool_results:
                result_content = part.get("content", "")
                if isinstance(result_content, list):
                    result_content = "".join(block.get("text", "") for block in result_content if block.get("type") == "text")
                messages.append({"role": "tool", "content": result_content})
            continue
        messages.append({"role": message.get("role", "user"), "content": content})
    result: dict[str, Any] = {
        "model": settings.model_name(payload.get("model", "")),
        "messages": messages,
        "stream": bool(payload.get("stream", False)),
        "options": {"num_predict": payload.get("max_tokens", 1024)},
    }
    if "temperature" in payload:
        result["options"]["temperature"] = payload["temperature"]
    if payload.get("tools"):
        result["tools"] = [
            {"type": "function", "function": {"name": tool["name"], "description": tool.get("description", ""), "parameters": tool.get("input_schema", {})}}
            for tool in payload["tools"]
        ]
    return result


def _ollama_to_anthropic(data: dict[str, Any], model: str) -> dict[str, Any]:
    message = data.get("message", {})
    content: list[dict[str, Any]] = []
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        function = call.get("function", {})
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {"value": arguments}
        content.append({"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex}", "name": function.get("name", ""), "input": arguments})
    return {
        "id": f"msg_{uuid.uuid4().hex}", "type": "message", "role": "assistant", "model": model,
        "content": content,
        "stop_reason": "tool_use" if message.get("tool_calls") else "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": data.get("prompt_eval_count", 0), "output_tokens": data.get("eval_count", 0)},
    }


async def _simple_forward(runtime: Runtime, request: Request, session: Session | None, path: str, backend_name: str | None):
    backend = runtime.settings.backend(backend_name or (session.backend if session else None))
    assert runtime.client
    try:
        response = await runtime.client.request(request.method, backend.url.rstrip("/") + path)
        data = response.json()
        if path == "/api/tags":
            data = {"object": "list", "data": [{"id": item["name"], "object": "model", "owned_by": "ollama"} for item in data.get("models", [])]}
        return JSONResponse(data, status_code=response.status_code)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Backend unavailable: {exc}") from exc


def _resolve_session(runtime: Runtime, session_id: str | None, payload: dict[str, Any], backend: str | None) -> Session:
    if session_id:
        return _require_session(runtime, session_id)
    return runtime.sessions.create({
        "client": "unregistered", "model": payload.get("model"),
        "backend": backend or runtime.settings.default_backend,
        "tags": {"implicit": True},
    })


def _require_session(runtime: Runtime, session_id: str) -> Session:
    session = runtime.sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session

def _require_idle_session(runtime: Runtime, session: Session) -> None:
    if any(record["session_id"] == session.session_id for record in runtime.active.values()):
        raise HTTPException(status_code=409, detail="This session has requests in flight. Wait for them to finish before clearing or deleting it.")


def _telemetry(session: Session, request_id: str, endpoint: str, body: bytes, payload: dict[str, Any], model: str) -> RequestTelemetry:
    options = payload.get("options") or {}
    return RequestTelemetry(
        session_id=session.session_id, request_id=request_id, client=session.client,
        model=model, backend=session.backend, endpoint=endpoint, request_bytes=len(body),
        context_size=options.get("num_ctx"), temperature=payload.get("temperature", options.get("temperature")),
        max_output_tokens=payload.get("max_tokens", options.get("num_predict")),
    )


def _complete(runtime: Runtime, session: Session, telemetry: RequestTelemetry, request_payload: Any, response_payload: Any) -> dict[str, Any]:
    runtime.active.pop(f"{session.session_id}:{telemetry.request_id}", None)
    record = telemetry.finish()
    runtime.sessions.append_request(session, record)
    runtime.recent.append(record)
    runtime.total_input_tokens += record["input_tokens"]
    runtime.total_output_tokens += record["output_tokens"]
    if session.trace:
        runtime.sessions.write_trace(session, telemetry.request_id, {
            "metadata": {"proxy_instance_id": runtime.proxy_instance_id, **record},
            "request": request_payload, "response": response_payload,
        })
    return record


def _proxy_error(runtime: Runtime, session: Session, telemetry: RequestTelemetry, payload: Any, exc: Exception) -> JSONResponse:
    telemetry.status = 502
    telemetry.error_type = type(exc).__name__
    record = _complete(runtime, session, telemetry, payload, {"error": str(exc)})
    return JSONResponse({"error": {"message": f"Backend unavailable: {exc}", "type": "backend_error"}}, status_code=502, headers=_telemetry_headers(record))


def _json(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body or b"{}")
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def _upstream_headers(headers: Any) -> dict[str, str]:
    excluded = {"host", "content-length", "connection", "x-ai-proxy-session", "x-ai-proxy-backend"}
    return {key: value for key, value in headers.items() if key.lower() not in excluded}


def _active_record(item: RequestTelemetry) -> dict[str, Any]:
    return {"session_id": item.session_id, "request_id": item.request_id, "client": item.client, "model": item.model, "backend": item.backend, "response_bytes": 0, "ttft_ms": None}


def _live_ttft(item: RequestTelemetry) -> float | None:
    return round((item.first_token - item.started) * 1000, 3) if item.first_token else None


def _telemetry_headers(record: dict[str, Any]) -> dict[str, str]:
    return {"X-Request-ID": str(record["request_id"]), "X-Session-ID": record["session_id"]}


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


def _configure_logging(path: Path) -> None:
    if LOGGER.handlers:
        return
    LOGGER.setLevel(logging.INFO)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local AI agent proxy")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    settings = Settings.load()
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
