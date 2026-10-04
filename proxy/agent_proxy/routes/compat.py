"""Agent-facing endpoints: OpenAI, Anthropic and native Ollama APIs."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Callable

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..runtime import Runtime, telemetry_headers
from ..sessions import Session
from ..telemetry import RequestTelemetry, has_content, parse_json_line
from ..translate import (
    AnthropicStreamTranslator,
    OpenAIStreamTranslator,
    anthropic_to_ollama,
    apply_profile,
    estimate_tokens,
    ollama_to_anthropic,
    ollama_to_openai,
    openai_to_ollama,
)


LOGGER = logging.getLogger("agent-proxy")
NATIVE_PROFILE_PATHS = {"/api/chat", "/api/generate"}


def register(app: FastAPI, runtime: Runtime) -> None:
    @app.api_route("/v1/{path:path}", methods=["GET", "POST"])
    @app.api_route("/session/{session_id}/v1/{path:path}", methods=["GET", "POST"])
    async def openai_proxy(
        request: Request,
        path: str,
        session_id: str | None = None,
        x_ai_proxy_session: str | None = Header(default=None),
        x_ai_proxy_backend: str | None = Header(default=None),
    ):
        session_id = session_id or x_ai_proxy_session
        if path == "models" and request.method == "GET":
            session = runtime.sessions.get(session_id or "")
            return await _list_models(runtime, x_ai_proxy_backend or (session.backend if session else None))
        if path == "chat/completions" and request.method == "POST":
            payload = _json(await request.body())
            profile = runtime.settings.profile(payload.get("model") or _session_model(runtime, session_id))
            if profile.translate_openai:
                return await _native_chat(
                    runtime, request, session_id, x_ai_proxy_backend, payload,
                    to_ollama=lambda: openai_to_ollama(payload, profile),
                    full=lambda data: ollama_to_openai(data, payload.get("model", "")),
                    stream=lambda: OpenAIStreamTranslator(
                        payload.get("model", ""), bool((payload.get("stream_options") or {}).get("include_usage"))
                    ),
                    error=lambda body, status: {"error": {"message": _error_message(body), "type": "backend_error", "code": status}},
                    media_type="text/event-stream",
                )
        return await _forward(runtime, request, session_id, f"/v1/{path}", x_ai_proxy_backend)

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "DELETE"])
    @app.api_route("/session/{session_id}/api/{path:path}", methods=["GET", "POST", "DELETE"])
    async def ollama_proxy(
        request: Request,
        path: str,
        session_id: str | None = None,
        x_ai_proxy_session: str | None = Header(default=None),
        x_ai_proxy_backend: str | None = Header(default=None),
    ):
        return await _forward(runtime, request, session_id or x_ai_proxy_session, f"/api/{path}", x_ai_proxy_backend)

    @app.post("/anthropic/v1/messages")
    @app.post("/session/{session_id}/anthropic/v1/messages")
    async def anthropic_messages(
        request: Request,
        session_id: str | None = None,
        x_ai_proxy_session: str | None = Header(default=None),
        x_ai_proxy_backend: str | None = Header(default=None),
    ):
        payload = _json(await request.body())
        session_id = session_id or x_ai_proxy_session
        session = runtime.sessions.get(session_id or "")
        backend = _backend(runtime, x_ai_proxy_backend or (session.backend if session else None))
        if backend.type == "anthropic":
            return await _forward(runtime, request, session_id, "/v1/messages", x_ai_proxy_backend)
        profile = runtime.settings.profile(payload.get("model") or _session_model(runtime, session_id))
        return await _native_chat(
            runtime, request, session_id, x_ai_proxy_backend, payload,
            to_ollama=lambda: anthropic_to_ollama(payload, profile),
            full=lambda data: ollama_to_anthropic(data, payload.get("model", "")),
            stream=lambda: AnthropicStreamTranslator(payload.get("model", "")),
            error=lambda body, status: {"type": "error", "error": {"type": "api_error", "message": _error_message(body)}},
            media_type="text/event-stream",
        )

    @app.post("/anthropic/v1/messages/count_tokens")
    @app.post("/session/{session_id}/anthropic/v1/messages/count_tokens")
    async def anthropic_count_tokens(
        request: Request,
        session_id: str | None = None,
        x_ai_proxy_session: str | None = Header(default=None),
        x_ai_proxy_backend: str | None = Header(default=None),
    ):
        session_id = session_id or x_ai_proxy_session
        session = runtime.sessions.get(session_id or "")
        backend = _backend(runtime, x_ai_proxy_backend or (session.backend if session else None))
        if backend.type == "anthropic":
            return await _forward(runtime, request, session_id, "/v1/messages/count_tokens", x_ai_proxy_backend)
        return {"input_tokens": estimate_tokens(_json(await request.body()))}


async def _native_chat(
    runtime: Runtime,
    request: Request,
    session_id: str | None,
    backend_override: str | None,
    original: dict[str, Any],
    *,
    to_ollama: Callable[[], dict[str, Any]],
    full: Callable[[dict[str, Any]], dict[str, Any]],
    stream: Callable[[], Any],
    error: Callable[[Any, int], dict[str, Any]],
    media_type: str,
):
    """Send a translated request to Ollama's /api/chat and translate the reply back."""
    session = runtime.resolve_session(session_id, original, backend_override)
    backend = _backend(runtime, backend_override or session.backend)
    if backend.type != "ollama":
        raise HTTPException(status_code=400, detail="Ollama translation requires an Ollama backend")
    request_id = await runtime.sessions.next_request_id(session)
    ollama_payload = to_ollama()
    raw = json.dumps(original).encode()
    telemetry = _telemetry(session, request_id, request.url.path, raw, ollama_payload, original.get("model", ""))
    telemetry.backend = backend.name
    runtime.track(telemetry)
    runtime.attach(telemetry, original, ollama_payload)
    assert runtime.client
    try:
        upstream = await runtime.client.send(
            runtime.client.build_request("POST", backend.url.rstrip("/") + "/api/chat", json=ollama_payload),
            stream=True,
        )
    except httpx.HTTPError as exc:
        return runtime.proxy_error(session, telemetry, original, exc)
    telemetry.status = upstream.status_code
    if upstream.status_code >= 400:
        body = _json(await upstream.aread())
        await upstream.aclose()
        telemetry.response_bytes = len(json.dumps(body))
        telemetry.error_type = "upstream_error"
        record = runtime.complete(session, telemetry, original, body, ollama_payload)
        return JSONResponse(error(body, upstream.status_code), status_code=upstream.status_code, headers=telemetry_headers(record))
    if not original.get("stream", False):
        data = _json(await upstream.aread())
        await upstream.aclose()
        telemetry.apply_ollama_stats(data)
        if (data.get("message") or {}).get("content") or (data.get("message") or {}).get("tool_calls"):
            telemetry.saw_content()
        response = full(data)
        telemetry.response_bytes = len(json.dumps(response).encode())
        record = runtime.complete(session, telemetry, original, response, ollama_payload)
        return JSONResponse(response, headers=telemetry_headers(record))
    return StreamingResponse(
        _translated_stream(runtime, session, telemetry, original, ollama_payload, upstream, stream()),
        media_type=media_type,
        headers={"X-Request-ID": request_id, "X-Session-ID": session.session_id, "Cache-Control": "no-cache"},
    )


