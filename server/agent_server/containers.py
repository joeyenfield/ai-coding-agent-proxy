"""Model servers in containers that the fleet's machines run for the server.

A backend in backends.yaml with `machine:` and `container:` is a container on that
machine. The machine's agent-machine runs it: it knows the engines it can run (Strata,
Ollama), builds the `docker run` (or `podman run`) from the engine's template, and
starts, stops and switches containers when the server asks over its fleet connection
(fleet.Fleet.ask). The server keeps what's needed across machines: the specs, the API
keys it generates for Strata containers (so it can always talk to them), which
containers share a machine's GPUs, and whether each one answers yet.

Running a container is as powerful as a shell on that machine, so a machine only takes
these requests after `agent-machine containers on`, checks every spec itself, and only
its owner can create containers there. Specs only accept the machine's engines, their
own settings and plain image names, here and again on the machine.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from . import strata
from .config import Backend, Settings
from .fleet import Fleet, FleetError, Machine


PREFIX = "ai-proxy-"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}")
# A plain image reference (registry/name:tag or @digest); never something the CLI reads as a flag.
IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}")
VOLUME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
GPUS = re.compile(r"all|none|\d+(,\d+)*")
ENV_VALUE = re.compile(r"[^\x00-\x1f]{0,200}")
# Removing waits for the container to stop, which Strata takes up to two minutes over.
REMOVE_SECONDS = 240.0


def container_name(backend: str) -> str:
    return PREFIX + backend


def validate_spec(raw: Any, engine: dict[str, Any]) -> dict[str, Any]:
    """A container block for one of the machine's engines, checked and normalized. Raises ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("container must be an object")
    if str(raw.get("engine") or "") != engine["id"]:
        raise ValueError(f"This spec is for {raw.get('engine')}, not {engine['id']}")
    image = str(raw.get("image") or engine["image"]).strip()
    if not IMAGE.fullmatch(image):
        raise ValueError(f"Image must be an image name such as {engine['image']}")
    try:
        port = int(raw.get("port") or engine["port"])
    except (TypeError, ValueError) as exc:
        raise ValueError("Port must be a number") from exc
    if not 1024 <= port <= 65535:
        raise ValueError("Port must be between 1024 and 65535")
    gpus = str(raw.get("gpus") if raw.get("gpus") is not None else "all").replace(" ", "").lower() or "none"
    if not GPUS.fullmatch(gpus):
        raise ValueError("GPUs must be all, none, or card numbers such as 0 or 0,1")
    allowed = {option["env"] for option in engine.get("options") or []}
    env: dict[str, str] = {}
    for key, value in (raw.get("env") or {}).items():
        if key not in allowed:
            raise ValueError(f"{key} isn't a {engine['label']} setting; use one of: {', '.join(sorted(allowed))}")
        text = "" if value is None else str(value).strip()
        if not ENV_VALUE.fullmatch(text):
            raise ValueError(f"{key} has an invalid value")
        if text:
            env[key] = text
    spec: dict[str, Any] = {"engine": engine["id"], "image": image, "port": port, "gpus": gpus, "env": env}
    volume = str(raw.get("volume") or "").strip()
    if volume:
        spec["volume"] = _volume(volume)
    return spec


def _volume(value: str) -> str:
    """A named volume, or an absolute folder on the machine to bind-mount."""
    if VOLUME.fullmatch(value) or value.startswith("/") or re.fullmatch(r"[A-Za-z]:[\\/].*", value):
        if "," in value or "\n" in value:
            raise ValueError("Volume can't contain commas or line breaks")
        return value
    raise ValueError("Volume must be a volume name such as strata-data or an absolute folder")


def gpu_set(spec: dict[str, Any]) -> set[str]:
    gpus = spec.get("gpus", "all")
    if gpus == "none":
        return set()
    return {"*"} if gpus == "all" else set(gpus.split(","))


def gpus_overlap(first: dict[str, Any], second: dict[str, Any]) -> bool:
    a, b = gpu_set(first), gpu_set(second)
    return bool(a and b) and ("*" in a or "*" in b or bool(a & b))


