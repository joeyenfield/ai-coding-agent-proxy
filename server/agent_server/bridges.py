"""Bridges: an organization here and an organization on a linked server letting each other use chosen resources.

A link (links.py) joins two servers. A bridge joins one organization on each, over a link, and
says what crosses: providers, MCP servers, agent definitions, and functions of machines (launching
agents there, terminals). Both organizations' admins open it, each entering the other's one-use
code (msb_...) and confirming the other organization's id, so a leaked code can't pair the wrong one.

Four gates. An item crosses only if the giving server's link policy lets its kind out, the
receiving server's lets it in, the giving organization lets the item out (and, for anything on a
machine, the machine's owner granted the bridge: a bridge:<id> grantee in its access), and the
receiving organization lets it in, for the teams it names. Each side signs only its own policy
(its out and in lists) with its link key (links.sign_policy); what crosses is the overlap of both
sides' current policies (crossing). Adding something is a new version of your own policy, which
crosses once the other side's also has it; taking something away is a new version too, applied at
once. Both digests travel in every request's assertion, so a request made under an older version
is refused (409) and the sender fetches the new one.

Who is who on the far side. Someone of the other server acting here is a Guest: its own type, not
an auth.Identity, so every existing permission helper refuses it, and no ordinary route can serve
one. Guests reach this server only through the link routes (routes/links.py), which ask one
check: permits (what this side lets out to that bridge). A guest is named by the other server's
per-user id (user_uuid), never the user name, so a recreated account inherits nothing.

What travels. A provider let in here appears as a backend of type link under its full name
(routing.py), owned by the receiving organization only (never SHARED, never picked by backend_for
on its own); compat sends its requests to the giving server, which records them in a bridge
session (tenant: the giving organization, tags: bridge, link, guest) and forwards them to its own
provider. MCP servers work the same way. A launch on the other server's machine is that server's
session, run by the machine owner's agent-proxy as console launches are, tagged with the guest,
the bridge and the backends it may use; its records are mirrored back into a mirror session of the
person who launched it. Keys never leave their server: requests are carried, credentials aren't.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from . import routing
from .auth import Identity
from .config import Backend
from .db import kv, schema
from .links import CODE_LIFETIME, KINDS, PEER_PREFIX, Link, LinkDown, Peer, Refused, canonical, digest

LOGGER = logging.getLogger("agent-proxy")

BRIDGE_CODE_MAX = 86400.0
ASSERTION_LIFETIME = 60.0
REFRESH_SECONDS = 60.0
MIRROR_SECONDS = 3.0
MIRROR_BATCH = 200
# Machine-function kinds: launch (start agents there, browse its launch folders), terminal (type into them).
MACHINE_KINDS = ("launch", "terminal")
# Backend types an organization may let out: its own providers (never the anthropic pass-through, which
# is nobody's, nor a provider it received over a bridge: one hop only).
GIVABLE_TYPES = {"ollama", "strata", "docker", "llamacpp", "api"}
NAMESPACE = uuid.UUID("6f1b3c1e-7a51-4e3f-9d56-0b3f6a4c1d20")
# What a bridged agent definition may set in the environment: the endpoints and model it's pointed at,
# never something that changes what runs (PATH, LD_PRELOAD, NODE_OPTIONS, ...).
SAFE_ENV_SUFFIXES = ("_BASE_URL", "_API_BASE", "_BASE", "_MODEL", "_API_KEY", "_HOST", "_URL", "_TIMEOUT", "_CONTEXT_WINDOW")
SAFE_ENV_PREFIXES = ("OPENAI_", "ANTHROPIC_", "OLLAMA_", "AI_PROXY_", "LLM_", "GOOSE_", "AIDER_", "QWEN_", "OPENCODE_", "CODEX_", "COPILOT_")
UNSAFE_ENV = {"PATH", "HOME", "SHELL", "ENV", "BASH_ENV", "NODE_OPTIONS", "PYTHONPATH", "PYTHONSTARTUP", "PROMPT_COMMAND", "PERL5OPT", "RUBYOPT", "GIT_SSH_COMMAND", "EDITOR", "VISUAL", "PAGER"}


def user_uuid(name: str, created_at: Any) -> str:
    """A person's id towards linked servers: a new account of the same name is someone else."""
    return str(uuid.uuid5(NAMESPACE, f"{name}|{created_at!r}"))


@dataclass(frozen=True)
class Guest:
    """Someone of a linked server, acting here over a bridge. Not an Identity: the permission helpers refuse it."""

    user: str
    name: str
    org: str
    server: str
    link: str
    bridge: str
    teams: tuple[str, ...] = field(default_factory=tuple)

    @property
    def label(self) -> str:
        return f"{self.name}@{self.org} ({self.server})"

    def tags(self) -> dict[str, Any]:
        return {"guest": self.user, "guest_name": self.label, "bridge": self.bridge, "link": self.link}

    def public(self) -> dict[str, Any]:
        return {"user": self.user, "name": self.name, "org": self.org, "server": self.server, "teams": list(self.teams), "label": self.label}


@dataclass
class Bridge:
    id: str
    link: str
    org: str
    peer_org: str
    state: str
    data: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def complete(self) -> bool:
        return bool(self.data.get("we_confirmed") and self.data.get("they_confirmed"))


def bridge_id(link: str, one: tuple[str, str], other: tuple[str, str]) -> str:
    """The link and both (server, organization) pairs, sorted: the same on each side."""
    return "brg_" + hashlib.sha256(canonical([link, sorted([list(one), list(other)])])).hexdigest()[:20]


def item_key(item: dict[str, Any]) -> tuple[str, str, str, str]:
    return (str(item.get("kind") or ""), str(item.get("machine") or ""), str(item.get("name") or ""), str(item.get("scope") or ""))


def _match(giver: dict[str, Any], taker: dict[str, Any]) -> bool:
    if item_key(giver) != item_key(taker):
        return False
    # An agent definition is code: what's let in can pin the version (digest), and an edit then doesn't cross until taken.
    return not (giver.get("kind") == "agent" and taker.get("digest") and taker.get("digest") != giver.get("digest"))


def overlap(out: list[dict[str, Any]], into: list[dict[str, Any]], giver_link: dict[str, Any], taker_link: dict[str, Any]) -> list[dict[str, Any]]:
    """What one side lets out and the other lets in, as far as both link policies let those kinds through."""
    found = []
    for given in out:
        kind = given.get("kind")
        if kind not in (giver_link.get("out") or []) or kind not in (taker_link.get("in") or []):
            continue
        for taken in into:
            if _match(given, taken):
                found.append({**given, "teams": list(taken.get("teams") or [])})
                break
    return found