async def _translated_stream(
    runtime: Runtime,
    session: Session,
    telemetry: RequestTelemetry,
    original: dict[str, Any],
    ollama_payload: dict[str, Any],
    upstream: httpx.Response,
    translator: Any,
) -> AsyncIterator[bytes]:
    collected: list[dict[str, Any]] = []
    telemetry.streaming = True
    try:
        for event in translator.start():
            telemetry.response_bytes += len(event)
            yield event
        async for line in _keepalive(upstream.aiter_lines()):
            if line is None:
                # Ollama is still evaluating the prompt; keep the agent from timing out.
                event = translator.ping()
                telemetry.response_bytes += len(event)
                yield event
                continue
            obj = parse_json_line(line.encode())
            if not obj:
                continue
            collected.append(obj)
            telemetry.apply_ollama_stats(obj)
            if has_content(obj):
                telemetry.saw_content()
            runtime.stream_object(telemetry, obj)
            if obj.get("error"):
                telemetry.error_type = "upstream_error"
            for event in translator.feed(obj):
                telemetry.response_bytes += len(event)
                yield event
            runtime.progress(telemetry)
        for event in translator.finish():
            telemetry.response_bytes += len(event)
            yield event
    except (asyncio.CancelledError, GeneratorExit):
        telemetry.error_type = "client_closed"
        LOGGER.info("client closed stream session=%s request=%s", session.session_id, telemetry.request_id)
        raise
    except Exception as exc:
        telemetry.error_type = type(exc).__name__
        LOGGER.exception("stream failed session=%s request=%s", session.session_id, telemetry.request_id)
        raise
    finally:
        await upstream.aclose()
        runtime.complete(session, telemetry, original, collected, ollama_payload)


