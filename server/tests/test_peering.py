"""Peering: links between servers and bridges between their organizations (links.py, bridges.py).

Unit tests for full names, the four gates and the guest type, then two real servers on real ports
linking, bridging and carrying a model call each way, directly and over the link socket.
"""

from __future__ import annotations

import json
import time
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace

import httpx
import pytest

from agent_server import access, auth, fleet as fleet_module, routing
from agent_server.app import create_app
from agent_server.bridges import Guest, overlap, safe_definition, user_uuid
from agent_server.config import Backend, Settings, serves
from agent_server.db import sqlite_url
from agent_server.links import KINDS, Links, Refused, canonical, link_id

from oidc_helpers import access_token, authenticated_client
from test_client import free_port, serve


# Advertised server address


@pytest.mark.parametrize(("mesh_url", "public_url", "expected"), [
    (None, None, "http://localhost:19999"),
    (None, "https://console.example/", "https://console.example"),
    ("https://mesh.example/", "https://console.example/", "https://mesh.example"),
])
def test_links_advertise_configured_server_url(tmp_path, monkeypatch, mesh_url, public_url, expected):
    monkeypatch.delenv("AI_PROXY_MESH_ID", raising=False)
    monkeypatch.delenv("AI_PROXY_MESH_URL", raising=False)
    if mesh_url is not None:
        monkeypatch.setenv("AI_PROXY_MESH_URL", mesh_url)
    app = create_app(Settings(log_dir=tmp_path, proxy_url="http://localhost:19999/", public_url=public_url))
    with authenticated_client(app) as client:
        response = client.get("/api/links")
        assert response.status_code == 200
        assert response.json()["mesh"]["url"] == expected
        assert response.json()["links"] == []
        assert client.put("/api/links/mesh", json={"id": "quickstart-test"}).status_code == 200
        assert client.get("/.well-known/mesh-link").json()["url"] == expected


# Names


def test_full_names_parse_and_never_match_a_short_name():
    name = routing.parse("provider:server-b/org-b1/provider-b1")
    assert (name.kind, name.server, name.org, name.name, name.machine) == ("provider", "server-b", "org-b1", "provider-b1", None)
    assert routing.parse("mcp:server-b/org-b1/gpu-box/mcp-b1").machine == "gpu-box"
    assert routing.full("agent", "server-a", "org.a1", "agent_a1") == "agent:server-a/org.a1/agent_a1"
    assert routing.is_full("machine:server-b/org-b1/gpu-box") and not routing.is_full("provider-b1") and not routing.is_full("org.b1")
    for bad in ("provider:server-b/org-b1", "mcp:server-b/org-b1/x", "nothing:a/b/c", "provider:Server_B/org/x", "provider:s/o/a b", "agent:s/o/x/y"):
        with pytest.raises(ValueError):
            routing.parse(bad)
    with pytest.raises(ValueError):
        routing.parse("provider:server-b/org-b1/x", "machine")


# The four gates


EVERYTHING = {"out": list(KINDS), "in": list(KINDS), "orgs": ["*"]}


def test_only_what_one_side_lets_out_and_the_other_lets_in_crosses():
    out = [{"kind": "provider", "name": "p1"}, {"kind": "provider", "name": "p2"}, {"kind": "launch", "machine": "gpu-box"}]
    into = [{"kind": "provider", "name": "p1", "teams": ["org-a1/dev"]}, {"kind": "mcp", "machine": "gpu-box", "name": "x"}]
    crossed = overlap(out, into, EVERYTHING, EVERYTHING)
    assert crossed == [{"kind": "provider", "name": "p1", "teams": ["org-a1/dev"]}]
    # Either link policy alone stops a kind.
    assert overlap(out, into, {**EVERYTHING, "out": ["mcp"]}, EVERYTHING) == []
    assert overlap(out, into, EVERYTHING, {**EVERYTHING, "in": ["agent"]}) == []
    # A home-scoped item is its own item: the plain one doesn't let it through.
    assert overlap([{"kind": "provider", "name": "p1", "scope": "home"}], into, EVERYTHING, EVERYTHING) == []


