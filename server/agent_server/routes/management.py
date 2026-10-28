"""Management API used by the web UI, launcher and benchmark runner."""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from .. import access, auth, mcp, remote, strata, version
from ..config import DIRECT_BACKEND, LOCAL_PROXY_URL, ModelProfile, serves
from ..fleet import PROVIDER_TYPES, SHELL_CLIENT, FleetError, parse_provider, provider_id, provider_request
from ..runtime import Runtime
from ..sessions import Session


LOGGER = logging.getLogger("agent-proxy")
# An open session with no traffic for this long is treated as finished by "delete all".
STALE_SESSION_SECONDS = 15 * 60


def register(app: FastAPI, runtime: Runtime) -> None:
    settings = runtime.settings

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
        if identity is None:
            return None

        def visible(session_id: str) -> bool:
            session = runtime.sessions.get(session_id)
            return session is not None and access.can_view(identity, session)

        return visible

    def described(session: Session, request: Request) -> dict[str, Any]:
        machine = runtime.fleet.machine_for(session.tags)
        return {**session.public(), "machine": machine.id if machine else None, "you": access.viewer(auth.request_identity(request), session)}

    @app.get("/health")
    async def health() -> dict[str, Any]:
        # The version and protocol tell agent, agent-machine and the Android app whether they need updating.
        return {"status": "ok", "proxy_instance_id": runtime.proxy_instance_id, **version.public()}

    @app.get("/api/status")
    async def status(request: Request) -> dict[str, Any]:
        return runtime.status(visible_ids(request))

    @app.post("/api/server/restart", status_code=202)
    async def restart(request: Request) -> dict[str, Any]:
        """Restart with the configuration on disk, after in-flight requests finish (up to 30 s)."""
        # With accounts only admins get here (auth.ADMIN_ONLY); without, only this computer or a paired device.
        if not runtime.remote.trusted(request) or not remote.same_origin(request):
            raise HTTPException(status_code=401, detail="Only this computer or a paired device can restart the proxy.")
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

    @app.post("/api/sessions", status_code=201)
    async def create_session(request: Request) -> dict[str, Any]:
        data = await request.json()
        try:
            if data.get("backend") != DIRECT_BACKEND:
                settings.backend(data.get("backend"))
            return described(runtime.sessions.create({**data, "tags": _new_tags(data, data.get("tags"), request)}), request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/sessions")
    async def list_sessions(request: Request) -> list[dict[str, Any]]:
        identity = auth.request_identity(request)
        return [described(item, request) for item in runtime.sessions.list() if access.can_view(identity, item)]

    @app.get("/api/requests")
    async def list_requests(request: Request, session_id: str | None = None) -> list[dict[str, Any]]:
        identity = auth.request_identity(request)
        sessions = [viewable(session_id, request)] if session_id else [item for item in runtime.sessions.list() if access.can_view(identity, item)]
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
    async def get_session(session_id: str, request: Request) -> dict[str, Any]:
        return described(viewable(session_id, request), request)

    @app.delete("/api/sessions/{session_id}/data")
    async def clear_session_data(session_id: str, request: Request) -> dict[str, Any]:
        session = controllable(session_id, request)
        runtime.require_idle(session)
        try:
            runtime.sessions.clear_data(session)
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
        flight are skipped. With accounts, only your own sessions are touched.
        """
        identity = auth.request_identity(request)
        busy = {record["session_id"] for record in runtime.active.values()}
        cutoff = time.time() - STALE_SESSION_SECONDS
        deleted, cleared, skipped = 0, 0, []
        for session in runtime.sessions.list():
            if identity is not None and not (access.is_owner(identity, session) or (session.tags.get("owner") is None and identity.admin)):
                continue
            if session.session_id in busy:
                skipped.append(session.session_id)
                continue
            try:
                if session.ended_at or _timestamp(session.last_activity_at or session.started_at) < cutoff:
                    runtime.sessions.delete(session)
                    runtime.chats.forget(session)
                    runtime.terminals.forget(session.session_id)
                    deleted += 1
                else:
                    runtime.sessions.clear_data(session)
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
            runtime.sessions.delete(session)
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
        if identity is not None and any(key in data for key in ("visibility", "team", "org", "access")):
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
        if data.get("ended", not any(key in data for key in ("visibility", "access", "team", "org"))):
            runtime.sessions.end(session, data.get("exit_status"))
        if "trace" in data:
            runtime.sessions.set_trace(session, bool(data["trace"]))
        return described(session, request)

    @app.get("/api/sessions/{session_id}/telemetry")
    async def download_telemetry(session_id: str, request: Request) -> PlainTextResponse:
        session = viewable(session_id, request)
        return PlainTextResponse(
            runtime.sessions.telemetry(session),
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
        return runtime.sessions.traces(viewable(session_id, request))

    @app.get("/api/sessions/{session_id}/traces/{request_id}")
    async def get_trace(session_id: str, request_id: str, request: Request) -> dict[str, Any]:
        trace = runtime.sessions.read_trace(viewable(session_id, request), request_id)
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
            if visible is not None and not visible(key.split(":", 1)[0]):
                return False
            return not session_id or key.startswith(f"{session_id}:")

        async def events():
            try:
                snapshot = runtime.live.snapshot(session_id)
                if visible is not None:
                    snapshot = {**snapshot, **{part: [entry for entry in snapshot[part] if visible(entry["session_id"])] for part in ("active", "recent")}}
                yield _sse("snapshot", snapshot)
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
    async def live_details(session_id: str, request_id: str, request: Request) -> dict[str, Any]:
        """Payloads and the latest raw chunks for one live or recently finished request."""
        viewable(session_id, request)
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
        # The backends each agent's protocol works with, so the UI offers only those, and the
        # one a launch picks when none is named (the client asks for it, not re-deriving it).
        listed = []
        for agent in agents.values():
            backends = [] if agent.intercept else [
                name for name, backend in settings.backends.items() if serves(backend, agent.backend_type, agent.protocol)
            ]
            listed.append({**agent.public(), "backends": backends, "default_backend": DIRECT_BACKEND if agent.intercept else _default_backend(agent)})
        return listed

    def _default_backend(agent: Any) -> str | None:
        try:
            return settings.backend_for(agent.backend_type, None, agent.protocol).name
        except ValueError:
            return None

    @app.post("/api/agents/{agent_id}/sessions", status_code=201)
    async def prepare_agent_session(agent_id: str, request: Request) -> dict[str, Any]:
        """Create a session for an agent and return the environment and command to start it.

        The console uses it for running an agent by hand, and the client's `agent` launcher
        for every launch: the server owns the agent definitions, so it renders them. The
        launcher names its own agent-proxy (proxy_url), its copy of the CA (intercept), the
        project folder, sharing, and the folder it writes the agent's files to (files_dir);
        the console leaves them out and gets the defaults.
        """
        agent = runtime.agents().get(agent_id)
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
        try:
            backend = DIRECT_BACKEND if agent.intercept else settings.backend_for(agent.backend_type, data.get("backend") or None, agent.protocol).name
            tags = {**access.client_tags(data.get("tags")), "prepared_by": "agent" if data.get("launcher") else "ui"}
            if project_path:
                tags["project_path"] = project_path
            session = runtime.sessions.create({
                "client": agent.id, "project": data.get("project"), "model": model or None,
                "backend": backend,
                "trace": bool(data.get("trace")),
                # The shared MCP servers it may reach: only these, whatever the agent asks for later.
                "tags": {**_new_tags(data, tags, request), **({"mcp": [item["id"] for item in chosen]} if chosen else {})},
            })
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        files_dir = str(data.get("files_dir") or "").strip() or None
        # Agents guess the context size from the model's name (Qwen Code takes any qwen3.x for 1M); tell them the real one.
        context_window = None if agent.intercept or not model else await _context_window(runtime, backend, model)
        servers = [{**item, "name": mcp.entry_name(item, chosen), "url": mcp.url(proxy_url, session.session_id, item)} for item in chosen]
        launch = agent.render(
            proxy_url, session.session_id, model, project_path or ".", intercept=intercept, files_dir=files_dir,
            context_window=context_window, mcp_servers=servers,
        )
        container = settings.backends.get(backend)
        return {
            "session": described(session, request),
            **launch,
            # The launcher starts a managed container backend before running the agent.
            "container": bool(container and container.container),
            "mcp": [{"id": item["id"], "name": item["name"], "url": item["url"]} for item in servers],
            "shell": {"bash": _bash(launch), "powershell": _powershell(launch)},
        }

    @app.get("/api/intercept")
    async def intercept() -> dict[str, Any]:
        return runtime.intercept_info()

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
    async def list_models(backend: str | None = None) -> dict[str, Any]:
        """The models agents can use on each backend, with the profile the server applies to them.

        Each machine's backends are listed by that machine (agent-machine); the server
        doesn't contact a model server for anything but agents' traffic.
        """
        names = [backend] if backend else list(settings.backends)
        results = await asyncio.gather(*(_backend_models(runtime, name) for name in names))
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
    """A new session's tags: the client's own, plus its owner and sharing when accounts are on."""
    tags = access.client_tags(tags)
    identity = auth.request_identity(request)
    if identity is not None:
        if data.get("client") == SHELL_CLIENT:
            # A shell is a remote terminal: only the person who opened it sees it, whatever was asked.
            data = {**data, "visibility": "private", "access": "view"}
        tags.update(access.sharing_tags(identity, data))
    return tags


async def _backend_models(runtime: Runtime, name: str) -> dict[str, Any]:
    try:
        backend = runtime.settings.backend(name)
    except ValueError as exc:
        return {"online": False, "error": str(exc), "models": []}
    base = {"type": backend.type, "url": backend.url}
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


async def _context_window(runtime: Runtime, backend_name: str, model: str) -> int | None:
    """The model's context size on this backend: Ollama's num_ctx from the model profile, or what the machine reports."""
    try:
        backend = runtime.settings.backend(backend_name)
    except ValueError:
        return None
    if backend.type == "ollama":
        size = runtime.settings.profile(model).options.get("num_ctx")
        return int(size) if isinstance(size, (int, float)) and size > 0 else None
    if backend.type not in PROVIDER_TYPES:
        return None
    try:
        listed = (await _backend_models(runtime, backend_name)).get("models") or []
    except Exception:  # a launch never waits on this: without it the agent keeps its own guess
        LOGGER.warning("Couldn't read the context size of %s on %s", model, backend_name, exc_info=True)
        return None
    # A Strata server runs one model whatever the agent calls it, so its only model counts too.
    found = [item for item in listed if item.get("name") == model] or (listed if len(listed) == 1 else [])
    size = found[0].get("context_length") if found else None
    return int(size) if isinstance(size, (int, float)) and size > 0 else None


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

