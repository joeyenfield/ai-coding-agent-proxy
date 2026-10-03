"""Management API used by the web UI, launcher and benchmark runner."""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from .. import host_metrics
from ..runtime import Runtime


LOGGER = logging.getLogger("agent-proxy")


def register(app: FastAPI, runtime: Runtime) -> None:
    settings = runtime.settings

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "proxy_instance_id": runtime.proxy_instance_id}

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return runtime.status()

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        return {"config": settings.network_config(), "restart_required": runtime.status()["restart_required"]}

    @app.put("/api/config")
    async def update_config(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
            async with runtime.config_lock:
                config = settings.update_network_config(data)
        except (ValueError, TypeError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        LOGGER.info("network configuration updated")
        return {"config": config, "restart_required": runtime.status()["restart_required"]}

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
        sessions = [runtime.require_session(session_id)] if session_id else runtime.sessions.list()
        records = []
        for session in sessions:
            traces = set(runtime.sessions.traces(session))
            for record in runtime.records(session):
                records.append({
                    **record,
                    "session_id": session.session_id,
                    "trace_available": f"{record.get('request_id')}.json" in traces,
                })
        return sorted(records, key=lambda item: item.get("timestamp", ""), reverse=True)

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        return runtime.require_session(session_id).public()

    @app.delete("/api/sessions/{session_id}/data")
    async def clear_session_data(session_id: str) -> dict[str, Any]:
        session = runtime.require_session(session_id)
        runtime.require_idle(session)
        try:
            runtime.sessions.clear_data(session)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        runtime.reload_history()
        runtime.live.clear_session(session.session_id)
        return session.public()

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, Any]:
        session = runtime.require_session(session_id)
        runtime.require_idle(session)
        try:
            runtime.sessions.delete(session)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        runtime.reload_history()
        runtime.live.clear_session(session.session_id)
        return {"deleted": session_id}

    @app.patch("/api/sessions/{session_id}")
    async def update_session(session_id: str, request: Request) -> dict[str, Any]:
        session = runtime.require_session(session_id)
        data = await request.json()
        if data.get("ended", True):
            runtime.sessions.end(session, data.get("exit_status"))
        if "trace" in data:
            runtime.sessions.set_trace(session, bool(data["trace"]))
        return session.public()

    @app.get("/api/sessions/{session_id}/telemetry")
    async def download_telemetry(session_id: str) -> PlainTextResponse:
        session = runtime.require_session(session_id)
        return PlainTextResponse(
            runtime.sessions.telemetry(session),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="{session_id}-requests.jsonl"'},
        )

    @app.get("/api/sessions/{session_id}/traces")
    async def list_traces(session_id: str) -> list[str]:
        return runtime.sessions.traces(runtime.require_session(session_id))

    @app.get("/api/sessions/{session_id}/traces/{request_id}")
    async def get_trace(session_id: str, request_id: str) -> dict[str, Any]:
        trace = runtime.sessions.read_trace(runtime.require_session(session_id), request_id)
        if trace is None:
            raise HTTPException(status_code=404, detail="Trace not found")
        return trace

    @app.get("/api/live")
    async def live(request: Request, session_id: str | None = None) -> StreamingResponse:
        """Server-sent events: a snapshot, then start/delta/end events for streamed requests.

        Pass session_id to receive only that session's traffic.
        """
        if session_id:
            runtime.require_session(session_id)
        queue = runtime.live.subscribe()

        def wanted(event: dict[str, Any]) -> bool:
            if not session_id:
                return True
            key = event["request"]["key"] if event["type"] == "start" else event["key"]
            return key.startswith(f"{session_id}:")

        async def events():
            try:
                yield _sse("snapshot", runtime.live.snapshot(session_id))
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield b": keep-alive\n\n"
                        continue
                    if event is None:
                        return
                    if wanted(event):
                        yield _sse(event["type"], event)
            finally:
                runtime.live.unsubscribe(queue)

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/live/{session_id}/{request_id}")
    async def live_details(session_id: str, request_id: str) -> dict[str, Any]:
        """Payloads and the latest raw chunks for one live or recently finished request."""
        details = runtime.live.details(f"{session_id}:{request_id}")
        if details is None:
            raise HTTPException(status_code=404, detail="This request is no longer in the live view. Find it in the request history.")
        return details

    @app.get("/api/agents")
    async def list_agents() -> list[dict[str, Any]]:
        try:
            agents = runtime.agents()
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return [agent.public() for agent in agents.values()]

    @app.post("/api/agents/{agent_id}/sessions", status_code=201)
    async def prepare_agent_session(agent_id: str, request: Request) -> dict[str, Any]:
        """Create a session for an agent and return the environment and command to start it."""
        agent = runtime.agents().get(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        data = await request.json()
        model = str(data.get("model") or "").strip()
        if not model:
            raise HTTPException(status_code=400, detail="A model is required")
        try:
            settings.backend(data.get("backend"))
            session = runtime.sessions.create({
                "client": agent.id, "project": data.get("project"), "model": model,
                "backend": data.get("backend") or settings.default_backend,
                "trace": bool(data.get("trace")), "tags": {"prepared_by": "ui"},
            })
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        launch = agent.render(settings.proxy_url, session.session_id, model)
        return {
            "session": session.public(),
            **launch,
            "shell": {"bash": _bash(launch), "powershell": _powershell(launch)},
        }

    @app.get("/api/host/metrics")
    async def local_metrics() -> dict[str, Any]:
        return await host_metrics.sample()

    @app.get("/api/hosts/{name}")
    async def host(name: str) -> dict[str, Any]:
        """Loaded models, machine load and proxy traffic for one backend, sampled now.

        One backend per call so the UI only contacts the hosts a user has open.
        """
        if name not in settings.backends:
            raise HTTPException(status_code=404, detail=f"Unknown backend '{name}'")
        return {"sampled_at": time.time(), "name": name, **await _host(runtime, name)}

    @app.get("/api/models")
    async def list_models(backend: str | None = None) -> dict[str, Any]:
        """List models on each backend along with the profile the proxy applies to them."""
        names = [backend] if backend else list(settings.backends)
        results = await asyncio.gather(*(_backend_models(runtime, name) for name in names))
        return {
            "backends": dict(zip(names, results)),
            "aliases": {name: settings.profile(name).public() for name in settings.models},
        }


async def _backend_models(runtime: Runtime, name: str) -> dict[str, Any]:
    try:
        backend = runtime.settings.backend(name)
    except ValueError as exc:
        return {"online": False, "error": str(exc), "models": []}
    assert runtime.client
    base = backend.url.rstrip("/")
    try:
        tags, running, version = await asyncio.gather(
            runtime.client.get(f"{base}/api/tags", timeout=5),
            runtime.client.get(f"{base}/api/ps", timeout=5),
            runtime.client.get(f"{base}/api/version", timeout=5),
        )
        tags.raise_for_status()
    except httpx.HTTPError as exc:
        return {"online": False, "url": backend.url, "error": f"{type(exc).__name__}: {exc}", "models": []}
    loaded = {item.get("name"): item for item in _safe_json(running).get("models", [])}
    models = []
    for item in _safe_json(tags).get("models", []):
        details = item.get("details") or {}
        state = loaded.get(item.get("name"))
        models.append({
            "name": item.get("name"),
            "size": item.get("size"),
            "modified_at": item.get("modified_at"),
            "family": details.get("family"),
            "parameter_size": details.get("parameter_size"),
            "quantization": details.get("quantization_level"),
            "loaded": state is not None,
            "context_length": state.get("context_length") if state else None,
            "size_loaded": state.get("size") if state else None,
            "size_vram": state.get("size_vram") if state else None,
            "profile": runtime.settings.profile(item.get("name") or "").public(),
        })
    return {
        "online": True, "url": backend.url,
        "version": _safe_json(version).get("version"),
        "models": sorted(models, key=lambda model: model["name"] or ""),
    }


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'), ensure_ascii=False)}\n\n".encode()


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


async def _host(runtime: Runtime, name: str) -> dict[str, Any]:
    backend = runtime.settings.backends[name]
    base = backend.url.rstrip("/")
    is_local = (urlparse(backend.url).hostname or "") in LOCAL_HOSTS
    assert runtime.client

    async def ollama() -> dict[str, Any]:
        try:
            running, version = await asyncio.gather(
                runtime.client.get(f"{base}/api/ps", timeout=4),
                runtime.client.get(f"{base}/api/version", timeout=4),
            )
            running.raise_for_status()
        except httpx.HTTPError as exc:
            return {"online": False, "error": f"{type(exc).__name__}: {exc}", "models": []}
        models = []
        for item in _safe_json(running).get("models", []):
            size = int(item.get("size") or 0)
            vram = int(item.get("size_vram") or 0)
            details = item.get("details") or {}
            models.append({
                "name": item.get("name"), "size": size, "size_vram": vram,
                "gpu_percent": round(vram * 100 / size) if size else None,
                "context_length": item.get("context_length"), "expires_at": item.get("expires_at"),
                "parameter_size": details.get("parameter_size"), "quantization": details.get("quantization_level"),
            })
        return {"online": True, "version": _safe_json(version).get("version"), "models": models}

    async def machine() -> dict[str, Any]:
        if backend.metrics_url:
            try:
                response = await runtime.client.get(f"{backend.metrics_url.rstrip('/')}/api/host/metrics", timeout=4)
                response.raise_for_status()
                return {"source": "remote", "data": response.json()}
            except (httpx.HTTPError, ValueError) as exc:
                return {"source": "remote", "error": f"Couldn't reach {backend.metrics_url}: {type(exc).__name__}"}
        if is_local:
            return {"source": "local", "data": await host_metrics.sample()}
        return {"source": None, "error": "Run agent-metrics on this machine and set its metrics URL in Settings to chart CPU and GPU load."}

    state, load = await asyncio.gather(ollama(), machine())
    recent = [record for record in runtime.recent if record.get("backend") == name][-20:]
    rates = [record["generation_tps"] for record in recent if record.get("generation_tps")]
    return {
        "url": backend.url, "local": is_local, **state, "machine": load,
        "traffic": {
            "in_flight": sum(item["backend"] == name for item in runtime.active.values()),
            "recent_requests": len(recent),
            "average_tps": round(sum(rates) / len(rates), 1) if rates else None,
        },
    }


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        value = response.json()
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def _bash(launch: dict[str, Any]) -> str:
    lines = [f"export {key}={shlex.quote(value)}" for key, value in launch["env"].items()]
    return "\n".join([*lines, shlex.join(launch["command"])])


def _powershell(launch: dict[str, Any]) -> str:
    def quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    lines = [f"$env:{key} = {quote(value)}" for key, value in launch["env"].items()]
    command = launch["command"]
    return "\n".join([*lines, " ".join([command[0], *(quote(arg) for arg in command[1:])])])
