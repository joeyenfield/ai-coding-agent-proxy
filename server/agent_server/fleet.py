"""The fleet: machines signed in to this server, and what they can do for it.

The server is the hub, agents connect to it, and machines supply the LLM providers:
every local model server a backend points at runs on one of the fleet's machines.

Every machine's agent-machine (the machine/ component) holds one WebSocket here
(/api/machines/connect) while it runs. Over it, the machine says what it is (name, OS,
agent-machine's version), which of the server's agents are installed on it, which model servers run on it (Ollama, Docker
Model Runner, Strata), whether it runs containers for the server (and which engines),
and whether it takes launches. The server shows the fleet, starts agents on machines
that allow it, adds a machine's model servers as backends, and asks machines to start
and stop the containers they run (containers.py), over the same connection.

The server never manages a model server itself: it only forwards agents' traffic to
them. Everything else (model lists, pulls, loading, logs, Ollama's settings, the
machine's load, searching model catalogues) is a request to the machine running it,
on the "provider" and "server" channels (Fleet.ask), which agent-machine answers from
the machine itself.

Running containers on a machine is as powerful as a shell there, so it's off until
`agent-machine containers on`, and the machine checks every request itself.

Starting an agent on a machine runs a program there as its owner, so:
  - the machine has to switch it on itself (`agent-machine launches on`), and lists the
    folders agents may start in (`agent-machine roots add <dir>`);
  - only the machine's owner can start agents on it, not admins;
  - the folder is checked here and again on the machine;
  - every launch is recorded with who asked, and the session it opened.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit
from typing import Any

from . import strata, version
from .auth import Identity


# Launch records kept per machine, newest last.
LAUNCH_HISTORY = 30
# How long a launch may take to open its session on the machine.
LAUNCH_TIMEOUT = 60.0
PROVIDER_TYPES = ("ollama", "docker", "strata")
ENGINE_FIELDS = ("id", "label", "backend_type", "image", "images", "port", "data_path", "volume", "health_path", "stop_timeout", "options", "summary")


@dataclass
class Machine:
    id: str
    name: str
    owner: str | None = None
    # The computer's own name, which agents started there by hand report (their sessions' hostname tag).
    hostname: str = ""
    os: str = ""
    arch: str = ""
    version: str = ""
    # Where the server reaches the machine: its address as seen here, unless the machine names one.
    address: str = ""
    agents: list[str] = field(default_factory=list)
    providers: list[dict[str, Any]] = field(default_factory=list)
    # Whether it runs containers for the server: its CLI, whether the engine answers, the engines it can run.
    containers: dict[str, Any] = field(default_factory=dict)
    # Whether the server may start and stop the Ollama and Strata installed there, and their state.
    servers: dict[str, Any] = field(default_factory=dict)
    launches: bool = False
    # Whether the agent command launches run is installed there; None from a machine that doesn't say.
    launcher: bool | None = None
    # The protocol level its agent-machine speaks (version.py); 0 from one too old to say.
    protocol: int = 0
    # Whether it carries agents' requests to its model servers itself (tunnels.py).
    tunnel: bool = False
    roots: list[str] = field(default_factory=list)
    first_seen: float = 0.0
    last_seen: float = 0.0
    history: list[dict[str, Any]] = field(default_factory=list)
    # The API keys of the Strata servers it runs, by port, for forwarding agents' traffic.
    # Held in memory only: never saved to machines.json or shown in the console.
    keys: dict[int, str] = field(default_factory=dict, repr=False)

    def stored(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if key != "keys"}


class FleetError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def can_see(identity: Identity | None, machine: Machine) -> bool:
    """Its owner and admins see a machine; without accounts, everyone does."""
    return identity is None or identity.admin or machine.owner == identity.user


def can_launch(identity: Identity | None, machine: Machine) -> bool:
    """Only the owner starts programs on a machine: admins included, private means private."""
    return identity is None or machine.owner == identity.user


def within_roots(project: str, roots: list[str]) -> bool:
    """Whether project is one of the roots or a folder inside one (no "..", any OS's paths)."""
    if not project or ".." in project.replace("\\", "/").split("/"):
        return False
    for root in roots:
        windows = ":" in root[:3] or "\\" in root
        kind = PureWindowsPath if windows else PurePosixPath
        try:
            path, base = kind(project), kind(root)
        except ValueError:
            continue
        if windows:
            path, base = PureWindowsPath(str(path).lower()), PureWindowsPath(str(base).lower())
        if path == base or base in path.parents:
            return True
    return False


class Fleet:
    """The machines: those connected now, and the rest from machines.json."""

    def __init__(self, path: Path):
        self.path = path
        self.machines: dict[str, Machine] = {}
        self.sockets: dict[str, Any] = {}
        self._waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # Which machine each waiting request went to, so its answer fails when the machine goes.
        self._asked: dict[str, str] = {}
        # Progress of streamed requests (pulls), by request id.
        self._progress: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._load()

    # Storage

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for item in data.get("machines", []) if isinstance(data, dict) else []:
            try:
                machine = Machine(**{key: value for key, value in item.items() if key in Machine.__dataclass_fields__})
            except TypeError:
                continue
            self.machines[machine.id] = machine

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"machines": [machine.stored() for machine in self.machines.values()]}, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    # Connections

    def connect(self, socket: Any, identity: Identity | None, client_host: str, hello: dict[str, Any]) -> Machine:
        machine_id = str(hello.get("id") or "").strip()
        if not machine_id or len(machine_id) > 64 or not machine_id.replace("-", "").isalnum():
            raise FleetError(4400, "A machine id is letters, numbers and '-'")
        existing = self.machines.get(machine_id)
        owner = identity.user if identity else None
        if existing and existing.owner and owner and existing.owner != owner:
            # Another user's machine id: refuse rather than take the machine over.
            raise FleetError(4403, "That machine is registered to another user")
        machine = existing or Machine(id=machine_id, name=machine_id, first_seen=time.time())
        machine.owner = owner or machine.owner
        self._update(machine, hello, client_host)
        old = self.sockets.get(machine_id)
        self.sockets[machine_id] = socket
        self.machines[machine_id] = machine
        self._save()
        if old is not None and old is not socket:
            # The machine reconnected before the old connection was noticed as gone.
            asyncio.ensure_future(_close_quietly(old))
        return machine

    def disconnect(self, machine_id: str, socket: Any) -> None:
        if self.sockets.get(machine_id) is socket:
            del self.sockets[machine_id]
            for request_id, asked in list(self._asked.items()):
                future = self._waiting.get(request_id)
                if asked == machine_id and future and not future.done():
                    future.set_exception(ConnectionError("the machine disconnected"))
            machine = self.machines.get(machine_id)
            if machine:
                machine.last_seen = time.time()
                machine.keys = {}
                self._save()

    def message(self, machine: Machine, data: dict[str, Any], client_host: str) -> None:
        kind = data.get("type")
        if kind == "update":
            self._update(machine, data, client_host)
            self._save()
        elif kind in {"launched", "launch_failed"}:
            future = self._waiting.pop(str(data.get("launch") or ""), None)
            if future and not future.done():
                future.set_result(data)
        elif kind in {"container_result", "server_result", "provider_result"}:
            if kind == "server_result" and isinstance(data.get("servers"), dict):
                # Every answer carries the servers' state, refused ones too.
                machine.servers = _servers(data["servers"])
            request_id = str(data.get("request") or "")
            if self._asked.get(request_id) != machine.id:
                return  # not a request to this machine
            future = self._waiting.pop(request_id, None)
            if future and not future.done():
                future.set_result(data)
        elif kind == "provider_progress":
            request_id = str(data.get("request") or "")
            queue = self._progress.get(request_id)
            if queue is not None and self._asked.get(request_id) == machine.id and isinstance(data.get("progress"), dict):
                queue.put_nowait(data["progress"])
        elif kind == "ping":
            machine.last_seen = time.time()

    def _update(self, machine: Machine, data: dict[str, Any], client_host: str) -> None:
        machine.name = str(data.get("name") or machine.name)[:80]
        machine.hostname = str(data.get("hostname") or machine.hostname)[:255]
        machine.os = str(data.get("os") or machine.os)[:40]
        machine.arch = str(data.get("arch") or machine.arch)[:40]
        machine.version = str(data.get("version") or machine.version)[:40]
        machine.address = str(data.get("address") or "").strip()[:255] or client_host or machine.address
        if isinstance(data.get("agents"), list):
            machine.agents = [str(item)[:80] for item in data["agents"]][:100]
        if isinstance(data.get("providers"), list):
            listed = [item for item in data["providers"] if isinstance(item, dict) and item.get("type") in PROVIDER_TYPES][:10]
            machine.providers = [_provider(item) for item in listed]
            machine.keys = {
                provider["port"]: str(item["api_key"])[:500]
                for item, provider in zip(listed, machine.providers)
                if provider["type"] == "strata" and isinstance(item.get("api_key"), str) and item["api_key"]
            }
        if isinstance(data.get("containers"), dict):
            machine.containers = _containers(data["containers"])
        if isinstance(data.get("servers"), dict):
            machine.servers = _servers(data["servers"])
        if "launches" in data:
            machine.launches = bool(data.get("launches"))
        if isinstance(data.get("protocol"), int):
            machine.protocol = data["protocol"]
        if "tunnel" in data:
            machine.tunnel = bool(data.get("tunnel"))
        if "launcher" in data:
            machine.launcher = bool(data.get("launcher"))
        if isinstance(data.get("roots"), list):
            machine.roots = [str(item)[:400] for item in data["roots"]][:20]
        machine.last_seen = time.time()

    def connected(self, machine_id: str) -> bool:
        return machine_id in self.sockets

    def runs_containers(self, machine: Machine) -> bool:
        return self.connected(machine.id) and bool(machine.containers.get("enabled"))

    def engine(self, machine: Machine, engine_id: str) -> dict[str, Any] | None:
        return next((engine for engine in machine.containers.get("engines") or [] if engine["id"] == engine_id), None)

    async def ask(
        self, machine: Machine, op: str, timeout: float | None = 30.0, channel: str = "container",
        progress: asyncio.Queue[dict[str, Any]] | None = None, **fields: Any,
    ) -> dict[str, Any]:
        """One request to the machine, and its answer; FleetError when it can't be done.

        channel is "container" (containers.py), "server" (starting Ollama and Strata and
        Ollama's settings) or "provider" (a model server's models, status and logs, the
        machine's load, model catalogues). Containers and servers are off on the machine
        until it allows them; on the provider channel the machine itself refuses changes
        unless its servers are on. progress, for pulls, receives the machine's progress
        messages until the answer comes. timeout None waits for as long as the machine
        stays connected.
        """
        socket = self.sockets.get(machine.id)
        if socket is None:
            raise FleetError(409, f"{machine.name} isn't connected: agent-machine isn't running there, or isn't signed in here (agent-machine login, then agent-machine start).")
        if channel == "container" and not machine.containers.get("enabled"):
            raise FleetError(409, f"{machine.name} doesn't run containers. Run `agent-machine containers on` there to allow it.")
        if channel == "server" and not machine.servers.get("enabled"):
            raise FleetError(409, f"{machine.name} doesn't let the server manage its model servers. Run `agent-machine servers on` there to allow it.")
        request_id = secrets.token_hex(6)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = future
        self._asked[request_id] = machine.id
        if progress is not None:
            self._progress[request_id] = progress
        try:
            await socket.send_text(json.dumps({"type": channel, "request": request_id, "op": op, **fields}))
            answer = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise FleetError(504, f"{machine.name} didn't answer within {int(timeout or 0)} s") from exc
        except Exception as exc:  # noqa: BLE001 - the machine went away mid-request
            raise FleetError(502, f"lost the connection to {machine.name} ({exc})") from exc
        finally:
            self._waiting.pop(request_id, None)
            self._asked.pop(request_id, None)
            self._progress.pop(request_id, None)
        if not answer.get("ok"):
            raise FleetError(409, str(answer.get("error") or f"{machine.name} refused it"))
        return answer

    def key(self, machine_id: str | None, port: int | None) -> str | None:
        """The API key a machine reported for the Strata server on that port."""
        machine = self.machines.get(machine_id or "")
        return machine.keys.get(port or 0) if machine and port else None

    def moved(self, machine: Machine, backends: dict[str, Any]) -> dict[str, str]:
        """The machine's backends whose address isn't the machine's any more, with the URL at its address now.

        A machine's address changes when it says another one (`agent-machine address`) or
        connects from another; its model servers and containers move with it, on the same ports.
        """
        if not machine.address:
            return {}
        found = {}
        for name, backend in backends.items():
            if backend.machine != machine.id:
                continue
            parsed = urlsplit(backend.url)
            if not parsed.port or parsed.scheme not in {"http", "https"}:
                continue
            url = normal_url(self.provider_url(machine, {"port": parsed.port}))
            if normal_url(f"{parsed.scheme}://{parsed.netloc}") != url:
                found[name] = url + (parsed.path.rstrip("/") if parsed.path not in ("", "/") else "")
        return found

    def unclaimed(self, machine: Machine, backends: dict[str, Any]) -> list[str]:
        """Backends from before machines supplied providers that point at one of this machine's model servers."""
        found = []
        for provider in machine.providers:
            url = normal_url(self.provider_url(machine, provider))
            for name, backend in backends.items():
                if not backend.machine and backend.type == provider["type"] and normal_url(backend.url) == url:
                    found.append(name)
        return found

    def machine_for(self, tags: dict[str, Any]) -> Machine | None:
        """The machine a session runs on, from its tags.

        A launch from the console names the machine (agent-machine sets it). An agent started
        by hand only knows its computer's name: that's the machine of the same owner with that
        hostname (or, from an agent-machine too old to report it, that name). Tags come from
        the client, so a session only joins a machine of its own owner.
        """
        owner = tags.get("owner")

        def ours(machine: Machine) -> bool:
            return not owner or not machine.owner or machine.owner == owner

        named = self.machines.get(str(tags.get("machine") or ""))
        if named is not None:
            return named if ours(named) else None
        hostname = str(tags.get("hostname") or tags.get("machine_name") or "").lower()
        if not hostname:
            return None
        found = [machine for machine in self.machines.values() if ours(machine) and (machine.hostname or machine.name).lower() == hostname]
        # Two machines on one computer (two agent-machine homes) can't be told apart; the connected one wins.
        found.sort(key=lambda machine: (not self.connected(machine.id), -machine.last_seen))
        return found[0] if found else None

    async def close_all(self) -> None:
        """Before a restart: machines reconnect by themselves."""
        for socket in list(self.sockets.values()):
            await _close_quietly(socket, 1012, "Server restarting")

    def forget(self, machine_id: str) -> None:
        if self.connected(machine_id):
            raise FleetError(409, "That machine is connected; stop agent-machine there (agent-machine stop) first")
        self.machines.pop(machine_id, None)
        self._save()

    # What the console sees

    def public(self, machine: Machine, identity: Identity | None, backends: dict[str, Any]) -> dict[str, Any]:
        """The machine for the console; each provider names the backend that already uses it, if any."""
        by_target: dict[tuple[str, str], str] = {}
        for name, backend in backends.items():
            # A container can share a model server's address; the first backend is the server's own.
            by_target.setdefault((backend.type, normal_url(backend.url)), name)
        providers = []
        for provider in machine.providers:
            url = self.provider_url(machine, provider)
            providers.append({**provider, "url": url, "backend": by_target.get((provider["type"], normal_url(url)))})
        return {
            **{key: value for key, value in machine.stored().items() if key != "history"},
            "connected": self.connected(machine.id),
            "providers": providers,
            # The backends it supplies: model servers added from it and containers it runs.
            "backends": sorted(name for name, backend in backends.items() if backend.machine == machine.id),
            "history": machine.history[-10:],
            "you": {"owner": identity is None or machine.owner == identity.user, "launch": can_launch(identity, machine)},
            # A machine on another build than the server's updates itself from the server's downloads.
            "update": {"available": bool(machine.version) and machine.version != version.current(), "server_version": version.current(), "command": "agent-machine update"},
        }

    @staticmethod
    def provider_url(machine: Machine, provider: dict[str, Any]) -> str:
        host = machine.address or "127.0.0.1"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{int(provider['port'])}"

    @staticmethod
    def loopback(machine: Machine) -> bool:
        """Whether the machine is this computer, so what it runs only needs to listen on loopback."""
        host = (machine.address or "127.0.0.1").strip("[]")
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    # Launches

    async def launch(self, machine: Machine, identity: Identity | None, request: dict[str, Any]) -> dict[str, Any]:
        if not can_launch(identity, machine):
            raise FleetError(403, "Only the machine's owner can start agents on it.")
        socket = self.sockets.get(machine.id)
        if socket is None:
            raise FleetError(409, f"{machine.name} isn't connected: agent-machine isn't running there, or isn't signed in here (agent-machine login, then agent-machine start).")
        if not machine.launches:
            raise FleetError(409, f"{machine.name} doesn't take launches. Run `agent-machine launches on` there to allow it.")
        project = str(request.get("project") or "").strip()
        if not within_roots(project, machine.roots):
            raise FleetError(400, "Choose a folder inside one of the machine's launch folders (agent-machine roots add <dir>).")
        agent = str(request.get("agent") or "")
        if agent not in machine.agents:
            raise FleetError(400, f"{agent or 'That agent'} isn't installed on {machine.name}.")
        launch_id = secrets.token_hex(6)
        message = {
            "type": "launch", "launch": launch_id, "agent": agent, "project": project,
            "model": str(request.get("model") or ""), "backend": str(request.get("backend") or ""), "trace": bool(request.get("trace")),
            "visibility": str(request.get("visibility") or "private"), "team": str(request.get("team") or request.get("org") or ""),
            "access": "control" if request.get("access") == "control" else "view",
        }
        record = {"launch": launch_id, "at": time.time(), "by": identity.user if identity else None, "agent": agent, "project": project, "session_id": None, "error": None}
        machine.history = [*machine.history, record][-LAUNCH_HISTORY:]
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiting[launch_id] = future
        try:
            await socket.send_text(json.dumps(message))
            answer = await asyncio.wait_for(future, LAUNCH_TIMEOUT)
        except asyncio.TimeoutError:
            answer = {"type": "launch_failed", "error": f"{machine.name} didn't open a session within {int(LAUNCH_TIMEOUT)} s"}
        except Exception as exc:  # noqa: BLE001 - the machine went away mid-launch
            answer = {"type": "launch_failed", "error": f"lost the connection to {machine.name} ({exc})"}
        finally:
            self._waiting.pop(launch_id, None)
        if answer.get("type") == "launched" and answer.get("session_id"):
            record["session_id"] = str(answer["session_id"])
        else:
            record["error"] = str(answer.get("error") or "the launch failed")
        self._save()
        if record["error"]:
            raise FleetError(502, record["error"])
        return record


