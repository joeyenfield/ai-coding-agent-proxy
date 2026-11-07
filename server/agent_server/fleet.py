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

Every machine belongs to one tenant (organization): that of the user who registered it, set when
it first connects and never changed. Nobody outside that tenant sees it or uses anything it runs,
except site admins (who may use every feature of every machine, to look after the server), and its
model servers are backends only that tenant's agents (and site admins) can use.

Starting an agent on a machine runs a program there as its owner, so:
  - the machine has to switch it on itself (`agent-machine launches on`), and lists the
    folders agents may start in (`agent-machine roots add <dir>`);
  - only the machine's owner (and those it grants, and site admins) can start agents on it, not its tenant's admins;
  - the folder is checked here and again on the machine;
  - every launch is recorded with who asked, and the session it opened.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import json
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit
from typing import Any

from sqlalchemy import select

from . import access, api_providers, strata, version
from .auth import ROOT_TENANT, Identity, same_tenant
from .cluster import Cluster, ClusterError, LocalCluster
from .config import MACHINE_TYPES
from .provider_accounts import reported_clis
from .db import Database, kv, schema
from .sessions import SessionStore

LOGGER = logging.getLogger("agent-proxy")

# Launch records kept per machine, newest last.
LAUNCH_HISTORY = 30
# How long a launch may take to open its session on the machine.
LAUNCH_TIMEOUT = 60.0
# A machine that dropped its connection this recently is likely reconnecting (a phone changing
# networks, a token it couldn't renew in time): a request waits up to RECONNECT_WAIT for it.
RECONNECT_WINDOW = 60.0
RECONNECT_WAIT = 10.0
# Requests that only read, so one cut off by the machine dropping is asked again once it's back:
# (channel, op). Changes (pull, start, delete, ...) never are: the machine may have done them already.
READ_ONLY = {
    ("provider", "status"), ("provider", "models"), ("provider", "log"), ("provider", "metrics"), ("provider", "search"),
    ("container", "status"), ("container", "logs"), ("folders", "list"), ("files", "list"), ("files", "read"), ("server", "settings"),
}
# The client a shell's session names (`agent --shell`): it stays private, as it's a remote terminal.
SHELL_CLIENT = "shell"
# api: an OpenAI-compatible server the machine found on its loopback (LM Studio, vLLM, SGLang; its "flavor").
PROVIDER_TYPES = ("ollama", "docker", "strata", "llamacpp", "api")
API_FLAVORS = ("lmstudio", "vllm", "sglang", "openai")
# A shared MCP server's name (agent-machine mcp add), and how it runs there.
MCP_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
MCP_TRANSPORTS = ("stdio", "http")
ENGINE_FIELDS = ("id", "label", "backend_type", "image", "images", "port", "data_path", "volume", "health_path", "stop_timeout", "options", "summary")


@dataclass
class Machine:
    id: str
    name: str
    owner: str | None = None
    # The tenant (organization) it belongs to: its owner's when it first connected. None is nobody's.
    tenant: str | None = None
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
    # Whether its owner may open a shell there from the console (agent-machine commands on): a remote terminal.
    commands: bool = False
    # Whether the server may update its programs to the server's own build (agent-machine updates on).
    updates: bool = False
    # Whether the console may browse and read the files in its launch folders (agent-machine files on).
    files: bool = False
    # Who else may use each feature (FEATURES): {"launch": {"orgs": [...], "teams": ["org/team", ...]}}.
    # Its owner may use them all; the machine's own switches (launches on, ...) still apply on top.
    access: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    # Whether the agent command launches run is installed there; None from a machine that doesn't say.
    launcher: bool | None = None
    # The protocol level its agent-machine speaks (version.py); 0 from one too old to say.
    protocol: int = 0
    # Whether it carries agents' requests to its model servers itself (tunnels.py).
    tunnel: bool = False
    roots: list[str] = field(default_factory=list)
    # The MCP servers it shares (mcp.py): its bridge's loopback port and the servers by name.
    mcp: dict[str, Any] = field(default_factory=dict)
    # The provider CLIs agent-machine found there and whether each is signed in (`agent accounts detect`):
    # metadata only, never a token. None from an agent-machine too old to report them.
    provider_clis: dict[str, Any] | None = None
    first_seen: float = 0.0
    last_seen: float = 0.0
    history: list[dict[str, Any]] = field(default_factory=list)
    # The API keys of the Strata servers it runs, by port, for forwarding agents' traffic.
    # Held in memory only: never saved in the database or shown in the console.
    keys: dict[int, str] = field(default_factory=dict, repr=False)
    # The key its MCP bridge takes, new each time agent-machine starts; in memory only, like keys.
    mcp_key: str | None = field(default=None, repr=False)

    def stored(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if key not in {"keys", "mcp_key"}}


class FleetError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class MachineGone(FleetError):
    """The machine's connection went while it was answering."""


# What a machine's owner can let organizations and teams do there, in the order the console lists them.
FEATURES: dict[str, str] = {
    "view": "See it: its model servers, their models and logs, its load and the agents running there",
    "launch": "Start agents there, and browse its launch folders",
    "terminal": "Open terminals there: a shell as the user agent-machine runs as",
    "files": "Browse the files in its launch folders and read them",
    "models": "Pull, delete, load and unload models on its model servers",
    "servers": "Install llama.cpp; start, stop and configure the Ollama, llama.cpp and Strata installed there",
    "update": "Update agent-machine and agent there to this server's build",
    "mcp": "Use the MCP servers it shares in the agents they start (they run there, as its user)",
}
# What admins may do on any machine without a grant, as before machines had grants. Starting
# programs there (agents, terminals) stays the owner's: private means private.
ADMIN_FEATURES = frozenset({"view", "models", "update"})