KEEPALIVE_SECONDS = 10.0


async def _keepalive(lines: AsyncIterator[str], interval: float = KEEPALIVE_SECONDS) -> AsyncIterator[str | None]:
    """Yield upstream lines, or None after each interval of silence.

    The pending read is never cancelled by a tick, so no upstream data is lost.
    """
    iterator = lines.__aiter__()
    pending: asyncio.Future[str] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield None
                continue
            try:
                line = pending.result()
            except StopAsyncIteration:
                return
            finally:
                pending = None
            yield line
    finally:
        if pending is not None:
            pending.cancel()


async def _forward(
    runtime: Runtime,
    request: Request,
    session_id: str | None,
    upstream_path: str,
    backend_override: str | None,
):
    """Pass a request through to Ollama unchanged apart from model aliases and profiles."""
    body = await request.body()
    payload = _json(body)
    original_payload = json.loads(json.dumps(payload))
    session = runtime.resolve_session(session_id, payload, backend_override)
    backend = _backend(runtime, backend_override or session.backend)
    if backend.type == "anthropic" and upstream_path not in {"/v1/messages", "/v1/messages/count_tokens"}:
        raise HTTPException(status_code=400, detail="Anthropic backends require the Anthropic Messages endpoint")
    request_id = await runtime.sessions.next_request_id(session)
    model = payload.get("model") or session.model or ""
    if model and request.method == "POST" and backend.type == "ollama":
        profile = runtime.settings.profile(model)
        if upstream_path in NATIVE_PROFILE_PATHS:
            apply_profile(payload, profile)
        else:
            payload["model"] = profile.ollama_model
        body = json.dumps(payload).encode()
    telemetry = _telemetry(session, request_id, request.url.path, body, payload, model)
    telemetry.backend = backend.name
    runtime.track(telemetry)
    runtime.attach(telemetry, original_payload, payload if payload != original_payload else None)
    assert runtime.client
    try:
        upstream = await runtime.client.send(
            runtime.client.build_request(
                request.method, backend.url.rstrip("/") + upstream_path,
                content=body or None, headers=_upstream_headers(request.headers), params=request.query_params,
            ),
            stream=True,
        )
    except httpx.HTTPError as exc:
        return runtime.proxy_error(session, telemetry, original_payload, exc)
    upstream_request = payload if payload != original_payload else None
    telemetry.status = upstream.status_code
    content_type = upstream.headers.get("content-type", "application/json")
    response_headers = {
        name: value for name, value in upstream.headers.items()
        if name in {"retry-after", "x-should-retry", "request-id"} or name.startswith("anthropic-ratelimit-")
    }
    is_stream = upstream.status_code < 400 and (bool(payload.get("stream")) or "ndjson" in content_type or "event-stream" in content_type)
    if upstream_path == "/api/chat" or upstream_path == "/api/generate":
        # Native Ollama streams by default when "stream" is omitted.
        is_stream = is_stream or ("stream" not in payload and "ndjson" in content_type)
    if not is_stream:
        response_body = await upstream.aread()
        await upstream.aclose()
        telemetry.response_bytes = len(response_body)
        obj = _json(response_body)
        telemetry.apply_ollama_stats(obj)
        if has_content(obj):
            telemetry.saw_content()
        if upstream.status_code >= 400:
            telemetry.error_type = "upstream_error"
        record = runtime.complete(session, telemetry, original_payload, obj, upstream_request)
        if backend.type == "anthropic":
            return Response(response_body, status_code=upstream.status_code, media_type=content_type, headers={**response_headers, **telemetry_headers(record)})
        return JSONResponse(obj, status_code=upstream.status_code, headers=telemetry_headers(record))
    return StreamingResponse(
        _stream_upstream(runtime, session, telemetry, original_payload, upstream_request, upstream),
        status_code=upstream.status_code,
        media_type=content_type,
        headers={**response_headers, "X-Request-ID": request_id, "X-Session-ID": session.session_id},
    )


