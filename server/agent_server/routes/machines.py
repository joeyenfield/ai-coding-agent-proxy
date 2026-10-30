"""The fleet's API: machines' connections, the console's list, launches, and the LLM providers machines run.

See fleet.py. The machines' WebSocket needs the machine's sign-in (the middleware checks
it with accounts; without, this computer or the access key). Starting an agent is for
the machine's owner; turning a machine's model server into a backend is admin-only
(auth.ADMIN_ONLY).

Everything about a model server goes through here to the machine that runs it, which
does it there: its status, models and logs, the machine's load and model catalogue
searches, for whoever sees the machine (its owner and admins); starting, stopping and
configuring the Ollama and Strata installed there, for its owner only. The machine
refuses changes unless it allows them (agent-machine servers on).
The server itself never calls a model server except to forward agents' traffic. Local backends only come from machines this way, or as containers a
machine runs (routes/containers.py); backends.yaml entries from before that are linked
to their machine when it reports the same model server.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import PlainTextResponse, StreamingResponse

from .. import auth, mcp, remote
from ..fleet import (
    FEATURES, PROVIDER_TYPES, FleetError, allowed, can_manage, can_see, checked_access, normal_url, parse_provider, provider_request, within_roots,
)
from ..fleet import provider_id as fleet_provider_id
from ..runtime import Runtime
from .terminals import CLOSE_UNAUTHORIZED


NAME = re.compile(r"[^a-z0-9_-]+")
MODEL_ACTIONS = {"pull", "delete", "load", "unload"}
CATALOGS = ("ollama", "huggingface", "huggingface-quants", "docker-hub", "docker-hub-tags")


def register(app: FastAPI, runtime: Runtime) -> None:
    fleet = runtime.fleet

    def require_trusted(request: Request) -> None:
        if not runtime.remote.trusted(request):
            raise HTTPException(status_code=401, detail="Sign in first.")

    def visible(machine_id: str, request: Request) -> Any:
        machine = fleet.machines.get(machine_id)
        if machine is None or not can_see(auth.request_identity(request), machine):
            raise HTTPException(status_code=404, detail="No such machine")
        return machine

    def described(machine: Any, request: Request) -> dict[str, Any]:
        return fleet.public(machine, auth.request_identity(request), runtime.settings.backends)

    async def claim(machine: Any) -> None:
        """The machine's backends follow its address; older backends at one of its model servers become its."""
        moved = fleet.moved(machine, runtime.settings.backends)
        # A self-reported address isn't proof that a signed-in machine owns a legacy backend.
        names = [] if runtime.auth.enabled else fleet.unclaimed(machine, runtime.settings.backends)
        if not moved and not names:
            return
        async with runtime.config_lock:
            for name, url in moved.items():
                backend = runtime.settings.backends.get(name)
                if backend and backend.machine == machine.id:
                    runtime.settings.save_backend(name, {**backend.public(), "url": url})
            for name in names:
                backend = runtime.settings.backends.get(name)
                if backend and not backend.machine:
                    runtime.settings.save_backend(name, {**backend.public(), "machine": machine.id})

    @app.websocket("/api/machines/connect")
    async def connect(websocket: WebSocket) -> None:
        """One machine's agent-machine, for as long as it runs: its hello and updates in, launches out."""
        await websocket.accept()
        if not (remote.same_origin(websocket) and runtime.remote.trusted(websocket)):
            await websocket.close(code=CLOSE_UNAUTHORIZED, reason="Sign this machine in first: agent login <server>")
            return
        client_host = websocket.client.host if websocket.client else ""
        try:
            hello = json.loads(await websocket.receive_text())
            machine = fleet.connect(websocket, auth.request_identity(websocket), client_host, hello if isinstance(hello, dict) else {})
        except FleetError as exc:
            await websocket.close(code=exc.status, reason=exc.detail[:120])
            return
        except (WebSocketDisconnect, ValueError):
            return
        await claim(machine)
        await websocket.send_text(json.dumps({"type": "welcome", "machine": machine.id}))
        try:
            while True:
                try:
                    data = json.loads(await websocket.receive_text())
                except ValueError:
                    continue
                if isinstance(data, dict):
                    fleet.message(machine, data, client_host)
                    if data.get("type") == "update":
                        await claim(machine)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            fleet.disconnect(machine.id, websocket)

    @app.websocket("/api/machines/tunnel/{tunnel_id}")
    async def tunnel(websocket: WebSocket, tunnel_id: str) -> None:
        """A machine dialling back for a tunnel the server asked it for: agents' bytes to one of its model servers."""
        await websocket.accept()
        if not runtime.remote.trusted(websocket):
            await websocket.close(code=CLOSE_UNAUTHORIZED, reason="Sign this machine in first")
            return
        if not await runtime.tunnels.attach(tunnel_id, websocket, auth.request_identity(websocket)):
            await websocket.close(code=4404, reason="No such tunnel")

    @app.get("/api/machines")
    async def machines(request: Request) -> dict[str, Any]:
        require_trusted(request)
        identity = auth.request_identity(request)
        listed = [machine for machine in fleet.machines.values() if can_see(identity, machine)]
        listed.sort(key=lambda machine: (not fleet.connected(machine.id), machine.name.lower()))
        return {"machines": [described(machine, request) for machine in listed], "removed": fleet.removed_for(identity), "features": FEATURES}

    @app.get("/api/machines/{machine_id}")
    async def machine(machine_id: str, request: Request) -> dict[str, Any]:
        require_trusted(request)
        return described(visible(machine_id, request), request)

    @app.post("/api/machines/{machine_id}/launch", status_code=201)
    async def launch(machine_id: str, request: Request) -> dict[str, Any]:
        """Start an agent on the machine, in a shared terminal; answers once its session is open."""
        require_trusted(request)
        if not remote.same_origin(request):
            raise HTTPException(status_code=403, detail="Start agents from the console's own page.")
        target = visible(machine_id, request)
        data = await request.json()
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Send a JSON object")
        agent = runtime.agents().get(str(data.get("agent") or ""))
        if agent is None:
            raise HTTPException(status_code=400, detail="Choose one of the server's agents")
        if agent.needs_model and not str(data.get("model") or "").strip():
            raise HTTPException(status_code=400, detail=f"{agent.name} needs a model")
        try:
            # The MCP servers it gets are checked for who asked; the machine's launcher asks again for itself.
            chosen = mcp.choose(fleet, auth.request_identity(request), data.get("mcp"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if chosen and not agent.mcp:
            raise HTTPException(status_code=400, detail=f"{agent.name} can't take MCP servers")
        data = {**data, "mcp": [item["id"] for item in chosen]}
        try:
            return await fleet.launch(target, auth.request_identity(request), data)
        except FleetError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    @app.post("/api/machines/{machine_id}/shell", status_code=201)
    async def shell(machine_id: str, request: Request) -> dict[str, Any]:
        """Open a shell on the machine in a terminal only you see (agent-machine commands on); answers once it's open."""
        require_trusted(request)
        if not remote.same_origin(request):
            raise HTTPException(status_code=403, detail="Open terminals from the console's own page.")
        target = visible(machine_id, request)
        data = await request.json()
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Send a JSON object")
        try:
            return await fleet.shell(target, auth.request_identity(request), data)
        except FleetError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    @app.put("/api/machines/{machine_id}/access")
    async def set_access(machine_id: str, request: Request) -> dict[str, Any]:
        """Which organizations and teams may use each of the machine's features (its owner, or an admin)."""
        require_trusted(request)
        if not remote.same_origin(request):
            raise HTTPException(status_code=403, detail="Change who can use machines from the console's own page.")
        target = visible(machine_id, request)
        identity = auth.request_identity(request)
        if not can_manage(identity, target):
            raise HTTPException(status_code=403, detail="Only the machine's owner or an admin can change who can use it.")
        data = await request.json()
        try:
            target.access = checked_access(identity, (data or {}).get("access") if isinstance(data, dict) else None, runtime.auth.store if runtime.auth.enabled else None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        fleet._save()
        return described(target, request)

    @app.post("/api/machines/{machine_id}/update")
    async def update(machine_id: str, request: Request) -> dict[str, Any]:
        """Have the machine update agent-machine (and with agent: true, its agent) to this server's build, and restart."""
        require_trusted(request)
        if not remote.same_origin(request):
            raise HTTPException(status_code=403, detail="Update machines from the console's own page.")
        target = visible(machine_id, request)
        if not allowed(auth.request_identity(request), target, "update"):
            raise HTTPException(status_code=403, detail="Only the machine's owner, an admin, or the teams it lets can update it.")
        data = await request.json()
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Send a JSON object")
        answer = await ask(target, "update", timeout=60, channel="update", agent=bool(data.get("agent")))
        return {"updating": bool(answer.get("updating")), "from": answer.get("from"), "to": answer.get("to"), "message": answer.get("message")}

    @app.get("/api/machines/{machine_id}/folders")
    async def folders(machine_id: str, request: Request, path: str = "") -> dict[str, Any]:
        """The folders inside one of the machine's launch folders, as the machine lists them; its roots without a path."""
        require_trusted(request)
        target = visible(machine_id, request)
        # Who can start agents or terminals there may look at its folders, and only inside the launch folders.
        identity = auth.request_identity(request)
        if not (allowed(identity, target, "launch") or allowed(identity, target, "terminal")):
            raise HTTPException(status_code=403, detail="Only who can start agents on this machine can browse its folders.")
        if path and not within_roots(path, target.roots):
            raise HTTPException(status_code=400, detail="That folder isn't inside one of the machine's launch folders.")
        if not path:
            return {"path": None, "parent": None, "folders": [{"name": root, "path": root} for root in target.roots]}
        answer = await ask(target, "list", timeout=15, channel="folders", path=path)
        return {"path": path, "parent": answer.get("parent"), "folders": answer.get("folders") or [], "truncated": bool(answer.get("truncated"))}

    @app.post("/api/machines/{machine_id}/providers/adopt", status_code=201)
    async def adopt(machine_id: str, request: Request) -> dict[str, Any]:
        """Add a model server the machine runs as a backend (admins only, through auth.ADMIN_ONLY)."""
        require_trusted(request)
        target = visible(machine_id, request)
        data = await request.json()
        kind = str((data or {}).get("type") or "")
        provider = next((item for item in target.providers if item["type"] == kind), None)
        if kind not in PROVIDER_TYPES or provider is None:
            raise HTTPException(status_code=404, detail=f"{target.name} doesn't run that model server")
        url = fleet.provider_url(target, provider)
        async with runtime.config_lock:
            existing = next((backend for backend in runtime.settings.backends.values() if backend.type == kind and normal_url(backend.url) == normal_url(url)), None)
            if existing is not None:
                if existing.machine:
                    raise HTTPException(status_code=409, detail="A backend already uses it")
                # Explicit, admin-authorized adoption keeps the legacy backend's name and settings.
                runtime.settings.save_backend(existing.name, {**existing.public(), "machine": target.id})
                return {"backend": existing.name, "url": existing.url}
        base = NAME.sub("-", target.name.lower()).strip("-") or "machine"
        name = f"{base}-{kind}" if kind != "ollama" else base
        taken = set(runtime.settings.backends)
        suffix = 2
        while name in taken:
            name = f"{base}-{kind}-{suffix}"
            suffix += 1
        async with runtime.config_lock:
            runtime.settings.save_backend(name, {"type": kind, "url": url, "machine": target.id})
        return {"backend": name, "url": url}

    async def ask(target: Any, op: str, timeout: float | None = 30.0, channel: str = "provider", **fields: Any) -> dict[str, Any]:
        try:
            return await fleet.ask(target, op, timeout=timeout, channel=channel, **fields)
        except FleetError as exc:
            raise HTTPException(status_code=exc.status if 400 <= exc.status < 600 else 409, detail=exc.detail) from exc

    def provider(target: Any, provider_id: str) -> tuple[str, int, Any]:
        """A model server on the machine, "{kind}-{port}", and the backend that uses it, if any."""
        parsed = parse_provider(provider_id)
        if parsed is None:
            raise HTTPException(status_code=404, detail="Name a model server as ollama-11434, strata-18500 or docker-12434")
        kind, port = parsed
        backend = next(
            (backend for backend in runtime.settings.backends.values() if backend.machine == target.id and fleet_provider_id(backend) == provider_id),
            None,
        )
        return kind, port, backend

    @app.get("/api/machines/{machine_id}/metrics")
    async def metrics(machine_id: str, request: Request) -> dict[str, Any]:
        """The machine's CPU, memory and GPU load, sampled there now."""
        require_trusted(request)
        answer = await ask(visible(machine_id, request), "metrics", timeout=15)
        return answer.get("metrics") or {}

    @app.get("/api/machines/{machine_id}/catalog/{catalog}")
    async def catalog(machine_id: str, catalog: str, request: Request, q: str = "", repo: str = "") -> dict[str, Any]:
        """Search a model catalogue from the machine that would pull from it: ollama.com, Hugging Face or Docker Hub."""
        require_trusted(request)
        if catalog not in CATALOGS:
            raise HTTPException(status_code=404, detail=f"Search one of: {', '.join(CATALOGS)}")
        answer = await ask(visible(machine_id, request), "search", timeout=30, catalog=catalog, query=q.strip()[:200], repo=repo.strip()[:200])
        return answer.get("result") or {}

    @app.get("/api/machines/{machine_id}/providers/{provider_id}")
    async def provider_status(machine_id: str, provider_id: str, request: Request) -> dict[str, Any]:
        """A model server on the machine, as the machine sees it now, with the traffic agents sent it here."""
        require_trusted(request)
        target = visible(machine_id, request)
        kind, port, backend = provider(target, provider_id)
        answer = await ask(target, "status", timeout=15, **provider_request(backend, kind, port))
        return {
            "machine": target.id, "provider": provider_id, "kind": kind, "port": port,
            "url": fleet.provider_url(target, {"port": port}), "backend": backend.name if backend else None,
            "sampled_at": time.time(), "traffic": runtime.backend_traffic(backend.name) if backend else None,
            **(answer.get("state") or {}),
        }

    @app.post("/api/machines/{machine_id}/providers/{provider_id}/models/{action}", response_model=None)
    async def provider_model(machine_id: str, provider_id: str, action: str, request: Request) -> dict[str, Any] | StreamingResponse:
        """Pull, delete, load or unload a model on the machine's model server (Strata: load or unload its model).

        Pull answers with the machine's progress as NDJSON lines, Ollama's style, ending in
        success or {"status": "error"}.
        """
        require_trusted(request)
        if not remote.same_origin(request):
            raise HTTPException(status_code=403, detail="Change models from the console's own page.")
        target = visible(machine_id, request)
        if not allowed(auth.request_identity(request), target, "models"):
            raise HTTPException(status_code=403, detail="Only the machine's owner, an admin, or the teams it lets can change its models.")
        kind, port, backend = provider(target, provider_id)
        if action not in MODEL_ACTIONS or (kind == "strata" and action not in {"load", "unload"}):
            raise HTTPException(status_code=404, detail="Pull, delete, load or unload a model (Strata: load or unload)")
        data = await request.json() if request.headers.get("content-length") not in (None, "0") else {}
        name = str((data or {}).get("name") or (data or {}).get("model") or "").strip() if isinstance(data, dict) else ""
        if kind != "strata" and not name:
            raise HTTPException(status_code=400, detail="A model name is required")
        fields = {**provider_request(backend, kind, port), "model": name[:300]}
        if action != "pull":
            # Loading a big model takes minutes; Strata drains requests in flight before unloading.
            answer = await ask(target, action, timeout=660 if action == "load" else 330, **fields)
            return answer.get("result") or {}
        progress: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def lines() -> Any:
            # A pull lasts as long as the download, so it waits for as long as the machine stays connected.
            task = asyncio.ensure_future(fleet.ask(target, "pull", timeout=None, channel="provider", progress=progress, **fields))
            try:
                while True:
                    getter = asyncio.ensure_future(progress.get())
                    done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
                    if getter in done:
                        yield _ndjson(getter.result())
                        continue
                    getter.cancel()
                    while not progress.empty():
                        yield _ndjson(progress.get_nowait())
                    try:
                        task.result()
                        yield _ndjson({"status": "success"})
                    except FleetError as exc:
                        yield _ndjson({"status": "error", "error": exc.detail})
                    return
            finally:
                if not task.done():
                    task.cancel()

        return StreamingResponse(lines(), media_type="application/x-ndjson", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/machines/{machine_id}/providers/{provider_id}/log")
    async def provider_log(machine_id: str, provider_id: str, request: Request, source: str = "server", tail: int = 200_000) -> PlainTextResponse:
        """The end of a model server's log on the machine: a Strata profile's server or engine log, or the Ollama agent-machine started."""
        require_trusted(request)
        if source not in {"server", "engine"}:
            raise HTTPException(status_code=400, detail="Log source must be server or engine")
        target = visible(machine_id, request)
        kind, port, backend = provider(target, provider_id)
        answer = await ask(target, "log", timeout=20, source=source, tail=max(1024, min(int(tail), 2_000_000)), **provider_request(backend, kind, port))
        return PlainTextResponse(str(answer.get("text") or ""))

    @app.get("/api/machines/{machine_id}/servers/ollama")
    async def ollama_settings(machine_id: str, request: Request) -> dict[str, Any]:
        """The settings agent-machine starts the machine's Ollama with, and how it's doing."""
        require_trusted(request)
        target = visible(machine_id, request)
        answer = await ask(target, "settings", timeout=20, channel="server", kind="ollama")
        return {**(answer.get("ollama_settings") or {}), "status": (target.servers.get("ollama") or {})}

    @app.put("/api/machines/{machine_id}/servers/ollama")
    async def configure_ollama(machine_id: str, request: Request) -> dict[str, Any]:
        """Save the settings the machine's Ollama starts with (its owner only); they apply when it next starts."""
        require_trusted(request)
        if not remote.same_origin(request):
            raise HTTPException(status_code=403, detail="Change model servers from the console's own page.")
        target = visible(machine_id, request)
        if not allowed(auth.request_identity(request), target, "servers"):
            raise HTTPException(status_code=403, detail="Only the machine's owner, or the teams it lets, can change its Ollama settings.")
        data = await request.json()
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Send the settings as a JSON object")
        answer = await ask(target, "configure", timeout=20, channel="server", kind="ollama", config=data)
        return {**(answer.get("ollama_settings") or {}), "status": (target.servers.get("ollama") or {})}

    @app.post("/api/machines/{machine_id}/servers/{kind}/{action}")
    async def model_server(machine_id: str, kind: str, action: str, request: Request) -> dict[str, Any]:
        """Start, stop or restart the Ollama or a Strata profile installed on the machine (its owner only)."""
        require_trusted(request)
        if not remote.same_origin(request):
            raise HTTPException(status_code=403, detail="Start model servers from the console's own page.")
        target = visible(machine_id, request)
        if kind not in {"ollama", "strata"} or action not in {"start", "stop", "restart"} or (kind == "strata" and action == "restart"):
            raise HTTPException(status_code=404, detail="Start, stop or restart ollama, or start or stop a strata profile")
        if not allowed(auth.request_identity(request), target, "servers"):
            raise HTTPException(status_code=403, detail="Only the machine's owner, or the teams it lets, can start or stop its model servers.")
        fields: dict[str, Any] = {"kind": kind}
        if kind == "strata":
            data = await request.json() if request.headers.get("content-length") not in (None, "0") else {}
            profile = str((data or {}).get("profile") or "")
            if not profile:
                raise HTTPException(status_code=400, detail="Name the Strata profile to start or stop")
            fields["profile"] = profile
        else:
            # Ollama listens on loopback when the machine is this computer, else where the server can reach it.
            # Agents' requests reach a machine that tunnels through it, so its Ollama stays on loopback there too.
            fields["bind"] = "127.0.0.1" if fleet.loopback(target) or target.tunnel else "0.0.0.0"
        # Strata loads (or drains and unloads) a model, which takes minutes for a big one.
        timeout = (90.0 if action == "restart" else 60.0) if kind == "ollama" else (200.0 if action == "start" else 330.0)
        try:
            await fleet.ask(target, action, timeout=timeout, channel="server", **fields)
        except FleetError as exc:
            raise HTTPException(status_code=exc.status if 400 <= exc.status < 600 else 409, detail=exc.detail) from exc
        return {"servers": target.servers}

    @app.delete("/api/machines/{machine_id}")
    async def remove(machine_id: str, request: Request) -> dict[str, Any]:
        """Take the machine out of the fleet, connected or not; it's refused if it connects again until allowed back."""
        require_trusted(request)
        if not remote.same_origin(request):
            raise HTTPException(status_code=403, detail="Remove machines from the console's own page.")
        target = visible(machine_id, request)
        if not can_manage(auth.request_identity(request), target):
            raise HTTPException(status_code=403, detail="Only its owner or an admin can remove a machine")
        try:
            await fleet.remove(machine_id, auth.request_identity(request))
        except FleetError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
        return {"removed": machine_id}

    @app.post("/api/machines/{machine_id}/restore")
    async def restore(machine_id: str, request: Request) -> dict[str, Any]:
        """Let a removed machine join again: its owner or an admin."""
        require_trusted(request)
        if not remote.same_origin(request):
            raise HTTPException(status_code=403, detail="Allow machines back from the console's own page.")
        identity = auth.request_identity(request)
        if machine_id not in {item["id"] for item in fleet.removed_for(identity)}:
            raise HTTPException(status_code=404, detail="No removed machine of yours with that id")
        fleet.restore(machine_id)
        return {"restored": machine_id}


def _ndjson(value: dict[str, Any]) -> bytes:
    return (json.dumps(value) + "\n").encode()
