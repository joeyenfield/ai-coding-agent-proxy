"""Links between servers: the site admins' routes, and the routes a linked server calls.

The site admins' routes (/api/links*, SITE_ADMIN_ONLY) set this server's mesh id, make link
codes, join another server, set the link policy, pause, remove and rotate keys.

The peer routes (/api/links/present, /api/links/connect, /api/links/relay/*) take no person's
token: they're public paths in AuthMiddleware on purpose, and each checks the request's link-key
signature first (links.Links.verify), then what the bridge lets out (bridges.Bridges.checked).
A request carried over a link socket arrives here the same way, through this server's own app.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, Response

from .. import auth, routing
from ..bridges import Guest
from ..fleet import FleetError, within_roots
from ..links import CODE_LIFETIME, PEER_PREFIX, WELL_KNOWN, LinkDown, Peer, Refused
from ..runtime import Runtime

LOGGER = logging.getLogger("agent-proxy")
RELAY = PEER_PREFIX.rstrip("/")


def register(app: FastAPI, runtime: Runtime) -> None:
    links = runtime.links
    bridges = runtime.bridges
    # Requests carried over a link socket run through this app (links.Channel).
    links.app = app

    def json_body(raw: bytes) -> dict[str, Any]:
        try:
            value = json.loads(raw or b"{}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Send a JSON object") from exc
        if not isinstance(value, dict):
            raise HTTPException(status_code=400, detail="Send a JSON object")
        return value

    async def admin_body(request: Request) -> dict[str, Any]:
        site_admin(request)
        return json_body(await request.body())

    def site_admin(request: Request) -> None:
        # SITE_ADMIN_ONLY already asked for one; checked again here, beside the public peer routes.
        identity = auth.request_identity(request)
        if not isinstance(identity, auth.Identity) or not identity.site_admin:
            raise HTTPException(status_code=403, detail="Only a site admin can do this.")

    def refused(exc: Refused) -> JSONResponse:
        return JSONResponse({"detail": exc.detail, **exc.extra}, status_code=exc.status)

    def link_for(link_id: str) -> Any:
        link = links.links.get(link_id)
        if link is None:
            raise HTTPException(status_code=404, detail="No such link")
        return link

    def audit(request: Request, kind: str, target: dict[str, Any], changes: dict[str, Any] | None = None) -> None:
        identity = auth.request_identity(request)
        runtime.audit.record(runtime.audit.event(kind, tenant=auth.ROOT_TENANT, actor=getattr(identity, "user", None), target=target, changes=changes), now=True)

    # What this server is, for another server's site admin to check

    @app.get(WELL_KNOWN, include_in_schema=False)
    async def well_known() -> Any:
        if not links.mesh_id:
            raise HTTPException(status_code=404, detail="This server takes no links: it has no mesh id")
        return links.well_known()

    # Site admins

    @app.get("/api/links")
    async def list_links(request: Request) -> dict[str, Any]:
        """This server's mesh id and key fingerprints, and its links."""
        site_admin(request)
        return await asyncio.to_thread(links.public)

    @app.put("/api/links/mesh")
    async def set_mesh(request: Request) -> dict[str, Any]:
        """This server's mesh id: chosen once, before the first link (other servers know it by it)."""
        data = await admin_body(request)
        try:
            links.set_mesh_id(str(data.get("id") or ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit(request, "link.mesh_id", {"mesh_id": data.get("id")})
        return links.public()

    @app.get("/api/links/codes")
    async def list_codes(request: Request) -> list[dict[str, Any]]:
        site_admin(request)
        return links.codes("link")

    @app.post("/api/links/codes", status_code=201)
    async def make_code(request: Request) -> dict[str, Any]:
        """A one-use code (msl_..., an hour) for the other server's site admin to enter, with this server's id and fingerprint."""
        data = await admin_body(request)
        try:
            me = links.me()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        lifetime = min(max(float(data.get("lifetime") or CODE_LIFETIME), 60.0), CODE_LIFETIME)
        code, expires = links.make_code("link", auth.request_identity(request).user, lifetime=lifetime)
        audit(request, "link.code", {"mesh_id": me})
        info = links.well_known()
        return {"code": code, "expires_at": expires, "mesh_id": me, "url": info["url"], "fingerprint": info["fingerprint"]}

    @app.delete("/api/links/codes/{code_id}")
    async def revoke_code(code_id: str, request: Request) -> dict[str, Any]:
        site_admin(request)
        if not links.revoke_code(code_id, "link"):
            raise HTTPException(status_code=404, detail="No such code")
        return {"revoked": True}

    @app.post("/api/links/inspect")
    async def inspect(request: Request) -> dict[str, Any]:
        """What the server at url says it is: its mesh id and the fingerprint its site admin reads out to you."""
        data = await admin_body(request)
        try:
            info = await links.inspect(str(data.get("url") or ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {key: info[key] for key in ("mesh_id", "url", "fingerprint", "link_protocol")}

    @app.post("/api/links/join")
    async def join(request: Request) -> dict[str, Any]:
        """Enter the other server's code: with its url and the fingerprint you confirmed, or the id of a link it already presented over."""
        data = await admin_body(request)
        identity = auth.request_identity(request)
        address = request.client.host if request.client else "unknown"
        if links.too_many_wrong(address):
            raise HTTPException(status_code=429, detail="Too many wrong codes: wait a few minutes")
        try:
            link = await links.join(
                str(data.get("code") or ""), identity.user, url=str(data.get("url") or "") or None,
                link_id_=str(data.get("link") or "") or None, expected=str(data.get("fingerprint") or "") or None,
            )
        except ValueError as exc:
            links.wrong_code(address)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit(request, "link.joined", {"link": link.id, "peer": link.peer})
        return link.public(links.connected(link.id))

    @app.post("/api/links/keys/rotate")
    async def rotate(request: Request) -> dict[str, Any]:
        """A new link key: the old one stays valid on both sides for a day, so linked servers follow without a gap."""
        site_admin(request)
        old = links.keys.current()["kid"]
        new = links.keys.rotate()
        audit(request, "link.key_rotated", {"kid": new["kid"]})
        for link in list(links.links.values()):
            if link.state == "active":
                bridges.soon(links.announce_keys(link, old))
        return links.public()

    @app.delete("/api/links/keys/{kid}")
    async def remove_key(kid: str, request: Request) -> dict[str, Any]:
        """A key that leaked: it stops working at once. Links made with it have to be set up again."""
        site_admin(request)
        if not links.keys.remove(kid):
            raise HTTPException(status_code=400, detail="No such key, or it's the only one")
        audit(request, "link.key_removed", {"kid": kid})
        return links.public()

    # What a linked server calls: signed with its link key, nothing else

    async def peer_of(request: Request) -> Peer:
        body = await request.body()
        try:
            return links.verify(request.scope, request.headers, body)
        except Refused as exc:
            raise _PeerError(exc) from exc

    @app.post("/api/links/present")
    async def present(request: Request) -> Any:
        body = await request.body()
        address = request.client.host if request.client else "unknown"
        if links.too_many_wrong(address):
            return JSONResponse({"detail": "Too many wrong codes"}, status_code=429)
        try:
            link = await asyncio.to_thread(links.presented, request.scope, request.headers, body)
        except Refused as exc:
            if exc.status == 403:
                links.wrong_code(address)
            runtime.audit.record(runtime.audit.event("link.refused", tenant=auth.ROOT_TENANT, outcome="denied", reason=exc.detail, ip=address, action="present"))
            return refused(exc)
        runtime.audit.record(runtime.audit.event("link.presented", tenant=auth.ROOT_TENANT, actor=link.peer, ip=address, target={"link": link.id}))
        return {"link": link.id, "mesh_id": links.mesh_id, "state": link.state}

    @app.websocket("/api/links/connect")
    async def connect(websocket: WebSocket) -> None:
        await links.accept(websocket)

    @app.get(RELAY + "/policies")
    async def peer_policies(request: Request) -> Any:
        try:
            peer = await peer_of(request)
        except _PeerError as exc:
            return refused(exc.refusal)
        return bridges.policies_for(peer.link)

    @app.post(RELAY + "/changed")
    async def peer_changed(request: Request) -> Any:
        try:
            peer = await peer_of(request)
        except _PeerError as exc:
            return refused(exc.refusal)
        bridges.soon(bridges.refresh_link(peer.link))
        return {"ok": True}

    @app.post(RELAY + "/keys")
    async def peer_keys(request: Request) -> Any:
        """The other server rotated its link key: the new one, sent signed by the old one and proven by itself."""
        try:
            peer = await peer_of(request)
            await asyncio.to_thread(links.accept_keys, peer.link, json_body(await request.body()))
        except _PeerError as exc:
            return refused(exc.refusal)
        except Refused as exc:
            return refused(exc)
        return {"ok": True}

    @app.post(RELAY + "/bridge/inspect")
    async def bridge_inspect(request: Request) -> Any:
        try:
            peer = await peer_of(request)
            return bridges.present_inspect(peer, json_body(await request.body()))
        except _PeerError as exc:
            return refused(exc.refusal)
        except Refused as exc:
            return refused(exc)

    @app.post(RELAY + "/bridge/present")
    async def bridge_present(request: Request) -> Any:
        try:
            peer = await peer_of(request)
            return await asyncio.to_thread(bridges.presented, peer, json_body(await request.body()))
        except _PeerError as exc:
            return refused(exc.refusal)
        except Refused as exc:
            return refused(exc)

    @app.get(RELAY + "/catalogue/{bridge_id}")
    async def catalogue(bridge_id: str, request: Request) -> Any:
        try:
            peer = await peer_of(request)
        except _PeerError as exc:
            return refused(exc.refusal)
        bridge = bridges.bridges.get(bridge_id)
        if bridge is None or bridge.link != peer.link.id:
            return JSONResponse({"detail": "No such bridge"}, status_code=404)
        return bridges.catalogue_for(bridge)

    @app.api_route(RELAY + "/provider/{org}/{name}/{path:path}", methods=["GET", "POST"])
    async def relay_provider(org: str, name: str, path: str, request: Request) -> Any:
        """A guest's model call to one of this organization's providers, recorded in its bridge session here."""
        from . import compat

        try:
            peer = await peer_of(request)
            bridge, guest, item = bridges.checked(peer, org, "provider", name=name)
        except _PeerError as exc:
            return refused(exc.refusal)
        except Refused as exc:
            return refused(exc)
        backend = runtime.settings.backends.get(name)
        if backend is None:
            return JSONResponse({"detail": f"{name} is no longer here"}, status_code=404)
        model = None
        if request.method == "POST":
            try:
                model = (json.loads(await request.body() or b"{}") or {}).get("model")
            except (ValueError, AttributeError):
                model = None
        if item.get("scope") == "home":
            session = home_session(peer, bridge, guest)
            if session is None:
                return JSONResponse({"detail": "No launch of yours on the other server matches"}, status_code=403)
        else:
            session = bridges.bridge_session(bridge, guest, (peer.assertion or {}).get("session"), name, model)
            if request.method == "POST" and bridges.limit_missing(bridge, f"backend:{name}"):
                bridges._refused(peer, org, f"no usage limit for the bridge on {name}")
                return JSONResponse(
                    {"detail": f"{bridge.org} hasn't set a usage limit for this bridge on {name} yet: nothing is used until it does"},
                    status_code=429,
                )
        if session is None:
            return JSONResponse({"detail": "Couldn't open the bridge session"}, status_code=500)
        return await compat.relay(runtime, request, path, session, backend)

    def home_session(peer: Peer, bridge: Any, guest: Guest) -> Any:
        """A call back home from a launch on the other server: its mirror session here, which its person owns."""
        session = runtime.sessions.get(str((peer.assertion or {}).get("session") or ""))
        if session is None or session.ended_at is not None or session.tags.get("mirror_bridge") != bridge.id or session.tags.get("tenant") != bridge.org:
            return None
        record = runtime.auth.store.user(str(session.tags.get("owner") or "")) or {}
        from ..bridges import user_uuid

        if user_uuid(str(session.tags.get("owner")), record.get("created_at")) != guest.user:
            return None
        return session

    @app.api_route(RELAY + "/mcp/{org}/{machine}/{name}", methods=["GET", "POST", "DELETE"])
    async def relay_mcp(org: str, machine: str, name: str, request: Request) -> Any:
        """A guest's MCP request to a server one of this organization's machines shares: to its bridge, with its key."""
        from .mcp import _pass, _recorded, bridge_target

        try:
            peer = await peer_of(request)
            bridge, guest, item = bridges.checked(peer, org, "mcp", name=name, machine=machine)
        except _PeerError as exc:
            return refused(exc.refusal)
        except Refused as exc:
            return refused(exc)
        target_machine = bridges._machine_quiet(org, machine)
        if target_machine is None or bridge.id not in ((target_machine.access.get("mcp") or {}).get("bridges") or []):
            return JSONResponse({"detail": f"{machine} no longer lets this bridge use its MCP servers"}, status_code=403)
        try:
            target, headers = await bridge_target(runtime, target_machine, name, request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        body = await request.body()
        if request.method != "POST":
            return await _pass(runtime, request.method, target, headers, body)
        session = bridges.bridge_session(bridge, guest, (peer.assertion or {}).get("session"), f"mcp:{machine}/{name}", None)
        return await _recorded(runtime, session, f"{machine}/{name}", target, headers, body)

    @app.get(RELAY + "/machine/{org}/{machine}/folders")
    async def relay_folders(org: str, machine: str, request: Request, path: str = "") -> Any:
        try:
            peer = await peer_of(request)
            bridge, guest, item = bridges.checked(peer, org, "launch", machine=machine)
        except _PeerError as exc:
            return refused(exc.refusal)
        except Refused as exc:
            return refused(exc)
        target = bridges._machine_quiet(org, machine)
        if target is None:
            return JSONResponse({"detail": "No such machine"}, status_code=404)
        if not path:
            return {"path": None, "parent": None, "folders": [{"name": root, "path": root} for root in target.roots]}
        if not within_roots(path, target.roots):
            return JSONResponse({"detail": "That folder isn't inside one of the machine's launch folders."}, status_code=400)
        try:
            answer = await runtime.fleet.ask(target, "list", 15, "folders", path=path)
        except FleetError as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status)
        return {"path": path, "parent": answer.get("parent"), "folders": answer.get("folders") or [], "truncated": bool(answer.get("truncated"))}

    @app.post(RELAY + "/machine/{org}/{machine}/launch")
    async def relay_launch(org: str, machine: str, request: Request) -> Any:
        """A guest starts an agent on one of this organization's machines: the machine owner's session, as console
        launches are, tagged by the server with the guest, the bridge and the backends it may use."""
        try:
            peer = await peer_of(request)
            bridge, guest, item = bridges.checked(peer, org, "launch", machine=machine)
        except _PeerError as exc:
            return refused(exc.refusal)
        except Refused as exc:
            return refused(exc)
        data = json_body(await request.body())
        target = bridges._machine_quiet(org, machine)
        if target is None or bridge.id not in ((target.access.get("launch") or {}).get("bridges") or []):
            return JSONResponse({"detail": f"{machine}'s owner doesn't let this bridge start agents there"}, status_code=403)
        owner = runtime.auth.identity_for(str(target.owner or ""), "machine") if target.owner else None
        agents = runtime.agents(owner) if owner else runtime.agents_of(org)
        agent = agents.get(str(data.get("agent") or ""))
        if agent is None or agent.intercept:
            return JSONResponse({"detail": "Choose one of the agents this server serves (account agents can't be started for guests)"}, status_code=400)
        allowed = bridges.launch_backends(bridge)
        try:
            backend = runtime.settings.backend_for(agent.backend_type, str(data.get("backend") or "") or None, agent.protocol, lambda value: value.name in allowed).name
        except ValueError as exc:
            return JSONResponse({"detail": f"{exc}. A launch over this bridge may use: {', '.join(allowed) or 'nothing yet'}"}, status_code=400)
        wanted_mcp = [str(value) for value in data.get("mcp") or []]
        shared = bridges.launch_mcp(bridge)
        mcp_ids = []
        for value in wanted_mcp:
            if value not in shared:
                return JSONResponse({"detail": f"{value} isn't shared over this bridge"}, status_code=400)
            machine_name, _, server = value.partition("/")
            host = bridges._machine_quiet(org, machine_name)
            if host is None:
                return JSONResponse({"detail": f"No machine {machine_name}"}, status_code=400)
            mcp_ids.append(f"{host.id}/{server}")
        mirror = str(data.get("mirror") or "")
        tags = {
            **guest.tags(), "launched_by": f"bridge:{bridge.id}", "remote_session": mirror or None, "peering": "launch",
            "allowed_backends": allowed,
        }
        launch = {
            "agent": agent.id, "project": str(data.get("project") or ""), "model": str(data.get("model") or ""), "backend": backend,
            "trace": True, "visibility": "private", "mcp": mcp_ids,
        }
        try:
            record = await runtime.fleet.launch(target, None, launch, launch_id=secrets.token_hex(8), guest={"tags": tags})
        except FleetError as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status)
        runtime.audit.record(runtime.audit.event(
            "bridge.launch", tenant=org, actor=guest.label, target={"bridge": bridge.id, "machine": target.id, "agent": agent.id},
            session_id=record.get("session_id"), machine_id=target.id,
        ))
        return {"session_id": record.get("session_id"), "launch": record.get("launch"), "agent": agent.id, "backend": backend}

    @app.websocket(RELAY + "/session/{session_id}/terminal")
    async def relay_terminal(websocket: WebSocket, session_id: str) -> None:
        """A guest watches (and, if the bridge lets terminals out, types into) the terminal of a launch it started here."""
        from .terminals import serve_guest_viewer

        await websocket.accept()
        try:
            peer = links.verify(websocket.scope, websocket.headers, b"")
            session = runtime.sessions.get(session_id)
            machine = runtime.fleet.machines.get(str((session.tags if session else {}).get("machine") or ""))
            if session is None or machine is None:
                raise Refused(404, "No such session")
            bridge, guest, _ = bridges.checked(peer, machine.tenant or "", "launch", machine=machine.name)
        except Refused as exc:
            await websocket.close(code=4400 + exc.status % 100, reason=exc.detail[:120])
            return
        if not bridges.can_view(guest, session):
            await websocket.close(code=4404, reason="Not your launch")
            return

        def typing_allowed() -> bool:
            current = runtime.sessions.get(session_id)
            return bool(current and bridges.can_view(guest, current) and any(
                item.get("kind") == "terminal" and item.get("machine") == machine.name for item in bridges.crossing(bridge)["out"]
            ))

        def still_allowed() -> bool:
            current = runtime.sessions.get(session_id)
            return bool(current and bridge.id in bridges.bridges and bridges.bridges[bridge.id].state == "active" and bridges.can_view(guest, current) and any(
                item.get("kind") == "launch" and item.get("machine") == machine.name for item in bridges.crossing(bridges.bridges[bridge.id])["out"]
            ))

        await serve_guest_viewer(runtime, websocket, session, guest, still_allowed, typing_allowed)

    @app.post(RELAY + "/mirror")
    async def mirror(request: Request) -> Any:
        try:
            peer = await peer_of(request)
            return {"written": await asyncio.to_thread(bridges.mirrored, peer, json_body(await request.body()))}
        except _PeerError as exc:
            return refused(exc.refusal)
        except Refused as exc:
            return refused(exc)

    @app.post(RELAY + "/offboard")
    async def offboard(request: Request) -> Any:
        try:
            peer = await peer_of(request)
            return {"ended": bridges.offboarded(peer, json_body(await request.body()))}
        except _PeerError as exc:
            return refused(exc.refusal)
        except Refused as exc:
            return refused(exc)


    # Site admins: one link. Registered after the peer routes, whose literal paths these would otherwise take.

    @app.get("/api/links/{link_id}")
    async def get_link(link_id: str, request: Request) -> dict[str, Any]:
        site_admin(request)
        link = link_for(link_id)
        return {
            **link.public(links.connected(link.id)),
            "policy": links.link_policy(link, "local"), "peer_policy": links.link_policy(link, "peer"),
            "bridges": [bridges.public(bridge) for bridge in bridges.bridges.values() if bridge.link == link.id],
        }

    @app.put("/api/links/{link_id}/policy")
    async def set_policy(link_id: str, request: Request) -> dict[str, Any]:
        """This server's link policy: what kinds its organizations may let out and in, and which organizations may bridge."""
        link = link_for(link_id)
        data = await admin_body(request)
        try:
            envelope = links.set_link_policy(link, data.get("out"), data.get("in"), data.get("orgs"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit(request, "link.policy", {"link": link.id, "peer": link.peer}, {"version": envelope["document"]["version"]})
        bridges.rebuild()
        await links.notify(link, "link", link.id)
        return envelope["document"]

    @app.post("/api/links/{link_id}/{action}")
    async def link_action(link_id: str, action: str, request: Request) -> dict[str, Any]:
        site_admin(request)
        link = link_for(link_id)
        if action not in ("pause", "resume"):
            raise HTTPException(status_code=404, detail="Pause or resume")
        if action == "resume" and not link.complete:
            raise HTTPException(status_code=409, detail="Both sides haven't presented yet")
        links.set_state(link, "paused" if action == "pause" else "active")
        audit(request, f"link.{action}d", {"link": link.id, "peer": link.peer})
        return link.public(links.connected(link.id))

    @app.delete("/api/links/{link_id}")
    async def remove_link(link_id: str, request: Request) -> dict[str, Any]:
        """Ends the link and every bridge over it. What each side recorded stays, under its own retention."""
        site_admin(request)
        link = link_for(link_id)
        links.remove(link.id)
        audit(request, "link.removed", {"link": link.id, "peer": link.peer})
        return {"removed": True}


class _PeerError(Exception):
    def __init__(self, refusal: Refused):
        super().__init__(refusal.detail)
        self.refusal = refusal