class KeyStore:
    """API keys the server generated for Strata containers, by backend name, next to the access key."""

    def __init__(self, path: Path):
        self.path = path
        self._keys: dict[str, str] | None = None
        self._lock = threading.Lock()

    def all(self) -> dict[str, str]:
        with self._lock:
            return dict(self._load())

    def get(self, name: str, create: bool = False) -> str | None:
        with self._lock:
            keys = self._load()
            if name not in keys and create:
                keys[name] = secrets.token_urlsafe(24)
                self._save(keys)
            return keys.get(name)

    def drop(self, name: str) -> None:
        with self._lock:
            keys = self._load()
            if keys.pop(name, None) is not None:
                self._save(keys)

    def _load(self) -> dict[str, str]:
        if self._keys is None:
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                self._keys = {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}
            except (OSError, ValueError):
                self._keys = {}
            self._publish(self._keys)
        return self._keys

    def _save(self, keys: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(keys, handle, indent=2)
        temporary.replace(self.path)
        self._publish(keys)

    @staticmethod
    def _publish(keys: dict[str, str]) -> None:
        # Strata requests look their key up here (strata.key_for).
        strata.managed_keys.clear()
        strata.managed_keys.update(keys)


class Conflict(Exception):
    """Other running containers on the same machine hold the GPUs this one needs."""

    def __init__(self, names: list[str]):
        super().__init__(", ".join(names))
        self.names = names


@dataclass
class Placement:
    """Where a container backend runs: its machine (None once the machine is forgotten) and the engine there."""

    machine: Machine | None
    engine: dict[str, Any] | None


class ContainerManager:
    def __init__(self, settings: Settings, keys: KeyStore, fleet: Fleet):
        self.settings = settings
        self.keys = keys
        self.fleet = fleet
        keys.all()

    def managed(self) -> dict[str, Backend]:
        return {name: backend for name, backend in self.settings.backends.items() if backend.container}

    def backend(self, name: str) -> Backend:
        backend = self.managed().get(name)
        if backend is None:
            raise KeyError(name)
        return backend

    def placement(self, backend: Backend) -> Placement:
        machine = self.fleet.machines.get(backend.machine or "")
        engine = self.fleet.engine(machine, str((backend.container or {}).get("engine"))) if machine else None
        return Placement(machine, engine)

    def bind(self, machine: Machine) -> str:
        """Containers on this computer stay on loopback; on another machine the server has to reach them."""
        return "127.0.0.1" if self.fleet.loopback(machine) else "0.0.0.0"

    def url_for(self, machine: Machine, port: int) -> str:
        return self.fleet.provider_url(machine, {"port": port})

    def request(self, name: str, backend: Backend, machine: Machine) -> dict[str, Any]:
        """What the machine needs to build a container's command: its spec, Strata's key and where to listen."""
        spec = dict(backend.container or {})
        key = self.keys.get(name, create=True) if spec.get("engine") == "strata" else ""
        return {"spec": spec, "key": key or "", "bind": self.bind(machine)}

    def taken_ports(self, machine: Machine, leave_out: str | None = None) -> set[int]:
        """Ports the machine's other containers and model servers use."""
        ports = {int(provider["port"]) for provider in machine.providers}
        for name, backend in self.managed().items():
            if name != leave_out and backend.machine == machine.id:
                ports.add(int((backend.container or {}).get("port") or 0))
        return ports

    async def status(self, client: httpx.AsyncClient | None, names: list[str] | None = None) -> list[dict[str, Any]]:
        """Every container backend (or the named ones), with what its machine says about it."""
        managed = {name: backend for name, backend in self.managed().items() if names is None or name in names}
        reports: dict[str, dict[str, Any]] = {}
        problems: dict[str, str] = {}
        by_machine: dict[str, list[str]] = {}
        for name, backend in managed.items():
            by_machine.setdefault(backend.machine or "", []).append(name)
        for machine_id, members in by_machine.items():
            machine = self.fleet.machines.get(machine_id)
            if machine is None:
                for name in members:
                    problems[name] = "Its machine isn't in the fleet: sign that machine in, or delete this container."
                continue
            if not self.fleet.runs_containers(machine):
                reason = (
                    f"{machine.name} isn't connected." if not self.fleet.connected(machine.id)
                    else f"{machine.name} has containers off: run `agent-machine containers on` there."
                )
                for name in members:
                    problems[name] = reason
                continue
            # Every container on the machine, so GPU conflicts see the ones not asked about.
            everything = [name for name, backend in self.managed().items() if backend.machine == machine_id]
            try:
                answer = await self.fleet.ask(machine, "status", containers={name: self.request(name, self.settings.backends[name], machine) for name in everything})
            except FleetError as exc:
                for name in members:
                    problems[name] = exc.detail
                continue
            for name, report in (answer.get("containers") or {}).items():
                if isinstance(report, dict):
                    reports[name] = report
        states = {name: (report.get("state") or None) for name, report in reports.items()}
        result = []
        for name, backend in managed.items():
            spec = dict(backend.container or {})
            place = self.placement(backend)
            report = reports.get(name) or {}
            state = report.get("state") or None
            job = report.get("job") or None
            ready = False
            if state and state.get("running") and place.engine and client is not None:
                ready = state.get("health") == "healthy" or (state.get("health") in (None, "") and await _answers(client, backend.url + str(place.engine.get("health_path") or "/")))
            problem = problems.get(name) or report.get("problem") or None
            result.append({
                "name": name, "engine": spec.get("engine"),
                "engine_label": (place.engine or {}).get("label") or str(spec.get("engine") or "").title(),
                "backend_type": backend.type, "url": backend.url, "spec": spec, "container": container_name(name),
                "machine": backend.machine, "machine_name": place.machine.name if place.machine else None,
                "machine_online": bool(place.machine and self.fleet.runs_containers(place.machine)),
                "state": state, "ready": ready, "phase": _phase(state, ready, job), "outdated": bool(report.get("outdated")),
                "job": job, "command": report.get("command") or None, "problem": problem,
                "conflicts": self.conflicts(name, states) if not problems.get(name) else [],
                "volume": spec.get("volume") or (place.engine or {}).get("volume"),
                "data_path": (place.engine or {}).get("data_path"),
                "default": name == self.settings.default_backend,
            })
        return result

    def conflicts(self, name: str, states: dict[str, dict[str, Any] | None]) -> list[str]:
        """Running containers on the same machine that use the GPUs this one needs."""
        mine = self.backend(name)
        return [
            other for other, backend in self.managed().items()
            if other != name and backend.machine == mine.machine and (states.get(other) or {}).get("running")
            and gpus_overlap(mine.container or {}, backend.container or {})
        ]

    def _machine(self, name: str) -> tuple[Backend, Machine]:
        backend = self.backend(name)
        machine = self.fleet.machines.get(backend.machine or "")
        if machine is None:
            raise FleetError(409, f"{name}'s machine isn't in the fleet any more")
        return backend, machine

    async def start(self, name: str, switch: bool = False) -> dict[str, Any]:
        """Start (or create) a container. With switch, the machine first stops the ones holding its GPUs."""
        backend, machine = self._machine(name)
        everything = [other for other, item in self.managed().items() if item.machine == machine.id]
        states = {item["name"]: item["state"] for item in await self.status(None, everything)}
        conflicts = self.conflicts(name, states)
        if conflicts and not switch:
            raise Conflict(conflicts)
        stop_first = [{"name": other, "engine": (self.settings.backends[other].container or {}).get("engine")} for other in conflicts]
        answer = await self.fleet.ask(machine, "start", name=name, stop_first=stop_first, **self.request(name, backend, machine))
        return answer.get("job") or {}

    async def stop(self, name: str) -> dict[str, Any]:
        backend, machine = self._machine(name)
        answer = await self.fleet.ask(machine, "stop", name=name, engine=(backend.container or {}).get("engine"))
        return answer.get("job") or {}

    async def restart(self, name: str) -> dict[str, Any]:
        backend, machine = self._machine(name)
        answer = await self.fleet.ask(machine, "restart", name=name, **self.request(name, backend, machine))
        return answer.get("job") or {}

    async def remove(self, name: str) -> None:
        """Delete the container on its machine (the shared model storage stays)."""
        backend, machine = self._machine(name)
        await self.fleet.ask(machine, "remove", timeout=REMOVE_SECONDS, name=name, engine=(backend.container or {}).get("engine"))

    async def logs(self, name: str, tail: int = 400) -> str:
        _, machine = self._machine(name)
        answer = await self.fleet.ask(machine, "logs", name=name, key=self.keys.get(name) or "", tail=tail)
        return str(answer.get("text") or "")


def _phase(state: dict[str, Any] | None, ready: bool, job: dict[str, Any] | None) -> str:
    """One word for the console: what the container is doing now."""
    if job and job.get("state") == "running":
        lines = list(job.get("lines") or [])
        if lines and lines[-1].startswith("Stopping") or job.get("action") == "stop":
            return "stopping"
        pulling = any(line.startswith("Pulling") for line in lines) and not any(line.startswith("Creating") for line in lines)
        return "pulling" if pulling else "starting"
    failed = bool(job and job.get("state") == "failed")
    if not state:
        return "failed" if failed else "not_created"
    if state.get("running"):
        if ready:
            return "ready"
        return "unhealthy" if state.get("health") == "unhealthy" else "loading"
    return "failed" if failed else "stopped"


async def _answers(client: httpx.AsyncClient, url: str) -> bool:
    try:
        return (await client.get(url, timeout=1.5)).status_code < 500
    except httpx.HTTPError:
        return False
