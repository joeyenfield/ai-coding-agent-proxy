"""Management API used by the web UI, launcher and benchmark runner."""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from .. import access, api_providers, auth, config_io, mcp, remote, strata, version
from ..agents import stored_agents
from ..db import redacted
from ..config import DIRECT_BACKEND, LOCAL_PROXY_URL, ModelProfile, serves, shared_model
from ..fleet import PROVIDER_TYPES, SHELL_CLIENT, FleetError, can_see, parse_provider, provider_id, provider_request
from ..provider_accounts import AccountError
from ..runtime import Runtime
from ..sessions import Session
from ..stats import ServerStats


LOGGER = logging.getLogger("agent-proxy")
# An open session with no traffic for this long is treated as finished by "delete all".
STALE_SESSION_SECONDS = 15 * 60


# How often a live stream's credential is checked with the issuer.
LIVE_RECHECK_SECONDS = 15.0


def register(app: FastAPI, runtime: Runtime) -> None:
    settings = runtime.settings
    stats = ServerStats(settings)

    def viewable(session_id: str, request: Request) -> Session:
        """A session this user may see; others look missing, so their ids aren't confirmed."""
        session = runtime.sessions.get(session_id)
        if session is None or not access.can_view(auth.request_identity(request), session):
            raise HTTPException(status_code=404, detail="Session not found")
        return session

    def controllable(session_id: str, request: Request) -> Session:
        session = viewable(session_id, request)
        if not access.can_control(auth.request_identity(request), session):
            raise HTTPException(status_code=403, detail="This session is shared with you to watch, not to change.")
        return session

    def visible_ids(request: Request) -> Any:
        """A test of session ids for this user, checked as it's used: sharing changes apply at once."""
        identity = auth.request_identity(request)

        def visible(session_id: str) -> bool:
            session = runtime.sessions.get(session_id)
            return session is not None and access.can_view(identity, session)

        return visible

    def usable(request: Request) -> Any:
        """A test of backends for this user's agents: their tenant's, and the shared hosted ones."""
        return runtime.fleet.usable_by(auth.request_identity(request))

    def described(session: Session, request: Request) -> dict[str, Any]:
        machine = runtime.fleet.machine_for(session.tags)
        return {**session.public(), "machine": machine.id if machine else None, "you": access.viewer(auth.request_identity(request), session)}

    @app.get("/health")
    async def health() -> dict[str, Any]:
        # The version and protocol tell agent, agent-machine and the Android app whether they need updating.
        return {"status": "ok", "proxy_instance_id": runtime.proxy_instance_id, **version.public()}

    @app.get("/api/status")
    async def status(request: Request, recent: bool = True) -> dict[str, Any]:
        """Totals, backends and traffic. recent=0 leaves out recent_requests (up to 100 whole records): the
        console's header polls this every few seconds and needs only the counts."""
        identity = auth.request_identity(request)
        result = runtime.status(visible_ids(request), usable(request), lambda backend: runtime.fleet.model_limit(identity, backend) is not None)
        if not recent:
            result.pop("recent_requests", None)
        identity = auth.request_identity(request)
        if not identity.site_admin:
            result["proxy_url"] = str(request.base_url).rstrip("/")
            result["listen_address"] = request.url.netloc
            result["configured_listen_address"] = request.url.netloc
            for backend in result["backends"].values():
                backend.pop("url", None)
            if isinstance(result.get("intercept"), dict):
                result["intercept"] = public_intercept(result["intercept"])
        return result

    @app.get("/api/server/stats")
    async def server_stats() -> dict[str, Any]:
        """The server computer's CPU, memory and disk use (site admins only, auth.SITE_ADMIN_ONLY)."""
        return await asyncio.to_thread(stats.sample)

    @app.get("/api/server/access-log")
    async def access_log(failed: bool = False, q: str = "", limit: int = 200, before: int | None = None) -> dict[str, Any]:
        """Every HTTP request and WebSocket the server received since it started, newest first, and how each ended
        (site admins only, auth.SITE_ADMIN_ONLY). failed keeps only refusals, errors and dropped WebSockets; q matches
        words in the path, address, user, user agent, status and reason; before pages back by id."""
        return runtime.access_log.query(failed=failed, search=q, limit=max(1, min(limit, 1000)), before=before)

    @app.get("/api/server/access-log/stats")
    async def access_log_stats() -> dict[str, Any]:
        """The access log's numbers: since the server started (memory) and from what's saved (failures and fleet connections, by day and machine)."""
        saved = await asyncio.to_thread(runtime.access_log.saved_stats)
        return {**runtime.access_log.stats(), "saved": saved}

    @app.get("/api/server/access-log/history")
    async def access_log_history(q: str = "", failed: bool = False, fleet: bool = False, limit: int = 200, before: float | None = None) -> dict[str, Any]:
        """What's saved, newest first, across restarts: failures, dropped WebSockets and every fleet connection. before (a time) pages back."""
        entries = await asyncio.to_thread(runtime.access_log.history, q, failed, fleet, max(1, min(limit, 1000)), before)
        return {"entries": entries, "kept_days": runtime.access_log.kept_days()}

    @app.get("/api/server/request-log")
    async def request_log(
        q: str = "", method: str = "", status: str = "", user: str = "", limit: int = 100,
        before: float | None = None, since: float | None = None,
    ) -> dict[str, Any]:
        """Every saved request, newest first (site admins): filter by words, method, status class and user."""
        try:
            entries = await asyncio.to_thread(runtime.request_log.query, q, method, status, user, limit, before, since)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"entries": entries, "settings": dict(settings.request_log)}

    @app.get("/api/server/request-log/summary")
    async def request_log_summary() -> dict[str, Any]:
        return await asyncio.to_thread(runtime.request_log.summary)

    @app.get("/api/server/request-log/{entry_id}")
    async def request_log_entry(entry_id: str) -> dict[str, Any]:
        entry = await asyncio.to_thread(runtime.request_log.get, entry_id[:32])
        if entry is None:
            raise HTTPException(status_code=404, detail="No such request in the log: it may have been deleted after the log's days")
        return entry

    @app.post("/api/server/restart", status_code=202)
    async def restart(request: Request) -> dict[str, Any]:
        """Restart with the stored settings, after in-flight requests finish (up to 30 s)."""
        # Only admins get here (auth.ADMIN_ONLY).
        if not runtime.remote.trusted(request) or not remote.same_origin(request):
            raise HTTPException(status_code=401, detail="Sign in as an admin to restart the server.")
        try:
            return runtime.request_restart()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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

    # The stored settings beyond the network ones (admins only, auth.ADMIN_ONLY).

    @app.get("/api/config/export")
    async def export_config(format: str = "yaml") -> Any:
        """Every setting as one document (YAML, or JSON with format=json): agents, model tuning, backends, the rest."""
        document = await asyncio.to_thread(config_io.export, settings)
        if format == "json":
            return document
        return PlainTextResponse(
            config_io.dump(document), media_type="application/yaml",
            headers={"Content-Disposition": 'attachment; filename="agent-server-settings.yaml"'},
        )

    @app.post("/api/config/import")
    async def import_config(request: Request, replace: bool = False) -> dict[str, Any]:
        """Store a settings document (YAML or JSON). Only the parts it has, unless replace."""
        try:
            document = config_io.parse((await request.body()).decode("utf-8"))
            async with runtime.config_lock:
                changed = await asyncio.to_thread(config_io.apply, settings, document, replace=replace)
        except (ValueError, TypeError, UnicodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        LOGGER.info("settings imported: %s", ", ".join(changed))
        return {"changed": changed, "restart_required": runtime.status()["restart_required"]}

    @app.get("/api/config/agents")
    async def config_agents() -> list[dict[str, Any]]:
        """The agents as stored: each definition (and as YAML, to edit), whether it ships with the server and whether it was changed."""
        agents = await asyncio.to_thread(stored_agents, runtime.db)
        return [{**agent, "yaml": config_io.dump(agent["definition"])} for agent in agents]

    @app.put("/api/config/agents/{agent_id}")
    async def put_config_agent(agent_id: str, request: Request) -> dict[str, Any]:
        """Add or change an agent; the body is its definition (YAML or JSON)."""
        try:
            body = await request.body()
            definition = config_io.parse(body.decode("utf-8")) if body else None
            async with runtime.config_lock:
                return await asyncio.to_thread(config_io.put_agent, settings, agent_id, definition)
        except (ValueError, TypeError, UnicodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/config/agents/{agent_id}", status_code=204)
    async def delete_config_agent(agent_id: str) -> None:
        async with runtime.config_lock:
            if not await asyncio.to_thread(config_io.delete_agent, settings, agent_id):
                raise HTTPException(status_code=404, detail="No such agent")

    @app.post("/api/config/agents/{agent_id}/reset")
    async def reset_config_agent(agent_id: str) -> dict[str, Any]:
        """A shipped agent back to this version's definition."""
        try:
            async with runtime.config_lock:
                return await asyncio.to_thread(config_io.reset_agent, settings, agent_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Only an agent that ships with the server can be reset") from exc

    @app.get("/api/config/models")
    async def config_models(format: str = "json") -> Any:
        """Ollama model tuning: defaults, profiles (the first whose match fits the model applies) and aliases (YAML with format=yaml)."""
        if format == "yaml":
            return PlainTextResponse(config_io.dump(settings.models_document()), media_type="application/yaml")
        return settings.models_document()

    @app.put("/api/config/models")
    async def put_config_models(request: Request) -> dict[str, Any]:
        try:
            body = await request.body()
            document = config_io.parse(body.decode("utf-8"))
            async with runtime.config_lock:
                return await asyncio.to_thread(settings.update_models, document)
        except (ValueError, TypeError, UnicodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/config/storage")
    async def config_storage() -> dict[str, Any]:
        """Where the server keeps everything, and for how long it keeps recordings."""
        return await asyncio.to_thread(storage)

    @app.put("/api/config/storage")
    async def put_config_storage(request: Request) -> dict[str, Any]:
        """Set retention_days (how long traces and terminal recordings are kept, null: forever) and/or
        request_log (any of enabled, days, payloads, payload_days)."""
        try:
            data = await request.json()
            if not isinstance(data, dict) or not ({"retention_days", "request_log"} & set(data)):
                raise ValueError("Send an object with retention_days (a number of days, or null) and/or request_log")
            async with runtime.config_lock:
                if "request_log" in data:
                    await asyncio.to_thread(settings.set_request_log, data["request_log"])
                if "retention_days" in data:
                    await asyncio.to_thread(settings.set_retention, data.get("retention_days"))
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await asyncio.to_thread(runtime.prune)
        return await asyncio.to_thread(storage)

    def storage() -> dict[str, Any]:
        return {
            "database": {"kind": "sqlite" if runtime.db.sqlite else "postgresql", "url": redacted(runtime.db.url), "size": runtime.db.size()},
            "retention_days": settings.retention_days,
            "request_log": dict(settings.request_log),
        }

    @app.post("/api/sessions", status_code=201)
    async def create_session(request: Request) -> dict[str, Any]:
        data = await request.json()
        try:
            if data.get("backend") != DIRECT_BACKEND:
                data = {**data, "backend": settings.backend(data.get("backend"), usable(request)).name}
            return described(runtime.sessions.create({**data, "tags": _new_tags(data, data.get("tags"), request)}), request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/sessions")
    async def list_sessions(request: Request) -> list[dict[str, Any]]:
        identity = auth.request_identity(request)
        return [described(item, request) for item in runtime.sessions.list() if access.can_view(identity, item)]

    @app.get("/api/requests")
    async def list_requests(
        request: Request, session_id: str | None = None, limit: int | None = None, before: str | None = None,
        model: str | None = None, kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """Request records, newest first. limit and before (the oldest timestamp of the last page) page through them."""
        identity = auth.request_identity(request)
        sessions = [viewable(session_id, request)] if session_id else [item for item in runtime.sessions.list() if access.can_view(identity, item)]
        if limit is not None and not 1 <= limit <= 5000:
            raise HTTPException(status_code=400, detail="limit is between 1 and 5000")

        def query() -> list[dict[str, Any]]:
            records = runtime.sessions.query_requests([item.session_id for item in sessions], limit=limit, before=before, model=model, kind=kind)
            traced = runtime.sessions.traced((record["session_id"], str(record.get("request_id"))) for record in records)
            return [{**record, "trace_available": (record["session_id"], str(record.get("request_id"))) in traced} for record in records]

        return await asyncio.to_thread(query)

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str, request: Request) -> dict[str, Any]:
        return described(viewable(session_id, request), request)

    @app.delete("/api/sessions/{session_id}/data")
    async def clear_session_data(session_id: str, request: Request) -> dict[str, Any]:
        session = controllable(session_id, request)
        runtime.require_idle(session)
        try:
            # Off the event loop: clearing flushes the write queue and may touch blobs.
            await asyncio.to_thread(runtime.sessions.clear_data, session)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        runtime.chats.forget(session)
        runtime.reload_history()
        runtime.live.clear_session(session.session_id)
        return session.public()

    @app.delete("/api/sessions")
    async def delete_all_sessions(request: Request) -> dict[str, Any]:
        """Remove all saved traffic.

        Ended sessions are deleted, and so are open sessions idle for longer than
        STALE_SESSION_SECONDS (their agent most likely exited without ending them).
        Recently active open sessions keep running, since an agent may still be
        using the session URL, but lose their saved data. Sessions with requests in
        flight are skipped. Only your own sessions are touched (admins: also those without an owner).
        """
        identity = auth.request_identity(request)
        busy = {record["session_id"] for record in runtime.active.values()}
        cutoff = time.time() - STALE_SESSION_SECONDS
        deleted, cleared, skipped = 0, 0, []
        for session in runtime.sessions.list():
            if not (access.is_owner(identity, session) or (session.tags.get("owner") is None and identity is not None and identity.admin and access.in_tenant(identity, session))):
                continue
            if session.session_id in busy:
                skipped.append(session.session_id)
                continue
            try:
                # Off the event loop: each delete or clear flushes the write queue and may
                # touch blobs, and this loops over every session.
                if session.ended_at or _timestamp(session.last_activity_at or session.started_at) < cutoff:
                    await asyncio.to_thread(runtime.sessions.delete, session)
                    runtime.chats.forget(session)
                    runtime.terminals.forget(session.session_id)
                    deleted += 1
                else:
                    await asyncio.to_thread(runtime.sessions.clear_data, session)
                    cleared += 1
            except (OSError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            runtime.live.clear_session(session.session_id)
        runtime.reload_history()
        LOGGER.info("deleted all sessions: deleted=%s cleared=%s skipped=%s", deleted, cleared, len(skipped))
        return {"deleted": deleted, "cleared": cleared, "skipped": skipped}

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str, request: Request) -> dict[str, Any]:
        session = controllable(session_id, request)
        runtime.require_idle(session)
        try:
            # Off the event loop: deleting flushes the write queue and may touch blobs.
            await asyncio.to_thread(runtime.sessions.delete, session)
            runtime.chats.forget(session)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        runtime.terminals.forget(session_id)
        runtime.reload_history()
        runtime.live.clear_session(session.session_id)
        return {"deleted": session_id}

    @app.patch("/api/sessions/{session_id}")
    async def update_session(session_id: str, request: Request) -> dict[str, Any]:
        """End a session, switch capture, or (its owner) change who it's shared with."""
        identity = auth.request_identity(request)
        session = controllable(session_id, request)
        data = await request.json()
        if any(key in data for key in ("visibility", "team", "org", "access")):
            if not access.is_owner(identity, session):
                raise HTTPException(status_code=403, detail="Only the person who started a session can change who it's shared with.")
            if session.client == SHELL_CLIENT:
                raise HTTPException(status_code=400, detail="A shell stays private: it's a terminal on your machine.")
            try:
                runtime.sessions.update_tags(session, access.sharing_tags(identity, data, access.sharing(session)))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            # People who can't see it any more stop watching its terminal now.
            runtime.terminals.drop_viewers(session.session_id, lambda viewer: access.can_view(viewer, session))
        # "ended" is explicit; for compatibility an empty body (or exit_status alone, as old
        # launchers sent) still ends the session, but a sharing or capture change alone never does.
        if data.get("ended", not any(key in data for key in ("visibility", "access", "team", "org", "trace"))):
            runtime.sessions.end(session, data.get("exit_status"))
            # Replies still being read for a client that went (the console's chat) end with it.
            runtime.stop_finishing(session.session_id)
        if "trace" in data:
            try:
                runtime.sessions.set_trace(session, bool(data["trace"]))
            except ValueError as exc:
                # Its organization's policy records every session, or none (retention.py).
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        return described(session, request)

    @app.post("/api/sessions/{session_id}/stop")
    async def stop_replies(session_id: str, request: Request) -> dict[str, Any]:
        """Stop the session's replies that are read to the end for their client (Ask a model's Stop)."""
        session = controllable(session_id, request)
        return {"stopped": runtime.stop_finishing(session.session_id)}

    @app.get("/api/sessions/{session_id}/telemetry")
    async def download_telemetry(session_id: str, request: Request) -> PlainTextResponse:
        session = viewable(session_id, request)
        return PlainTextResponse(
            await asyncio.to_thread(runtime.sessions.telemetry, session),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="{session_id}-requests.jsonl"'},
        )

    @app.get("/api/sessions/{session_id}/chat")
    async def session_chat(session_id: str, request: Request, thread: str | None = None, before: int | None = None, limit: int = 60) -> dict[str, Any]:
        """A page of the session's model calls read back as chats; traces are read off the event loop, as they can be large."""
        session = viewable(session_id, request)
        return await asyncio.to_thread(runtime.chats.view, session, runtime.records(session), thread, before, min(max(limit, 1), 500))

    @app.get("/api/sessions/{session_id}/chat/turns/{position}")
    async def session_chat_turn(session_id: str, position: int, request: Request, thread: str) -> dict[str, Any]:
        """One turn of a chat in full, where a page had only a preview."""
        session = viewable(session_id, request)
        turn = await asyncio.to_thread(runtime.chats.turn, session, runtime.records(session), thread, position)
        if turn is None:
            raise HTTPException(status_code=404, detail="Turn not found")
        return turn

    @app.get("/api/sessions/{session_id}/traces")
    async def list_traces(session_id: str, request: Request) -> list[str]:
        # Off the event loop: reading traces flushes the write queue and may fetch blobs.
        return await asyncio.to_thread(runtime.sessions.traces, viewable(session_id, request))

    @app.get("/api/sessions/{session_id}/traces/{request_id}")
    async def get_trace(session_id: str, request_id: str, request: Request) -> dict[str, Any]:
        trace = await asyncio.to_thread(runtime.sessions.read_trace, viewable(session_id, request), request_id)
        if trace is None:
            raise HTTPException(status_code=404, detail="Trace not found")
        return trace

    @app.get("/api/live")
    async def live(request: Request, session_id: str | None = None) -> StreamingResponse:
        """Server-sent events: a snapshot, then start/delta/end events for streamed requests.

        Pass session_id to receive only that session's traffic.
        """
        if session_id:
            viewable(session_id, request)
        visible = visible_ids(request)
        queue = runtime.live.subscribe()

        def wanted(event: dict[str, Any]) -> bool:
            key = event["request"]["key"] if event["type"] == "start" else event["key"]
            if not visible(key.split(":", 1)[0]):
                return False
            return not session_id or key.startswith(f"{session_id}:")

        async def events():
            nonlocal visible
            checked_at = time.monotonic()
            try:
                snapshot = runtime.live.snapshot(session_id)
                snapshot = {**snapshot, **{part: [entry for entry in snapshot[part] if visible(entry["session_id"])] for part in ("active", "recent")}}
                # The stream ends when its access token does (below); the browser reopens it with its renewed
                # cookie after this many ms rather than its own default of a few seconds.
                yield b"retry: 1000\n\n"
                yield _sse("snapshot", snapshot)
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        event = {"type": "keep-alive"}
                    if time.monotonic() - checked_at >= LIVE_RECHECK_SECONDS:
                        # The credential, every few seconds (not the issuer on every token of a reply);
                        # what this user may see, from memory, on every event.
                        try:
                            identity = await asyncio.to_thread(runtime.auth.recheck, request)
                        except auth.AuthUnavailable:
                            return
                        if identity is None:
                            return
                        request.scope["state"]["identity"] = identity
                        checked_at = time.monotonic()
                    visible = visible_ids(request)
                    if event is not None and event.get("type") == "keep-alive":
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
    async def live_details(session_id: str, request_id: str, request: Request) -> dict[str, Any]:
        """Payloads and the latest raw chunks for one live or recently finished request."""
        viewable(session_id, request)
        key = f"{session_id}:{request_id}"
        pod = runtime.live.elsewhere(key)
        details = None
        if pod:
            # It runs on another process, which holds its payloads.
            try:
                answer = await runtime.cluster.call(pod, "GET", f"/internal/live?key={quote(key)}", timeout=10)
                details = answer.json() if answer.status_code == 200 else None
            except Exception:  # noqa: BLE001 - that process went: the request is gone from the live view
                details = None
        else:
            details = runtime.live.details(key)
        if details is None:
            raise HTTPException(status_code=404, detail="This request is no longer in the live view. Find it in the request history.")
        return details

    @app.get("/api/agents")
    async def list_agents(request: Request) -> list[dict[str, Any]]:
        try:
            # The server's own, the caller's organization's, and those let in over bridges (full names).
            agents = runtime.agents(auth.request_identity(request))
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        # The backends each agent's protocol works with, so the UI offers only those, and the
        # one a launch picks when none is named (the client asks for it, not re-deriving it).
        listed = []
        mine = usable(request)
        remote = {**settings.remote_backends}
        for agent in agents.values():
            backends = [] if agent.intercept else [
                name for name, backend in {**settings.backends, **remote}.items() if serves(backend, agent.backend_type, agent.protocol) and mine(backend)
            ]
            listed.append({**agent.public(), "backends": backends, "default_backend": DIRECT_BACKEND if agent.intercept else _default_backend(agent, mine)})
        return listed

    def _default_backend(agent: Any, mine: Any) -> str | None:
        try:
            return settings.backend_for(agent.backend_type, None, agent.protocol, mine).name
        except ValueError:
            return None

    # An agent let in over a bridge has its full name as its id (agent:server/org/name), so the id may hold "/".
    @app.post("/api/agents/{agent_id:path}/sessions", status_code=201)
    async def prepare_agent_session(agent_id: str, request: Request) -> dict[str, Any]:
        """Create a session for an agent and return the environment and command to start it.

        The console uses it for running an agent by hand, and the client's `agent` launcher
        for every launch: the server owns the agent definitions, so it renders them. The
        launcher names its own agent-proxy (proxy_url), its copy of the CA (intercept), the
        project folder, sharing, and the folder it writes the agent's files to (files_dir);
        the console leaves them out and gets the defaults.
        """
        agent = runtime.agents(auth.request_identity(request)).get(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        data = await request.json()
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Send a JSON object")
        model = str(data.get("model") or "").strip()
        if not model and agent.needs_model:
            raise HTTPException(status_code=400, detail=f"{agent.name} needs a model")
        proxy_url = _proxy_url(data.get("proxy_url"))
        project_path = str(data.get("project_path") or "").strip()
        intercept = None
        if agent.intercept:
            intercept = runtime.intercept_info()
            if not intercept["running"]:
                raise HTTPException(status_code=409, detail=f"The agent-server's HTTPS interception is off: {intercept['error'] or 'unknown error'}")
            local = data.get("intercept") if isinstance(data.get("intercept"), dict) else {}
            # The launcher's own HTTPS proxy and CA copy; the server's paths mean nothing on its machine.
            intercept = {**intercept, **{key: str(local[key]) for key in ("url", "ca_path", "bundle_path") if local.get(key)}}
        try:
            chosen = mcp.choose(runtime.fleet, auth.request_identity(request), data.get("mcp"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if chosen and not agent.mcp:
            raise HTTPException(status_code=400, detail=f"{agent.name} can't take MCP servers")
        mcp_token, mcp_token_hash = mcp.issue_token() if chosen else (None, None)
        try:
            # The provider account (subscription) it runs with, checked here: for a launch the server sent,
            # against its grant; otherwise for whoever asks. The tag is the server's, never the client's.
            account = runtime.provider_accounts.for_session(
                auth.request_identity(request), agent, data.get("account"), data.get("tags") if isinstance(data.get("tags"), dict) else {},
            ) if data.get("account") else None
        except AccountError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        launch_tag = str((data.get("tags") or {}).get("launch") or "") if isinstance(data.get("tags"), dict) else ""
        # A guest of a linked server's launch on this machine (bridges.py): the server set what its session gets
        # (the guest, the bridge, the backends it may use); only a launch some process really waits on.
        guest = runtime.fleet.guest_launch(launch_tag) if launch_tag and await asyncio.to_thread(runtime.fleet.pending_launch, launch_tag) else None
        mine = usable(request)
        if guest is not None:
            allowed = list((guest.get("tags") or {}).get("allowed_backends") or [])
            mine = lambda backend: backend.name in allowed  # noqa: E731
        try:
            backend = DIRECT_BACKEND if agent.intercept else settings.backend_for(agent.backend_type, data.get("backend") or None, agent.protocol, mine).name
            if guest is None and backend in settings.backends and model:
                # A provider shared with every organization only for some models: say so now, not at the first request.
                limit = runtime.fleet.model_limit(auth.request_identity(request), settings.backends[backend])
                if limit is not None and not shared_model(limit, model):
                    raise HTTPException(status_code=403, detail=f"{model} isn't shared with your organization on {backend}")
            tags = {**access.client_tags(data.get("tags")), "prepared_by": "agent" if data.get("launcher") else "ui"}
            # A launch the console sent to a machine runs under the machine's sign-in: its usage, and the
            # check, are for whoever asked (routes/machines.launch), not the machine's owner.
            # A console launch was already checked by routes/machines.py for whoever asked, so its
            # session isn't checked again — but only for a launch some process is really waiting on
            # (this one's own, or the grant the cluster holds): the tag is client-sent, and a
            # made-up value must not skip the limit.
            launch = str(tags.get("launch") or "")
            granted = bool(launch) and await asyncio.to_thread(runtime.fleet.pending_launch, launch)
            refused = None if granted else runtime.launch_blocked(auth.request_identity(request), agent, backend)
            if refused:
                raise HTTPException(status_code=429, detail=refused)
            if project_path:
                tags["project_path"] = project_path
            session = runtime.sessions.create({
                "client": agent.id, "project": data.get("project"), "model": model or None,
                "backend": backend,
                "trace": bool(data.get("trace")),
                # The shared MCP servers it may reach: only these, whatever the agent asks for later.
                "tags": {
                    **_new_tags(data, tags, request), **({"mcp": [item["id"] for item in chosen]} if chosen else {}),
                    **({"provider_account": account.id} if account else {}),
                    **((guest.get("tags") or {}) if guest is not None else {}),
                },
            }, mcp_token_hash=mcp_token_hash)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        files_dir = str(data.get("files_dir") or "").strip() or None
        container = settings.backends.get(backend)
        # Agents guess the context size from the model's name (Qwen Code takes any qwen3.x for 1M); tell them the real one.
        context_window = None if agent.intercept or not model else await _context_window(runtime, backend, model)
        servers = [
            {**item, "name": mcp.entry_name(item, chosen), "url": mcp.url(proxy_url, session.session_id, item, mcp_token)}
            for item in chosen
        ]
        launch_model = api_providers.claude_launch_model(container, agent, model, context_window)
        launch = agent.render(
            proxy_url, session.session_id, launch_model, project_path or ".", intercept=intercept, files_dir=files_dir,
            context_window=context_window, mcp_servers=servers,
        )
        api_providers.configure_claude_launch(container, agent, model, context_window, launch)
        return {
            "session": described(session, request),
            **launch,
            # The launcher starts a managed container backend before running the agent.
            "container": bool(container and container.container),
            "mcp": [{"id": item["id"], "name": item["name"], "url": item["url"]} for item in servers],
            # The subscription it runs with; credential means a managed token waits for the launcher at
            # POST /api/sessions/{id}/provider-credential (never in this answer, its env or its command).
            "account": {
                "id": account.id, "provider": account.provider, "display_name": account.display_name,
                "credential": account.authentication_mode == "token",
            } if account else None,
            "shell": {"bash": _bash(launch), "powershell": _powershell(launch)},
        }

    def public_intercept(info: dict[str, Any]) -> dict[str, Any]:
        """Launchers need the state, url and CA fingerprint; the server's own file paths
        and listen address are site-admin detail (as /api/status strips addresses)."""
        return {key: value for key, value in info.items() if key in ("enabled", "running", "error", "url", "ca_fingerprint")}

    @app.get("/api/intercept")
    async def intercept(request: Request) -> dict[str, Any]:
        info = runtime.intercept_info()
        if not auth.request_identity(request).site_admin:
            info = public_intercept(info)
        return info

    @app.get("/api/intercept/ca.pem")
    async def intercept_ca() -> PlainTextResponse:
        if not runtime.intercept_info()["running"]:
            raise HTTPException(status_code=404, detail="The HTTPS intercept proxy isn't running")
        return PlainTextResponse(runtime.ca.pem, media_type="application/x-pem-file")

    @app.get("/api/intercept/bundle.pem")
    async def intercept_bundle() -> PlainTextResponse:
        """Common root certificates plus the CA, for tools that take one CA file (curl, git, Python).

        The client fetches it rather than building it: Windows and macOS keep their roots in
        a store, not a file.
        """
        if not runtime.intercept_info()["running"]:
            raise HTTPException(status_code=404, detail="The HTTPS intercept proxy isn't running")
        return PlainTextResponse(runtime.ca.bundle_path.read_text(encoding="utf-8"), media_type="application/x-pem-file")

    @app.get("/api/models")
    async def list_models(request: Request, backend: str | None = None) -> dict[str, Any]:
        """The models agents can use on each backend of the user's tenant, with the profile the server applies to them.

        Each machine's backends are listed by that machine (agent-machine); the server
        doesn't contact a model server for anything but agents' traffic.
        """
        mine = usable(request)
        # Providers let in over bridges are listed with the rest, under their full names, with what the giving server said.
        known = {**settings.backends, **{name: item for name, item in settings.remote_backends.items() if mine(item)}}
        if backend is not None and (backend not in known or not mine(known[backend])):
            raise HTTPException(status_code=404, detail="No such backend")
        names = [backend] if backend else [name for name, item in known.items() if mine(item)]
        results = await asyncio.gather(*(_backend_models(runtime, name) for name in names))
        identity = auth.request_identity(request)
        for name, result in zip(names, results):
            limit = runtime.fleet.model_limit(identity, known[name])
            if limit is not None:
                # Reached through a share with every organization: only the models shared.
                result["models"] = [model for model in result.get("models", []) if shared_model(limit, str(model.get("name") or ""))]
                result["shared"] = True
            machine = runtime.fleet.machines.get(known[name].machine or "")
            if not identity.site_admin and (machine is None or not can_see(identity, machine)):
                result.pop("url", None)
                result.pop("version", None)
                result.pop("error", None)
                result["models"] = [{key: model[key] for key in ("name", "profile") if key in model} for model in result.get("models", [])]
        return {
            "backends": dict(zip(names, results)),
            "aliases": {name: settings.profile(name).public() for name in settings.models},
        }


def _proxy_url(value: Any) -> str:
    """The agent-proxy URL to render into an agent's environment: the launcher's own, or the default."""
    if not value:
        return LOCAL_PROXY_URL
    parsed = urlparse(str(value))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="proxy_url must be an http:// URL such as http://127.0.0.1:8184")
    return str(value).rstrip("/")


def _new_tags(data: dict[str, Any], tags: Any, request: Request) -> dict[str, Any]:
    """A new session's tags: the client's own, plus its owner and sharing."""
    tags = access.client_tags(tags)
    identity = auth.request_identity(request)
    if identity is not None:
        if data.get("client") == SHELL_CLIENT:
            # A shell is a remote terminal: only the person who opened it sees it, whatever was asked.
            data = {**data, "visibility": "private", "access": "view"}
        tags.update(access.sharing_tags(identity, data))
    return tags


async def _backend_models(runtime: Runtime, name: str, *, discover: bool = True) -> dict[str, Any]:
    try:
        backend = runtime.settings.backend(name)
    except ValueError as exc:
        return {"online": False, "error": str(exc), "models": []}
    base = {"type": backend.type, "url": backend.url}
    if backend.type == "link":
        options = backend.options or {}
        models = [
            {"name": model, "size": None, "modified_at": None, "family": None, "parameter_size": None, "quantization": None,
             "loaded": None, "size_loaded": None, "size_vram": None, "context_length": None, "upstream": model,
             "profile": ModelProfile(ollama_model=model, translate_openai=False).public()}
            for model in options.get("models") or []
        ]
        return {"type": "link", "online": True, "remote": {key: options.get(key) for key in ("server", "remote_org", "remote_name", "remote_type")}, "models": models}
    if backend.type == "api" and not backend.machine:
        catalog = runtime.api_discovery.get(name)
        # Without discover, a stale catalog is used as it is, but one this process never filled is
        # still fetched: otherwise a launch on a fresh process would know no context size at all.
        if discover or catalog is None or not catalog.result:
            from .api_providers import auto_discover

            await auto_discover(runtime, backend)
        return {**base, "online": (runtime.api_health.get(name) or {}).get("ok", True), "models": _api_models(runtime, backend, [])}
    if backend.type not in PROVIDER_TYPES:
        return {**base, "online": False, "error": "Hosted backend: select a model in the signed-in client. Model discovery is unavailable.", "models": []}
    provider = provider_id(backend)
    machine = runtime.fleet.machines.get(backend.machine or "")
    if provider is None or machine is None:
        return {**base, "online": False, "error": "Not on a machine: run agent-machine where this model server runs and add it from the Machines page.", "models": []}
    kind, port = parse_provider(provider) or (backend.type, 0)
    try:
        answer = await runtime.fleet.ask(machine, "status", timeout=8, channel="provider", **provider_request(backend, kind, port))
    except FleetError as exc:
        return {**base, "online": False, "error": exc.detail, "models": []}
    state = answer.get("state") if isinstance(answer.get("state"), dict) else {}
    if not state.get("online") or state.get("error"):
        return {**base, "online": False, "error": str(state.get("error") or "Offline"), "models": []}
    if backend.type == "ollama":
        models = [
            {**_ollama_model(item), "profile": runtime.settings.profile(str(item.get("name") or "")).public()}
            for item in state.get("models") or [] if isinstance(item, dict)
        ]
        return {**base, "online": True, "version": state.get("version"), "models": sorted(models, key=lambda model: model["name"] or "")}
    if backend.type == "strata":
        models = [
            {
                "name": item["id"], "size": None, "modified_at": None, "family": None, "parameter_size": None,
                "quantization": None, "loaded": (item.get("status") or {}).get("value") == "loaded",
                "context_length": (item.get("meta") or {}).get("n_ctx"), "size_loaded": None, "size_vram": None,
                "profile": ModelProfile(ollama_model=item["id"], translate_openai=False).public(),
            }
            for item in state.get("models") or [] if isinstance(item, dict) and item.get("id")
        ]
        return {**base, "online": True, "version": (state.get("status") or {}).get("engine"), "models": models}
    if backend.type == "llamacpp":
        # What the router can serve: downloads still running (or left unfinished) can't answer yet.
        models = [
            {
                "name": item.get("name"), "size": item.get("size"), "modified_at": None, "family": None,
                "parameter_size": item.get("parameters"), "quantization": _quant(str(item.get("name") or "")), "loaded": bool(item.get("loaded")),
                "context_length": None, "size_loaded": None, "size_vram": None,
                "profile": ModelProfile(ollama_model=str(item.get("name") or ""), translate_openai=False).public(),
            }
            for item in state.get("models") or [] if isinstance(item, dict) and item.get("state") not in {"downloading", "incomplete"}
        ]
        return {**base, "online": True, "version": None, "models": models}
    if backend.type == "api":
        found = [
            {"id": str(item.get("id")), "context_length": item.get("context_length")}
            for item in state.get("models") or [] if isinstance(item, dict) and item.get("id")
        ]
        return {**base, "online": True, "version": state.get("version") or None, "models": _api_models(runtime, backend, found)}
    models = [
        {
            "name": item.get("name"), "size": None, "modified_at": None, "family": item.get("architecture"),
            "parameter_size": item.get("parameters"), "quantization": item.get("quantization"), "loaded": bool(item.get("loaded")),
            "context_length": item.get("context_size"), "size_loaded": None, "size_vram": None,
            "profile": ModelProfile(ollama_model=str(item.get("name") or ""), translate_openai=False).public(),
        }
        for item in state.get("models") or [] if isinstance(item, dict)
    ]
    return {**base, "online": True, "version": None, "models": models}


def _quant(name: str) -> str | None:
    """The quant a llama.cpp model is named with (user/repo:Q4_K_M), if any."""
    _, _, quant = name.rpartition(":")
    return quant or None if ":" in name else None


async def _context_window(runtime: Runtime, backend_name: str, model: str) -> int | None:
    """The model's context size on this backend: Ollama's num_ctx from the model profile, or what the machine reports."""
    try:
        backend = runtime.settings.backend(backend_name)
    except ValueError:
        return None
    if backend.type == "ollama":
        size = runtime.settings.profile(model).options.get("num_ctx")
        return int(size) if isinstance(size, (int, float)) and size > 0 else None
    if api_providers.deepseek(backend):
        model = model.removesuffix("[1m]")
    if backend.type not in PROVIDER_TYPES:
        return None
    try:
        listed = (await _backend_models(runtime, backend_name, discover=False)).get("models") or []
    except Exception:  # a launch never waits on this: without it the agent keeps its own guess
        LOGGER.warning("Couldn't read the context size of %s on %s", model, backend_name, exc_info=True)
        return None
    # A Strata server runs one model whatever the agent calls it, so its only model counts too.
    found = [item for item in listed if item.get("name") == model] or (listed if backend.type == "strata" and len(listed) == 1 else [])
    size = found[0].get("context_length") if found else None
    return int(size) if isinstance(size, (int, float)) and size > 0 else None


def _api_models(runtime: Runtime, backend: Any, found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """An api backend's models as /api/models lists them: its aliases first, then the ids it reports."""
    configured = api_providers.options(backend)
    catalog = runtime.api_discovery.get(backend.name)
    discovered = found or ([{"id": item} for item in catalog.models] if catalog and configured["discover"] else [])
    sizes = {**(catalog.contexts if catalog and configured["discover"] else {}), **{item["id"]: item.get("context_length") for item in found}}
    names = list(configured["models"]) + [item["id"] for item in discovered if item["id"] not in configured["models"]]
    return [
        {
            "name": name, "size": None, "modified_at": None, "family": None, "parameter_size": None, "quantization": None,
            "loaded": None, "size_loaded": None, "size_vram": None,
            "context_length": sizes.get(configured["models"].get(name, name)) if isinstance(sizes.get(configured["models"].get(name, name)), int) else None,
            "upstream": configured["models"].get(name, name),
            "profile": ModelProfile(ollama_model=configured["models"].get(name, name), translate_openai=False).public(),
        }
        for name in names
    ]


def _ollama_model(item: dict[str, Any]) -> dict[str, Any]:
    fields = ("name", "size", "modified_at", "family", "parameter_size", "quantization", "loaded", "context_length", "size_loaded", "size_vram")
    return {key: item.get(key) for key in fields}


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'), ensure_ascii=False)}\n\n".encode()


def _bash(launch: dict[str, Any]) -> str:
    lines = [f"export {key}={shlex.quote(value)}" for key, value in launch["env"].items()]
    return "\n".join([*lines, shlex.join(launch["command"])])


def _powershell(launch: dict[str, Any]) -> str:
    def quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    lines = [f"$env:{key} = {quote(value)}" for key, value in launch["env"].items()]
    command = launch["command"]
    return "\n".join([*lines, " ".join([command[0], *(quote(arg) for arg in command[1:])])])


def _timestamp(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0