def _provider(item: dict[str, Any]) -> dict[str, Any]:
    port = int(item.get("port") or 0)
    if not 0 < port < 65536:
        port = {"ollama": 11434, "docker": 12434, "strata": 18500}[item["type"]]
    models = item.get("models")
    return {"type": item["type"], "port": port, "version": str(item.get("version") or "")[:40], "models": models if isinstance(models, int) else None}


def _servers(item: dict[str, Any]) -> dict[str, Any]:
    """What a machine says about the Ollama and Strata it can start, kept to known fields."""
    if not item.get("enabled"):
        return {"enabled": False}
    ollama = item.get("ollama") if isinstance(item.get("ollama"), dict) else {}
    strata = item.get("strata") if isinstance(item.get("strata"), dict) else {}
    profiles = []
    for profile in strata.get("profiles") or []:
        if isinstance(profile, dict) and isinstance(profile.get("id"), str):
            profiles.append({
                "id": profile["id"][:64], "state": str(profile.get("state") or "unknown")[:40],
                "url": str(profile.get("url") or "")[:255] or None, "error": str(profile.get("error") or "")[:300] or None,
            })
    return {
        "enabled": True,
        "ollama": {
            "found": bool(ollama.get("found")), "running": bool(ollama.get("running")), "managed": bool(ollama.get("managed")),
            "version": str(ollama.get("version") or "")[:40] or None, "bind": str(ollama.get("bind") or "")[:40] or None,
            # An Ollama run by systemd, and whether agent-machine may restart it (sudo agent-machine servers service).
            "service": "systemd" if ollama.get("service") == "systemd" else None, "service_allowed": bool(ollama.get("service_allowed")),
        },
        "strata": {"found": bool(strata.get("found")), "profiles": profiles[:20], "error": str(strata.get("error") or "")[:300] or None},
    }