def owns(identity: Identity | None, machine: Machine) -> bool:
    """Its owner. No identity (never the case behind the sign-in middleware) owns nothing."""
    return identity is not None and same_tenant(identity, machine.tenant) and machine.owner == identity.user


def granted(identity: Identity, machine: Machine, feature: str) -> bool:
    """A grant to identity's organization or one of its teams; only ever within the machine's tenant."""
    grant = machine.access.get(feature) or {}
    teams = {team for team in grant.get("teams") or [] if team.split("/", 1)[0] == machine.tenant}
    return machine.tenant in (set(grant.get("orgs") or []) & set(identity.orgs)) or bool(teams & set(identity.teams))


def allowed(identity: Identity | None, machine: Machine, feature: str) -> bool:
    """Whether identity may use one of the machine's features: nobody outside its tenant; inside, its owner
    always, the tenant's admins the ADMIN_FEATURES, and the organization and teams the owner granted it.
    Any grant includes seeing the machine. No identity may use nothing."""
    if identity is None or not same_tenant(identity, machine.tenant):
        return False
    # Site admins may use every feature of every machine; the machine's own switches (launches on, ...) still apply.
    if owns(identity, machine) or identity.site_admin:
        return True
    if identity.admin and feature in ADMIN_FEATURES:
        return True
    if feature == "view":
        return any(granted(identity, machine, name) for name in FEATURES)
    return granted(identity, machine, feature)


def can_manage(identity: Identity | None, machine: Machine) -> bool:
    """Who sets a machine's grants: its owner, or an admin of its tenant (site admins: of every one)."""
    return owns(identity, machine) or bool(identity and identity.admin and same_tenant(identity, machine.tenant))


def runs_containers(identity: Identity | None, machine: Machine) -> bool:
    """Who may create, change and remove containers on a machine (with ADMIN_ONLY on top): `docker run` is root
    there, so its owner, and site admins, who may use everything on every machine."""
    return owns(identity, machine) or bool(identity and identity.site_admin)


def can_see(identity: Identity | None, machine: Machine) -> bool:
    return allowed(identity, machine, "view")


def can_launch(identity: Identity | None, machine: Machine) -> bool:
    return allowed(identity, machine, "launch")


def can_update(identity: Identity | None, machine: Machine) -> bool:
    return allowed(identity, machine, "update")


