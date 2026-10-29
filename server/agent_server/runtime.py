from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from http.cookiejar import CookieJar, DefaultCookiePolicy
from functools import cached_property
from typing import Any

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from .agents import AgentDefinition, load_agents
from .auth import Auth, UserStore
from .certs import CertificateAuthority
from .chat import Chats
from .config import LOCAL_INTERCEPT_PORT, Settings
from . import strata
from .containers import ContainerManager, KeyStore
from .live import LiveHub
from .remote import RemoteAccess
from .sessions import Session, SessionStore
from .telemetry import RequestTelemetry, stream_deltas
from .fleet import Fleet, provider_id
from .tunnels import TunnelHub
from .terminals import TerminalHub


LOGGER = logging.getLogger("agent-proxy")


def upstream_client(**options: Any) -> httpx.AsyncClient:
    """The HTTP client every forwarded request uses (Runtime.client). It keeps no cookies: it carries
    every user's traffic, so a cookie one agent's service set (a sign-in, say) must never go out with
    another's request. Each request takes only the cookies its own agent sent."""
    return httpx.AsyncClient(cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])), **options)


class Runtime:
    """Process-wide proxy state shared by the management and compatibility routes."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.bound_host = settings.host
        self.bound_port = settings.port
        self.proxy_instance_id = str(uuid.uuid4())
        self.sessions = SessionStore(settings.log_dir)
        self.chats = Chats(self.sessions)
        self.terminals = TerminalHub(self.sessions)
        # Machines signed in to this server: their agents, model servers and launches.
        self.fleet = Fleet(settings.log_dir / "machines.json")
        # Strata traffic is sent with the key its machine reported.
        strata.fleet = self.fleet
        # Agents' requests to machines the server can't reach go through the machines themselves.
        self.tunnels = TunnelHub(self.fleet)
        self.remote = RemoteAccess(settings.remote_key_path)
        self.auth = Auth(UserStore(settings.users_path), self.remote)
        self.remote.auth = self.auth
        # Containers the machines run for the server, asked for over the fleet's connections.
        self.containers = ContainerManager(settings, KeyStore(settings.container_keys_path), self.fleet)
        self.client: httpx.AsyncClient | None = None
        self.intercept_client: httpx.AsyncClient | None = None
        self.recent: deque[dict[str, Any]] = deque(maxlen=100)
        self.active: dict[str, dict[str, Any]] = {}
        self.config_lock = asyncio.Lock()
        # Set AI_PROXY_LIVE=0 to stop streaming request text to the UI (counts still stream).
        self.live = LiveHub(enabled=os.getenv("AI_PROXY_LIVE", "1") != "0")
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        # Set by the app's lifespan when the intercept proxy is enabled.
        self.intercept: Any = None
        # Set by `agent-server`'s supervisor: restarts the server with freshly loaded settings.
        self.restart_hook: Any = None
        # The listen address came from --host/--port, which a restart keeps.
        self.address_pinned = False
        self.restarting = False
        self.started_at = time.time()
        self.reload_history()

    @cached_property
    def ca(self) -> CertificateAuthority:
        return CertificateAuthority(self.settings.intercept.ca_dir)

    def intercept_info(self) -> dict[str, Any]:
        """How agents reach the HTTPS intercept proxy, for the launcher and the UI.

        Agents use the agent-proxy on their own machine, which tunnels here, so url is
        its default address; the agent-proxy puts its own in when the launcher asks.
        """
        config = self.settings.intercept
        running = self.intercept is not None
        info: dict[str, Any] = {
            **config.public(),
            "running": running,
            "error": self.intercept.error if self.intercept else ("Disabled in config/backends.yaml" if not config.enabled else None),
            "url": f"http://127.0.0.1:{LOCAL_INTERCEPT_PORT}",
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

    def status(self, visible: Any = None) -> dict[str, Any]:
        """Totals and traffic; with visible (a session-id test), only the sessions it allows."""
        sessions = [item for item in self.sessions.list() if visible is None or visible(item.session_id)]
        allowed = (lambda record: True) if visible is None else (lambda record: visible(record.get("session_id", "")))
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
            # Each with the machine that runs it, by name, and its model server there ("ollama-11434"), for the console.
            "backends": {
                name: {**backend.public(), "machine_name": self._machine_name(backend.machine), "provider": provider_id(backend)}
                for name, backend in self.settings.backends.items()
            },
            "intercept": self.intercept_info(),
            "active_sessions": sum(item.ended_at is None for item in sessions),
            "total_sessions": len(sessions),
            "total_requests": sum(item.request_count for item in sessions),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "started_at": self.started_at,
            "restart": {"available": self.restart_hook is not None, "pending": self.restarting, "address_pinned": self.address_pinned},
            "current_requests": [record for record in self.active.values() if allowed(record)],
            "recent_requests": [record for record in list(self.recent)[::-1] if allowed(record)],
        }

    def _machine_name(self, machine_id: str | None) -> str | None:
        machine = self.fleet.machines.get(machine_id or "")
        return machine.name if machine else None

    def request_restart(self, wait_seconds: float = 30) -> dict[str, Any]:
        """Restart the server with the configuration on disk once in-flight work finishes (or after wait_seconds)."""
        if self.restart_hook is None:
            raise RuntimeError("This server wasn't started by agent-server (or runs with --reload), so it can't restart itself.")
        # Container actions run on the machines, so a restart here doesn't wait for (or stop) them.
        waiting = {"requests": len(self.active)}
        host, port = (self.bound_host, self.bound_port) if self.address_pinned else self.settings.upcoming_listen()
        if not self.restarting:
            self.restarting = True
            LOGGER.info("restart requested; waiting for %s", waiting)
            self._restart_task = asyncio.ensure_future(self._restart_when_idle(wait_seconds))
        return {"restarting": True, "waiting_for": waiting, "listen": {"host": host, "port": port}}

    async def _restart_when_idle(self, wait_seconds: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_seconds
        while self.active and loop.time() < deadline:
            await asyncio.sleep(0.25)
        # Long-lived streams would hold up the shutdown; end them, their clients reconnect.
        self.live.close()
        await self.terminals.close_all()
        await self.fleet.close_all()
        await self.tunnels.close_all()
        if self.intercept:
            await self.intercept.close_tunnels()
        self.restart_hook()

    def backend_traffic(self, name: str) -> dict[str, Any]:
        """Requests in flight to a backend, and its recent generation speed."""
        recent = [record for record in self.recent if record.get("backend") == name][-20:]
        rates = [record["generation_tps"] for record in recent if record.get("generation_tps")]
        return {
            "in_flight": sum(item["backend"] == name for item in self.active.values()),
            "recent_requests": len(recent),
            "average_tps": round(sum(rates) / len(rates), 1) if rates else None,
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

    def resolve_session(self, session_id: str | None, payload: dict[str, Any], backend: str | None, owner: str | None = None) -> Session:
        if session_id:
            return self.require_session(session_id)
        tags: dict[str, Any] = {"implicit": True}
        if owner:
            # A signed-in user's request without a session: theirs, private.
            tags.update(owner=owner, visibility="private", access="view")
        return self.sessions.create({
            "client": "unregistered", "model": payload.get("model"),
            "backend": backend or self.settings.default_backend, "tags": tags,
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

    def proxy_error(self, session: Session, telemetry: RequestTelemetry, payload: Any, exc: Exception, backend: Any = None) -> JSONResponse:
        telemetry.status = 502
        telemetry.error_type = type(exc).__name__
        message = self.unavailable(exc, backend)
        record = self.complete(session, telemetry, payload, {"error": message})
        LOGGER.warning("backend unavailable session=%s request=%s: %s", session.session_id, telemetry.request_id, message)
        return JSONResponse(
            {"error": {"message": f"Backend unavailable: {message}", "type": "backend_error"}},
            status_code=502, headers=telemetry_headers(record),
        )


    def unavailable(self, exc: Exception, backend: Any = None) -> str:
        """Why a backend couldn't be reached, in words: httpx's timeouts have no message of their own."""
        detail = str(exc) or {
            "ConnectTimeout": "timed out connecting", "ReadTimeout": "timed out waiting for a reply",
            "WriteTimeout": "timed out sending the request", "PoolTimeout": "too many requests at once",
        }.get(type(exc).__name__, type(exc).__name__)
        if backend is None:
            return detail
        message = f"{backend.name} ({backend.url}): {detail}"
        machine = self.fleet.machines.get(getattr(backend, "machine", None) or "")
        if machine and not self.tunnels.tunnelled(backend) and isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            # A machine behind NAT, or on the server's own public address, is only reachable through its fleet connection.
            if not self.fleet.connected(machine.id):
                message += f". Its machine, {machine.name}, isn't connected, and the server reaches it through that connection: start agent-machine there."
            elif not machine.tunnel and not self.fleet.loopback(machine):
                message += f". {machine.name}'s agent-machine is too old to carry its traffic, so the server tried the address directly: run agent-machine update there."
        return message


def telemetry_headers(record: dict[str, Any]) -> dict[str, str]:
    return {"X-Request-ID": str(record["request_id"]), "X-Session-ID": record["session_id"]}


def _key(telemetry: RequestTelemetry) -> str:
    return f"{telemetry.session_id}:{telemetry.request_id}"
