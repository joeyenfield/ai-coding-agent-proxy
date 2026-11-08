"""Several processes as one server (cluster.py, roles.py, internal.py).

Two apps share one database and one Redis (fakeredis, in this process), each with its own internal
port on loopback, as two pods would. Requests go to whichever one: what one process learns (a
session, a machine, a sharing change, a launch grant, a usage count) the other one finds.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from agent_server import roles
from agent_server.app import create_app
from agent_server.cluster import LocalCluster, RedisCluster
from agent_server.config import Backend, Settings
from oidc_helpers import access_token, issuer
from test_auth import BASE, HOST

TOKEN = "internal-test-token-0123456789"


def eventually(check, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("never happened")
        time.sleep(0.05)


# The shared store and the roles

def test_local_and_redis_clusters_keep_keys_counters_and_leases_alike():
    for cluster in (LocalCluster("a"), RedisCluster(f"fakeredis://{uuid.uuid4()}", "a", TOKEN)):
        assert cluster.set("k", "v", ttl=60) and cluster.get("k") == "v"
        assert not cluster.set("k", "other", nx=True) and cluster.get("k") == "v"
        assert cluster.getdel("k") == "v" and cluster.get("k") is None
        assert cluster.incr("n") == 1 and cluster.incr("n", 4) == 5
        assert cluster.hincrby("h", "1", 3) == 3 and cluster.hgetall("h") == {"1": "3"}
        cluster.hset("h", {"2": "7"})
        assert cluster.hgetall("h") == {"1": "3", "2": "7"}
        assert set(cluster.keys("h")) == {"h"}
        assert cluster.leader("job") and cluster.leader("job")
        cluster.set("short", "x", ttl=0.05)
        time.sleep(0.1)
        assert cluster.get("short") is None


def test_a_lease_has_one_holder_and_seals_open_only_with_the_token():
    url = f"fakeredis://{uuid.uuid4()}"
    first, second = RedisCluster(url, "a", TOKEN), RedisCluster(url, "b", TOKEN)
    assert first.leader("retention") and not second.leader("retention")
    sealed = first.seal({"keys": {"18500": "secret"}})
    assert "secret" not in sealed and second.unseal(sealed) == {"keys": {"18500": "secret"}}
    assert RedisCluster(url, "c", "another-token-0123456789").unseal(sealed) is None
    assert first.authentic(TOKEN) and not first.authentic("wrong") and not LocalCluster("x").authentic("")


def test_roles_split_the_routes_and_need_shared_state():
    assert roles.parse(None) == roles.EVERY and roles.parse("gateway, mcp") == {"gateway", "mcp"}
    with pytest.raises(roles.RoleError):
        roles.parse("everything")
    roles.check(roles.EVERY, 1, scaled=False)
    with pytest.raises(roles.RoleError):
        roles.check(frozenset({"api"}), 1, scaled=False)
    with pytest.raises(roles.RoleError):
        roles.check(roles.EVERY, 2, scaled=False)
    roles.check(frozenset({"api"}), 4, scaled=True)
    assert roles.role_of("/api/machines/connect", "machines") == "fleet"
    assert roles.role_of("/api/machines/tunnel/{tunnel_id}", "machines") == "fleet"
    assert roles.role_of("/api/machines/{machine_id}", "machines") == "api"
    assert roles.role_of("/api/live/{session_id}/{request_id}", "management") == "realtime"
    assert roles.role_of("/api/mcp", "mcp") == "api" and roles.role_of("/session/{id}/mcp/{m}/{n}", "mcp") == "mcp"
    assert roles.keep("/session/{id}/mcp/{m}/{n}", "mcp", {"gateway"}) and not roles.keep("/v1/{path}", "compat", {"api"})
    # Agents' bare native Ollama calls arrive with the console's /api/* at the api role.
    assert roles.keep("/api/{path:path}", "compat", {"api"})
    assert roles.keep("/health", "management", {"fleet"}) and roles.keep("/ready", "app", {"worker"})


def test_the_charts_routes_send_every_path_role_to_its_role():
    # A path roles.py gives to a role other than api must have its own route in the chart: anything
    # unlisted falls to the api Deployment, which drops that route in scaled mode (a 404 there).
    from pathlib import Path
    import re

    helpers = (Path(__file__).resolve().parents[2] / "deploy" / "helm" / "agent-mesh" / "templates" / "_helpers.tpl").read_text(encoding="utf-8")
    table = helpers[helpers.index('define "agent-mesh.routeTable"'):]
    table = table[: table.index("{{- end -}}\n\n")]
    routes = {(fn, path.rstrip("/")) for fn, path in re.findall(r"\{fn: (\w+), type: \w+, path: ([^,}\s]+)", table)}
    for prefix, role in roles.PATH_ROLES:
        assert (role, prefix.rstrip("/")) in routes, f"add {prefix} (role {role}) to agent-mesh.routeTable"


def test_a_role_serves_only_its_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PROXY_ROLES", "fleet")
    cluster = RedisCluster(f"fakeredis://{uuid.uuid4()}", "fleet-1", TOKEN)
    app = create_app(Settings(log_dir=tmp_path / "logs"), cluster=cluster)
    paths = {getattr(route, "path", "") for route in app.router.routes}
    assert "/api/machines/connect" in paths and "/health" in paths and "/ready" in paths
    assert "/api/sessions" not in paths and "/v1/{path:path}" not in paths and "/api/terminals/{session_id}/view" not in paths
    monkeypatch.delenv("AI_PROXY_ROLES")
    with pytest.raises(roles.RoleError):
        monkeypatch.setenv("AI_PROXY_ROLES", "api")
        create_app(Settings(log_dir=tmp_path / "other"))


def test_the_internal_paths_are_never_served_on_the_public_port(tmp_path):
    app = create_app(Settings(log_dir=tmp_path / "logs"))
    with TestClient(app) as client:
        assert client.post("/internal/fleet/m1/ask", json={}).status_code == 404
        ready = client.get("/ready")
        assert ready.status_code == 200 and ready.json()["checks"]["database"]["ok"]


# Two processes

class Pods:
    """Two apps sharing a database and a Redis, each with its own internal port: pods a and b."""

    def __init__(self, tmp_path, stack: ExitStack):
        url = f"fakeredis://{uuid.uuid4()}"
        self.apps = {}
        self.clients = {}
        tokens = None
        for name in ("a", "b"):
            settings = Settings(log_dir=tmp_path / "logs", default_backend="desk", backends={"desk": Backend("desk", "http://10.0.0.5:11434", machine="m1")})
            app = create_app(settings, cluster=RedisCluster(url, f"pod-{name}", TOKEN), internal_port=0)
            store = app.state.runtime.auth.store
            if name == "a":
                store.add_org("acme")
                store.add_user("alice", "alice-password", role="admin", tenant="acme")
                store.add_user("bob", "bob-password", role="member", tenant="acme")
            # One issuer for both: a token it gave out is good on either process.
            if tokens is None:
                tokens = issuer(app).tokens
            else:
                issuer(app).tokens = tokens
            self.apps[name] = app
            self.clients[name] = stack.enter_context(TestClient(app, base_url=BASE))
        self.alice = access_token(self.apps["a"], "alice")
        self.bob = access_token(self.apps["a"], "bob")
        # Each hears the other's heartbeat.
        eventually(lambda: all(len(app.state.runtime.cluster.pods()) == 2 for app in self.apps.values()))

    def runtime(self, name: str):
        return self.apps[name].state.runtime

    def headers(self, token: str) -> dict[str, str]:
        return {**HOST, "x-ai-proxy-token": token}


@pytest.fixture
def pods(tmp_path):
    with ExitStack() as stack:
        yield Pods(tmp_path, stack)


def test_a_session_made_on_one_process_is_found_and_counted_on_the_other(pods):
    a, b = pods.runtime("a"), pods.runtime("b")
    session = a.sessions.create({"client": "qwen", "backend": "desk", "tags": {"owner": "alice", "tenant": "acme", "visibility": "private"}})
    found = b.sessions.get(session.session_id)
    assert found is not None and found.tags["owner"] == "alice"

    async def numbers():
        ids = []
        for store in (a.sessions, b.sessions, a.sessions, b.sessions):
            ids.append(await store.next_request_id(store.get(session.session_id)))
        return ids

    ids = asyncio.run(numbers())
    assert ids == ["000001", "000002", "000003", "000004"]
    # The other process holds the same object a streaming request or a terminal viewer would: sharing narrows there at once.
    held = b.sessions.get(session.session_id)
    a.sessions.update_tags(a.sessions.get(session.session_id), {"visibility": "org", "access": "view"})
    eventually(lambda: held.tags.get("visibility") == "org")
    # A counter written by one process never lowers what the other recorded.
    b.sessions.update_tags(held, {"team": None})
    a.db.writer.flush()
    b.db.writer.flush()
    fresh = a.sessions._fetch(session.session_id)
    assert fresh.request_sequence == 4 and fresh.tags["visibility"] == "org"


def test_a_machine_on_one_process_is_asked_from_the_other(pods):
    from test_fleet import hello

    alice_a = pods.clients["a"]
    with alice_a.websocket_connect("/api/machines/connect", headers=pods.headers(pods.alice)) as socket:
        socket.send_text(json.dumps(hello(providers=[{"type": "strata", "port": 18500, "api_key": "strata-secret"}])))
        assert json.loads(socket.receive_text())["type"] == "welcome"
        b = pods.runtime("b")
        # The other process knows it's connected, and has its Strata key (sent sealed, never in the clear).
        eventually(lambda: b.fleet.connected("m1"))
        assert b.fleet.key("m1", 18500) == "strata-secret"
        answer: dict = {}

        def ask_from_b():
            answer["response"] = pods.clients["b"].get("/api/machines/m1/folders", params={"path": "C:\\work"}, headers=pods.headers(pods.alice))

        thread = threading.Thread(target=ask_from_b)
        thread.start()
        request = json.loads(socket.receive_text())
        assert request["type"] == "folders" and request["op"] == "list"
        socket.send_text(json.dumps({"type": "folders_result", "request": request["request"], "ok": True, "folders": ["api", "web"], "path": "C:\\work"}))
        thread.join(10)
        assert answer["response"].status_code == 200 and answer["response"].json()["folders"] == ["api", "web"]
    # Gone from both once it disconnects.
    eventually(lambda: not pods.runtime("b").fleet.connected("m1"))


def test_a_launch_grant_is_handed_over_once_whichever_process_asks(pods):
    from agent_server.provider_accounts import Grant

    a, b = pods.runtime("a"), pods.runtime("b")
    a.provider_accounts.grants["launch-1"] = Grant("launch-1", "acct", "m1", "alice", time.time() + 60)
    grant = b.provider_accounts.grants.get("launch-1")
    assert grant is not None and grant.account_id == "acct" and not grant.delivered
    assert b.provider_accounts.grants.claim(grant) and not a.provider_accounts.grants.claim(grant)
    assert a.provider_accounts.grants.get("launch-1").delivered
    a.provider_accounts.grants.pop("launch-1")
    assert b.provider_accounts.grants.get("launch-1") is None


def test_usage_on_both_processes_counts_against_one_limit(pods):
    a, b = pods.runtime("a"), pods.runtime("b")
    session = a.sessions.create({"client": "qwen", "backend": "desk", "tags": {"owner": "bob", "tenant": "acme"}})
    a.usage.set_limit("acme", "bob", "backend:desk", "24h", 100, "alice")
    eventually(lambda: ("acme", "bob", "backend:desk", "24h") in b.usage.limits)
    record = {"model": "m", "backend": "desk", "kind": "api", "input_tokens": 40, "output_tokens": 20}
    a.usage.observe(session, record)
    assert b.usage.blocked("acme", "bob", "backend:desk") is None
    b.usage.observe(b.sessions.get(session.session_id), record)
    b.usage._shared.clear()
    assert b.usage.blocked("acme", "bob", "backend:desk")["used"] == 120
    a.usage._shared.clear()
    assert a.usage.blocked("acme", "bob", "backend:desk") is not None


def test_settings_changed_on_one_process_apply_on_the_other(pods):
    a, b = pods.runtime("a"), pods.runtime("b")
    a.settings.set_retention(9)
    eventually(lambda: b.settings.retention_days == 9)


def test_a_terminal_viewer_on_the_other_process_watches_and_types(pods):
    a = pods.runtime("a")
    session = a.sessions.create({"client": "claude", "backend": "desk", "tags": {"owner": "alice", "tenant": "acme", "visibility": "private"}})
    with pods.clients["a"].websocket_connect(f"/api/terminals/{session.session_id}/host", headers=pods.headers(pods.alice)) as host:
        host.send_text(json.dumps({"cols": 80, "rows": 24, "command": "claude"}))
        assert json.loads(host.receive_text())["type"] == "resume"
        host.send_bytes(b"hello from pod a\r\n")
        eventually(lambda: pods.runtime("b").terminals.host_of(session.session_id) == "pod-a")
        with pods.clients["b"].websocket_connect(f"/api/terminals/{session.session_id}/view", headers=pods.headers(pods.alice)) as viewer:
            received = b""
            while b"hello from pod a" not in received:
                message = viewer.receive()
                if message.get("bytes"):
                    received += message["bytes"]
            viewer.send_text(json.dumps({"type": "input", "text": "y"}))
            typed = json.loads(host.receive_text())
            assert typed["type"] == "input" and "y" in "".join(typed["parts"])
        # Someone it isn't shared with is refused there too.
        with pytest.raises(Exception):
            with pods.clients["b"].websocket_connect(f"/api/terminals/{session.session_id}/view", headers=pods.headers(pods.bob)) as viewer:
                viewer.receive_text()