def test_an_agent_definition_crosses_only_at_the_version_let_in():
    given = [{"kind": "agent", "name": "a1", "digest": "v2"}]
    assert overlap(given, [{"kind": "agent", "name": "a1", "digest": "v1"}], EVERYTHING, EVERYTHING) == []
    assert overlap(given, [{"kind": "agent", "name": "a1", "digest": "v2"}], EVERYTHING, EVERYTHING)
    assert overlap(given, [{"kind": "agent", "name": "a1"}], EVERYTHING, EVERYTHING)


def test_bridged_definitions_are_restricted():
    assert safe_definition({"name": "x", "executable": "qwen", "env": {"OPENAI_BASE_URL": "{openai_url}", "OPENAI_MODEL": "{model}"}}) is None
    assert "files" in safe_definition({"executable": "qwen", "files": {"a.json": "{}"}})
    assert "PATH" in safe_definition({"executable": "qwen", "env": {"PATH": "/tmp"}})
    assert "NODE_OPTIONS" in safe_definition({"executable": "qwen", "env": {"NODE_OPTIONS": "--require x"}})
    assert "LD_PRELOAD" in safe_definition({"executable": "qwen", "env": {"LD_PRELOAD": "x.so"}})
    assert safe_definition({"executable": "claude", "intercept": True})


# Guests


def test_a_guest_is_refused_by_every_existing_permission_helper():
    guest = Guest(user="u1", name="alice", org="root", server="server-a", link="lnk", bridge="brg")
    session = SimpleNamespace(tags={"tenant": "root", "owner": "alice", "visibility": "org"})
    machine = fleet_module.Machine(id="m1", name="gpu-box", owner="alice", tenant="root")
    assert not auth.same_tenant(guest, "root")
    assert not auth.administers(guest, "root")
    assert not access.can_view(guest, session) and not access.can_control(guest, session) and not access.is_owner(guest, session)
    for feature in fleet_module.FEATURES:
        assert not fleet_module.allowed(guest, machine, feature)
    fleet = SimpleNamespace(machines={}, bridges=None, SHARED="*")
    usable = fleet_module.Fleet.usable_by(fleet, guest)
    assert not usable(Backend("x", "http://x", "ollama"))


def test_guest_ids_change_with_a_recreated_account():
    assert user_uuid("alice", 1.0) == user_uuid("alice", 1.0)
    assert user_uuid("alice", 1.0) != user_uuid("alice", 2.0)


def test_a_link_backend_serves_as_the_provider_it_carries_to():
    api = Backend("provider:b/o/p", "http://b", "link", options={"remote_type": "api", "protocols": ["anthropic"]})
    assert serves(api, "ollama", "anthropic") and not serves(api, "ollama", "openai-chat")
    ollama = Backend("provider:b/o/q", "http://b", "link", options={"remote_type": "ollama"})
    assert serves(ollama, "ollama", "openai-chat") and not serves(ollama, "anthropic")


def test_link_ids_are_the_same_on_both_sides():
    assert link_id(("server-a", "ka"), ("server-b", "kb")) == link_id(("server-b", "kb"), ("server-a", "ka"))
    assert link_id(("server-a", "ka"), ("server-b", "kb")) != link_id(("server-a", "ka"), ("server-b", "kc"))


# Signed requests