def checked_access(identity: Identity | None, requested: Any, store: Any, tenant: str | None = None) -> dict[str, dict[str, list[str]]]:
    """A machine's grants as asked for, checked: known features, and only the machine's own organization
    (tenant) and its teams (unless an admin asks, only teams the asker is in, as with sharing a session).
    Raises ValueError."""
    if identity is None:
        raise ValueError("Sign in first")
    tenant = tenant or identity.tenant
    if not isinstance(requested, dict):
        raise ValueError("Send the grants as {feature: {orgs: [...], teams: [...]}}")
    access: dict[str, dict[str, list[str]]] = {}
    for feature, grant in requested.items():
        if feature not in FEATURES:
            raise ValueError(f"No feature {feature}: one of {', '.join(FEATURES)}")
        grant = grant if isinstance(grant, dict) else {}
        orgs = sorted({str(item) for item in grant.get("orgs") or []})
        teams = sorted({str(item) for item in grant.get("teams") or []})
        for org in orgs:
            if org != tenant:
                raise ValueError(f"No organization {org}: a machine is only ever shared within its own organization ({tenant})")
        for team in teams:
            org, _, name = team.partition("/")
            known = store.org(org) if org == tenant else None
            if known is None or not any(item["id"] == name for item in known["teams"]):
                raise ValueError(f"No team {team} in {tenant} (name it as <organization>/<team>)")
            if not identity.admin and team not in identity.teams:
                raise ValueError(f"You aren't in the team {team}")
        if orgs or teams:
            access[feature] = {"orgs": orgs, "teams": teams}
    return access


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
    """The machines: those connected now, and the rest from the database (machines table).

    With several processes (cluster.py) a machine's connection is held by the one it reached. That one
    announces the machine's state to the others whenever it changes (fleet.machine), its Strata and MCP
    keys sealed with the internal token, so every process knows every machine. Requests to a machine
    (ask, launch, shell, tunnel) whose connection another process holds are made by that process, through
    its internal port (internal.py), and answered as if they'd been made here.
    """

    def __init__(self, db: Database, sessions: SessionStore | None = None, cluster: Cluster | None = None):
        self.db = db
        self.sessions = sessions
        self.cluster = cluster or LocalCluster()
        self.machines: dict[str, Machine] = {}
        self.sockets: dict[str, Any] = {}
        # Machines whose connection another process holds: id -> that process.
        self.owners: dict[str, str] = {}
        self._waiting: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # Which machine each waiting request went to, so its answer fails when the machine goes.
        self._asked: dict[str, str] = {}
        # Which connection each waiting request went out on: its answer can only come back on that one.
        self._via: dict[str, Any] = {}
        # Progress of streamed requests (pulls), by request id.
        self._progress: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        # Machines removed from the server, by id: refused when they connect until allowed back.
        self.removed: dict[str, dict[str, Any]] = {}
        self._load()
        self.cluster.on("fleet.machine", self._remote_state)
        self.cluster.on("fleet.removed", self._remote_removed)

    # Storage

    def _load(self) -> None:
        with self.db.read() as connection:
            machines = [row[0] for row in connection.execute(select(schema.machines.c.data))]
            removed = {key: value for key, value in connection.execute(select(schema.machines_removed.c.id, schema.machines_removed.c.data))}
        for item in machines:
            try:
                machine = Machine(**{key: value for key, value in item.items() if key in Machine.__dataclass_fields__})
            except TypeError:
                continue
            self.machines[machine.id] = machine
        self.removed = {str(key): value for key, value in removed.items() if isinstance(value, dict)}

    def _save(self, *machines: Machine) -> None:
        """Write these machines as they are now, behind the request (machines send updates every minute).
        Only the ones that changed: another process may hold newer copies of the rest."""
        rows = {machine.id: machine.stored() for machine in machines}

        def write(connection: Any) -> None:
            for key, data in rows.items():
                kv.upsert(connection, schema.machines, {"id": key, "data": data})

        if rows:
            self.db.writer.submit(write)

    def _save_removed(self, machine_id: str, record: dict[str, Any] | None) -> None:
        """A machine taken out (record) or allowed back (None)."""
        def write(connection: Any) -> None:
            if record is None:
                connection.execute(schema.machines_removed.delete().where(schema.machines_removed.c.id == machine_id))
            else:
                connection.execute(schema.machines.delete().where(schema.machines.c.id == machine_id))
                kv.upsert(connection, schema.machines_removed, {"id": machine_id, "data": dict(record)})

        self.db.writer.submit(write)

    # Several processes

    def _announce(self, machine: Machine) -> None:
        """Tell the other processes how this machine is now (its connection is here, or just went)."""
        if not self.cluster.scaled:
            return
        self.cluster.publish("fleet.machine", {
            "machine": machine.stored(), "connected": machine.id in self.sockets,
            "keys": self.cluster.seal({"keys": {str(port): key for port, key in machine.keys.items()}, "mcp_key": machine.mcp_key}),
        })

    def _remote_state(self, data: dict[str, Any], sender: str) -> None:
        """Another process said how a machine is: the copy here follows, in place (code here may hold it)."""
        stored = data.get("machine")
        if not isinstance(stored, dict) or not stored.get("id"):
            return
        machine_id = str(stored["id"])
        if machine_id in self.removed:
            return
        try:
            fresh = Machine(**{key: value for key, value in stored.items() if key in Machine.__dataclass_fields__})
        except TypeError:
            return
        sealed = self.cluster.unseal(data.get("keys")) or {}
        fresh.keys = {int(port): str(key) for port, key in (sealed.get("keys") or {}).items() if str(port).isdigit()}
        fresh.mcp_key = sealed.get("mcp_key") or None
        known = self.machines.get(machine_id)
        if known is None:
            self.machines[machine_id] = fresh
        else:
            for name in Machine.__dataclass_fields__:
                setattr(known, name, getattr(fresh, name))
        if data.get("connected"):
            self.owners[machine_id] = sender
            old = self.sockets.pop(machine_id, None)
            if old is not None:
                # It connected to another process since: what was asked over the old connection here won't be answered.
                self._fail_sent(old)
                asyncio.ensure_future(_close_quietly(old))
        elif self.owners.get(machine_id) == sender:
            self.owners.pop(machine_id, None)

    def _remote_removed(self, data: dict[str, Any], sender: str) -> None:
        machine_id = str(data.get("id") or "")
        record = data.get("record")
        if not machine_id:
            return
        if isinstance(record, dict):
            self.removed[machine_id] = record
            self.machines.pop(machine_id, None)
            self.owners.pop(machine_id, None)
            socket = self.sockets.pop(machine_id, None)
            if socket is not None:
                asyncio.ensure_future(_close_quietly(socket, 4403, "This machine was removed from the server"))
        else:
            self.removed.pop(machine_id, None)

    def owner_of(self, machine_id: str) -> str | None:
        """The other process holding this machine's connection, if one does and is still running."""
        pod = self.owners.get(machine_id)
        if pod and pod != self.cluster.pod_id and self.cluster.alive(pod):
            return pod
        return None

    async def _where(self, machine: Machine) -> str | None:
        """Where the machine's connection is: "" for here, another process's id, or None. One that just
        dropped gets a few seconds to come back, to this process or another."""
        deadline = time.monotonic() + RECONNECT_WAIT
        while True:
            if machine.id in self.sockets:
                return ""
            pod = self.owner_of(machine.id)
            if pod:
                return pod
            if not self.reconnecting(machine) or time.monotonic() >= deadline or machine.id not in self.machines:
                return None
            await asyncio.sleep(0.25)

    async def _remote(self, pod: str, machine: Machine, action: str, body: dict[str, Any], timeout: float | None) -> dict[str, Any]:
        """One request to the process holding the machine's connection; its FleetError comes back as one here."""
        try:
            response = await self.cluster.call(
                pod, "POST", f"/internal/fleet/{machine.id}/{action}", json_body=body, timeout=None if timeout is None else timeout + 15,
            )
        except ClusterError as exc:
            raise MachineGone(502, f"lost the connection to {machine.name} ({exc})") from exc
        try:
            answer = response.json()
        except ValueError:
            answer = {}
        if response.status_code >= 400:
            error = (answer.get("error") if isinstance(answer, dict) else None) or {}
            status = int(error.get("status") or response.status_code)
            detail = str(error.get("detail") or f"{machine.name}: {response.status_code}")
            raise (MachineGone if error.get("gone") else FleetError)(status, detail)
        return answer

    async def send_tunnel(self, machine: Machine, tunnel_id: str, port: int) -> None:
        """Ask the machine to dial back for a tunnel, over its connection wherever that is held."""
        where = await self._where(machine)
        if where is None:
            raise ConnectionError(f"{machine.name} isn't connected")
        if where:
            await self._remote(where, machine, "tunnel", {"tunnel": tunnel_id, "port": port}, 15)
            return
        socket = self.sockets.get(machine.id)
        if socket is None:
            raise ConnectionError(f"{machine.name} isn't connected")
        try:
            await socket.send_text(json.dumps({"type": "tunnel", "tunnel": tunnel_id, "port": port}))
        except Exception:
            # A connection that can't be written to is gone: the next try waits for the new one.
            self.disconnect(machine.id, socket)
            raise

    # Connections

    def connect(self, socket: Any, identity: Identity | None, client_host: str, hello: dict[str, Any]) -> Machine:
        machine_id = str(hello.get("id") or "").strip()
        if not machine_id or len(machine_id) > 64 or not machine_id.replace("-", "").isalnum():
            raise FleetError(4400, "A machine id is letters, numbers and '-'")
        if machine_id in self.removed:
            raise FleetError(4403, "This machine was removed from the server; its owner or an admin can allow it back on the Machines page")
        existing = self.machines.get(machine_id)
        # The route only connects signed-in machines; tests register ownerless ones directly.
        owner = identity.user if identity else None
        tenant = identity.tenant if identity else None
        if existing and existing.owner and owner and (existing.owner != owner or existing.tenant != tenant):
            # Another user's (or another tenant's) machine id: refuse rather than take the machine over.
            raise FleetError(4403, "That machine is registered to another user")
        machine = existing or Machine(id=machine_id, name=machine_id, first_seen=time.time(), tenant=tenant)
        machine.owner = owner or machine.owner
        machine.tenant = machine.tenant or tenant
        self._update(machine, hello, client_host)
        old = self.sockets.get(machine_id)
        self.sockets[machine_id] = socket
        self.owners.pop(machine_id, None)
        self.machines[machine_id] = machine
        self._save(machine)
        self._announce(machine)
        if old is not None and old is not socket:
            # The machine reconnected before the old connection was noticed as gone: what was asked over
            # it will never be answered (reads are asked again over this one).
            self._fail_sent(old)
            asyncio.ensure_future(_close_quietly(old))
        return machine

    def disconnect(self, machine_id: str, socket: Any) -> None:
        if socket is None:
            return
        self._fail_sent(socket)
        if self.sockets.get(machine_id) is socket:
            del self.sockets[machine_id]
            machine = self.machines.get(machine_id)
            if machine:
                # Its Strata and MCP keys stay while it's likely to be back (requests wait for it); its
                # next hello replaces them.
                machine.last_seen = time.time()
                self._save(machine)
                self._announce(machine)

    def _fail_sent(self, socket: Any) -> None:
        """Ends the requests waiting for an answer over a connection that's gone."""
        for request_id, via in list(self._via.items()):
            future = self._waiting.get(request_id)
            if via is socket and future and not future.done():
                future.set_exception(ConnectionError("the machine disconnected"))

    def message(self, machine: Machine, data: dict[str, Any], client_host: str) -> None:
        kind = data.get("type")
        if kind == "update":
            self._update(machine, data, client_host)
            self._save(machine)
            self._announce(machine)
        elif kind in {"launched", "launch_failed"}:
            launch_id = str(data.get("launch") or "")
            if self._asked.get(launch_id) != machine.id:
                return
            future = self._waiting.pop(launch_id, None)
            if future and not future.done():
                future.set_result(data)
        elif kind in {"container_result", "server_result", "provider_result", "folders_result", "files_result", "update_result"}:
            if kind == "server_result" and isinstance(data.get("servers"), dict):
                # Every answer carries the servers' state, refused ones too.
                machine.servers = _servers(data["servers"])
                self._announce(machine)
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
        if "commands" in data:
            machine.commands = bool(data.get("commands"))
        if "updates" in data:
            machine.updates = bool(data.get("updates"))
        if "files" in data:
            machine.files = bool(data.get("files"))
        if isinstance(data.get("protocol"), int):
            machine.protocol = data["protocol"]
        if "tunnel" in data:
            machine.tunnel = bool(data.get("tunnel"))
        if "launcher" in data:
            machine.launcher = bool(data.get("launcher"))
        if "mcp" in data:
            machine.mcp, machine.mcp_key = _mcp(data.get("mcp"))
        if "accounts" in data:
            machine.provider_clis = reported_clis(data.get("accounts"))
        if isinstance(data.get("roots"), list):
            machine.roots = [str(item)[:400] for item in data["roots"]][:20]
        machine.last_seen = time.time()

    def connected(self, machine_id: str) -> bool:
        return machine_id in self.sockets or self.owner_of(machine_id) is not None

    def reconnecting(self, machine: Machine) -> bool:
        """Whether a machine that isn't connected was a moment ago, so it's probably on its way back."""
        return not self.connected(machine.id) and time.time() - machine.last_seen <= RECONNECT_WINDOW

    async def wait_socket(self, machine: Machine) -> Any:
        """The machine's connection; one that just dropped gets a few seconds to come back first."""
        socket = self.sockets.get(machine.id)
        if socket is not None or not self.reconnecting(machine):
            return socket
        deadline = time.monotonic() + RECONNECT_WAIT
        while socket is None and time.monotonic() < deadline and machine.id in self.machines:
            await asyncio.sleep(0.25)
            socket = self.sockets.get(machine.id)
        return socket

    def runs_containers(self, machine: Machine) -> bool:
        return self.connected(machine.id) and bool(machine.containers.get("enabled"))

    def engine(self, machine: Machine, engine_id: str) -> dict[str, Any] | None:
        return next((engine for engine in machine.containers.get("engines") or [] if engine["id"] == engine_id), None)

    async def ask(
        self, machine: Machine, op: str, timeout: float | None = 30.0, channel: str = "container",
        progress: asyncio.Queue[dict[str, Any]] | None = None, **fields: Any,
    ) -> dict[str, Any]:
        """One request to the machine and its answer (see _ask); a read cut off by the machine dropping is asked again."""
        try:
            return await self._routed(machine, op, timeout, channel, progress, fields)
        except MachineGone:
            if (channel, op) not in READ_ONLY:
                raise
            return await self._routed(machine, op, timeout, channel, progress, fields)

    async def _routed(self, machine: Machine, op: str, timeout: float | None, channel: str, progress: Any, fields: dict[str, Any]) -> dict[str, Any]:
        """_ask here, or through the process that holds the machine's connection."""
        if machine.id in self.sockets or not self.cluster.scaled:
            return await self._ask(machine, op, timeout, channel, progress, **fields)
        where = await self._where(machine)
        if not where:
            return await self._ask(machine, op, timeout, channel, progress, **fields)
        body = {"op": op, "timeout": timeout, "channel": channel, "fields": fields}
        if progress is None:
            return (await self._remote(where, machine, "ask", body, timeout))["answer"]
        return await self._remote_with_progress(where, machine, body, progress)

    async def _remote_with_progress(self, pod: str, machine: Machine, body: dict[str, Any], progress: Any) -> dict[str, Any]:
        """A streamed request (a pull) through another process: its progress lines, then the answer."""
        try:
            async with self.cluster.stream(pod, "POST", f"/internal/fleet/{machine.id}/ask?progress=1", json_body=body) as response:
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if "progress" in item:
                        progress.put_nowait(item["progress"])
                    elif "answer" in item:
                        return item["answer"]
                    elif "error" in item:
                        error = item["error"] or {}
                        raise (MachineGone if error.get("gone") else FleetError)(int(error.get("status") or 502), str(error.get("detail") or "failed"))
        except FleetError:
            raise
        except Exception as exc:  # noqa: BLE001 - the other process or the network went
            raise MachineGone(502, f"lost the connection to {machine.name} ({exc})") from exc
        raise MachineGone(502, f"lost the connection to {machine.name}")

    async def _ask(
        self, machine: Machine, op: str, timeout: float | None = 30.0, channel: str = "container",
        progress: asyncio.Queue[dict[str, Any]] | None = None, **fields: Any,
    ) -> dict[str, Any]:
        """One request to the machine, and its answer; FleetError when it can't be done.

        channel is "container" (containers.py), "server" (starting Ollama and Strata and
        Ollama's settings) or "provider" (a model server's models, status and logs, the
        machine's load, model catalogues) or "folders" (the folders inside its launch folders, for
        choosing where to start an agent or a shell) or "files" (the file viewer: listing and
        reading the files inside its launch folders). Containers and servers are off on the machine
        until it allows them; on the provider channel the machine itself refuses changes
        unless its servers are on. progress, for pulls, receives the machine's progress
        messages until the answer comes. timeout None waits for as long as the machine
        stays connected.
        """
        socket = await self.wait_socket(machine)
        if socket is None:
            raise FleetError(409, f"{machine.name} isn't connected: agent-machine isn't running there, or isn't signed in here (agent-machine login, then agent-machine start).")
        if channel == "container" and not machine.containers.get("enabled"):
            raise FleetError(409, f"{machine.name} doesn't run containers. Run `agent-machine containers on` there to allow it.")
        if channel == "server" and not machine.servers.get("enabled"):
            raise FleetError(409, f"{machine.name} doesn't let the server manage its model servers. Run `agent-machine servers on` there to allow it.")
        if channel == "update" and not machine.updates:
            raise FleetError(409, f"{machine.name} doesn't take updates from here. Run `agent-machine updates on` there to allow it.")
        if channel == "folders" and not (machine.launches or machine.commands):
            raise FleetError(409, f"{machine.name} doesn't take launches. Run `agent-machine launches on` there to allow it.")
        if channel == "files" and not machine.files:
            raise FleetError(409, f"{machine.name} doesn't share its files. Run `agent-machine files on` there to allow it.")
        request_id = secrets.token_hex(6)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = future
        self._asked[request_id] = machine.id
        self._via[request_id] = socket
        if progress is not None:
            self._progress[request_id] = progress
        try:
            try:
                await socket.send_text(json.dumps({"type": channel, "request": request_id, "op": op, **fields}))
            except Exception:
                # A connection that can't be written to is gone, whether or not its reader noticed yet.
                self.disconnect(machine.id, socket)
                raise
            answer = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise FleetError(504, f"{machine.name} didn't answer within {int(timeout or 0)} s") from exc
        except Exception as exc:  # noqa: BLE001 - the machine went away mid-request
            raise MachineGone(502, f"lost the connection to {machine.name} ({exc})") from exc
        finally:
            self._waiting.pop(request_id, None)
            self._asked.pop(request_id, None)
            self._via.pop(request_id, None)
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

    def machine_for(self, tags: dict[str, Any]) -> Machine | None:
        """The machine a session runs on, from its tags.

        A launch from the console names the machine (agent-machine sets it). An agent started
        by hand only knows its computer's name: that's the machine of the same owner with that
        hostname (or, from an agent-machine too old to report it, that name). Tags come from
        the client, so a session only joins a machine of its own owner.
        """
        owner = tags.get("owner")
        tenant = tags.get("tenant")

        def ours(machine: Machine) -> bool:
            return machine.tenant == tenant and (not owner or not machine.owner or machine.owner == owner)

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

    async def remove(self, machine_id: str, identity: Identity | None) -> None:
        """Takes a machine out of the fleet, connected or not, and refuses it when it connects again until
        it's allowed back (restore): what it ran for the server stops reaching agents."""
        machine = self.machines.pop(machine_id, None)
        if machine is None:
            raise FleetError(404, "No such machine")
        record = {"name": machine.name, "owner": machine.owner, "tenant": machine.tenant, "at": time.time(), "by": identity.user if identity else None}
        self.removed[machine_id] = record
        self.owners.pop(machine_id, None)
        socket = self.sockets.pop(machine_id, None)
        self._save_removed(machine_id, record)
        # The process holding its connection (if another) closes it, and every one refuses it from now on.
        self.cluster.publish("fleet.removed", {"id": machine_id, "record": record})
        if socket is not None:
            await _close_quietly(socket, 4403, "This machine was removed from the server")

    def restore(self, machine_id: str) -> None:
        """Lets a removed machine join again; it comes back as new, with its owner the one it signs in as."""
        if self.removed.pop(machine_id, None) is None:
            raise FleetError(404, "That machine wasn't removed")
        self._save_removed(machine_id, None)
        self.cluster.publish("fleet.removed", {"id": machine_id, "record": None})

    def removed_for(self, identity: Identity | None) -> list[dict[str, Any]]:
        """The removed machines someone may allow back: of their own tenant, their own, and every one for its admins."""
        return [
            {"id": machine_id, **record} for machine_id, record in sorted(self.removed.items(), key=lambda item: -float(item[1].get("at") or 0))
            if identity is not None and same_tenant(identity, record.get("tenant")) and (identity.admin or record.get("owner") == identity.user)
        ]

    # Which tenant's agents may use a backend

    SHARED = "*"

    def backend_tenant(self, backend: Any) -> str | None:
        """The tenant a backend belongs to: its machine's (nobody's once the machine is removed); the root's
        for a model server from before machines; SHARED for the anthropic pass-through, which site admins set
        up for everyone (agents bring their own credentials to it). A hosted API provider carries a key, so
        it's its owner's (api_providers.owner), shared further only with the organizations it names (api_tenants)."""
        if backend.machine:
            machine = self.machines.get(backend.machine)
            return machine.tenant if machine else None
        if api_providers.external(backend):
            return api_providers.owner(backend)
        return ROOT_TENANT if backend.type in MACHINE_TYPES else self.SHARED

    def api_tenants(self, backend: Any) -> set[str]:
        """The organizations whose agents may use an api backend: its owner's, and for a hosted one also
        those its orgs names ("*" for every one)."""
        tenant = self.backend_tenant(backend)
        tenants = {tenant} if tenant else set()
        if api_providers.external(backend):
            tenants |= set(api_providers.options(backend)["orgs"])
        return tenants

    def usable_by(self, identity: Identity | None) -> Any:
        """A test of backends for identity's agents: their tenant's and the shared hosted ones; an api
        backend only for the teams it names, in its own organization. No identity, none."""
        def usable(backend: Any) -> bool:
            if identity is None:
                return False
            if backend.type == "api":
                if identity.site_admin:
                    return True
                tenants = self.api_tenants(backend)
                if identity.tenant == self.backend_tenant(backend):
                    # Its own organization's admins always may: they manage it, and test what agents get.
                    return identity.admin or api_providers.for_teams(backend, identity.teams)
                return self.SHARED in tenants or identity.tenant in tenants
            tenant = self.backend_tenant(backend)
            return tenant == self.SHARED or same_tenant(identity, tenant)
        return usable

    # What the console sees

    def public(self, machine: Machine, identity: Identity | None, backends: dict[str, Any]) -> dict[str, Any]:
        """The machine for the console; each provider names the backend that already uses it, if any."""
        by_target: dict[tuple[str, str], str] = {}
        for name, backend in backends.items():
            # A container can share a model server's address; the first backend is the server's own.
            if backend.machine == machine.id:
                by_target.setdefault((backend.type, provider_address(backend.type, backend.url)), name)
        providers = []
        for provider in machine.providers:
            url = self.provider_url(machine, provider)
            providers.append({**provider, "url": url, "backend": by_target.get((provider["type"], provider_address(provider["type"], url)))})
        return {
            **{key: value for key, value in machine.stored().items() if key != "history"},
            "connected": self.connected(machine.id),
            "providers": providers,
            # The backends it supplies: model servers added from it and containers it runs.
            "backends": sorted(name for name, backend in backends.items() if backend.machine == machine.id),
            "history": [record for record in machine.history if self._visible_launch(record, identity)][-10:],
            # What the console may offer this user here: each feature, and managing the machine itself.
            "you": {"owner": owns(identity, machine), "manage": can_manage(identity, machine), "containers": runs_containers(identity, machine), **{feature: allowed(identity, machine, feature) for feature in FEATURES}},
            # Who else may use what: only for those who set it.
            "access": machine.access if can_manage(identity, machine) else {},
            # A machine on another build than the server's updates itself from the server's downloads.
            "update": {"available": bool(machine.version) and machine.version != version.current(), "server_version": version.current(), "command": "agent-machine update"},
        }

    @staticmethod
    def provider_url(machine: Machine, provider: dict[str, Any]) -> str:
        host = machine.address or "127.0.0.1"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{int(provider['port'])}"

    def _visible_launch(self, record: dict[str, Any], identity: Identity | None) -> bool:
        if identity is None:
            return False
        session_id = record.get("session_id")
        if session_id:
            session = self.sessions.get(str(session_id)) if self.sessions else None
            return session is not None and access.can_view(identity, session)
        return record.get("by") == identity.user

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

    async def launch(self, machine: Machine, identity: Identity | None, request: dict[str, Any], launch_id: str | None = None) -> dict[str, Any]:
        """Starts an agent there. request["account"] is a provider account the route already checked for
        whoever asked (provider_accounts.for_launch), with launch_id the grant it made for it."""
        if not can_launch(identity, machine):
            raise FleetError(403, "Only the machine's owner, or the teams it lets, can start agents on it.")
        where = await self._where(machine) if self.cluster.scaled else ""
        if where:
            body = {"user": identity.user if identity else None, "request": request, "launch": launch_id}
            return await self._remote(where, machine, "launch", body, LAUNCH_TIMEOUT)
        socket = await self.wait_socket(machine)
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
        account = str(request.get("account") or "")
        if account and machine.provider_clis is None:
            raise FleetError(409, f"{machine.name}'s agent-machine is too old to start agents with a provider account: run agent-machine update there.")
        launch_id = launch_id or secrets.token_hex(6)
        message = {
            "type": "launch", "launch": launch_id, "agent": agent, "project": project,
            "model": str(request.get("model") or ""), "backend": str(request.get("backend") or ""), "trace": bool(request.get("trace")),
            "visibility": str(request.get("visibility") or "private"), "team": str(request.get("team") or request.get("org") or ""),
            "access": "control" if request.get("access") == "control" else "view",
            # Shared MCP servers for the agent, as checked by the route (mcp.choose).
            "mcp": [str(item) for item in request.get("mcp") or []],
        }
        if account:
            # The provider account the agent runs with (agent --account): its id, never a credential.
            message["account"] = account
        return await self._start(machine, socket, identity, message, agent, project)

    async def shell(self, machine: Machine, identity: Identity | None, request: dict[str, Any]) -> dict[str, Any]:
        """A shell on the machine, in a terminal only its owner sees: a remote terminal, so it's off until
        the machine allows it (agent-machine commands on), and the machine checks that again."""
        if not allowed(identity, machine, "terminal"):
            raise FleetError(403, "Only the machine's owner, or the teams it lets, can open a terminal on it.")
        where = await self._where(machine) if self.cluster.scaled else ""
        if where:
            return await self._remote(where, machine, "shell", {"user": identity.user if identity else None, "request": request}, LAUNCH_TIMEOUT)
        socket = await self.wait_socket(machine)
        if socket is None:
            raise FleetError(409, f"{machine.name} isn't connected: agent-machine isn't running there, or isn't signed in here (agent-machine login, then agent-machine start).")
        if not machine.commands:
            raise FleetError(409, f"{machine.name} doesn't take commands. Run `agent-machine commands on` there to open terminals on it from here.")
        project = str(request.get("project") or "").strip() or (machine.roots[0] if machine.roots else "")
        if not within_roots(project, machine.roots):
            raise FleetError(400, "Choose a folder inside one of the machine's launch folders (agent-machine roots add <dir>).")
        launch_id = secrets.token_hex(6)
        message = {"type": "shell", "launch": launch_id, "project": project}
        return await self._start(machine, socket, identity, message, SHELL_CLIENT, project)

    # The grant another process can check while a launch runs (the session POST may land
    # on a gateway pod while the fleet pod holds the launch).
    GRANT_PREFIX = "fleet:launch-grant:"

    def pending_launch(self, launch_id: str) -> bool:
        """Whether a launch some process sent to a machine is still running: what makes a
        session's launch tag believable (the id is server-generated and secret). This
        process's own launches are known directly; others' through the cluster's grant."""
        if not launch_id:
            return False
        return launch_id in self._asked or bool(self.cluster.get(self.GRANT_PREFIX + launch_id))

    async def _start(self, machine: Machine, socket: Any, identity: Identity | None, message: dict[str, Any], agent: str, project: str) -> dict[str, Any]:
        """Sends a launch or shell to the machine and waits for the session it opens, recording it in the machine's history."""
        launch_id = message["launch"]
        record = {"launch": launch_id, "at": time.time(), "by": identity.user if identity else None, "agent": agent, "project": project, "session_id": None, "error": None}
        machine.history = [*machine.history, record][-LAUNCH_HISTORY:]
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiting[launch_id] = future
        self._asked[launch_id] = machine.id
        self._via[launch_id] = socket
        try:
            # The grant other processes check (pending_launch) while this launch runs.
            # Best effort: in-process launches are known through _asked either way, and a
            # cluster blip must not fail the launch itself.
            try:
                await asyncio.to_thread(self.cluster.set, self.GRANT_PREFIX + launch_id, machine.id, LAUNCH_TIMEOUT + 60)
            except Exception:  # noqa: BLE001
                # Only a prefix: a live grant id in the log would let a reader skip the launch-limit check.
                LOGGER.warning("couldn't write the launch grant for %s…; other processes will re-check the limit", launch_id[:4])
            await socket.send_text(json.dumps(message))
            answer = await asyncio.wait_for(future, LAUNCH_TIMEOUT)
        except asyncio.TimeoutError:
            answer = {"type": "launch_failed", "error": f"{machine.name} didn't open a session within {int(LAUNCH_TIMEOUT)} s"}
        except Exception as exc:  # noqa: BLE001 - the machine went away mid-launch
            answer = {"type": "launch_failed", "error": f"lost the connection to {machine.name} ({exc})"}
        finally:
            self._waiting.pop(launch_id, None)
            self._asked.pop(launch_id, None)
            self._via.pop(launch_id, None)
            try:
                # Best effort: the grant's TTL cleans it up anyway, and a cluster blip
                # here must not turn a successful launch into an error.
                await asyncio.to_thread(self.cluster.delete, self.GRANT_PREFIX + launch_id)
            except Exception:  # noqa: BLE001
                LOGGER.warning("couldn't delete the launch grant for %s…; its TTL will", launch_id[:4])
        if answer.get("type") == "launched" and answer.get("session_id"):
            session = self.sessions.get(str(answer["session_id"])) if self.sessions else None
            if (
                session is None or session.client != agent
                or session.tags.get("owner") != machine.owner
                or session.tags.get("tenant") != machine.tenant
                or session.tags.get("machine") != machine.id
                or session.tags.get("launch") != launch_id
                or str(session.tags.get("provider_account") or "") != str(message.get("account") or "")
            ):
                record["error"] = "The machine returned a session that doesn't belong to this launch"
            else:
                record["session_id"] = session.session_id
                if identity is not None and session.tags.get("owner") != identity.user:
                    self.sessions.update_tags(session, {"launched_by": identity.user})
        else:
            record["error"] = str(answer.get("error") or "the launch failed")
        self._save(machine)
        self._announce(machine)
        if record["error"]:
            raise FleetError(502, record["error"])
        return record


