"""Agent-facing endpoints: OpenAI, Anthropic and native Ollama APIs."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Callable

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .. import api_providers, auth, model_runner, strata
from ..config import shared_model
from ..links import LinkDown
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
# Backends that speak the Anthropic Messages API themselves, so /anthropic/v1/messages passes through
# (an api backend only when it was declared to: _forward checks its protocols).
MESSAGES_BACKENDS = {"anthropic", "strata", "docker", "llamacpp", "api", "link"}
# The native Ollama calls agents make. The rest (pull, delete, create, copy, push, ...) manage a
# model server, which only its machine does (agent-machine), so the server doesn't forward them.
NATIVE_AGENT_PATHS = {"chat", "generate", "embed", "embeddings", "tags", "show", "ps", "version"}


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
        session = runtime.sessions.get(session_id or "")
        backend_name = x_ai_proxy_backend or (session.backend if session else None)
        if path == "models" and request.method == "GET":
            return await _list_models(runtime, request, backend_name, session)
        # Only Ollama needs translating; Strata, Docker Model Runner and llama.cpp take OpenAI requests as they are.
        if path == "chat/completions" and request.method == "POST" and _backend(runtime, request, backend_name, session).type == "ollama":
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
        if path.strip("/") not in NATIVE_AGENT_PATHS:
            raise HTTPException(status_code=404, detail="Not an agent API. Models are managed from the machine that runs the model server (console: Machines).")
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
        backend = _backend(runtime, request, x_ai_proxy_backend or (session.backend if session else None), session)
        if backend.type in MESSAGES_BACKENDS:
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
        backend = _backend(runtime, request, x_ai_proxy_backend or (session.backend if session else None), session)
        if backend.type in MESSAGES_BACKENDS and (backend.type != "api" or api_providers.speaks(backend, "anthropic")):
            return await _forward(runtime, request, session_id, "/v1/messages/count_tokens", x_ai_proxy_backend)
        return {"input_tokens": estimate_tokens(_json(await request.body()))}

    # Registered after the Messages routes, so this only sees Claude Code's other
    # calls (model lists, account and feature endpoints) for hosted Anthropic.
    @app.api_route("/anthropic/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    @app.api_route("/session/{session_id}/anthropic/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def anthropic_other(
        request: Request,
        path: str,
        session_id: str | None = None,
        x_ai_proxy_session: str | None = Header(default=None),
        x_ai_proxy_backend: str | None = Header(default=None),
    ):
        session_id = session_id or x_ai_proxy_session
        session = runtime.sessions.get(session_id or "")
        backend = _backend(runtime, request, x_ai_proxy_backend or (session.backend if session else None), session)
        if backend.type != "anthropic":
            raise HTTPException(status_code=404, detail=f"/{path} is only available with a hosted Anthropic backend")
        return await _forward(runtime, request, session_id, f"/{path}", x_ai_proxy_backend)


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
    session = _resolve(runtime, request, session_id, original, backend_override)
    backend = _backend(runtime, request, backend_override or session.backend, session)
    if backend.type != "ollama":
        raise HTTPException(status_code=400, detail="Ollama translation requires an Ollama backend")
    _check_model(runtime, request, backend, session, original.get("model") or session.model)
    request_id = await runtime.sessions.next_request_id(session)
    ollama_payload = to_ollama()
    raw = json.dumps(original).encode()
    telemetry = _telemetry(session, request_id, request.url.path, raw, ollama_payload, original.get("model", ""))
    telemetry.backend = backend.name
    runtime.track(telemetry)
    runtime.attach(telemetry, original, ollama_payload)
    refused = _over_limit(runtime, request, session, telemetry, original, backend)
    if refused is not None:
        return refused
    assert runtime.client
    try:
        upstream = await runtime.client.send(
            runtime.client.build_request("POST", await runtime.tunnels.base_url(backend) + "/api/chat", json=ollama_payload),
            stream=True,
        )
    except httpx.HTTPError as exc:
        return runtime.proxy_error(session, telemetry, original, exc, backend)
    except asyncio.CancelledError:
        _cancel_request(runtime, session, telemetry, original, ollama_payload)
        raise
    telemetry.status = upstream.status_code
    if upstream.status_code >= 400 or not original.get("stream", False):
        response_body = await _read_upstream(runtime, session, telemetry, original, upstream, backend, ollama_payload)
        if isinstance(response_body, JSONResponse):
            return response_body
    if upstream.status_code >= 400:
        body = _json(response_body)
        telemetry.response_bytes = len(json.dumps(body))
        telemetry.error_type = "upstream_error"
        record = runtime.complete(session, telemetry, original, body, ollama_payload)
        return JSONResponse(error(body, upstream.status_code), status_code=upstream.status_code, headers=telemetry_headers(record))
    if not original.get("stream", False):
        data = _json(response_body)
        telemetry.apply_ollama_stats(data)
        if (data.get("message") or {}).get("content") or (data.get("message") or {}).get("tool_calls"):
            telemetry.saw_content()
        response = full(data)
        telemetry.response_bytes = len(json.dumps(response).encode())
        record = runtime.complete(session, telemetry, original, response, ollama_payload)
        return JSONResponse(response, headers=telemetry_headers(record))
    return StreamingResponse(
        _finishing(runtime, request, session, _translated_stream(runtime, session, telemetry, original, ollama_payload, upstream, stream())),
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


# A client that sends this header (the console's Ask a model) has its streamed reply read to the end
# even if it disconnects, so it can find the rest on /api/live and the reply is recorded whole.
FINISH_HEADER = "x-ai-proxy-finish"


def _finishing(runtime: Runtime, request: Request, session: Session, body: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """The reply's stream, read in a task of its own (Runtime.finish_later) when the client asked for it."""
    if request.headers.get(FINISH_HEADER) != "1":
        return body
    return _outliving(runtime, session.session_id, body)