async def _stream_upstream(
    runtime: Runtime,
    session: Session,
    telemetry: RequestTelemetry,
    request_payload: dict[str, Any],
    upstream_request: dict[str, Any] | None,
    upstream: httpx.Response,
) -> AsyncIterator[bytes]:
    response_objects: list[dict[str, Any]] = []
    buffer = b""

    def consume(line: bytes) -> None:
        obj = parse_json_line(line)
        if obj:
            response_objects.append(obj)
            telemetry.apply_ollama_stats(obj)
            if has_content(obj):
                telemetry.saw_content()
            runtime.stream_object(telemetry, obj)

    telemetry.streaming = True
    is_sse = "event-stream" in upstream.headers.get("content-type", "")
    at_event_boundary = True
    try:
        async for chunk in _keepalive(upstream.aiter_bytes()):
            if chunk is None:
                # SSE comments are ignored by clients; only insert one between events.
                if is_sse and at_event_boundary:
                    yield b": keep-alive\n\n"
                continue
            telemetry.response_bytes += len(chunk)
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                consume(line)
            runtime.progress(telemetry)
            at_event_boundary = chunk.endswith(b"\n\n")
            yield chunk
        if buffer:
            consume(buffer)
    except (asyncio.CancelledError, GeneratorExit):
        telemetry.error_type = "client_closed"
        LOGGER.info("client closed stream session=%s request=%s", session.session_id, telemetry.request_id)
        raise
    except Exception as exc:
        telemetry.error_type = type(exc).__name__
        LOGGER.exception("stream failed session=%s request=%s", session.session_id, telemetry.request_id)
        raise
    finally:
        await upstream.aclose()
        runtime.complete(session, telemetry, request_payload, response_objects, upstream_request)


async def _list_models(runtime: Runtime, backend_name: str | None) -> JSONResponse:
    backend = _backend(runtime, backend_name)
    assert runtime.client
    try:
        response = await runtime.client.get(backend.url.rstrip("/") + "/api/tags")
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Backend unavailable: {exc}") from exc
    models = [{"id": item["name"], "object": "model", "owned_by": "ollama"} for item in data.get("models", [])]
    known = {item["id"] for item in models}
    models += [
        {"id": alias, "object": "model", "owned_by": "agent-proxy"}
        for alias in runtime.settings.models if alias not in known
    ]
    return JSONResponse({"object": "list", "data": models}, status_code=response.status_code)


def _backend(runtime: Runtime, name: str | None):
    try:
        return runtime.settings.backend(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _session_model(runtime: Runtime, session_id: str | None) -> str:
    session = runtime.sessions.get(session_id or "")
    return (session.model if session else None) or ""


def _telemetry(session: Session, request_id: str, endpoint: str, body: bytes, payload: dict[str, Any], model: str) -> RequestTelemetry:
    options = payload.get("options") or {}
    return RequestTelemetry(
        session_id=session.session_id, request_id=request_id, client=session.client,
        model=model, backend=session.backend, endpoint=endpoint, request_bytes=len(body),
        context_size=options.get("num_ctx"), temperature=payload.get("temperature", options.get("temperature")),
        max_output_tokens=payload.get("max_tokens", options.get("num_predict")),
        estimated_input_tokens=estimate_tokens(payload) if any(key in payload for key in ("messages", "input", "prompt")) else None,
    )


def _json(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body or b"{}")
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def _error_message(body: Any) -> str:
    if isinstance(body, dict):
        value = body.get("error", body)
        if isinstance(value, dict):
            return str(value.get("message", value))
        return str(value)
    return str(body)


def _upstream_headers(headers: Any) -> dict[str, str]:
    excluded = {"host", "content-length", "connection", "x-ai-proxy-session", "x-ai-proxy-backend", "accept-encoding"}
    return {key: value for key, value in headers.items() if key.lower() not in excluded}