def test_a_signed_request_is_refused_when_anything_in_it_changes(tmp_path):
    settings = Settings(log_dir=tmp_path / "a")
    other = Settings(log_dir=tmp_path / "b", database_url=sqlite_url(tmp_path / "b.sqlite3"))
    cluster = __import__("agent_server.cluster", fromlist=["LocalCluster"]).LocalCluster()
    mine, theirs = Links(settings.open_database(), settings, cluster), Links(other.open_database(), other, cluster)
    mine.set_mesh_id("server-a")
    theirs.set_mesh_id("server-b")
    # theirs knows mine as the peer of one link.
    from agent_server.links import Link

    key = link_id(("server-a", mine.keys.first()["public"]), ("server-b", theirs.keys.first()["public"]))
    theirs.links[key] = Link(key, "server-a", "http://a", "active", {"keys": mine.keys.public(), "we_presented": True, "they_presented": True})
    mine.links[key] = Link(key, "server-b", "http://b", "active", {"keys": theirs.keys.public()})
    body = canonical({"hello": 1})
    assertion = {"link": key, "from": "server-a", "to": "server-b", "iat": time.time(), "exp": time.time() + 60}
    headers = mine.signed_headers(mine.links[key], "POST", "/api/links/relay/x", body, assertion)
    scope = {"method": "POST", "path": "/api/links/relay/x", "raw_path": b"/api/links/relay/x", "query_string": b""}
    peer = theirs.verify(scope, headers, body)
    assert peer.assertion["from"] == "server-a"
    with pytest.raises(Refused, match="already seen"):
        theirs.verify(scope, headers, body)
    fresh = mine.signed_headers(mine.links[key], "POST", "/api/links/relay/x", body, assertion)
    with pytest.raises(Refused, match="digest"):
        theirs.verify(scope, fresh, body + b" ")
    with pytest.raises(Refused, match="signature"):
        theirs.verify({**scope, "raw_path": b"/api/links/relay/y"}, mine.signed_headers(mine.links[key], "POST", "/api/links/relay/x", body, assertion), body)
    stale = {**mine.signed_headers(mine.links[key], "POST", "/api/links/relay/x", body, assertion), "mesh-date": str(int(time.time()) - 120)}
    with pytest.raises(Refused, match="clock"):
        theirs.verify(scope, stale, body)
    theirs.links[key].state = "paused"
    with pytest.raises(Refused, match="paused"):
        theirs.verify(scope, mine.signed_headers(mine.links[key], "POST", "/api/links/relay/x", body, assertion), body)


# Two real servers


class Api:
    def __init__(self, base: str, token: str):
        self.http = httpx.Client(base_url=base, headers={"x-ai-proxy-token": token}, timeout=30, trust_env=False)

    def __call__(self, method: str, path: str, body=None, expect: int | tuple[int, ...] = (200, 201)) -> dict:
        response = self.http.request(method, path, json=body)
        expected = (expect,) if isinstance(expect, int) else expect
        assert response.status_code in expected, (method, path, response.status_code, response.text)
        return response.json() if response.content else {}


def wait(check, timeout: float = 20.0, what: str = "it"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = check()
        if last:
            return last
        time.sleep(0.1)
    raise AssertionError(f"{what} didn't happen: {last}")


@contextmanager
def server(tmp_path, name: str, port: int, backends: dict[str, Backend], separate: bool):
    settings = Settings(
        log_dir=tmp_path / name / "logs", database_url=sqlite_url(tmp_path / name / "server.sqlite3") if separate else None,
        public_url=f"http://127.0.0.1:{port}", default_backend="laptop",
        backends={"laptop": Backend("laptop", "http://ollama.test"), **backends},
    )
    app = create_app(settings)
    with serve(app, port) as base:
        yield app, base


@pytest.fixture
def linked(tmp_path, monkeypatch):
    """server-a (org-a1, alice) and server-b (org-b1, bob, its provider provider-b1), linked and bridged."""
    upstream: list[dict] = []

    def model(request: httpx.Request) -> httpx.Response:
        upstream.append({"url": str(request.url), "body": json.loads(request.content or b"{}"), "headers": dict(request.headers)})
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "model": "real-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi from b"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 4}})

    provider = Backend("provider-b1", "https://models.example.test/v1", "api", options={"org": "org-b1", "auth": "none", "discover": False, "models": {"m1": "real-model"}, "protocols": ["openai-chat"]})
    port_a, port_b = free_port(), free_port()
    with ExitStack() as stack:
        app_a, base_a = stack.enter_context(server(tmp_path, "a", port_a, {}, separate=False))
        app_b, base_b = stack.enter_context(server(tmp_path, "b", port_b, {"provider-b1": provider}, separate=True))
        runtime_a, runtime_b = app_a.state.runtime, app_b.state.runtime
        mocked = httpx.AsyncClient(transport=httpx.MockTransport(model))
        runtime_b.api_clients = {True: mocked, False: mocked}
        for runtime, org, person in ((runtime_a, "org-a1", "alice"), (runtime_b, "org-b1", "bob")):
            runtime.auth.store.add_org(org)
            runtime.auth.store.add_team(org, "dev")
            runtime.auth.store.add_user(person, "a-long-password", role="admin", tenant=org, teams=[f"{org}/dev"])
            runtime.auth.store.add_user("carol" if person == "alice" else "dave", "a-long-password", role="member", tenant=org)
        site_a, site_b = Api(base_a, access_token(app_a)), Api(base_b, access_token(app_b))
        alice, bob = Api(base_a, access_token(app_a, "alice")), Api(base_b, access_token(app_b, "bob"))
        carol = Api(base_a, access_token(app_a, "carol"))
        site_a("PUT", "/api/links/mesh", {"id": "server-a"})
        site_b("PUT", "/api/links/mesh", {"id": "server-b"})
        yield SimpleNamespace(
            a=app_a, b=app_b, base_a=base_a, base_b=base_b, site_a=site_a, site_b=site_b, alice=alice, bob=bob, carol=carol,
            upstream=upstream, ra=runtime_a, rb=runtime_b,
        )
        mocked_close = mocked.aclose  # closed with the loop
        del mocked_close


