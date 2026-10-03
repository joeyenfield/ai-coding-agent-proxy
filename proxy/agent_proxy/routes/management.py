"""Management API used by the web UI, launcher and benchmark runner."""

from __future__ import annotations

import asyncio
import logging
import shlex
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

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
