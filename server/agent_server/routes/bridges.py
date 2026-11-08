"""Bridges: organization admins' routes, organizations' own agent definitions, and what people use across.

/api/orgs/{org}/bridges* (ADMIN_ONLY, then administers): make a bridge code, check and enter the other
side's, set this side's policy (what it lets out and in), pause, end, and the per-bridge usage limits
without which nothing is used. /api/orgs/{org}/agents*: the organization's own agent definitions,
which only its admins change (and which alone may cross a bridge). /api/peer/*: what crosses in for
the person asking, under full names, and starting an agent on another server's machine (a launch
there, followed here in a mirror session).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from .. import auth, config_io, routing
from ..agents import stored_agents
from ..links import CODE_LIFETIME, PEER_PREFIX, LinkDown
from ..runtime import Runtime


def register(app: FastAPI, runtime: Runtime) -> None:
    bridges = runtime.bridges
    links = runtime.links

    def administered(org: str, request: Request) -> Any:
        identity = auth.request_identity(request)
        # The tenant first: another organization's bridges look like nothing.
        if not auth.administers(identity, org) or runtime.auth.store.org(org) is None:
            raise HTTPException(status_code=404, detail="No such organization")
        return identity

    def bridge_of(org: str, bridge_id: str) -> Any:
        bridge = bridges.bridges.get(bridge_id)
        if bridge is None or bridge.org != org:
            raise HTTPException(status_code=404, detail="No such bridge")
        return bridge

    def link_for(org: str, link_id: str) -> Any:
        link = links.links.get(link_id)
        if link is None or link.state != "active" or not links.org_allowed(links.link_policy(link, "local"), org):
            raise HTTPException(status_code=404, detail="No such link for your organization: a site admin links servers and says which organizations may bridge")
        return link

    async def body(request: Request) -> dict[str, Any]:
        data = await request.json()
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Send a JSON object")
        return data

    def audit(request: Request, kind: str, org: str, target: dict[str, Any], changes: dict[str, Any] | None = None) -> None:
        identity = auth.request_identity(request)
        runtime.audit.record(runtime.audit.event(kind, tenant=org, actor=getattr(identity, "user", None), target=target, changes=changes), now=True)

    # Organization admins: bridges

    @app.get("/api/orgs/{org}/bridges")
    async def list_bridges(org: str, request: Request) -> dict[str, Any]:
        """The organization's bridges, the links it may bridge over, and its codes not yet used."""
        administered(org, request)
        return {
            "bridges": [bridges.public(bridge) for bridge in bridges.for_org(org)],
            "links": [
                {"id": link.id, "server": link.peer, "connected": links.connected(link.id), "lets_out": links.link_policy(link, "local").get("out") or [],
                 "lets_in": links.link_policy(link, "local").get("in") or []}
                for link in links.links.values() if link.state == "active" and links.org_allowed(links.link_policy(link, "local"), org)
            ],
            "codes": links.codes("bridge", org),
            "mesh_id": links.mesh_id,
        }

    @app.post("/api/orgs/{org}/bridges/codes", status_code=201)
    async def bridge_code(org: str, request: Request) -> dict[str, Any]:
        """A one-use code (msb_..., at most a day) for the other organization's admin, with this organization's id to confirm."""
        identity = administered(org, request)
        data = await body(request)
        link = link_for(org, str(data.get("link") or ""))
        try:
            code, expires = bridges.make_code(org, link, identity.user, float(data.get("lifetime") or CODE_LIFETIME))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit(request, "bridge.code", org, {"link": link.id})
        return {"code": code, "expires_at": expires, "org": org, "server": links.mesh_id}

    @app.delete("/api/orgs/{org}/bridges/codes/{code_id}")
    async def revoke_bridge_code(org: str, code_id: str, request: Request) -> dict[str, Any]:
        administered(org, request)
        if not links.revoke_code(code_id, "bridge", org):
            raise HTTPException(status_code=404, detail="No such code")
        return {"revoked": True}

    @app.post("/api/orgs/{org}/bridges/inspect")
    async def inspect(org: str, request: Request) -> dict[str, Any]:
        """Which organization the other side's code is for: check it's the one you expected before entering it."""
        administered(org, request)
        data = await body(request)
        link = link_for(org, str(data.get("link") or ""))
        try:
            return await bridges.inspect(org, link, str(data.get("code") or ""))
        except (ValueError, LinkDown) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/orgs/{org}/bridges/join")
    async def join(org: str, request: Request) -> dict[str, Any]:
        identity = administered(org, request)
        data = await body(request)
        link = link_for(org, str(data.get("link") or ""))
        try:
            bridge = await bridges.join(org, link, str(data.get("code") or ""), str(data.get("peer_org") or ""), identity.user)
        except (ValueError, LinkDown) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit(request, "bridge.joined", org, {"bridge": bridge.id, "link": link.id, "peer_org": bridge.peer_org})
        return bridges.public(bridge)

    @app.get("/api/orgs/{org}/bridges/{bridge_id}")
    async def get_bridge(org: str, bridge_id: str, request: Request) -> dict[str, Any]:
        administered(org, request)
        bridge = bridge_of(org, bridge_id)
        limits = [limit.public() for limit in runtime.usage.tenant_limits(org) if limit.user == f"bridge:{bridge.id}"]
        return {**bridges.public(bridge), "limits": limits, "shareable": shareable(org, bridge)}

    def shareable(org: str, bridge: Any) -> dict[str, Any]:
        """What this organization has that it could let out over the bridge, for the console's picker."""
        providers = [
            {"name": name, "type": backend.type} for name, backend in runtime.settings.backends.items()
            if backend.type in ("ollama", "strata", "docker", "llamacpp", "api") and runtime.fleet.backend_tenant(backend) == org
        ]
        agents = [{"name": agent["id"], "display": (agent["definition"] or {}).get("name") or agent["id"]} for agent in stored_agents(runtime.db, org)]
        machines = []
        for machine in runtime.fleet.machines.values():
            if machine.tenant != org:
                continue
            granted = sorted(feature for feature, grant in machine.access.items() if bridge.id in (grant.get("bridges") or []))
            machines.append({
                "name": machine.name, "id": machine.id, "granted": granted, "connected": runtime.fleet.connected(machine.id),
                "mcp": [server["name"] for server in (machine.mcp or {}).get("servers") or []],
            })
        return {"providers": providers, "agents": agents, "machines": machines}

    @app.put("/api/orgs/{org}/bridges/{bridge_id}/policy")
    async def set_policy(org: str, bridge_id: str, request: Request) -> dict[str, Any]:
        """This organization's side of the bridge: what it lets out, and what it lets in (for which of its teams)."""
        identity = administered(org, request)
        bridge = bridge_of(org, bridge_id)
        data = await body(request)
        try:
            document = await bridges.set_policy(bridge, data.get("out") or [], data.get("in") or [], identity)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"policy": document, **bridges.public(bridge)}

    @app.post("/api/orgs/{org}/bridges/{bridge_id}/{action}")
    async def bridge_action(org: str, bridge_id: str, action: str, request: Request) -> dict[str, Any]:
        administered(org, request)
        bridge = bridge_of(org, bridge_id)
        if action == "refresh":
            await bridges.refresh(bridge)
            return bridges.public(bridges.bridges.get(bridge.id) or bridge)
        if action not in ("pause", "resume"):
            raise HTTPException(status_code=404, detail="Pause, resume or refresh")
        if action == "resume" and not bridge.complete:
            raise HTTPException(status_code=409, detail="Both organizations haven't entered each other's codes yet")
        bridges.set_state(bridge, "paused" if action == "pause" else "active")
        audit(request, f"bridge.{action}d", org, {"bridge": bridge.id})
        return bridges.public(bridge)

    @app.delete("/api/orgs/{org}/bridges/{bridge_id}")
    async def end_bridge(org: str, bridge_id: str, request: Request) -> dict[str, Any]:
        """Ends the bridge on both sides; launches over it stop. What each side recorded stays under its retention."""
        administered(org, request)
        bridge = bridge_of(org, bridge_id)
        bridges.end(bridge)
        audit(request, "bridge.ended", org, {"bridge": bridge.id, "peer_org": bridge.peer_org})
        return {"ended": True}

    @app.put("/api/orgs/{org}/bridges/{bridge_id}/limits")
    async def set_limit(org: str, bridge_id: str, request: Request) -> dict[str, Any]:
        """A usage limit for everything the other organization's people use of one of yours over this bridge.
        Nothing is used until there is one."""
        identity = administered(org, request)
        bridge = bridge_of(org, bridge_id)
        data = await body(request)
        provider = str(data.get("provider") or "")
        backend = runtime.settings.backends.get(provider.removeprefix("backend:"))
        if backend is None or runtime.fleet.backend_tenant(backend) != org:
            raise HTTPException(status_code=400, detail="Name one of your organization's providers")
        try:
            limit = runtime.usage.set_limit(org, f"bridge:{bridge.id}", f"backend:{backend.name}", str(data.get("window") or ""), data.get("tokens"), identity.user)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit(request, "bridge.limit", org, {"bridge": bridge.id, "provider": backend.name}, {"window": limit.window, "tokens": limit.tokens})
        return limit.public()

    @app.delete("/api/orgs/{org}/bridges/{bridge_id}/limits")
    async def remove_limit(org: str, bridge_id: str, request: Request, provider: str = "", window: str = "") -> dict[str, Any]:
        administered(org, request)
        bridge = bridge_of(org, bridge_id)
        removed = runtime.usage.remove_limit(org, f"bridge:{bridge.id}", f"backend:{provider.removeprefix('backend:')}", window)
        if not removed:
            raise HTTPException(status_code=404, detail="No such limit")
        return {"removed": True}

    # Organization admins: their own agent definitions (the only ones that cross a bridge)

    @app.get("/api/orgs/{org}/agents")
    async def org_agents(org: str, request: Request) -> list[dict[str, Any]]:
        identity = auth.request_identity(request)
        if not auth.same_tenant(identity, org) or runtime.auth.store.org(org) is None:
            raise HTTPException(status_code=404, detail="No such organization")
        agents = await asyncio.to_thread(stored_agents, runtime.db, org)
        return [{**agent, "yaml": config_io.dump(agent["definition"])} for agent in agents]

    @app.put("/api/orgs/{org}/agents/{agent_id}")
    async def put_org_agent(org: str, agent_id: str, request: Request) -> dict[str, Any]:
        administered(org, request)
        data = await request.json()
        definition = config_io.parse(data["yaml"]) if isinstance(data, dict) and isinstance(data.get("yaml"), str) else (data.get("definition") if isinstance(data, dict) else None)
        try:
            saved = await asyncio.to_thread(config_io.put_agent, runtime.settings, agent_id, definition, org)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit(request, "agent.saved", org, {"agent": agent_id})
        return saved

    @app.delete("/api/orgs/{org}/agents/{agent_id}")
    async def delete_org_agent(org: str, agent_id: str, request: Request) -> dict[str, Any]:
        administered(org, request)
        if not await asyncio.to_thread(config_io.delete_agent, runtime.settings, agent_id, org):
            raise HTTPException(status_code=404, detail="No such agent in your organization")
        audit(request, "agent.deleted", org, {"agent": agent_id})
        return {"deleted": True}

    # People: what crosses in for them

    @app.get("/api/peer/resources")
    async def resources(request: Request) -> dict[str, Any]:
        """Providers, MCP servers, agent definitions and machines other servers' organizations let in for you, under full names."""
        return bridges.remote_for(auth.request_identity(request))

    def remote_machine(request: Request, full: str, function: str) -> tuple[Any, Any, routing.Name]:
        identity = auth.request_identity(request)
        try:
            name = routing.parse(full, "machine")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        bridge = bridges.find(name.server, identity.tenant, name.org)
        if bridge is None or bridges.lets_in(identity, bridge, function, machine=name.name) is None:
            raise HTTPException(status_code=404, detail=f"{full} isn't shared with you")
        return identity, bridge, name

    @app.get("/api/peer/folders")
    async def folders(request: Request, machine: str, path: str = "") -> Any:
        """The launch folders of another server's machine shared with you, as that server lists them."""
        identity, bridge, name = remote_machine(request, machine, "launch")
        assertion = bridges.assertion(bridge, bridges.who(identity), {"kind": "launch", "org": name.org, "machine": name.name}, "folders")
        from urllib.parse import quote

        link = bridges.link_of(bridge)
        target = f"{PEER_PREFIX}machine/{name.org}/{name.name}/folders" + (f"?path={quote(path, safe='')}" if path else "")
        try:
            response = await links.send(link, "GET", target, assertion=assertion)
        except LinkDown as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=_detail(response))
        return response.json()

    @app.post("/api/peer/launch", status_code=201)
    async def launch(request: Request) -> dict[str, Any]:
        """Start an agent on another server's machine shared with you. The session lives there; you follow it here
        in a mirror session (its requests, traces and terminal), which is yours."""
        if not access_same_origin(request):
            raise HTTPException(status_code=403, detail="Start agents from the console's own page.")
        data = await body(request)
        identity, bridge, name = remote_machine(request, str(data.get("machine") or ""), "launch")
        agent = str(data.get("agent") or "")
        backend = str(data.get("backend") or "")
        link = bridges.link_of(bridge)
        me = links.me()

        def theirs(value: str, kind: str) -> str:
            """As the other server names it: its own by short name, anything else by full name."""
            if routing.is_full(value):
                parsed = routing.parse(value, kind)
                return parsed.name if parsed.server == link.peer and parsed.org == name.org else value
            return routing.full(kind, me, identity.tenant, value) if value else ""

        def their_mcp(value: str) -> str:
            # Only the machine's own organization's MCP servers there, as it names them: machine/name.
            parsed = routing.parse(value, "mcp")
            if parsed.server != link.peer or parsed.org != name.org:
                raise ValueError(f"{value} isn't one of {name.org}'s at {link.peer}")
            return f"{parsed.machine}/{parsed.name}"

        try:
            # The agent as the machine lists it there (its server's own, by short name), or a full name; the
            # backend as this server's people name it (theirs by full name, ours by short name).
            agent_there = theirs(agent, "agent") if routing.is_full(agent) else agent
            backend_there = theirs(backend, "provider")
            mcp_there = [their_mcp(str(item)) for item in data.get("mcp") or []]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        mirror = bridges.mirror_session(identity, bridge, routing.full("machine", name.server, name.org, name.name), agent, str(data.get("project") or ""), backend or None, str(data.get("model") or "") or None)
        assertion = bridges.assertion(bridge, bridges.who(identity), {"kind": "launch", "org": name.org, "machine": name.name}, "launch", mirror.session_id)
        payload = {
            "agent": agent_there, "project": str(data.get("project") or ""), "model": str(data.get("model") or ""), "backend": backend_there,
            "mcp": mcp_there,
            "mirror": mirror.session_id,
        }
        try:
            response = await links.send(link, "POST", f"{PEER_PREFIX}machine/{name.org}/{name.name}/launch", json_body=payload, assertion=assertion, timeout=120)
        except LinkDown as exc:
            runtime.sessions.end(mirror)
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if response.status_code >= 400:
            runtime.sessions.end(mirror)
            if response.status_code == 409:
                bridges.soon(bridges.refresh(bridge))
            raise HTTPException(status_code=response.status_code, detail=_detail(response))
        answer = response.json()
        runtime.sessions.update_tags(mirror, {"mirror_of": answer.get("session_id")})
        audit(request, "bridge.launch", identity.tenant, {"bridge": bridge.id, "machine": data.get("machine"), "agent": agent}, {"session": mirror.session_id})
        return {"session_id": mirror.session_id, "remote_session": answer.get("session_id"), "machine": data.get("machine"), "agent": agent, "backend": answer.get("backend")}

    def access_same_origin(request: Request) -> bool:
        from .. import remote

        return remote.same_origin(request)


def _detail(response: Any) -> str:
    try:
        data = response.json()
        if isinstance(data, dict) and data.get("detail"):
            return str(data["detail"])
    except ValueError:
        pass
    return f"The other server answered {response.status_code}"