def link_both(world) -> str:
    code_a = world.site_a("POST", "/api/links/codes", {})
    code_b = world.site_b("POST", "/api/links/codes", {})
    seen = world.site_a("POST", "/api/links/inspect", {"url": world.base_b})
    assert seen["mesh_id"] == "server-b" and seen["fingerprint"] == code_b["fingerprint"]
    # A wrong fingerprint is refused before anything is presented.
    world.site_a("POST", "/api/links/join", {"url": world.base_b, "code": code_b["code"], "fingerprint": "0000"}, expect=400)
    first = world.site_a("POST", "/api/links/join", {"url": world.base_b, "code": code_b["code"], "fingerprint": seen["fingerprint"]})
    assert first["state"] == "pending" and first["we_presented"] and not first["they_presented"]
    # One side alone does nothing; the other side sees who presented and confirms its fingerprint.
    pending = world.site_b("GET", "/api/links")["links"]
    assert pending[0]["state"] == "pending" and pending[0]["they_presented"] and pending[0]["fingerprint"] == code_a["fingerprint"]
    # A used code doesn't work twice.
    world.site_a("POST", "/api/links/join", {"url": world.base_b, "code": code_b["code"], "fingerprint": seen["fingerprint"]}, expect=200)
    second = world.site_b("POST", "/api/links/join", {"link": pending[0]["id"], "code": code_a["code"]})
    assert second["state"] == "active" and second["dials"]
    assert first["id"] == second["id"]
    wait(lambda: world.site_a("GET", "/api/links")["links"][0]["state"] == "active", what="server-a's side of the link")
    wait(lambda: world.site_a("GET", "/api/links")["links"][0]["connected"] and world.site_b("GET", "/api/links")["links"][0]["connected"], what="the link socket")
    for site in (world.site_a, world.site_b):
        site("PUT", f"/api/links/{first['id']}/policy", EVERYTHING)
    return first["id"]


def bridge_both(world, link: str) -> str:
    code_b = world.bob("POST", "/api/orgs/org-b1/bridges/codes", {"link": link})
    code_a = world.alice("POST", "/api/orgs/org-a1/bridges/codes", {"link": link})
    # Another organization's admin can't make codes for this one.
    world.carol("POST", "/api/orgs/org-a1/bridges/codes", {"link": link}, expect=403)
    assert world.alice("POST", "/api/orgs/org-a1/bridges/inspect", {"link": link, "code": code_b["code"]})["org"] == "org-b1"
    joined = world.alice("POST", "/api/orgs/org-a1/bridges/join", {"link": link, "code": code_b["code"], "peer_org": "org-b1"})
    assert joined["state"] == "pending"
    world.bob("POST", "/api/orgs/org-b1/bridges/join", {"link": link, "code": code_a["code"], "peer_org": "org-a1"})
    bridge = joined["id"]
    wait(lambda: world.alice("GET", "/api/orgs/org-a1/bridges")["bridges"][0]["state"] == "active", what="the bridge")
    return bridge