def _provider(item: dict[str, Any]) -> dict[str, Any]:
    port = int(item.get("port") or 0)
    if not 0 < port < 65536:
        port = {"ollama": 11434, "docker": 12434, "strata": 18500, "llamacpp": 9931, "api": 1234}[item["type"]]
    models = item.get("models")
    provider = {"type": item["type"], "port": port, "version": str(item.get("version") or "")[:40], "models": models if isinstance(models, int) else None}
    if item["type"] == "api":
        provider["flavor"] = item.get("flavor") if item.get("flavor") in API_FLAVORS else "openai"
    return provider


def _mcp(item: Any) -> tuple[dict[str, Any], str | None]:
    """A machine's hello "mcp" as kept: its bridge's port and the servers by name, and the bridge's key apart."""
    if not isinstance(item, dict):
        return {}, None
    port = item.get("port")
    if not isinstance(port, int) or not 0 < port < 65536:
        return {}, None
    servers = []
    for server in item.get("servers") or []:
        if isinstance(server, dict) and isinstance(server.get("name"), str) and MCP_NAME.fullmatch(server["name"]):
            servers.append({
                "name": server["name"], "description": str(server.get("description") or "")[:200],
                "transport": server.get("transport") if server.get("transport") in MCP_TRANSPORTS else "stdio",
                "found": bool(server.get("found", True)),
            })
    key = item.get("key")
    return {"port": port, "servers": servers[:50]}, (str(key)[:200] if isinstance(key, str) and key else None)