def safe_definition(definition: Any) -> str | None:
    """Why an agent definition can't cross a bridge, or None. It's code the other side runs: no files, and only
    environment variables that point it somewhere."""
    if not isinstance(definition, dict):
        return "isn't a definition"
    if definition.get("files") or (definition.get("mcp") or {}).get("files"):
        return "writes files"
    if definition.get("intercept"):
        return "is an account agent (those don't cross yet)"
    for name in list((definition.get("env") or {}).keys()) + list(((definition.get("mcp") or {}).get("env") or {}).keys()):
        upper = str(name).upper()
        if upper in UNSAFE_ENV or upper.startswith(("LD_", "DYLD_")) or not (upper.startswith(SAFE_ENV_PREFIXES) or upper.endswith(SAFE_ENV_SUFFIXES)):
            return f"sets {name}, which a bridged definition may not"
    return None


class Bridges:
    def __init__(self, runtime: Any):
        self.runtime = runtime
        self.links = runtime.links
        self.db = runtime.db
        self.bridges: dict[str, Bridge] = {}
        self._tasks: list[asyncio.Task[Any]] = []
        self._mirrored: dict[str, int] = {}
        self.reload()
        self.links.listeners.append(self._changed)

    # Stored bridges

    def reload(self) -> None:
        with self.db.read() as connection:
            rows = connection.execute(select(schema.bridges)).mappings().all()
        self.bridges = {row["id"]: Bridge(row["id"], row["link"], row["org"], row["peer_org"], row["state"], dict(row["data"] or {}), row["created_at"] or 0.0, row["updated_at"] or 0.0) for row in rows}
        self.rebuild()

    def _save(self, bridge: Bridge) -> None:
        bridge.updated_at = time.time()
        values = {"id": bridge.id, "link": bridge.link, "org": bridge.org, "peer_org": bridge.peer_org, "state": bridge.state, "data": bridge.data, "created_at": bridge.created_at, "updated_at": bridge.updated_at}
        with self.db.write() as connection:
            kv.upsert(connection, schema.bridges, values)
        self.bridges[bridge.id] = bridge
        self.links.changed("bridge", bridge.id)

    def _changed(self, scope: str, scope_id: str) -> None:
        if scope in ("bridge", "link", "socket"):
            if scope != "socket":
                with self.db.read() as connection:
                    rows = connection.execute(select(schema.bridges)).mappings().all()
                self.bridges = {row["id"]: Bridge(row["id"], row["link"], row["org"], row["peer_org"], row["state"], dict(row["data"] or {}), row["created_at"] or 0.0, row["updated_at"] or 0.0) for row in rows}
            self.rebuild()
            self._close_withdrawn()
        if scope == "socket":
            link = self.links.links.get(scope_id)
            if link is not None and self.links.connected(link.id):
                self.soon(self.refresh_link(link))
        if scope == "link" and scope_id not in self.links.links:
            # The link went: so did every bridge over it.
            for bridge in [item for item in self.bridges.values() if item.link == scope_id]:
                self._end(bridge)

    def soon(self, work: Any) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            work.close()
            return
        task = asyncio.ensure_future(work)
        self._tasks.append(task)

        def done(finished: asyncio.Task[Any]) -> None:
            if finished in self._tasks:
                self._tasks.remove(finished)
            if not finished.cancelled() and finished.exception() is not None:
                LOGGER.warning("peering work failed: %r", finished.exception(), exc_info=finished.exception())

        task.add_done_callback(done)

    def link_of(self, bridge: Bridge) -> Link | None:
        return self.links.links.get(bridge.link)

    def for_org(self, org: str) -> list[Bridge]:
        return sorted((bridge for bridge in self.bridges.values() if bridge.org == org), key=lambda item: (item.link, item.peer_org))

    def find(self, server: str, org: str, peer_org: str) -> Bridge | None:
        """The active bridge between an organization here and one on the server with this mesh id."""
        for bridge in self.bridges.values():
            link = self.link_of(bridge)
            if link is not None and link.peer == server and bridge.org == org and bridge.peer_org == peer_org and bridge.state == "active" and link.state == "active":
                return bridge
        return None

    def public(self, bridge: Bridge, viewer: Identity | None = None) -> dict[str, Any]:
        link = self.link_of(bridge)
        crossing = self.crossing(bridge)
        local, peer = self.policy(bridge, "local"), self.policy(bridge, "peer")
        return {
            "id": bridge.id, "link": bridge.link, "server": link.peer if link else None, "org": bridge.org, "peer_org": bridge.peer_org,
            "state": bridge.state, "we_confirmed": bool(bridge.data.get("we_confirmed")), "they_confirmed": bool(bridge.data.get("they_confirmed")),
            "link_state": link.state if link else "removed", "connected": bool(link and self.links.connected(link.id)),
            "policy": {"out": local.get("out") or [], "in": local.get("in") or [], "version": local.get("version")},
            "peer_policy": {"out": peer.get("out") or [], "in": peer.get("in") or [], "version": peer.get("version")},
            "crossing": crossing, "catalogue": bridge.data.get("catalogue") or {}, "refreshed_at": bridge.data.get("refreshed_at"),
            "link_policy": {"local": self.links.link_policy(link, "local") if link else {}, "peer": self.links.link_policy(link, "peer") if link else {}},
            "created_at": bridge.created_at, "updated_at": bridge.updated_at,
        }

    # Setting a bridge up

    def make_code(self, org: str, link: Link, by: str | None, lifetime: float = CODE_LIFETIME) -> tuple[str, float]:
        if not self.links.org_allowed(self.links.link_policy(link, "local"), org):
            raise ValueError("This server's link policy doesn't let your organization bridge over that link: ask a site admin")
        return self.links.make_code("bridge", by, org=org, link=link.id, lifetime=min(max(60.0, lifetime), BRIDGE_CODE_MAX))

    async def inspect(self, org: str, link: Link, code: str) -> dict[str, Any]:
        """Which organization a code the other side gave is for, so the admin can confirm it's the one they expected."""
        response = await self.links.post_json(link, PEER_PREFIX + "bridge/inspect", {"code": code, "org": org})
        if response.status_code >= 400:
            raise ValueError(_detail(response, "The other server doesn't know that code"))
        return response.json()

    async def join(self, org: str, link: Link, code: str, peer_org: str, by: str | None) -> Bridge:
        if not self.links.org_allowed(self.links.link_policy(link, "local"), org):
            raise ValueError("This server's link policy doesn't let your organization bridge over that link")
        response = await self.links.post_json(link, PEER_PREFIX + "bridge/present", {"code": code, "org": org, "peer_org": peer_org})
        if response.status_code >= 400:
            raise ValueError(_detail(response, "The other server refused the code"))
        answer = response.json()
        if answer.get("org") != peer_org:
            raise ValueError("That code is another organization's")
        bridge = self._upsert(link, org, peer_org)
        bridge.data["we_confirmed"] = True
        bridge.data["joined_by"] = by
        self._activate(bridge)
        self._save(bridge)
        self.soon(self.refresh(bridge))
        return bridge

    def present_inspect(self, peer: Peer, body: dict[str, Any]) -> dict[str, Any]:
        row = self.links.find_code(str(body.get("code") or ""), "bridge")
        if row is None or row["link"] != peer.link.id:
            raise Refused(403, "The code is wrong, used or expired")
        org = self.runtime.auth.store.org(row["org"]) or {}
        return {"org": row["org"], "name": org.get("name") or row["org"], "server": self.links.mesh_id}

    def presented(self, peer: Peer, body: dict[str, Any]) -> dict[str, Any]:
        """The other side entered a code of ours: the bridge, confirmed from their side."""
        peer_org = str(body.get("org") or "")
        if not routing.PART.fullmatch(peer_org):
            raise Refused(400, "Name your organization")
        row = self.links.find_code(str(body.get("code") or ""), "bridge")
        if row is None or row["link"] != peer.link.id or (body.get("peer_org") and body["peer_org"] != row["org"]):
            raise Refused(403, "The code is wrong, used or expired")
        if not self.links.org_allowed(self.links.link_policy(peer.link, "local"), row["org"]):
            raise Refused(403, "This server's link policy no longer lets that organization bridge")
        if self.links.spend_code(str(body.get("code")), "bridge") is None:
            raise Refused(403, "The code is wrong, used or expired")
        bridge = self._upsert(peer.link, row["org"], peer_org)
        bridge.data["they_confirmed"] = True
        self._activate(bridge)
        self._save(bridge)
        self._audit("bridge.presented", bridge, actor=f"{peer_org}@{peer.link.peer}")
        return {"bridge": bridge.id, "org": bridge.org}

    def _upsert(self, link: Link, org: str, peer_org: str) -> Bridge:
        me = self.links.me()
        key = bridge_id(link.id, (me, org), (link.peer, peer_org))
        bridge = self.bridges.get(key) or Bridge(key, link.id, org, peer_org, "pending", {}, time.time())
        return bridge

    @staticmethod
    def _activate(bridge: Bridge) -> None:
        if bridge.complete and bridge.state == "pending":
            bridge.state = "active"

    def set_state(self, bridge: Bridge, state: str) -> None:
        bridge.state = state
        self._save(bridge)
        link = self.link_of(bridge)
        if link is not None:
            self.soon(self.links.notify(link, "bridge", bridge.id))

    def end(self, bridge: Bridge) -> None:
        link = self.link_of(bridge)
        self._end(bridge)
        if link is not None:
            self.soon(self.links.notify(link, "bridge", bridge.id))

    def _end(self, bridge: Bridge) -> None:
        """A bridge ended: it's gone from both sides' lists; what each side recorded stays under its retention."""
        self.bridges.pop(bridge.id, None)
        with self.db.write() as connection:
            connection.execute(schema.bridges.delete().where(schema.bridges.c.id == bridge.id))
            connection.execute(schema.peer_policies.delete().where((schema.peer_policies.c.scope == "bridge") & (schema.peer_policies.c.scope_id == bridge.id)))
        # Launches on this side's machines over it stop: their sessions end.
        for session in self.runtime.sessions.list():
            if session.tags.get("bridge") == bridge.id and session.ended_at is None and session.tags.get("guest"):
                self.runtime.sessions.end(session)
        self.rebuild()
        self.links.changed("bridge", bridge.id)

    # Policies

    def policy(self, bridge: Bridge, side: str) -> dict[str, Any]:
        envelope = self.links.policy("bridge", bridge.id, side)
        return envelope["document"] if envelope else {"out": [], "in": []}

    def digests(self, bridge: Bridge) -> dict[str, str | None]:
        local = self.links.policy("bridge", bridge.id, "local")
        peer = self.links.policy("bridge", bridge.id, "peer")
        return {"local": local["digest"] if local else None, "peer": peer["digest"] if peer else None}

    def crossing(self, bridge: Bridge) -> dict[str, list[dict[str, Any]]]:
        """What crosses: out (to the other organization) and in (from it). Nothing unless both link policies let the
        organizations bridge, and the bridge and its link are active."""
        link = self.link_of(bridge)
        if link is None or link.state != "active" or bridge.state != "active":
            return {"out": [], "in": []}
        local_link, peer_link = self.links.link_policy(link, "local"), self.links.link_policy(link, "peer")
        if not self.links.org_allowed(local_link, bridge.org) or not self.links.org_allowed(peer_link, bridge.peer_org):
            return {"out": [], "in": []}
        local, peer = self.policy(bridge, "local"), self.policy(bridge, "peer")
        return {
            "out": overlap(local.get("out") or [], peer.get("in") or [], local_link, peer_link),
            "in": overlap(peer.get("out") or [], local.get("in") or [], peer_link, local_link),
        }

    async def set_policy(self, bridge: Bridge, out: Any, into: Any, identity: Identity) -> dict[str, Any]:
        """This organization's side: what it lets out (checked against what it has, and the machines' grants) and in."""
        checked_out = [self._checked_out(bridge, item) for item in _items(out)]
        checked_in = [self._checked_in(bridge, item) for item in _items(into)]
        envelope = self.links.sign_policy("bridge", bridge.id, {"type": "bridge", "bridge": bridge.id, "link": bridge.link, "org": bridge.org, "peer_org": bridge.peer_org, "out": checked_out, "in": checked_in})
        self._audit("bridge.policy", bridge, actor=identity.user, changes={"version": envelope["document"]["version"], "out": len(checked_out), "in": len(checked_in)})
        self.rebuild()
        self._close_withdrawn()
        link = self.link_of(bridge)
        if link is not None:
            await self.links.notify(link, "bridge", bridge.id)
        return envelope["document"]

    def _checked_out(self, bridge: Bridge, item: dict[str, Any]) -> dict[str, Any]:
        kind = item["kind"]
        scope = "home" if item.get("scope") == "home" else None
        if kind == "provider":
            backend = self.runtime.settings.backends.get(str(item.get("name") or ""))
            if backend is None or backend.type not in GIVABLE_TYPES or self.runtime.fleet.backend_tenant(backend) != bridge.org:
                raise ValueError(f"{item.get('name')} isn't one of your organization's providers")
            return _clean({"kind": kind, "name": backend.name, "scope": scope})
        if kind == "agent":
            agents = self.runtime.agents_of(bridge.org)
            agent = agents.get(str(item.get("name") or ""))
            if agent is None or agent.org != bridge.org:
                raise ValueError(f"{item.get('name')} isn't one of your organization's agent definitions (the server's own never cross)")
            definition = self.runtime.agent_definition(agent.id)
            why = safe_definition(definition)
            if why:
                raise ValueError(f"{agent.id} can't cross: it {why}")
            return {"kind": kind, "name": agent.id, "digest": digest(definition)}
        machine = self._machine(bridge.org, str(item.get("machine") or ""))
        if machine is None or not routing.PART.fullmatch(machine.name):
            raise ValueError(f"No machine {item.get('machine')} in your organization (its name must be letters, digits, '.', '_' or '-')")
        feature = {"mcp": "mcp", "launch": "launch", "terminal": "terminal"}[kind]
        if bridge.id not in ((machine.access.get(feature) or {}).get("bridges") or []):
            raise ValueError(f"{machine.name}'s owner hasn't let this bridge use its {feature}: they grant it on the machine's Access tab")
        if kind == "mcp":
            name = str(item.get("name") or "")
            if not any(server["name"] == name for server in (machine.mcp or {}).get("servers") or []):
                raise ValueError(f"{machine.name} shares no MCP server called {name}")
            return _clean({"kind": kind, "machine": machine.name, "name": name, "scope": scope})
        return {"kind": kind, "machine": machine.name}

    def _checked_in(self, bridge: Bridge, item: dict[str, Any]) -> dict[str, Any]:
        teams = sorted({str(team) for team in item.get("teams") or []})
        org = self.runtime.auth.store.org(bridge.org) or {"teams": []}
        for team in teams:
            owner, _, name = team.partition("/")
            if owner != bridge.org or not any(known["id"] == name for known in org.get("teams") or []):
                raise ValueError(f"No team {team} in {bridge.org}")
        kept = {key: item.get(key) for key in ("kind", "machine", "name", "digest", "scope") if item.get(key)}
        if kept.get("scope") not in (None, "home"):
            kept.pop("scope", None)
        if item["kind"] in ("provider", "agent") and not kept.get("name") or item["kind"] in ("launch", "terminal", "mcp") and not kept.get("machine"):
            raise ValueError("Name what you let in")
        return {**kept, "teams": teams}

    def _machine(self, org: str, name: str) -> Any:
        found = [machine for machine in self.runtime.fleet.machines.values() if machine.tenant == org and machine.name == name]
        if len(found) > 1:
            raise ValueError(f"Your organization has two machines called {name}: rename one first")
        return found[0] if found else None

    def accept_peer_policy(self, peer: Peer, body: dict[str, Any]) -> bool:
        scope = body.get("scope")
        envelope = body.get("policy")
        if scope == "link":
            return self.links.accept_policy(peer.link, "link", peer.link.id, envelope)
        bridge = self.bridges.get(str(body.get("id") or ""))
        if bridge is None or bridge.link != peer.link.id:
            raise Refused(404, "No such bridge")
        document = (envelope or {}).get("document") or {}
        if document.get("bridge") != bridge.id or document.get("org") != bridge.peer_org or document.get("peer_org") != bridge.org:
            raise Refused(403, "That policy isn't this bridge's")
        accepted = self.links.accept_policy(peer.link, "bridge", bridge.id, envelope)
        if accepted:
            self._audit("bridge.peer_policy", bridge, actor=f"{bridge.peer_org}@{peer.link.peer}", changes={"version": document.get("version")})
        return accepted

    def policies_for(self, link: Link) -> dict[str, Any]:
        """This side's current policies on a link, for the other side to fetch."""
        bridges = {bridge.id: self.links.policy("bridge", bridge.id, "local") for bridge in self.bridges.values() if bridge.link == link.id}
        return {"link": self.links.policy("link", link.id, "local"), "bridges": {key: value for key, value in bridges.items() if value}}

    async def refresh_link(self, link: Link) -> None:
        """Fetch the other side's policies and catalogues for every bridge on a link."""
        try:
            data = await self.links.get_json(link, PEER_PREFIX + "policies")
        except (LinkDown, ValueError) as exc:
            LOGGER.info("couldn't fetch %s's policies: %s", link.peer, exc)
            return
        newer = False
        try:
            if data.get("link"):
                newer = self.links.accept_policy(link, "link", link.id, data["link"]) or newer
            for key, envelope in (data.get("bridges") or {}).items():
                bridge = self.bridges.get(key)
                if bridge is not None and bridge.link == link.id:
                    document = (envelope or {}).get("document") or {}
                    if document.get("org") == bridge.peer_org and document.get("peer_org") == bridge.org:
                        newer = self.links.accept_policy(link, "bridge", key, envelope) or newer
        except Refused as exc:
            LOGGER.warning("%s sent a policy that doesn't check out: %s", link.peer, exc.detail)
        for bridge in [item for item in self.bridges.values() if item.link == link.id and item.state == "active"]:
            await self._catalogue(bridge, link)
        if newer:
            # What crosses to the other side may have changed with its new policy: it fetches this side's
            # catalogue again. It finds no newer policy of ours, so this ends there.
            await self.links.notify(link, "bridge", "*")

    async def refresh(self, bridge: Bridge) -> None:
        link = self.link_of(bridge)
        if link is not None and link.state == "active":
            await self.refresh_link(link)

    async def _catalogue(self, bridge: Bridge, link: Link) -> None:
        try:
            catalogue = await self.links.get_json(link, PEER_PREFIX + f"catalogue/{bridge.id}")
        except (LinkDown, ValueError) as exc:
            LOGGER.info("couldn't fetch the catalogue of %s: %s", bridge.id, exc)
            return
        if isinstance(catalogue, dict):
            bridge.data["catalogue"] = catalogue
            bridge.data["refreshed_at"] = time.time()
            self._save(bridge)

    def catalogue_for(self, bridge: Bridge) -> dict[str, Any]:
        """What crosses to the other organization, described: what its people see and choose from."""
        runtime = self.runtime
        found: dict[str, list[dict[str, Any]]] = {"providers": [], "mcp": [], "agents": [], "machines": []}
        for item in self.crossing(bridge)["out"]:
            kind = item["kind"]
            if kind == "provider":
                backend = runtime.settings.backends.get(item["name"])
                if backend is None:
                    continue
                from . import api_providers

                options = api_providers.options(backend) if backend.type == "api" else {}
                models = list(options.get("models") or {}) if backend.type == "api" else []
                catalog = runtime.api_discovery.get(backend.name)
                models += [model for model in catalog.models if model not in models] if catalog else []
                found["providers"].append({
                    "name": backend.name, "type": backend.type, "scope": item.get("scope"),
                    "protocols": list(options.get("protocols") or []) if backend.type == "api" else [],
                    "models": models[:500], "connected": not backend.machine or runtime.fleet.connected(backend.machine),
                })
            elif kind == "agent":
                definition = runtime.agent_definition(item["name"])
                if definition is not None and digest(definition) == item.get("digest"):
                    found["agents"].append({"name": item["name"], "digest": item["digest"], "definition": definition})
            else:
                machine = self._machine_quiet(bridge.org, item["machine"])
                if machine is None:
                    continue
                entry = next((value for value in found["machines"] if value["name"] == machine.name), None)
                if kind == "mcp":
                    server = next((server for server in (machine.mcp or {}).get("servers") or [] if server["name"] == item["name"]), None)
                    if server is not None:
                        found["mcp"].append({"machine": machine.name, "name": item["name"], "description": server.get("description"), "scope": item.get("scope"), "connected": runtime.fleet.connected(machine.id)})
                    continue
                if entry is None:
                    entry = {"name": machine.name, "connected": runtime.fleet.connected(machine.id), "os": machine.os, "functions": [], "agents": [], "roots": list(machine.roots)}
                    found["machines"].append(entry)
                entry["functions"] = sorted({*entry["functions"], kind})
                if kind == "launch":
                    entry["agents"] = sorted(agent for agent in machine.agents if agent in runtime.agents_of(bridge.org))
                    entry["launches"] = bool(machine.launches)
        # What a guest's launch here may use: the providers this side lets out, as the launching side names them.
        return {"server": self.links.mesh_id, "org": bridge.org, **found, "at": time.time()}

    def _machine_quiet(self, org: str, name: str) -> Any:
        try:
            return self._machine(org, name)
        except ValueError:
            return None

    # The receiving side: what its people may use

    def lets_in(self, identity: Any, bridge: Bridge, kind: str, name: str | None = None, machine: str | None = None, scope: str | None = None) -> dict[str, Any] | None:
        """The item of what crosses in that this person may use (their organization's bridge, their teams), else None."""
        if not isinstance(identity, Identity) or identity.tenant != bridge.org:
            return None
        for item in self.crossing(bridge)["in"]:
            if item.get("kind") != kind or (name is not None and item.get("name") != name) or (machine is not None and item.get("machine") != machine):
                continue
            if (item.get("scope") or None) != scope:
                continue
            teams = item.get("teams") or []
            if teams and not identity.admin and not set(teams) & set(identity.teams):
                continue
            return item
        return None

    def remote_for(self, identity: Any) -> dict[str, Any]:
        """Everything that crosses in for this person, by kind, under full names."""
        found: dict[str, list[dict[str, Any]]] = {"providers": [], "mcp": [], "agents": [], "machines": []}
        if not isinstance(identity, Identity):
            return found
        for bridge in self.for_org(identity.tenant):
            link = self.link_of(bridge)
            catalogue = bridge.data.get("catalogue") or {}
            if link is None or bridge.state != "active":
                continue
            for provider in catalogue.get("providers") or []:
                if provider.get("scope") == "home":
                    continue
                if self.lets_in(identity, bridge, "provider", provider["name"]):
                    found["providers"].append({**provider, "id": routing.full("provider", link.peer, bridge.peer_org, provider["name"]), "bridge": bridge.id})
            for server in catalogue.get("mcp") or []:
                if server.get("scope") != "home" and self.lets_in(identity, bridge, "mcp", server["name"], server["machine"]):
                    found["mcp"].append({**server, "id": routing.full("mcp", link.peer, bridge.peer_org, server["name"], server["machine"]), "bridge": bridge.id})
            for agent in catalogue.get("agents") or []:
                if self.lets_in(identity, bridge, "agent", agent["name"]):
                    found["agents"].append({"id": routing.full("agent", link.peer, bridge.peer_org, agent["name"]), "name": agent["name"], "digest": agent["digest"], "bridge": bridge.id})
            for machine in catalogue.get("machines") or []:
                functions = [kind for kind in machine.get("functions") or [] if self.lets_in(identity, bridge, kind, machine=machine["name"])]
                if functions:
                    found["machines"].append({**machine, "functions": functions, "id": routing.full("machine", link.peer, bridge.peer_org, machine["name"]), "bridge": bridge.id})
        return found

    def rebuild(self) -> None:
        """The providers let in, as backends of type link (Settings.remote_backends), each its organization's own."""
        backends: dict[str, Backend] = {}
        for bridge in self.bridges.values():
            link = self.link_of(bridge)
            if link is None or bridge.state != "active":
                continue
            catalogue = bridge.data.get("catalogue") or {}
            received = {item_key(item): item for item in self.crossing(bridge)["in"] if item.get("kind") == "provider"}
            for provider in catalogue.get("providers") or []:
                item = received.get(("provider", "", str(provider.get("name") or ""), str(provider.get("scope") or "")))
                if item is None:
                    continue
                name = routing.full("provider", link.peer, bridge.peer_org, provider["name"])
                backends[name] = Backend(name=name, url=link.url, type="link", options={
                    "link": link.id, "bridge": bridge.id, "org": bridge.org, "server": link.peer, "remote_org": bridge.peer_org,
                    "remote_name": provider["name"], "remote_type": provider.get("type") or "api", "protocols": provider.get("protocols") or [],
                    "models": provider.get("models") or [], "teams": item.get("teams") or [], "scope": provider.get("scope"),
                })
        self.runtime.settings.remote_backends = backends

    def usable(self, identity: Any, backend: Any) -> bool:
        """A provider let in: only its organization's people (the teams it was let in for), never another tenant's,
        and one let in for a guest's launch (scope home) only through the launch's own allowed backends."""
        options = backend.options or {}
        if not isinstance(identity, Identity) or identity.tenant != options.get("org") or options.get("scope") == "home":
            return False
        bridge = self.bridges.get(str(options.get("bridge") or ""))
        if bridge is None or bridge.state != "active":
            return False
        teams = options.get("teams") or []
        return not teams or identity.admin or bool(set(teams) & set(identity.teams))

    # Sending a person's request across

    # What of an agent's request goes to the giving server: what the provider needs to read it, nothing that
    # authenticates anyone (the giving server adds its provider's own key).
    LINK_HEADERS = ("content-type", "accept", "anthropic-version", "anthropic-beta", "openai-beta", "user-agent", "mcp-session-id", "mcp-protocol-version", "last-event-id")

    def caller(self, request: Any, session: Any, bridge: Bridge, kind: str, name: str, machine: str | None, scope: str | None) -> tuple[dict[str, Any], str | None]:
        """Who a request across is for, and the session it's part of. A guest's launch here calling its own
        organization's provider at home (scope home) is that guest, with the mirror session it has there."""
        from fastapi import HTTPException

        if scope == "home":
            if session is None or session.tags.get("bridge") != bridge.id or not session.tags.get("guest"):
                raise HTTPException(status_code=403, detail="Only a launch its own people started here may use that")
            return {"user": session.tags["guest"], "name": str(session.tags.get("guest_name") or "").split("@", 1)[0], "org": bridge.peer_org}, session.tags.get("remote_session")
        from . import auth

        user = (session.tags.get("launched_by") or session.tags.get("owner")) if session is not None else None
        identity = self.runtime.auth.identity_for(str(user), "session") if user and ":" not in str(user) else auth.request_identity(request)
        if self.lets_in(identity, bridge, kind, name, machine) is None:
            raise HTTPException(status_code=403, detail="Your organization no longer lets that in for you")
        return self.who(identity), session.session_id if session is not None else None

    def upstream(self, request: Any, backend: Any, agent_path: str, body: bytes, session: Any) -> tuple[Any, str, dict[str, str]]:
        """A request to a provider let in over a bridge: to the server that gives it, signed, with an assertion."""
        options = backend.options or {}
        bridge = self.bridges.get(str(options.get("bridge") or ""))
        link = self.link_of(bridge) if bridge else None
        if bridge is None or link is None or bridge.state != "active":
            raise LinkDown(f"The bridge for {backend.name} isn't open")
        scope = options.get("scope")
        who, remote_session = self.caller(request, session, bridge, "provider", str(options.get("remote_name")), None, scope)
        resource = {"kind": "provider", "org": bridge.peer_org, "name": options.get("remote_name")}
        assertion = self.assertion(bridge, who, resource, "models", remote_session, scope)
        target = f"{PEER_PREFIX}provider/{bridge.peer_org}/{options.get('remote_name')}{agent_path}"
        headers = {key: value for key, value in request.headers.items() if key.lower() in self.LINK_HEADERS}
        return self.links.prepare(link, request.method, target, body, assertion, headers)

    def remote_mcp(self, full: str) -> tuple[Bridge, Link, routing.Name] | None:
        try:
            name = routing.parse(full, "mcp")
        except ValueError:
            return None
        for bridge in self.bridges.values():
            link = self.link_of(bridge)
            if link is not None and link.peer == name.server and bridge.peer_org == name.org and bridge.state == "active":
                return bridge, link, name
        return None

    def mcp_upstream(self, request: Any, full: str, body: bytes, session: Any) -> tuple[Any, str, dict[str, str]]:
        """An agent's MCP request to a server another organization shares over a bridge."""
        from fastapi import HTTPException

        found = self.remote_mcp(full)
        if found is None:
            raise HTTPException(status_code=404, detail=f"{full} isn't shared with you")
        bridge, link, name = found
        who, remote_session = self.caller(request, session, bridge, "mcp", name.name, name.machine, None)
        resource = {"kind": "mcp", "org": name.org, "machine": name.machine, "name": name.name}
        assertion = self.assertion(bridge, who, resource, "mcp", remote_session)
        target = f"{PEER_PREFIX}mcp/{name.org}/{name.machine}/{name.name}"
        headers = {key: value for key, value in request.headers.items() if key.lower() in self.LINK_HEADERS}
        return self.links.prepare(link, request.method, target, body, assertion, headers)

    # Assertions: who asks, for what, under which policies

    def assertion(self, bridge: Bridge, who: dict[str, Any], resource: dict[str, Any], function: str, session_id: str | None = None, scope: str | None = None) -> dict[str, Any]:
        link = self.link_of(bridge)
        if link is None:
            raise LinkDown("The link is gone")
        now = time.time()
        digests = self.digests(bridge)
        return {
            "link": link.id, "bridge": bridge.id, "from": self.links.me(), "to": link.peer,
            "user": who.get("user"), "name": who.get("name"), "org": who.get("org") or bridge.org, "teams": list(who.get("teams") or []),
            "session": session_id, "resource": resource, "function": function, "scope": scope,
            # The sender's own policy and the receiver's, as the sender last saw them.
            "policies": {"sender": digests["local"], "receiver": digests["peer"]},
            "iat": now, "exp": now + ASSERTION_LIFETIME, "jti": secrets.token_urlsafe(12),
        }

    def who(self, identity: Identity) -> dict[str, Any]:
        record = self.runtime.auth.store.user(identity.user) or {}
        return {"user": user_uuid(identity.user, record.get("created_at")), "name": identity.user, "org": identity.tenant, "teams": list(identity.teams)}

    def checked(self, peer: Peer, org: str, kind: str, *, name: str | None = None, machine: str | None = None) -> tuple[Bridge, Guest, dict[str, Any]]:
        """A peer's request for something of this organization's: the bridge, the guest, and the item that lets it
        through. Raises Refused: 403 not permitted, 409 stale policies (with the current digests)."""
        assertion = peer.assertion
        if not assertion:
            raise Refused(401, "No assertion")
        bridge = self.bridges.get(str(assertion.get("bridge") or ""))
        scope = "home" if assertion.get("scope") == "home" else None
        # Someone of the other organization; or, calling home from a launch on the other server, one of ours.
        expected = (bridge.org if scope else bridge.peer_org) if bridge else None
        if bridge is None or bridge.link != peer.link.id or bridge.org != org or assertion.get("org") != expected:
            self._refused(peer, org, "no bridge")
            raise Refused(403, "There's no bridge for that")
        if bridge.state != "active":
            raise Refused(403, "The bridge is paused" if bridge.state == "paused" else "The bridge isn't open yet")
        digests = self.digests(bridge)
        policies = assertion.get("policies") or {}
        if policies.get("sender") != digests["peer"] or policies.get("receiver") != digests["local"]:
            raise Refused(409, "Policy versions don't match: fetch them and try again", {"digests": {"yours": digests["peer"], "ours": digests["local"]}})
        resource = assertion.get("resource") or {}
        if resource.get("kind") != kind or resource.get("org") != org or (name is not None and resource.get("name") != name) or (machine is not None and resource.get("machine") != machine):
            raise Refused(403, "The assertion is for something else")
        for item in self.crossing(bridge)["out"]:
            if item.get("kind") != kind or (name is not None and item.get("name") != name) or (machine is not None and item.get("machine") != machine):
                continue
            if kind in ("provider", "mcp") and (item.get("scope") or None) != scope:
                continue
            guest = Guest(
                user=str(assertion.get("user") or ""), name=str(assertion.get("name") or "")[:40], org=str(assertion.get("org")), server=peer.link.peer,
                link=peer.link.id, bridge=bridge.id, teams=tuple(str(team) for team in assertion.get("teams") or [])[:50],
            )
            if not guest.user:
                break
            return bridge, guest, item
        self._refused(peer, org, f"{kind} {machine + '/' if machine else ''}{name or ''} not let out")
        raise Refused(403, "That isn't shared over this bridge")

    def _refused(self, peer: Peer, org: str, reason: str) -> None:
        self.runtime.audit.record(self.runtime.audit.event(
            "bridge.refused", tenant=org, actor=f"{(peer.assertion or {}).get('name')}@{peer.link.peer}", outcome="denied", reason=reason,
            action="peer request", target={"link": peer.link.id, "bridge": (peer.assertion or {}).get("bridge")},
        ))

    def _audit(self, kind: str, bridge: Bridge, actor: str | None, changes: dict[str, Any] | None = None) -> None:
        self.runtime.audit.record(self.runtime.audit.event(kind, tenant=bridge.org, actor=actor, target={"bridge": bridge.id, "link": bridge.link, "peer_org": bridge.peer_org}, changes=changes))

    # The giving side: guests' sessions here

    def bridge_session(self, bridge: Bridge, guest: Guest, remote_session: str | None, backend: str, model: str | None) -> Any:
        """The session a guest's calls are recorded in here: one per remote session, the giving organization's."""
        key = hashlib.sha256(canonical([bridge.id, guest.user, remote_session or ""])).hexdigest()[:32]
        session_id = f"bridge-{key}"
        session = self.runtime.sessions.get(session_id)
        if session is not None:
            return session
        tags = {
            **guest.tags(), "tenant": bridge.org, "owner": None, "visibility": "private", "access": "view",
            # Whose usage it is: the bridge's (usage_limits counts it against limits set for bridge:<id>).
            "launched_by": f"bridge:{bridge.id}", "remote_session": remote_session, "peering": "bridge",
        }
        try:
            return self.runtime.sessions.create({"session_id": session_id, "client": "bridge", "model": model, "backend": backend, "trace": True, "tags": tags})
        except ValueError:
            return self.runtime.sessions.get(session_id)

    def limit_missing(self, bridge: Bridge, provider: str) -> bool:
        """A bridge has no usage until the giving organization sets it a limit (so a forgotten bridge can't spend a budget)."""
        usage = self.runtime.usage
        return not any(limit.user == f"bridge:{bridge.id}" for limit in usage.applicable(bridge.org, f"bridge:{bridge.id}", provider))

    def can_view(self, guest: Any, session: Any) -> bool:
        """A guest sees only the sessions it launched here, over the bridge it asks through."""
        return isinstance(guest, Guest) and session is not None and session.tags.get("guest") == guest.user and session.tags.get("bridge") == guest.bridge

    # Guests launching on this side's machines

    def launch_backends(self, bridge: Bridge) -> list[str]:
        """What a guest's launch over this bridge may use: this side's providers let out to it, and the other side's
        providers let in for it (its own people's, scope home, or the organization's)."""
        allowed = [item["name"] for item in self.crossing(bridge)["out"] if item.get("kind") == "provider"]
        link = self.link_of(bridge)
        for name, backend in self.runtime.settings.remote_backends.items():
            if (backend.options or {}).get("bridge") == bridge.id:
                allowed.append(name)
        return sorted(set(allowed))

    def launch_mcp(self, bridge: Bridge) -> list[str]:
        return [f"{item['machine']}/{item['name']}" for item in self.crossing(bridge)["out"] if item.get("kind") == "mcp" and not item.get("scope")]

    def launch_mcp_allowed(self, session: Any, machine: Any, name: str) -> bool:
        """A guest's launch here may still use this machine's MCP server: the bridge still lets it out."""
        bridge = self.bridges.get(str(session.tags.get("bridge") or ""))
        return bool(
            bridge is not None and session.tags.get("guest") and machine.tenant == bridge.org
            and f"{machine.name}/{name}" in self.launch_mcp(bridge)
            and bridge.id in ((machine.access.get("mcp") or {}).get("bridges") or [])
        )

    # Mirror sessions: launches on the other server, followed from here

    def mirror_session(self, identity: Identity, bridge: Bridge, machine: str, agent: str, project: str, backend: str | None, model: str | None) -> Any:
        link = self.link_of(bridge)
        tags = {
            "tenant": identity.tenant, "owner": identity.user, "visibility": "private", "access": "view", "peering": "mirror",
            "mirror_link": bridge.link, "mirror_bridge": bridge.id, "mirror_server": link.peer if link else None,
            "mirror_machine": machine, "project_path": project,
        }
        return self.runtime.sessions.create({"client": agent, "project": project.replace("\\", "/").rsplit("/", 1)[-1] or project, "model": model, "backend": backend or "remote", "trace": True, "tags": tags})

    async def mirror_loop(self) -> None:
        """Sends the records of guests' launches here to the server they launched from, oldest first. What hasn't
        gone yet stays in this server's own records, so a link that drops loses nothing: they go when it's back."""
        while True:
            await asyncio.sleep(MIRROR_SECONDS)
            try:
                await self.mirror_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - try again next round
                LOGGER.exception("mirroring launches failed")

    async def mirror_once(self) -> None:
        sessions = [session for session in self.runtime.sessions.list() if session.tags.get("guest") and session.tags.get("remote_session") and session.tags.get("peering") == "launch"]
        for session in sessions:
            bridge = self.bridges.get(str(session.tags.get("bridge") or ""))
            link = self.link_of(bridge) if bridge else None
            if bridge is None or link is None or link.state != "active":
                continue
            sent = self._mirrored.get(session.session_id)
            if sent is None:
                with self.db.read() as connection:
                    sent = int(kv.get_meta(connection, f"mirror:{session.session_id}", 0) or 0)
            records = await asyncio.to_thread(self.runtime.sessions.records, session)
            ended = session.ended_at
            if len(records) <= sent and self._mirrored.get(session.session_id + ":ended") == ended:
                continue
            batch = records[sent:sent + MIRROR_BATCH]
            traces = await asyncio.to_thread(self.runtime.sessions.read_traces, session, [str(record.get("request_id")) for record in batch]) if batch else {}
            body = {"session": session.tags["remote_session"], "source": session.session_id, "records": batch, "traces": traces, "ended_at": ended}
            guest = {"user": session.tags.get("guest"), "name": session.tags.get("guest_name"), "org": bridge.org}
            assertion = self.assertion(bridge, guest, {"kind": "session", "org": bridge.peer_org, "name": session.tags["remote_session"]}, "mirror", session.session_id)
            try:
                response = await self.links.post_json(link, PEER_PREFIX + "mirror", json.loads(json.dumps(body, default=str)), assertion)
            except LinkDown:
                continue
            if response.status_code < 400:
                sent += len(batch)
                self._mirrored[session.session_id] = sent
                self._mirrored[session.session_id + ":ended"] = ended
                with self.db.write() as connection:
                    kv.put_meta(connection, f"mirror:{session.session_id}", sent)
            elif response.status_code == 409:
                self.soon(self.refresh(bridge))

    def mirrored(self, peer: Peer, body: dict[str, Any]) -> int:
        """Records of a launch on the other server, written into its mirror session here."""
        session = self.runtime.sessions.get(str(body.get("session") or ""))
        bridge = self.bridges.get(str((peer.assertion or {}).get("bridge") or ""))
        if session is None or bridge is None or session.tags.get("mirror_bridge") != bridge.id or session.tags.get("mirror_link") != peer.link.id:
            raise Refused(404, "No such mirror session")
        source = str(body.get("source") or "")
        if session.tags.get("mirror_of") not in (None, source):
            raise Refused(403, "That's another launch's mirror")
        if session.tags.get("mirror_of") is None:
            self.runtime.sessions.update_tags(session, {"mirror_of": source})
        traces = body.get("traces") if isinstance(body.get("traces"), dict) else {}
        written = 0
        for record in body.get("records") or []:
            if not isinstance(record, dict) or not record.get("request_id"):
                continue
            record = {**record, "session_id": session.session_id, "mirrored_from": source}
            self.runtime.sessions.append_request(session, record)
            trace = traces.get(str(record["request_id"]))
            if isinstance(trace, dict):
                self.runtime.sessions.write_trace(session, str(record["request_id"]), trace)
            written += 1
        if written:
            numbers = [int(record["request_id"]) for record in body.get("records") or [] if isinstance(record, dict) and str(record.get("request_id") or "").isdigit()]
            if numbers:
                self.runtime.sessions.raise_counts(session, max(numbers))
            self.runtime.sessions.touch(session)
        if body.get("ended_at") and session.ended_at is None:
            self.runtime.sessions.end(session)
        return written

    # Narrowing and offboarding

    def _close_withdrawn(self) -> None:
        """Guests' launches whose launch is no longer let out end, at once."""
        for session in self.runtime.sessions.list():
            if session.ended_at is not None or not session.tags.get("guest") or session.tags.get("peering") != "launch":
                continue
            bridge = self.bridges.get(str(session.tags.get("bridge") or ""))
            machine = self.runtime.fleet.machines.get(str(session.tags.get("machine") or ""))
            if bridge is None or bridge.state == "removed" or machine is None:
                continue
            if bridge.state == "active" and not any(item.get("kind") == "launch" and item.get("machine") == machine.name for item in self.crossing(bridge)["out"]):
                self.runtime.terminals.drop_viewers(session.session_id, lambda viewer: not isinstance(viewer, Guest))

    async def offboard(self, name: str, created_at: Any, tenant: str) -> None:
        """Someone was removed here: tell every server their organization bridges to, which ends their guests' work."""
        user = user_uuid(name, created_at)
        for bridge in self.for_org(tenant):
            link = self.link_of(bridge)
            if link is None or link.state != "active":
                continue
            assertion = self.assertion(bridge, {"user": user, "name": name, "org": tenant}, {"kind": "user", "org": bridge.peer_org, "name": user}, "offboard")
            try:
                await self.links.post_json(link, PEER_PREFIX + "offboard", {"user": user}, assertion)
            except LinkDown as exc:
                LOGGER.warning("couldn't tell %s that %s was removed: %s", link.peer, name, exc)

    def offboarded(self, peer: Peer, body: dict[str, Any]) -> int:
        user = str(body.get("user") or "")
        if user != (peer.assertion or {}).get("user"):
            raise Refused(403, "That's someone else")
        ended = 0
        for session in self.runtime.sessions.list():
            if session.tags.get("guest") == user and session.tags.get("link") == peer.link.id and session.ended_at is None:
                self.runtime.sessions.end(session)
                self.runtime.terminals.drop_viewers(session.session_id, lambda viewer: not isinstance(viewer, Guest))
                ended += 1
        return ended

    def start(self) -> None:
        self._tasks.append(asyncio.ensure_future(self._refresher()))
        self._tasks.append(asyncio.ensure_future(self.mirror_loop()))

    async def _refresher(self) -> None:
        while True:
            for link in list(self.links.links.values()):
                if link.state == "active":
                    try:
                        await self.refresh_link(link)
                    except Exception:  # noqa: BLE001
                        LOGGER.exception("refreshing a link's bridges failed")
            await asyncio.sleep(REFRESH_SECONDS)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()


def _items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("Send a list of items: {kind, name, machine, teams}")
    items = []
    for item in value:
        if not isinstance(item, dict) or item.get("kind") not in KINDS:
            raise ValueError(f"Each item needs a kind: one of {', '.join(KINDS)}")
        items.append(item)
    if len(items) > 500:
        raise ValueError("At most 500 items")
    keys = [item_key(item) for item in items]
    if len(set(keys)) != len(keys):
        raise ValueError("An item is listed twice")
    return items


def _clean(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if value is not None}


def _detail(response: Any, default: str) -> str:
    try:
        data = response.json()
        if isinstance(data, dict) and data.get("detail"):
            return str(data["detail"])
    except ValueError:
        pass
    return default
