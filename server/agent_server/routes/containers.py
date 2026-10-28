"""Containers the fleet's machines run as backends: create, start, stop, switch and read their logs.

Running a container is as powerful as a shell on the machine that runs it, so every
route here needs the same trust as shared terminals (this computer, a paired device or
a signed-in user), changes also refuse pages from other sites, and only the machine's
owner creates, changes or deletes containers on it (admins too, through
auth.ADMIN_ONLY, as they add backends). Starting and stopping a container that's set up
is open to anyone trusted: the launcher does it for `agent --backend <container>`.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .. import auth, containers, remote
from ..fleet import FleetError, can_see, owns
from ..runtime import Runtime


def register(app: FastAPI, runtime: Runtime) -> None:
    settings = runtime.settings
    manager = runtime.containers
    fleet = runtime.fleet

    def require_trusted(request: Request, change: bool = False) -> None:
        if not runtime.remote.trusted(request) or (change and not remote.same_origin(request)):
            raise HTTPException(status_code=401, detail="Containers can only be managed from this computer, a paired device or a signed-in account.")

    def managed(name: str) -> None:
        if name not in manager.managed():
            raise HTTPException(status_code=404, detail=f"{name} isn't a container a machine runs")

    def owned_machine(machine_id: str, request: Request) -> Any:
        """A machine this user may run containers on: theirs, connected, with containers on."""
        identity = auth.request_identity(request)
        machine = fleet.machines.get(machine_id)
        if machine is None or not can_see(identity, machine):
            raise HTTPException(status_code=404, detail="No such machine")
        # Containers stay the owner's (and admins', through ADMIN_ONLY): docker run is root on the machine.
        if not owns(identity, machine):
            raise HTTPException(status_code=403, detail=f"Only {machine.owner or 'its owner'} can run containers on {machine.name}.")
        if not fleet.connected(machine.id):
            raise HTTPException(status_code=409, detail=f"{machine.name} isn't connected: start agent-machine there (agent-machine start).")
        if not machine.containers.get("enabled"):
            raise HTTPException(status_code=409, detail=f"{machine.name} doesn't run containers. Run `agent-machine containers on` there.")
        return machine

    def failed(exc: FleetError) -> HTTPException:
        return HTTPException(status_code=exc.status if 400 <= exc.status < 600 else 409, detail=exc.detail)

    @app.get("/api/containers")
    async def overview(request: Request) -> dict[str, Any]:
        require_trusted(request)
        identity = auth.request_identity(request)
        hosts = []
        for machine in fleet.machines.values():
            if not can_see(identity, machine):
                continue
            hosts.append({
                "id": machine.id, "name": machine.name, "connected": fleet.connected(machine.id),
                "owner": machine.owner, "you": {"owner": owns(identity, machine)},
                **{key: machine.containers.get(key) for key in ("enabled", "cli", "found", "online", "version", "error")},
                "engines": machine.containers.get("engines") or [],
            })
        hosts.sort(key=lambda item: (not (item["connected"] and item["enabled"]), item["name"].lower()))
        return {"machines": hosts, "containers": await manager.status(runtime.client)}

    @app.get("/api/containers/{name}")
    async def one(name: str, request: Request) -> dict[str, Any]:
        require_trusted(request)
        managed(name)
        return (await manager.status(runtime.client, [name]))[0]

    @app.post("/api/containers", status_code=201)
    async def create(request: Request) -> dict[str, Any]:
        require_trusted(request, change=True)
        data = await request.json()
        name = str(data.get("name") or "").strip()
        if not containers.NAME.fullmatch(name):
            raise HTTPException(status_code=400, detail="Name it with letters, numbers, - and _ (up to 40)")
        if name in settings.backends:
            raise HTTPException(status_code=409, detail=f"There's already a backend called {name}")
        machine = owned_machine(str(data.get("machine") or ""), request)
        engine = fleet.engine(machine, str(data.get("engine") or ""))
        if engine is None:
            raise HTTPException(status_code=400, detail=f"{machine.name} can't run that engine")
        spec = _spec(data, engine, manager.taken_ports(machine))
        async with runtime.config_lock:
            backend = settings.save_backend(name, {"type": engine["backend_type"], "url": manager.url_for(machine, spec["port"]), "machine": machine.id, "container": spec})
        return {"name": name, "url": backend.url, "machine": machine.id, "spec": spec}

    @app.put("/api/containers/{name}")
    async def update(name: str, request: Request) -> dict[str, Any]:
        require_trusted(request, change=True)
        managed(name)
        current = settings.backends[name]
        machine = owned_machine(current.machine or "", request)
        data = await request.json()
        engine_id = (current.container or {}).get("engine")
        if data.get("engine") and data["engine"] != engine_id:
            raise HTTPException(status_code=400, detail="A container's engine can't change; add a new one instead")
        engine = fleet.engine(machine, str(engine_id))
        if engine is None:
            raise HTTPException(status_code=409, detail=f"{machine.name} doesn't offer the {engine_id} engine any more")
        spec = _spec({**data, "engine": engine_id}, engine, manager.taken_ports(machine, leave_out=name))
        value = {**current.public(), "url": manager.url_for(machine, spec["port"]), "container": spec}
        async with runtime.config_lock:
            settings.save_backend(name, value)
        # The new settings apply when the container is next started (the machine recreates it then).
        return {"name": name, "spec": spec}

    @app.delete("/api/containers/{name}")
    async def delete(name: str, request: Request) -> dict[str, Any]:
        require_trusted(request, change=True)
        managed(name)
        backend = settings.backends[name]
        if backend.machine in fleet.machines:
            # Still in the fleet: the container goes too, which needs the machine there.
            owned_machine(backend.machine or "", request)
            try:
                await manager.remove(name)
            except FleetError as exc:
                raise failed(exc) from exc
        try:
            async with runtime.config_lock:
                settings.remove_backend(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        manager.keys.drop(name)
        return {"deleted": name}

    @app.post("/api/containers/{name}/{action}", status_code=202)
    async def act(name: str, action: str, request: Request) -> Any:
        require_trusted(request, change=True)
        managed(name)
        if action not in {"start", "stop", "restart"}:
            raise HTTPException(status_code=404, detail="Unknown action")
        body = await request.json() if request.headers.get("content-length") not in (None, "0") else {}
        try:
            if action == "start":
                job = await manager.start(name, switch=bool((body or {}).get("switch")))
            elif action == "stop":
                job = await manager.stop(name)
            else:
                job = await manager.restart(name)
        except containers.Conflict as exc:
            return JSONResponse(status_code=409, content={
                "detail": f"{', '.join(exc.names)} {'is' if len(exc.names) == 1 else 'are'} using the GPU {name} needs. Switch to stop {'it' if len(exc.names) == 1 else 'them'} first.",
                "conflicts": exc.names,
            })
        except FleetError as exc:
            raise failed(exc) from exc
        return {"job": job}

    @app.get("/api/containers/{name}/log")
    async def log(name: str, request: Request, tail: int = 400) -> PlainTextResponse:
        require_trusted(request)
        managed(name)
        try:
            return PlainTextResponse(await manager.logs(name, max(10, min(tail, 5000))))
        except FleetError as exc:
            raise failed(exc) from exc


def _spec(data: dict[str, Any], engine: dict[str, Any], taken: set[int]) -> dict[str, Any]:
    """A validated container spec, with a free port on the machine picked when none was given."""
    raw = {key: data.get(key) for key in ("engine", "image", "port", "gpus", "env", "volume")}
    if not raw.get("port"):
        port = int(engine["port"])
        while port in taken and port < 65535:
            port += 1
        raw["port"] = port
    try:
        spec = containers.validate_spec(raw, engine)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if spec["port"] in taken:
        raise HTTPException(status_code=409, detail=f"Port {spec['port']} is already used on that machine")
    return spec