def _containers(item: dict[str, Any]) -> dict[str, Any]:
    engines = []
    for engine in item.get("engines") or []:
        if isinstance(engine, dict) and isinstance(engine.get("id"), str) and engine.get("backend_type") in PROVIDER_TYPES:
            engines.append({key: engine.get(key) for key in ENGINE_FIELDS})
    return {
        "enabled": bool(item.get("enabled")), "cli": str(item.get("cli") or "docker")[:40],
        "found": bool(item.get("found")), "online": bool(item.get("online")),
        "version": str(item.get("version") or "")[:40] or None, "error": str(item.get("error") or "")[-500:] or None,
        "engines": engines[:10],
    }


def provider_id(backend: Any) -> str | None:
    """The model server a machine's backend is on that machine: "{type}-{port}", such as ollama-11434."""
    if not backend.machine or backend.type not in PROVIDER_TYPES:
        return None
    try:
        port = urlsplit(backend.url).port
    except ValueError:
        return None
    return f"{backend.type}-{port}" if port else None


def parse_provider(value: str) -> tuple[str, int] | None:
    """"ollama-11434" as ("ollama", 11434); None for anything else."""
    kind, _, port = value.rpartition("-")
    if kind not in PROVIDER_TYPES or not port.isdigit() or not 0 < int(port) < 65536:
        return None
    return kind, int(port)


def provider_request(backend: Any, kind: str, port: int) -> dict[str, Any]:
    """What a provider request names: the kind and port, and a Strata container's key, which only the server knows."""
    fields: dict[str, Any] = {"kind": kind, "port": port}
    if kind == "strata" and backend is not None and backend.container:
        key, _ = strata.key_for(backend)
        if key:
            fields["key"] = key
    return fields


def normal_url(url: str) -> str:
    """For comparing model server addresses: localhost is 127.0.0.1, no trailing slash."""
    return url.rstrip("/").replace("://localhost:", "://127.0.0.1:").replace("://localhost/", "://127.0.0.1/")


async def _close_quietly(socket: Any, code: int = 4409, reason: str = "Replaced by a newer connection") -> None:
    try:
        await socket.close(code=code, reason=reason)
    except Exception:  # noqa: BLE001 - already closed
        pass