def _servers(item: dict[str, Any]) -> dict[str, Any]:
    """What a machine says about the Ollama, llama.cpp and Strata it can start, kept to known fields."""
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
                # The installed model it runs, and why it can't start when its run config has gone.
                "model": str(profile.get("model") or "")[:120] or None, "broken": str(profile.get("broken") or "")[:300] or None,
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
        "llamacpp": _llamacpp(item.get("llamacpp")),
    }


def _text(value: Any, size: int = 80) -> str | None:
    """A machine's field as text of at most size, or None when it's empty."""
    return str(value or "")[:size] or None


def _llamacpp(item: Any) -> dict[str, Any]:
    """The llama.cpp a machine can start (and install), kept to known fields; an older machine says nothing."""
    if not isinstance(item, dict):
        return {"found": False, "running": False, "managed": False, "known": False}
    install = item.get("install") if isinstance(item.get("install"), dict) else None
    return {
        "known": True, "found": bool(item.get("found")), "running": bool(item.get("running")), "managed": bool(item.get("managed")),
        "port": item.get("port") if isinstance(item.get("port"), int) else 9931, "version": _text(item.get("version"), 40),
        # Where it came from: the console's install (github or termux, which build), or found on the machine.
        "source": _text(item.get("source"), 20), "variant": _text(item.get("variant"), 40), "release": _text(item.get("release"), 40),
        "platform": _text(item.get("platform"), 40),
        "install": {
            "state": _text(install.get("state"), 20), "variant": _text(install.get("variant"), 40), "release": _text(install.get("release"), 40),
            "step": _text(install.get("step"), 200), "done": install.get("done") if isinstance(install.get("done"), int) else 0,
            "total": install.get("total") if isinstance(install.get("total"), int) else 0, "error": _text(install.get("error"), 500), "at": _text(install.get("at"), 40),
        } if install else None,
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
    """What a provider request names: the kind and port, and a Strata or llama.cpp container's key, which only the server knows."""
    fields: dict[str, Any] = {"kind": kind, "port": port}
    if kind == "strata" and backend is not None and backend.container:
        key, _ = strata.key_for(backend)
        if key:
            fields["key"] = key
    if kind == "llamacpp" and backend is not None and backend.container and strata.managed_keys.get(backend.name):
        fields["key"] = strata.managed_keys[backend.name]
    return fields


def provider_address(kind: str, url: str) -> str:
    """A machine's model server as compared with a backend: its normal URL, without an api backend's /v1 path."""
    if kind == "api":
        parts = urlsplit(url)
        return normal_url(f"{parts.scheme}://{parts.netloc}")
    return normal_url(url)


def normal_url(url: str) -> str:
    """For comparing model server addresses: localhost is 127.0.0.1, no trailing slash."""
    return url.rstrip("/").replace("://localhost:", "://127.0.0.1:").replace("://localhost/", "://127.0.0.1/")


async def _close_quietly(socket: Any, code: int = 4409, reason: str = "Replaced by a newer connection") -> None:
    try:
        await socket.close(code=code, reason=reason)
    except Exception:  # noqa: BLE001 - already closed
        pass
