from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections import deque
from functools import cached_property
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from .agents import AgentDefinition, load_agents
from .certs import CertificateAuthority
from .config import Settings
from .live import LiveHub
from .sessions import Session, SessionStore
from .telemetry import RequestTelemetry, stream_deltas


LOGGER = logging.getLogger("agent-proxy")


class Runtime:
    """Process-wide proxy state shared by the management and compatibility routes."""

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
        # Set AI_PROXY_LIVE=0 to stop streaming request text to the UI (counts still stream).
        self.live = LiveHub(enabled=os.getenv("AI_PROXY_LIVE", "1") != "0")
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        # Set by the app's lifespan when the intercept proxy is enabled.
        self.intercept: Any = None
        self.reload_history()

    @cached_property
    def ca(self) -> CertificateAuthority:
        return CertificateAuthority(self.settings.intercept.ca_dir)

    def intercept_info(self) -> dict[str, Any]:
        """How agents reach the HTTPS intercept proxy, for the launcher and the UI."""
        config = self.settings.intercept
        host = config.listen_host
        if host in {"0.0.0.0", "::"}:
            host = urlparse(self.settings.proxy_url).hostname or "127.0.0.1"
        running = bool(self.intercept and self.intercept.running)
        info: dict[str, Any] = {
            **config.public(),
            "running": running,
            "error": self.intercept.error if self.intercept else ("Disabled in config/backends.yaml" if not config.enabled else None),
            "url": f"http://{host}:{config.listen_port}",
        }
        if running:
            info.update(ca_path=str(self.ca.cert_path), bundle_path=str(self.ca.bundle_path), ca_fingerprint=self.ca.fingerprint())
        return info

    def agents(self) -> dict[str, AgentDefinition]:
        # Read on demand so edits to agents.yaml apply without a restart.
        return load_agents(self.settings.agents_file)

    def reload_history(self) -> None:
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        existing: list[dict[str, Any]] = []
        for session in self.sessions.list():
            for record in self.records(session):
                existing.append(record)
                self.total_input_tokens += int(record.get("input_tokens") or 0)
                self.total_output_tokens += int(record.get("output_tokens") or 0)
        self.recent.clear()
        self.recent.extend(sorted(existing, key=lambda item: item.get("timestamp", ""))[-100:])

    def records(self, session: Session) -> list[dict[str, Any]]:
        records = []
        for line in self.sessions.telemetry(session).splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def status(self) -> dict[str, Any]:
        sessions = self.sessions.list()
        return {
            "proxy_instance_id": self.proxy_instance_id,
            "status": "ok",
            "proxy_url": self.settings.proxy_url,
            "listen_address": f"{self.bound_host}:{self.bound_port}",
            "configured_listen_address": f"{self.settings.host}:{self.settings.port}",
            "restart_required": (
                self.bound_host != self.settings.host or self.bound_port != self.settings.port
            ),
            "default_backend": self.settings.default_backend,
            "backends": {
                name: backend.public()
                for name, backend in self.settings.backends.items()
            },
            "intercept": self.intercept_info(),
            "active_sessions": sum(item.ended_at is None for item in sessions),
            "total_sessions": len(sessions),
            "total_requests": sum(item.request_count for item in sessions),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "current_requests": list(self.active.values()),
            "recent_requests": list(self.recent)[::-1],
        }

    def require_session(self, session_id: str) -> Session:
        session = self.sessions.get(session_id)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        return session

    def require_idle(self, session: Session) -> None:
        if any(record["session_id"] == session.session_id for record in self.active.values()):
            raise HTTPException(
                status_code=409,
                detail="This session has requests in flight. Wait for them to finish before clearing or deleting it.",
            )

    def resolve_session(self, session_id: str | None, payload: dict[str, Any], backend: str | None) -> Session:
        if session_id:
            return self.require_session(session_id)
        return self.sessions.create({
            "client": "unregistered", "model": payload.get("model"),
            "backend": backend or self.settings.default_backend,
            "tags": {"implicit": True},
        })

    def track(self, telemetry: RequestTelemetry) -> None:
        self.active[_key(telemetry)] = {
            "session_id": telemetry.session_id, "request_id": telemetry.request_id,
            "client": telemetry.client, "model": telemetry.model, "backend": telemetry.backend,
            "endpoint": telemetry.endpoint, "timestamp": telemetry.timestamp,
            "kind": telemetry.kind, "method": telemetry.method, "host": telemetry.host,
            "response_bytes": 0, "ttft_ms": None, "output_tokens": 0,
        }
        self.live.start(_key(telemetry), telemetry)

    def attach(self, telemetry: RequestTelemetry, request: Any, upstream: Any = None) -> None:
        """Give the live view the agent's request and the payload sent to Ollama."""
        self.live.attach(_key(telemetry), request, upstream)

    def stream_object(self, telemetry: RequestTelemetry, obj: dict[str, Any]) -> None:
        """Publish the text in one streamed object to live subscribers."""
        self.live.raw(_key(telemetry), obj)
        deltas = stream_deltas(obj)
        if deltas:
            telemetry.streamed_chunks += 1
        for kind, text in deltas:
            self.live.delta(_key(telemetry), kind, text)

    def progress(self, telemetry: RequestTelemetry) -> None:
        active = self.active.get(_key(telemetry))
        if active:
            active.update({
                "response_bytes": telemetry.response_bytes,
                "ttft_ms": round((telemetry.first_token - telemetry.started) * 1000, 3) if telemetry.first_token else None,
                "output_tokens": telemetry.output_tokens,
            })

    def complete(
        self,
        session: Session,
        telemetry: RequestTelemetry,
        request_payload: Any,
        response_payload: Any,
        upstream_request: Any = None,
        http: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.active.pop(_key(telemetry), None)
        record = telemetry.finish()
        if isinstance(response_payload, dict):
            # Non-streamed replies arrive in one piece; show them in the live view too.
            self.stream_object(telemetry, response_payload)
        self.live.end(_key(telemetry), record)
        self.sessions.append_request(session, record)
        self.sessions.touch(session)
        self.recent.append(record)
        self.total_input_tokens += record["input_tokens"]
        self.total_output_tokens += record["output_tokens"]
        if session.trace:
            trace = {
                "metadata": {"proxy_instance_id": self.proxy_instance_id, **record},
                "request": request_payload, "response": response_payload,
            }
            if upstream_request is not None:
                trace["upstream_request"] = upstream_request
            if http is not None:
                trace["http"] = http
            self.sessions.write_trace(session, telemetry.request_id, trace)
        return record

    def proxy_error(self, session: Session, telemetry: RequestTelemetry, payload: Any, exc: Exception) -> JSONResponse:
        telemetry.status = 502
        telemetry.error_type = type(exc).__name__
        record = self.complete(session, telemetry, payload, {"error": str(exc)})
        LOGGER.warning("backend unavailable session=%s request=%s: %s", session.session_id, telemetry.request_id, exc)
        return JSONResponse(
            {"error": {"message": f"Backend unavailable: {exc}", "type": "backend_error"}},
            status_code=502, headers=telemetry_headers(record),
        )


def telemetry_headers(record: dict[str, Any]) -> dict[str, str]:
    return {"X-Request-ID": str(record["request_id"]), "X-Session-ID": record["session_id"]}


def _key(telemetry: RequestTelemetry) -> str:
    return f"{telemetry.session_id}:{telemetry.request_id}"