async def _outliving(runtime: Runtime, session_id: str, body: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def read() -> None:
        try:
            async for chunk in body:
                queue.put_nowait(chunk)
        finally:
            queue.put_nowait(None)

    # Everything the reply's generator does (telemetry, the live view, recording) runs in the task;
    # this only passes on what it yields, for as long as the client is there to take it.
    task = runtime.finish_later(session_id, read())
    while (chunk := await queue.get()) is not None:
        yield chunk
    if not task.cancelled():
        await task


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
    """Pass a request through to the backend unchanged, apart from Ollama model aliases and profiles, Strata's key,
    Docker Model Runner's path prefixes and, for an API provider, its key, model aliases and paths."""
    body = await request.body()
    payload = _json(body)
    original_payload = json.loads(json.dumps(payload))
    identity = auth.request_identity(request)
    session = _resolve(runtime, request, session_id, payload, backend_override) if session_id else None
    backend = _backend(runtime, request, backend_override or (session.backend if session else None), session)
    if request.method == "POST":
        _check_model(runtime, request, backend, session, payload.get("model") or (session.model if session else None))
    if backend.type == "anthropic" and "/anthropic/" not in request.url.path:
        raise HTTPException(status_code=400, detail="Anthropic backends are only reachable through the /anthropic endpoints")
    if backend.type in {"strata", "llamacpp", "api"} and upstream_path.startswith("/api/"):
        name = {"strata": "Strata", "llamacpp": "llama.cpp", "api": backend.name}[backend.type]
        raise HTTPException(status_code=400, detail=f"{name} speaks the OpenAI and Anthropic APIs, not Ollama's native /api. Point the agent at /v1 or /anthropic.")
    protocol = _api_protocol(backend, upstream_path, request.method) if backend.type == "api" else None
    if request.method == "GET" and upstream_path in ("/api/tags", "/api/ps"):
        limit = _model_limit(runtime, request, backend, session)
        if limit is not None:
            return await _shared_listing(runtime, request, backend, upstream_path, limit)
    if session is None and (request.method == "GET" or (request.method == "POST" and upstream_path == "/api/show")):
        client, url, headers, params, extra = await _upstream_for(runtime, request, backend, upstream_path, protocol, body, None)
        try:
            upstream = await client.request(request.method, url, headers=headers, params=params, content=body or None, **extra)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Backend unavailable") from exc
        return Response(upstream.content, status_code=upstream.status_code, media_type=upstream.headers.get("content-type"))
    if session is None:
        session = runtime.resolve_session(None, payload, backend.name, identity)
    request_id = await runtime.sessions.next_request_id(session)
    model = payload.get("model") or session.model or ""
    if model and request.method == "POST" and backend.type == "ollama":
        profile = runtime.settings.profile(model)
        if upstream_path in NATIVE_PROFILE_PATHS:
            apply_profile(payload, profile)
        else:
            payload["model"] = profile.ollama_model
        body = json.dumps(payload).encode()
    if backend.type == "api":
        payload, body = _api_payload(backend, original_payload, body)
    telemetry = _telemetry(session, request_id, request.url.path, body, payload, model)
    telemetry.backend = backend.name
    telemetry.protocol = protocol
    if backend.type == "api" and payload.get("model") and payload.get("model") != model:
        telemetry.upstream_model = str(payload["model"])
    runtime.track(telemetry)
    runtime.attach(telemetry, original_payload, payload if payload != original_payload else None)
    refused = _over_limit(runtime, request, session, telemetry, original_payload, backend)
    if refused is not None:
        return refused
    tried = [backend.name]
    while True:
        client, url, headers, params, extra = await _upstream_for(runtime, request, backend, upstream_path, protocol, body, session)
        started = time.perf_counter()
        try:
            upstream = await client.send(
                client.build_request(request.method, url, content=body or None, headers=headers, params=params, **extra),
                stream=True,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Nothing reached the provider, so another may take the request: only the ones a site admin listed (fallback).
            _observe(runtime, backend, None, started, exc)
            alternative = _fallback(runtime, request, session, backend, protocol, model, tried) if backend.type == "api" else None
            if alternative is None:
                return runtime.proxy_error(session, telemetry, original_payload, exc, backend)
            _record_fallback(runtime, request, session, telemetry, backend, alternative, exc)
            backend = alternative
            tried.append(backend.name)
            payload, body = _api_payload(backend, original_payload, json.dumps(original_payload).encode() if original_payload else body)
            continue
        except httpx.HTTPError as exc:
            _observe(runtime, backend, None, started, exc)
            return runtime.proxy_error(session, telemetry, original_payload, exc, backend)
        except asyncio.CancelledError:
            _cancel_request(runtime, session, telemetry, original_payload, payload if payload != original_payload else None)
            raise
        break
    _observe(runtime, backend, upstream.status_code, started, None)
    upstream_request = payload if payload != original_payload else None
    telemetry.status = upstream.status_code
    if upstream.status_code == 429:
        telemetry.rate_limited = True
    content_type = upstream.headers.get("content-type", "application/json")
    response_headers = {
        name: value for name, value in upstream.headers.items()
        if name in {"retry-after", "retry-after-ms", "x-should-retry", "request-id", "x-request-id"}
        or name.startswith(("anthropic-ratelimit-", "x-ratelimit-"))
    }
    # The upstream's own request id stays visible as upstream-request-id: X-Request-ID is this server's.
    if "x-request-id" in response_headers:
        response_headers["upstream-request-id"] = response_headers.pop("x-request-id")
    if telemetry.fallback_from:
        response_headers["x-ai-proxy-backend"] = backend.name
    is_stream = upstream.status_code < 400 and (bool(payload.get("stream")) or "ndjson" in content_type or "event-stream" in content_type)
    if upstream_path == "/api/chat" or upstream_path == "/api/generate":
        # Native Ollama streams by default when "stream" is omitted.
        is_stream = is_stream or ("stream" not in payload and "ndjson" in content_type)
    if not is_stream:
        response_body = await _read_upstream(runtime, session, telemetry, original_payload, upstream, backend, upstream_request)
        if isinstance(response_body, JSONResponse):
            return response_body
        telemetry.response_bytes = len(response_body)
        obj = _json(response_body)
        telemetry.apply_ollama_stats(obj)
        if has_content(obj):
            telemetry.saw_content()
        if upstream.status_code >= 400:
            telemetry.error_type = "upstream_error"
        record = runtime.complete(session, telemetry, original_payload, obj, upstream_request)
        if backend.type != "ollama":
            return Response(response_body, status_code=upstream.status_code, media_type=content_type, headers={**response_headers, **telemetry_headers(record)})
        return JSONResponse(obj, status_code=upstream.status_code, headers=telemetry_headers(record))
    return StreamingResponse(
        _finishing(runtime, request, session, _stream_upstream(runtime, session, telemetry, original_payload, upstream_request, upstream)),
        status_code=upstream.status_code,
        media_type=content_type,
        headers={**response_headers, "X-Request-ID": request_id, "X-Session-ID": session.session_id},
    )


def _api_protocol(backend: Any, upstream_path: str, method: str) -> str:
    """The protocol of an agent's request to an API provider, or the reason it can't take it."""
    protocol = api_providers.protocol_for(upstream_path)
    if protocol is None:
        raise HTTPException(status_code=404, detail=f"{upstream_path} isn't an agent API {backend.name} is used for")
    if not api_providers.speaks(backend, protocol):
        spoken = ", ".join(api_providers.options(backend)["protocols"])
        raise HTTPException(status_code=400, detail=f"{backend.name} isn't set up for {protocol} (it speaks {spoken}); choose a provider that does")
    return protocol


def _api_payload(backend: Any, original: dict[str, Any], body: bytes) -> tuple[dict[str, Any], bytes]:
    """The request as an API provider gets it: the agent's own, with its model alias replaced by the provider's id."""
    payload = json.loads(json.dumps(original))
    model = payload.get("model")
    if isinstance(model, str) and model:
        mapped = api_providers.upstream_model(backend, model)
        if mapped != model:
            payload["model"] = mapped
            return payload, json.dumps(payload).encode()
    return payload, body


async def _upstream_for(
    runtime: Runtime, request: Request, backend: Any, upstream_path: str, protocol: str | None, body: bytes = b"", session: Session | None = None,
) -> tuple[httpx.AsyncClient, str, dict[str, str], Any, dict[str, Any]]:
    """Where a request goes and with what: client, URL, headers, query and request options."""
    if backend.type == "link":
        # A provider let in over a bridge: to the server that gives it, signed (bridges.py), as the agent sent it.
        try:
            client, url, headers = runtime.bridges.upstream(request, backend, _agent_path(request), body, session)
        except LinkDown as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return client, url, headers, None, {}
    headers = _upstream_headers(request.headers)
    if backend.type == "api":
        from .api_providers import upstream

        try:
            client, base = await upstream(runtime, backend)
            headers = api_providers.headers(backend, headers, runtime.api_keys.get(backend, runtime.settings.backends), protocol)
        except api_providers.ProviderError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        url = api_providers.target(base, backend, upstream_path)
        return client, url, headers, api_providers.query(backend, request.query_params), {"timeout": api_providers.timeout(backend)}
    if backend.type == "strata":
        headers = strata.with_key(backend, headers)
    elif backend.type == "llamacpp":
        headers = strata.with_container_key(backend, headers)
    target = model_runner.upstream_path(upstream_path) if backend.type == "docker" else upstream_path
    assert runtime.client
    return runtime.client, await runtime.tunnels.base_url(backend) + target, headers, request.query_params, {}


def _observe(runtime: Runtime, backend: Any, status: int | None, started: float, error: Exception | None) -> None:
    """An API provider's health as this process sees it: the last answer's status and time to headers."""
    if backend.type == "api":
        runtime.api_health.record(backend.name, status, (time.perf_counter() - started) * 1000, runtime.unavailable(error) if error else None)


def _fallback(runtime: Runtime, request: Request, session: Session, backend: Any, protocol: str | None, model: str, tried: list[str]) -> Any:
    """The first of the provider's fallback list that may take this request: an enabled API provider the
    session's person may use, speaking the protocol, knowing the model, and not over their limit there."""
    usable = runtime.fleet.usable_by(auth.request_identity(request))
    for name in api_providers.options(backend)["fallback"]:
        candidate = runtime.settings.backends.get(name)
        if candidate is None or name in tried or candidate.type != "api" or not api_providers.enabled(candidate) or not usable(candidate):
            continue
        limit = _model_limit(runtime, request, candidate, session)
        if limit is not None and not shared_model(limit, model):
            continue
        configured = api_providers.options(candidate)
        if not api_providers.speaks(candidate, protocol) or (model and model not in configured["models"] and not configured["discover"]):
            continue
        if runtime.over_limit(session, f"backend:{candidate.name}") is not None:
            continue
        return candidate
    return None


def _record_fallback(runtime: Runtime, request: Request, session: Session, telemetry: RequestTelemetry, failed: Any, chosen: Any, exc: Exception) -> None:
    """A request moved to a fallback provider: on its record, in the log and in the audit log."""
    telemetry.fallback_from = telemetry.fallback_from or failed.name
    telemetry.routing.append({"backend": failed.name, "error": runtime.unavailable(exc), "next": chosen.name})
    telemetry.backend = chosen.name
    LOGGER.warning("fallback session=%s request=%s from=%s to=%s: %s", session.session_id, telemetry.request_id, failed.name, chosen.name, runtime.unavailable(exc))
    identity = auth.request_identity(request)
    runtime.audit.record(runtime.audit.event(
        "backend.fallback", tenant=getattr(identity, "tenant", None), actor=getattr(identity, "user", None), via=getattr(identity, "via", None),
        ip=request.client.host if request.client else None, user_agent=request.headers.get("user-agent"),
        action=f"{request.method} {request.url.path}", target={"backend": failed.name, "next": chosen.name},
        reason=runtime.unavailable(exc), request_id=telemetry.request_id, session_id=session.session_id,
    ))


async def _read_upstream(
    runtime: Runtime, session: Session, telemetry: RequestTelemetry, payload: Any,
    upstream: httpx.Response, backend: Any, upstream_request: Any = None,
) -> bytes | JSONResponse:
    """Buffer a reply, releasing its connection and completing failures even after headers arrived."""
    try:
        try:
            return await upstream.aread()
        finally:
            await upstream.aclose()
    except httpx.HTTPError as exc:
        return runtime.proxy_error(session, telemetry, payload, exc, backend)
    except asyncio.CancelledError:
        _cancel_request(runtime, session, telemetry, payload, upstream_request)
        raise


def _cancel_request(runtime: Runtime, session: Session, telemetry: RequestTelemetry, payload: Any, upstream_request: Any = None) -> None:
    telemetry.error_type = "client_closed"
    runtime.complete(session, telemetry, payload, {"error": "Client closed the request"}, upstream_request)


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


async def _list_models(runtime: Runtime, request: Request, backend_name: str | None, session: Session | None = None) -> JSONResponse:
    backend = _backend(runtime, request, backend_name, session)
    limit = _model_limit(runtime, request, backend, session)
    if limit is None:
        return await _all_models(runtime, request, backend, session)
    # Reached through a share: only the models shared, as the backend lists them (ids it doesn't list aren't made up).
    listed = await _all_models(runtime, request, backend, session)
    try:
        document = json.loads(listed.body)
    except ValueError:
        return listed
    if listed.status_code >= 400 or not isinstance(document, dict) or not isinstance(document.get("data"), list):
        return listed
    document["data"] = [item for item in document["data"] if isinstance(item, dict) and shared_model(limit, str(item.get("id") or ""))]
    return JSONResponse(document, status_code=listed.status_code)


async def _shared_listing(runtime: Runtime, request: Request, backend: Any, upstream_path: str, limit: list[str]) -> Response:
    """Ollama's own model lists (/api/tags, /api/ps) for someone it's shared with: only the shared models."""
    client, url, headers, params, extra = await _upstream_for(runtime, request, backend, upstream_path, None, b"", None)
    try:
        upstream = await client.request("GET", url, headers=headers, params=params, **extra)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Backend unavailable") from exc
    try:
        document = upstream.json()
    except ValueError:
        document = None
    if upstream.status_code >= 400 or not isinstance(document, dict) or not isinstance(document.get("models"), list):
        return Response(upstream.content, status_code=upstream.status_code, media_type=upstream.headers.get("content-type"))
    document["models"] = [
        item for item in document["models"] if isinstance(item, dict) and shared_model(limit, str(item.get("name") or item.get("model") or ""))
    ]
    return JSONResponse(document, status_code=upstream.status_code)


async def _all_models(runtime: Runtime, request: Request, backend: Any, session: Session | None) -> JSONResponse:
    if backend.type == "link":
        try:
            client, url, headers = runtime.bridges.upstream(request, backend, "/v1/models", b"", session)
            response = await client.get(url, headers=headers)
            return JSONResponse(response.json(), status_code=response.status_code)
        except (LinkDown, httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=f"Backend unavailable: {exc}") from exc
    if backend.type == "api":
        return JSONResponse(await _api_model_list(runtime, backend))
    if backend.type == "anthropic":
        return JSONResponse({"object": "list", "data": [
            {"id": alias, "object": "model", "owned_by": "agent-proxy"}
            for alias in runtime.settings.models
        ]})
    assert runtime.client
    if backend.type in {"strata", "docker", "llamacpp"}:
        path = model_runner.upstream_path("/v1/models") if backend.type == "docker" else "/v1/models"
        headers = strata.with_key(backend, {}) if backend.type == "strata" else strata.with_container_key(backend, {})
        try:
            response = await runtime.client.get(await runtime.tunnels.base_url(backend) + path, headers=headers)
            return JSONResponse(response.json(), status_code=response.status_code)
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=f"Backend unavailable: {exc}") from exc
    try:
        response = await runtime.client.get(await runtime.tunnels.base_url(backend) + "/api/tags")
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Backend unavailable: {exc}") from exc
    models = [{"id": item["name"], "object": "model", "owned_by": "ollama"} for item in data.get("models", [])]
    known = {item["id"] for item in models}
    models += [
        {"id": alias, "object": "model", "owned_by": "agent-proxy"}
        for alias in runtime.settings.models if alias not in known
    ]
    return JSONResponse({"object": "list", "data": models})


async def _api_model_list(runtime: Runtime, backend: Any) -> dict[str, Any]:
    """An API provider's models for agents: its aliases, then (with discover) what it lists itself."""
    from .api_providers import discover

    configured = api_providers.options(backend)
    data = [{"id": alias, "object": "model", "owned_by": backend.name} for alias in configured["models"]]
    if configured["discover"]:
        await discover(runtime, backend, force=False)
        catalog = runtime.api_discovery.get(backend.name)
        data += [{"id": item, "object": "model", "owned_by": backend.name} for item in catalog.models if item not in configured["models"]] if catalog else []
    return {"object": "list", "data": data}


def _over_limit(runtime: Runtime, request: Request, session: Session, telemetry: RequestTelemetry, payload: Any, backend: Any) -> JSONResponse | None:
    """A 429 when the session's person has used their organization's limit on this backend; None to go ahead."""
    if request.method != "POST":
        return None
    provider = f"backend:{backend.name}"
    blocked = runtime.over_limit(session, provider)
    if blocked is None:
        return None
    _, body, headers = runtime.refuse_over_limit(session, telemetry, payload, provider, blocked)
    return JSONResponse(body, status_code=429, headers=headers)


def _model_limit(runtime: Runtime, request: Request, backend: Any, session: Session | None) -> list[str] | None:
    """The only models the caller may use on backend, when they reach it through a share with every
    organization (fleet.model_limit); None for any. A request a peer relayed, or a guest's session, was
    decided by its bridge instead."""
    if _relayed(request) is not None or (session is not None and isinstance(session.tags.get("allowed_backends"), list)):
        return None
    return runtime.fleet.model_limit(auth.request_identity(request), backend)


def _check_model(runtime: Runtime, request: Request, backend: Any, session: Session | None, model: str | None) -> None:
    """Refuse, before anything goes upstream, a model the caller's share of this backend doesn't include."""
    limit = _model_limit(runtime, request, backend, session)
    if limit is not None and not shared_model(limit, model):
        raise HTTPException(status_code=403, detail=f"{model or 'A request without a model'} isn't shared with your organization on {backend.name}")


def _backend(runtime: Runtime, request: Request, name: str | None, session: Session | None = None):
    """The backend named (a session's, or X-AI-Proxy-Backend), if it's one the caller's tenant may use:
    another tenant's model servers look as unknown as ones that don't exist.

    A session a guest of a linked server launched here carries the backends it may use, set by the server
    from the bridge (allowed_backends); only those. A request a peer relayed (relay) has its backend already."""
    relayed = _relayed(request)
    if relayed is not None:
        return relayed["backend"]
    identity = auth.request_identity(request)
    usable = runtime.fleet.usable_by(identity)
    allowed = session.tags.get("allowed_backends") if session is not None else None
    if isinstance(allowed, list):
        usable = lambda backend: backend.name in allowed  # noqa: E731
    try:
        return runtime.settings.backend(name, usable=usable)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# Where the giving side of a bridge puts the session and backend of a request a peer relayed (relay).
RELAYED = "peer_relay"


def _relayed(request: Request) -> dict[str, Any] | None:
    return (request.scope.get("state") or {}).get(RELAYED)


def _resolve(runtime: Runtime, request: Request, session_id: str | None, payload: dict[str, Any], backend_override: str | None) -> Session:
    relayed = _relayed(request)
    if relayed is not None:
        return relayed["session"]
    return runtime.resolve_session(session_id, payload, backend_override, auth.request_identity(request))


def _agent_path(request: Request) -> str:
    """The path as an agent sent it, without its /session/<id>: what the giving server's relay serves."""
    path = request.url.path
    if path.startswith("/session/"):
        path = "/" + path.split("/", 3)[3] if path.count("/") >= 3 else "/"
    query = request.url.query
    return path + (f"?{query}" if query else "")


async def relay(runtime: Runtime, request: Request, path: str, session: Session, backend: Any) -> Any:
    """A request a guest of a linked server sent for one of this server's providers (routes/links.py, which
    checked it): served as compat serves agents, into the bridge session, with the backend already chosen."""
    request.scope.setdefault("state", {})[RELAYED] = {"session": session, "backend": backend}
    path = path.lstrip("/")
    method = request.method
    if path == "v1/models" and method == "GET":
        return await _list_models(runtime, request, None, session)
    if path == "v1/chat/completions" and method == "POST" and backend.type == "ollama":
        payload = _json(await request.body())
        profile = runtime.settings.profile(payload.get("model") or session.model or "")
        if profile.translate_openai:
            return await _native_chat(
                runtime, request, session.session_id, None, payload,
                to_ollama=lambda: openai_to_ollama(payload, profile),
                full=lambda data: ollama_to_openai(data, payload.get("model", "")),
                stream=lambda: OpenAIStreamTranslator(payload.get("model", ""), bool((payload.get("stream_options") or {}).get("include_usage"))),
                error=lambda body, status: {"error": {"message": _error_message(body), "type": "backend_error", "code": status}},
                media_type="text/event-stream",
            )
    if path.startswith("v1/") and method in ("GET", "POST"):
        return await _forward(runtime, request, session.session_id, "/" + path, None)
    if path == "anthropic/v1/messages" and method == "POST":
        if backend.type in MESSAGES_BACKENDS:
            return await _forward(runtime, request, session.session_id, "/v1/messages", None)
        payload = _json(await request.body())
        profile = runtime.settings.profile(payload.get("model") or session.model or "")
        return await _native_chat(
            runtime, request, session.session_id, None, payload,
            to_ollama=lambda: anthropic_to_ollama(payload, profile),
            full=lambda data: ollama_to_anthropic(data, payload.get("model", "")),
            stream=lambda: AnthropicStreamTranslator(payload.get("model", "")),
            error=lambda body, status: {"type": "error", "error": {"type": "api_error", "message": _error_message(body)}},
            media_type="text/event-stream",
        )
    if path == "anthropic/v1/messages/count_tokens" and method == "POST":
        if backend.type in MESSAGES_BACKENDS and (backend.type != "api" or api_providers.speaks(backend, "anthropic")):
            return await _forward(runtime, request, session.session_id, "/v1/messages/count_tokens", None)
        return {"input_tokens": estimate_tokens(_json(await request.body()))}
    if path.startswith("api/") and path[4:].strip("/") in NATIVE_AGENT_PATHS:
        return await _forward(runtime, request, session.session_id, "/" + path, None)
    raise HTTPException(status_code=404, detail=f"/{path} isn't an agent API a bridged provider serves")


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
    excluded = {"host", "content-length", "connection", "x-ai-proxy-session", "x-ai-proxy-backend", "x-ai-proxy-token", FINISH_HEADER, "cookie", "origin", "accept-encoding", "content-digest"}
    if not headers.get("x-ai-proxy-token") and headers.get("authorization", "").lower().startswith("bearer "):
        excluded.add("authorization")
    # A linked server's signature and assertion (links.py) are for this server, not the provider.
    return {key: value for key, value in headers.items() if key.lower() not in excluded and not key.lower().startswith("mesh-")}