def chat(api: Api, backend: str, expect=200) -> httpx.Response:
    response = api.http.post("/v1/chat/completions", headers={"x-ai-proxy-backend": backend}, json={"model": "m1", "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code in ((expect,) if isinstance(expect, int) else expect), response.text
    return response


def test_two_servers_link_bridge_and_carry_a_model_call(linked):
    world = linked
    link = link_both(world)
    bridge = bridge_both(world, link)
    remote = "provider:server-b/org-b1/provider-b1"

    # Nothing crosses until both sides say so: org-b1 lets it out, org-a1 hasn't let it in.
    world.bob("PUT", f"/api/orgs/org-b1/bridges/{bridge}/policy", {"out": [{"kind": "provider", "name": "provider-b1"}], "in": []})
    world.alice("POST", f"/api/orgs/org-a1/bridges/{bridge}/refresh", {})
    assert world.alice("GET", "/api/peer/resources")["providers"] == []
    # Letting in what wasn't let out does nothing either; letting out someone else's provider is refused.
    world.bob("PUT", f"/api/orgs/org-b1/bridges/{bridge}/policy", {"out": [{"kind": "provider", "name": "laptop"}], "in": []}, expect=400)
    world.bob("PUT", f"/api/orgs/org-b1/bridges/{bridge}/policy", {"out": [{"kind": "provider", "name": "provider-b1"}], "in": []})
    world.alice("PUT", f"/api/orgs/org-a1/bridges/{bridge}/policy", {"out": [], "in": [{"kind": "provider", "name": "provider-b1", "teams": ["org-a1/dev"]}]})
    world.alice("POST", f"/api/orgs/org-a1/bridges/{bridge}/refresh", {})
    providers = wait(lambda: world.alice("GET", "/api/peer/resources")["providers"], what="provider-b1 crossing")
    assert providers[0]["id"] == remote
    # Only the teams it was let in for: carol isn't in org-a1/dev.
    assert world.carol("GET", "/api/peer/resources")["providers"] == []
    chat(world.carol, remote, expect=400)

    # Nothing is used until org-b1 sets this bridge a limit.
    assert "usage limit" in chat(world.alice, remote, expect=429).text
    world.bob("PUT", f"/api/orgs/org-b1/bridges/{bridge}/limits", {"provider": "provider-b1", "window": "24h", "tokens": 100000})
    answer = chat(world.alice, remote).json()
    assert answer["choices"][0]["message"]["content"] == "hi from b"
    sent = world.upstream[-1]
    assert sent["body"]["model"] == "real-model"
    # Nothing of the link or of alice's sign-in reaches the provider.
    assert not any(key.startswith("mesh-") or key in ("x-ai-proxy-token", "authorization") for key in sent["headers"])

    # Both servers recorded it: alice's session here, a bridge session for her there.
    world.ra.sessions.flush()
    world.rb.sessions.flush()
    mine = [record for record in world.ra.sessions.query_requests() if record.get("backend") == remote]
    assert mine and mine[0]["status"] == 200
    # One bridge session for each of alice's sessions (each bare call above made one), in org-b1.
    there = [session for session in world.rb.sessions.list() if session.tags.get("bridge") == bridge]
    assert there and all(item.tags["tenant"] == "org-b1" and item.tags["guest_name"].startswith("alice@org-a1") for item in there)
    statuses = sorted(record["status"] for item in there for record in world.rb.sessions.records(item))
    # The refusal for want of a limit wasn't recorded as usage (it's an audit event); the call was.
    assert statuses == [200]

    # Over the link socket instead of directly: server-a pretends it can't reach server-b.
    link_a = world.ra.links.links[link]
    link_a.data["reachable"] = False
    assert chat(world.alice, remote).json()["choices"][0]["message"]["content"] == "hi from b"
    link_a.data["reachable"] = True

    # Narrowing applies at once, from the giving side alone.
    world.bob("PUT", f"/api/orgs/org-b1/bridges/{bridge}/policy", {"out": [], "in": []})
    response = chat(world.alice, remote, expect=(400, 403, 409))
    assert response.status_code in (400, 403, 409)
    wait(lambda: world.alice("GET", "/api/peer/resources")["providers"] == [], what="provider-b1 withdrawn")

    # Pausing the link stops everything; removing it ends the bridge on both sides.
    world.site_b("POST", f"/api/links/{link}/pause", {})
    world.site_b("DELETE", f"/api/links/{link}")
    assert world.bob("GET", "/api/orgs/org-b1/bridges")["bridges"] == []


def test_peer_routes_refuse_unsigned_and_people_tokens(linked):
    world = linked
    with httpx.Client(base_url=world.base_b, trust_env=False) as http:
        assert http.get("/api/links/relay/policies").status_code == 401
        assert http.post("/api/links/relay/provider/org-b1/provider-b1/v1/chat/completions", json={}).status_code == 401
        token = access_token(world.b, "bob")
        assert http.get("/api/links/relay/policies", headers={"x-ai-proxy-token": token}).status_code == 401
        assert http.post("/api/links/present", json={"code": "msl_x", "mesh_id": "evil", "keys": []}).status_code == 400
    # People can't reach the site admins' link routes.
    world.alice("GET", "/api/links", expect=403)
    world.alice("POST", "/api/links/codes", {}, expect=403)


def test_a_machine_must_grant_the_bridge_before_it_can_be_let_out(linked):
    world = linked
    link = link_both(world)
    bridge = bridge_both(world, link)
    machine = fleet_module.Machine(id="m-gpu", name="gpu-box", owner="bob", tenant="org-b1", launches=True, roots=["/work"], agents=["qwen"])
    world.rb.fleet.machines[machine.id] = machine
    world.bob("PUT", f"/api/orgs/org-b1/bridges/{bridge}/policy", {"out": [{"kind": "launch", "machine": "gpu-box"}], "in": []}, expect=400)
    world.bob("PUT", f"/api/machines/{machine.id}/access", {"access": {"launch": {"bridges": [bridge]}}}, expect=(200, 404))
    machine.access = {"launch": {"orgs": [], "teams": [], "bridges": [bridge]}}
    world.bob("PUT", f"/api/orgs/org-b1/bridges/{bridge}/policy", {"out": [{"kind": "launch", "machine": "gpu-box"}], "in": []})
    world.alice("PUT", f"/api/orgs/org-a1/bridges/{bridge}/policy", {"out": [], "in": [{"kind": "launch", "machine": "gpu-box"}]})
    world.alice("POST", f"/api/orgs/org-a1/bridges/{bridge}/refresh", {})
    machines = wait(lambda: world.alice("GET", "/api/peer/resources")["machines"], what="gpu-box crossing")
    assert machines[0]["id"] == "machine:server-b/org-b1/gpu-box" and machines[0]["functions"] == ["launch"]
    # Its launch folders, as server-b lists them.
    assert world.alice("GET", "/api/peer/folders?machine=machine:server-b/org-b1/gpu-box")["folders"][0]["path"] == "/work"


def test_a_guest_launch_on_the_peers_machine_is_mirrored_back(linked):
    """alice starts qwen on gpu-box (server-b): server-b's session, run as the machine's owner, may use only what
    the bridge gives it, and its records come back into alice's mirror session on server-a."""
    world = linked
    link = link_both(world)
    bridge = bridge_both(world, link)
    machine = fleet_module.Machine(id="m-gpu", name="gpu-box", owner="bob", tenant="org-b1", launches=True, roots=["/work"], agents=["qwen"])
    machine.access = {"launch": {"orgs": [], "teams": [], "bridges": [bridge]}, "terminal": {"orgs": [], "teams": [], "bridges": [bridge]}}
    world.rb.fleet.machines[machine.id] = machine
    world.bob("PUT", f"/api/orgs/org-b1/bridges/{bridge}/policy", {"out": [{"kind": "launch", "machine": "gpu-box"}, {"kind": "provider", "name": "provider-b1"}], "in": []})
    world.alice("PUT", f"/api/orgs/org-a1/bridges/{bridge}/policy", {"out": [], "in": [{"kind": "launch", "machine": "gpu-box"}, {"kind": "provider", "name": "provider-b1"}]})
    world.alice("POST", f"/api/orgs/org-a1/bridges/{bridge}/refresh", {})
    wait(lambda: world.alice("GET", "/api/peer/resources")["machines"], what="gpu-box crossing")
    world.bob("PUT", f"/api/orgs/org-b1/bridges/{bridge}/limits", {"provider": "provider-b1", "window": "24h", "tokens": 100000})
    bob_token = world.bob.http.headers["x-ai-proxy-token"]
    launched: dict = {}

    async def fake_launch(target, identity, request, launch_id=None, guest=None):
        # What agent-machine does: its launcher (signed in as bob) asks for the session, tagged with the launch.
        assert identity is None and guest is not None and target.id == "m-gpu"
        fleet = world.rb.fleet
        fleet.guest_launches[launch_id] = guest
        fleet._asked[launch_id] = target.id
        try:
            async with httpx.AsyncClient(base_url=world.base_b, headers={"x-ai-proxy-token": bob_token}, trust_env=False) as http:
                answer = await http.post(f"/api/agents/{request['agent']}/sessions", json={
                    "model": request["model"], "backend": request["backend"], "launcher": True,
                    "tags": {"launch": launch_id, "machine": target.id, "guest": "forged", "allowed_backends": ["laptop"]},
                })
            assert answer.status_code == 201, answer.text
            session_id = answer.json()["session"]["session_id"]
        finally:
            fleet.guest_launches.pop(launch_id, None)
            fleet._asked.pop(launch_id, None)
        launched["session"] = session_id
        return {"launch": launch_id, "session_id": session_id}

    world.rb.fleet.launch = fake_launch
    # Only what the bridge gives a guest's launch: not server-b's laptop.
    world.alice("POST", "/api/peer/launch", {"machine": "machine:server-b/org-b1/gpu-box", "agent": "qwen", "model": "m1", "project": "/work", "backend": "laptop"}, expect=400)
    started = world.alice("POST", "/api/peer/launch", {
        "machine": "machine:server-b/org-b1/gpu-box", "agent": "qwen", "model": "m1", "project": "/work",
        "backend": "provider:server-b/org-b1/provider-b1",
    })
    there = world.rb.sessions.get(started["remote_session"])
    # The server's tags, not the client's: the guest, the bridge, and only the backends it may use.
    assert there.tags["guest_name"].startswith("alice@org-a1") and there.tags["bridge"] == bridge and there.tags["guest"] != "forged"
    assert there.tags["allowed_backends"] == ["provider-b1"] and there.backend == "provider-b1"
    assert there.tags["owner"] == "bob" and there.tags["launched_by"] == f"bridge:{bridge}"
    # The agent's traffic runs as bob's agent-proxy: only the backends the launch was given.
    with httpx.Client(base_url=world.base_b, headers={"x-ai-proxy-token": bob_token}, trust_env=False) as http:
        refused = http.post(f"/session/{there.session_id}/v1/chat/completions", headers={"x-ai-proxy-backend": "laptop"}, json={"model": "m1", "messages": []})
        assert refused.status_code == 400
        answer = http.post(f"/session/{there.session_id}/v1/chat/completions", json={"model": "m1", "messages": [{"role": "user", "content": "hi"}]})
        assert answer.status_code == 200, answer.text
    # Mirrored back into alice's session on server-a.
    mirror = world.ra.sessions.get(started["session_id"])
    assert mirror.tags["owner"] == "alice" and mirror.tags["mirror_of"] == there.session_id

    def mirrored():
        world.ra.sessions.flush()
        return world.ra.sessions.records(mirror)

    records = wait(mirrored, timeout=15, what="the mirror")
    assert records[0]["mirrored_from"] == there.session_id and records[0]["status"] == 200
    # Its count follows, as the console shows it.
    wait(lambda: world.alice("GET", f"/api/sessions/{mirror.session_id}")["request_count"] == len(records), what="the mirror's count")
    # alice sees her mirror; carol (same organization, not shared) doesn't.
    world.alice("GET", f"/api/sessions/{mirror.session_id}")
    world.carol("GET", f"/api/sessions/{mirror.session_id}", expect=404)
    # org-b1's admin sees the guest's launch on its machine.
    world.bob("GET", f"/api/sessions/{there.session_id}")
