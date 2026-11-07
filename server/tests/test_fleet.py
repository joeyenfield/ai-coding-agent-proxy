"""The fleet: machines connecting, who sees them, launches and adopting their model servers.

The machine's side is played over the same WebSocket the client's agent-proxy uses
(/api/machines/connect); test_client.py runs the real client against it.
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager

import pytest
import yaml
from fastapi.testclient import TestClient

from agent_server.app import create_app
from agent_server.config import Backend
from agent_server.fleet import FleetError, within_roots
from oidc_helpers import access_token, issuer, stored_backends
from test_auth import BASE, HOST, ids, org_settings, signed_in, ws_headers


ROOT = "C:\\work"


def hello(machine_id: str = "m1", **extra) -> dict:
    return {
        "type": "hello", "id": machine_id, "name": "laptop", "os": "windows", "arch": "amd64", "version": "1.0",
        "agents": ["qwen", "claude-account"], "providers": [{"type": "ollama", "port": 11434, "models": 3}],
        "launches": True, "roots": [ROOT], **extra,
    }


@contextmanager
def machine(client: TestClient, message: dict | None = None):
    """A machine's connection, signed in as client's user."""
    with client.websocket_connect("/api/machines/connect", headers=ws_headers(client)) as socket:
        socket.send_text(json.dumps(message or hello()))
        assert json.loads(socket.receive_text())["type"] == "welcome"
        yield socket


def session_for_launch(app, request: dict, session_id: str | None = None):
    """The session the real launcher opens before reporting a successful launch."""
    return app.state.runtime.sessions.create({
        "session_id": session_id, "client": request.get("agent", "shell"), "backend": "test",
        "tags": {"owner": "alice", "tenant": "acme", "machine": "m1", "launch": request["launch"]},
    })


def test_project_folders_must_be_inside_a_root():
    assert within_roots("C:\\work\\api", ["C:\\work"]) and within_roots("c:\\WORK", ["C:\\work"])
    assert within_roots("/home/me/code/x", ["/home/me/code"])
    assert not within_roots("C:\\work\\..\\secrets", ["C:\\work"])
    assert not within_roots("C:\\workshop", ["C:\\work"])
    assert not within_roots("/home/me/codex", ["/home/me/code"])
    assert not within_roots("", ["/"]) and not within_roots("/etc", [])


def test_machines_are_seen_by_their_owner_and_admins_only(tmp_path):
    app = create_app(org_settings(tmp_path))
    alice, bob, dave = signed_in(app, "alice"), signed_in(app, "bob"), signed_in(app, "dave")
    with alice, bob, dave, machine(alice):
        [listed] = alice.get("/api/machines").json()["machines"]
        assert listed["connected"] and listed["owner"] == "alice" and listed["agents"] == ["qwen", "claude-account"]
        assert listed["you"] == {"owner": True, "manage": True, "containers": True, "view": True, "launch": True, "terminal": True, "files": True, "models": True, "servers": True, "update": True, "mcp": True}
        # The machine's address as the server sees it, for its model servers.
        assert listed["providers"][0]["url"].endswith(":11434") and listed["providers"][0]["backend"] is None
        assert bob.get("/api/machines").json()["machines"] == []
        assert bob.get("/api/machines/m1").status_code == 404
        # An admin sees it, but can't start programs on it.
        [seen] = [item for item in dave.get("/api/machines").json()["machines"] if item["id"] == "m1"]
        assert seen["you"]["launch"] is False
        assert dave.post("/api/machines/m1/launch", json={"agent": "qwen", "project": ROOT, "model": "m"}).status_code == 403
        # Another user can't take the machine's id over.
        with pytest.raises(Exception), bob.websocket_connect("/api/machines/connect", headers=ws_headers(bob)) as socket:
            socket.send_text(json.dumps(hello()))
            socket.receive_text()
    # Remembered once it disconnects, until it's forgotten.
    with signed_in(app, "alice") as again:
        [offline] = again.get("/api/machines").json()["machines"]
        assert offline["connected"] is False
        assert again.delete("/api/machines/m1").status_code == 200
        assert again.get("/api/machines").json()["machines"] == []


def test_machines_need_a_sign_in(tmp_path):
    app = create_app(org_settings(tmp_path))
    with TestClient(app, base_url=BASE) as anonymous:
        assert anonymous.get("/api/machines").status_code == 401
        with pytest.raises(Exception), anonymous.websocket_connect("/api/machines/connect", headers=HOST) as socket:
            socket.send_text(json.dumps(hello()))
            socket.receive_text()


def test_the_owner_starts_an_agent_on_a_machine_and_it_is_recorded(tmp_path):
    app = create_app(org_settings(tmp_path))
    alice = signed_in(app, "alice")
    with alice, machine(alice) as socket:
        answer: dict = {}
        thread = threading.Thread(target=lambda: answer.update(response=alice.post(
            "/api/machines/m1/launch", json={"agent": "qwen", "project": ROOT + "\\api", "model": "qwen3.6:35b", "visibility": "team", "team": "core"},
        )))
        thread.start()
        request = json.loads(socket.receive_text())
        assert request["type"] == "launch" and request["agent"] == "qwen" and request["project"] == ROOT + "\\api"
        assert (request["model"], request["visibility"], request["team"], request["access"]) == ("qwen3.6:35b", "team", "core", "view")
        session_for_launch(app, request, "s-123")
        socket.send_text(json.dumps({"type": "launched", "launch": request["launch"], "session_id": "s-123"}))
        thread.join(timeout=10)
        response = answer["response"]
        assert response.status_code == 201, response.text
        assert response.json()["session_id"] == "s-123" and response.json()["by"] == "alice"
        [launch] = alice.get("/api/machines/m1").json()["history"]
        assert launch["agent"] == "qwen" and launch["session_id"] == "s-123"

        # The machine's refusal comes back as the error.
        thread = threading.Thread(target=lambda: answer.update(response=alice.post("/api/machines/m1/launch", json={"agent": "qwen", "project": ROOT, "model": "m"})))
        thread.start()
        request = json.loads(socket.receive_text())
        socket.send_text(json.dumps({"type": "launch_failed", "launch": request["launch"], "error": "qwen isn't on PATH"}))
        thread.join(timeout=10)
        assert answer["response"].status_code == 502 and "PATH" in answer["response"].json()["detail"]

        # Checked on the server before anything reaches the machine.
        assert alice.post("/api/machines/m1/launch", json={"agent": "qwen", "project": "C:\\elsewhere", "model": "m"}).status_code == 400
        assert alice.post("/api/machines/m1/launch", json={"agent": "goose", "project": ROOT, "model": "m"}).status_code == 400
        assert alice.post("/api/machines/m1/launch", json={"agent": "qwen", "project": ROOT}).status_code == 400  # needs a model
        assert alice.post("/api/machines/m1/launch", json={"agent": "qwen", "project": ROOT, "model": "m"}, headers={"origin": "http://evil.example"}).status_code == 403


def test_a_machine_without_launches_turned_on_refuses(tmp_path):
    app = create_app(org_settings(tmp_path))
    alice = signed_in(app, "alice")
    with alice, machine(alice, hello(launches=False)):
        response = alice.post("/api/machines/m1/launch", json={"agent": "qwen", "project": ROOT, "model": "m"})
        assert response.status_code == 409 and "launches on" in response.json()["detail"]


def test_an_admin_adds_a_machines_model_server_as_a_backend(tmp_path):
    settings = org_settings(tmp_path)
    app = create_app(settings)
    alice, dave = signed_in(app, "alice"), signed_in(app, "dave")
    with alice, dave, machine(alice):
        assert alice.post("/api/machines/m1/providers/adopt", json={"type": "ollama"}).status_code == 403
        response = dave.post("/api/machines/m1/providers/adopt", json={"type": "ollama"})
        assert response.status_code == 201, response.text
        name = response.json()["backend"]
        assert name == "laptop" and settings.backends[name].type == "ollama"
        saved = stored_backends(settings)[name]
        assert saved["url"].endswith(":11434") and saved["machine"] == "m1"
        [listed] = [item for item in dave.get("/api/machines").json()["machines"] if item["id"] == "m1"]
        assert listed["providers"][0]["backend"] == "laptop" and listed["backends"] == ["laptop"]
        assert dave.post("/api/machines/m1/providers/adopt", json={"type": "ollama"}).status_code == 409
        assert dave.post("/api/machines/m1/providers/adopt", json={"type": "strata"}).status_code == 404


def test_machines_behind_one_address_each_add_their_own_model_server(tmp_path):
    # A PC and its WSL (or two computers behind a home router) reach the server from the same address,
    # so their Strata looks like the same URL from here; agents reach each through its own machine.
    settings = org_settings(tmp_path)
    app = create_app(settings)
    alice, dave = signed_in(app, "alice"), signed_in(app, "dave")
    strata = {"providers": [{"type": "strata", "port": 18500}], "address": "192.0.2.40"}
    with alice, dave, machine(alice, hello("m1", **strata)), machine(alice, {**hello("m2", **strata), "name": "laptop-wsl"}):
        first = dave.post("/api/machines/m1/providers/adopt", json={"type": "strata"})
        second = dave.post("/api/machines/m2/providers/adopt", json={"type": "strata"})
        assert first.status_code == 201 and second.status_code == 201, (first.text, second.text)
        assert first.json()["url"] == second.json()["url"] == "http://192.0.2.40:18500"
        assert settings.backends[first.json()["backend"]].machine == "m1"
        assert settings.backends[second.json()["backend"]].machine == "m2"
        assert dave.get("/api/machines/m2").json()["providers"][0]["backend"] == second.json()["backend"]
        # The same machine's server twice is still refused, and says which provider it is.
        again = dave.post("/api/machines/m1/providers/adopt", json={"type": "strata"})
        assert again.status_code == 409 and first.json()["backend"] in again.json()["detail"]


def test_legacy_backends_need_explicit_admin_adoption_with_accounts(tmp_path):
    settings = org_settings(tmp_path)
    settings.backends = {**settings.backends, "old": Backend("old", "http://localhost:11434/")}
    app = create_app(settings)
    alice, dave, sally = signed_in(app, "alice"), signed_in(app, "dave"), signed_in(app, "sally")
    # The machines are this computer, so their Ollama is http://127.0.0.1:11434: the same server.
    with alice, dave, sally, machine(alice, hello(address="127.0.0.1")) as socket:
        assert settings.backends["old"].machine is None
        assert dave.get("/api/machines/m1").json()["providers"][0]["backend"] is None
        socket.send_json({"type": "update", "address": "192.0.2.25"})
        assert alice.get("/api/machines/m1").status_code == 200
        assert settings.backends["old"].url == "http://localhost:11434/" and settings.backends["old"].machine is None
        socket.send_json({"type": "update", "address": "127.0.0.1"})
        assert alice.get("/api/machines/m1").status_code == 200
        assert alice.post("/api/machines/m1/providers/adopt", json={"type": "ollama"}).status_code == 403
        # A backend from before machines is the root's: another organization's admin can't take it onto their machine.
        assert dave.post("/api/machines/m1/providers/adopt", json={"type": "ollama"}).status_code == 409
        assert settings.backends["old"].machine is None
        # The root's own admin can, onto one of the root's machines.
        with machine(sally, hello("m2", address="127.0.0.1")):
            adopted = sally.post("/api/machines/m2/providers/adopt", json={"type": "ollama"})
            assert adopted.status_code == 201 and adopted.json()["backend"] == "old", adopted.text
            assert settings.backends["old"].machine == "m2" and settings.backends["test"].machine == "acme-box"
            assert stored_backends(settings)["old"]["machine"] == "m2"
            assert sally.get("/api/machines/m2").json()["backends"] == ["old"]
            assert dave.get("/api/machines/m2").status_code == 404


LEGACY_ACCESS_KEY = "Hb3xQ2v9mK1tW7pL0sZ4rN8cF6yJ5dA_eG-uI2oT3qE"


def test_legacy_access_key_cannot_join_fleet_without_oauth_accounts(tmp_path):
    from agent_server.config import Settings

    settings = Settings(log_dir=tmp_path / "logs", 
                        default_backend="old", backends={"old": Backend("old", "http://localhost:11434/")})
    app = create_app(settings)
    with TestClient(app, base_url=BASE) as client:
        # What an old agent-machine sent: the access key, which no longer authenticates anything.
        headers = {**HOST, "x-ai-proxy-token": LEGACY_ACCESS_KEY}
        with pytest.raises(Exception):
            with client.websocket_connect("/api/machines/connect", headers=headers) as socket:
                socket.send_json(hello(address="127.0.0.1"))
                socket.receive_json()
        assert settings.backends["old"].machine is None


def test_container_requests_go_to_the_machine_and_its_answer_comes_back(tmp_path):
    app = create_app(org_settings(tmp_path))
    alice = signed_in(app, "alice")
    capability = {"enabled": True, "cli": "podman", "found": True, "online": True, "version": "5.2",
                  "engines": [{"id": "ollama", "label": "Ollama", "backend_type": "ollama", "image": "ollama/ollama:latest", "port": 11434, "unknown": "dropped"}]}
    with alice, machine(alice, hello(containers=capability)) as socket:
        listed = alice.get("/api/machines/m1").json()
        assert listed["containers"]["cli"] == "podman" and listed["containers"]["engines"][0]["id"] == "ollama"
        assert "unknown" not in listed["containers"]["engines"][0]
        fleet = app.state.runtime.fleet
        answer: dict = {}

        def ask(op: str, **fields) -> None:
            async def call() -> None:
                try:
                    answer["value"] = await fleet.ask(fleet.machines["m1"], op, timeout=5, **fields)
                except Exception as exc:  # noqa: BLE001 - checked below
                    answer["value"] = exc
            # The server's own loop owns the socket; run the request there.
            alice.portal.call(call)

        thread = threading.Thread(target=ask, args=("logs",), kwargs={"name": "box", "key": "", "tail": 10})
        thread.start()
        request = json.loads(socket.receive_text())
        assert (request["type"], request["op"], request["name"], request["tail"]) == ("container", "logs", "box", 10)
        socket.send_text(json.dumps({"type": "container_result", "request": request["request"], "ok": True, "text": "hello"}))
        thread.join(timeout=10)
        assert answer["value"]["text"] == "hello"

        thread = threading.Thread(target=ask, args=("stop",), kwargs={"name": "box", "engine": "ollama"})
        thread.start()
        request = json.loads(socket.receive_text())
        socket.send_text(json.dumps({"type": "container_result", "request": request["request"], "ok": False, "error": "Containers are off on this machine"}))
        thread.join(timeout=10)
        assert isinstance(answer["value"], FleetError) and "off on this machine" in answer["value"].detail


def test_the_owner_starts_and_stops_the_machines_ollama_and_strata(tmp_path):
    app = create_app(org_settings(tmp_path))
    alice, dave = signed_in(app, "alice"), signed_in(app, "dave")
    servers = {"enabled": True, "ollama": {"found": True, "running": False}, "strata": {"found": True, "profiles": [{"id": "app-1", "state": "stopped"}]}}
    with alice, dave, machine(alice, hello(address="192.168.1.20", servers=servers)) as socket:
        assert alice.get("/api/machines/m1").json()["servers"]["strata"]["profiles"][0]["id"] == "app-1"
        # Starting programs on a machine is for its owner, as with launches.
        assert dave.post("/api/machines/m1/servers/ollama/start").status_code == 403
        assert alice.post("/api/machines/m1/servers/vllm/start").status_code == 404
        assert alice.post("/api/machines/m1/servers/strata/start", json={}).status_code == 400
        answer: dict = {}

        def call(path: str, body: dict | None = None) -> threading.Thread:
            thread = threading.Thread(target=lambda: answer.update(response=alice.post(path, json=body or {})))
            thread.start()
            return thread

        thread = call("/api/machines/m1/servers/ollama/start")
        request = json.loads(socket.receive_text())
        # Another machine: Ollama has to listen where the server reaches it.
        assert (request["type"], request["op"], request["kind"], request["bind"]) == ("server", "start", "ollama", "0.0.0.0")
        running = {**servers, "ollama": {"found": True, "running": True, "managed": True, "version": "0.12.3", "bind": "0.0.0.0"}}
        socket.send_text(json.dumps({"type": "server_result", "request": request["request"], "ok": True, "servers": running}))
        thread.join(timeout=10)
        assert answer["response"].status_code == 200 and answer["response"].json()["servers"]["ollama"]["managed"] is True

        thread = call("/api/machines/m1/servers/strata/stop", {"profile": "app-1"})
        request = json.loads(socket.receive_text())
        assert (request["op"], request["kind"], request["profile"]) == ("stop", "strata", "app-1")
        socket.send_text(json.dumps({"type": "server_result", "request": request["request"], "ok": False, "error": "strata runtime stop: busy", "servers": running}))
        thread.join(timeout=10)
        assert answer["response"].status_code == 409 and "busy" in answer["response"].json()["detail"]

        # Tidying Strata: remove names a profile; prune and cleanup are machine-wide; nothing else goes through.
        assert alice.post("/api/machines/m1/servers/strata/remove", json={}).status_code == 400
        assert alice.post("/api/machines/m1/servers/strata/restart", json={"profile": "app-1"}).status_code == 404
        assert alice.post("/api/machines/m1/servers/ollama/prune").status_code == 404
        assert dave.post("/api/machines/m1/servers/strata/cleanup").status_code == 403
        for path, body, expected in (
            ("remove", {"profile": "app-1"}, ("remove", "app-1")),
            ("prune", None, ("prune", None)),
            ("cleanup", None, ("cleanup", None)),
        ):
            thread = call(f"/api/machines/m1/servers/strata/{path}", body)
            request = json.loads(socket.receive_text())
            assert (request["op"], request.get("profile")) == expected and request["kind"] == "strata"
            socket.send_text(json.dumps({"type": "server_result", "request": request["request"], "ok": True, "servers": running}))
            thread.join(timeout=10)
            assert answer["response"].status_code == 200, path


def test_a_machine_without_servers_on_refuses_before_asking(tmp_path):
    app = create_app(org_settings(tmp_path))
    alice = signed_in(app, "alice")
    with alice, machine(alice, hello(servers={"enabled": False})):
        response = alice.post("/api/machines/m1/servers/ollama/start")
        assert response.status_code == 409 and "servers on" in response.json()["detail"]


def test_a_machine_says_whether_it_has_the_agent_command_for_launches(tmp_path):
    app = create_app(org_settings(tmp_path))
    alice = signed_in(app, "alice")
    with alice, machine(alice, hello(launcher=False)):
        assert alice.get("/api/machines/m1").json()["launcher"] is False
    with alice, machine(alice, hello()):
        # An older agent-machine doesn't say; the last word stands.
        assert alice.get("/api/machines/m1").json()["launcher"] is False


def test_a_machines_providers_follow_its_address(tmp_path):
    settings = org_settings(tmp_path)
    spec = {"engine": "ollama", "image": "ollama/ollama:latest", "port": 11500, "gpus": "all", "env": {}}
    settings.backends = {
        **settings.backends,
        "gamepc": Backend("gamepc", "http://109.77.112.130:11434", machine="m1"),
        "box": Backend("box", "http://109.77.112.130:11500", machine="m1", container=spec),
        "elsewhere": Backend("elsewhere", "http://10.0.0.9:11434", machine="m2"),
    }
    app = create_app(settings)
    alice = signed_in(app, "alice")
    # The machine says it's this computer (agent-machine address 127.0.0.1): its providers move there.
    with alice, machine(alice, hello(address="127.0.0.1")):
        assert settings.backends["gamepc"].url == "http://127.0.0.1:11434"
        assert settings.backends["box"].url == "http://127.0.0.1:11500" and settings.backends["box"].container == spec
        assert settings.backends["elsewhere"].url == "http://10.0.0.9:11434"
        saved = stored_backends(settings)
        assert saved["gamepc"]["url"] == "http://127.0.0.1:11434" and saved["gamepc"]["machine"] == "m1"
        # Its Ollama is the one gamepc points at, not a new one to add.
        assert alice.get("/api/machines/m1").json()["providers"][0]["backend"] == "gamepc"


def test_tunnels_only_go_to_the_machine_that_was_asked(tmp_path):
    """A machine dials back for its tunnel; an unknown id, or another user's machine, gets nothing."""
    settings = org_settings(tmp_path)
    settings.backends = {**settings.backends, "far": Backend("far", "http://203.0.113.9:11434", machine="m1")}
    app = create_app(settings)
    alice, bob = signed_in(app, "alice"), signed_in(app, "bob")
    with alice, bob, machine(alice, hello(tunnel=1, address="203.0.113.9")):
        hub = app.state.runtime.tunnels
        far = settings.backends["far"]
        assert hub.tunnelled(far)
        # A machine on this computer is reached directly, and so is one that can't tunnel.
        assert not hub.tunnelled(Backend("near", "http://127.0.0.1:11434", machine="nobody"))
        with alice.websocket_connect("/api/machines/tunnel/no-such-tunnel", headers=ws_headers(alice)) as socket:
            with pytest.raises(Exception) as closed:
                socket.receive_bytes()
        assert getattr(closed.value, "code", None) == 4404
        # Bob's machine can't take a tunnel the server asked Alice's for.
        import asyncio
        future = asyncio.new_event_loop().create_future()
        hub._pending["t-alice"] = ("m1", future)
        with bob.websocket_connect("/api/machines/tunnel/t-alice", headers=ws_headers(bob)) as socket:
            with pytest.raises(Exception):
                socket.receive_bytes()
        assert future.done() and isinstance(future.exception(), ConnectionError)
    # A machine that dropped a moment ago still tunnels (its requests wait for it); one gone longer doesn't.
    assert hub.tunnelled(far)
    app.state.runtime.fleet.machines["m1"].last_seen -= 3600
    assert not hub.tunnelled(far)


def test_a_tunnel_is_asked_for_again_once_and_renews_its_token():
    import asyncio
    from types import SimpleNamespace

    from agent_server.tunnels import TunnelHub, _pipe

    async def scenario():
        class Dying:
            async def send_text(self, text):
                raise ConnectionError("gone")

        class Answering:
            def __init__(self, hub):
                self.hub = hub

            async def send_text(self, text):
                tunnel_id = json.loads(text)["tunnel"]
                _, future = self.hub._pending[tunnel_id]
                future.set_result(("the-websocket", asyncio.Event()))

        sockets = []
        fleet = SimpleNamespace(machines={"m1": SimpleNamespace(id="m1")})

        async def wait_socket(machine):
            return sockets.pop(0) if sockets else None

        fleet.wait_socket = wait_socket
        hub = TunnelHub(fleet)
        sockets[:] = [Dying(), Answering(hub)]
        # The first ask went over a dying connection; nothing of the request was read, so it's asked again.
        first = await hub._open_tunnel("m1", 11434)
        second = await hub._open_tunnel("m1", 11434)
        assert first is None and second[0] == "the-websocket" and hub._pending == {}
        # No connection at all (and none came back): nothing to ask again.
        assert await hub._open_tunnel("m1", 11434) is False

        # The machine's text frames carry its next token; the model's bytes are binary.
        renewed = []

        async def renew(websocket, token):
            renewed.append(token)
            return True

        class Tunnel:
            def __init__(self):
                self.messages = [{"type": "websocket.receive", "text": json.dumps({"type": "token", "token": "next"})},
                                 {"type": "websocket.receive", "bytes": b"HTTP/1.1 200 OK"}, {"type": "websocket.disconnect"}]

            async def receive(self):
                return self.messages.pop(0) if self.messages else await asyncio.sleep(3600)

            async def send_bytes(self, data):
                pass

            async def close(self):
                pass

        written = []

        class Writer:
            def write(self, data):
                written.append(data)

            async def drain(self):
                pass

        reader = asyncio.StreamReader()
        await asyncio.wait_for(_pipe(reader, Writer(), Tunnel(), renew), 5)
        assert renewed == ["next"] and written == [b"HTTP/1.1 200 OK"]

    asyncio.run(scenario())


def test_the_owner_browses_folders_inside_the_launch_folders_only(tmp_path):
    app = create_app(org_settings(tmp_path))
    alice, dave = signed_in(app, "alice"), signed_in(app, "dave")
    with alice, dave, machine(alice) as socket:
        # Without a path, the launch folders themselves, from the hello: nothing to ask the machine.
        assert alice.get("/api/machines/m1/folders").json()["folders"] == [{"name": ROOT, "path": ROOT}]
        answer: dict = {}
        thread = threading.Thread(target=lambda: answer.update(response=alice.get("/api/machines/m1/folders", params={"path": ROOT})))
        thread.start()
        request = json.loads(socket.receive_text())
        assert (request["type"], request["op"], request["path"]) == ("folders", "list", ROOT)
        listed = [{"name": "api", "path": ROOT + "\\api"}, {"name": "web", "path": ROOT + "\\web"}]
        socket.send_text(json.dumps({"type": "folders_result", "request": request["request"], "ok": True, "parent": None, "folders": listed}))
        thread.join(timeout=10)
        assert answer["response"].status_code == 200, answer["response"].text
        assert answer["response"].json() == {"path": ROOT, "parent": None, "folders": listed, "truncated": False}
        # Checked on the server before anything reaches the machine.
        assert alice.get("/api/machines/m1/folders", params={"path": "C:\\Windows"}).status_code == 400
        assert alice.get("/api/machines/m1/folders", params={"path": ROOT + "\\..\\secrets"}).status_code == 400
        # Only the owner, as with launches: an admin sees the machine but not its files.
        assert dave.get("/api/machines/m1/folders", params={"path": ROOT}).status_code == 403
    with alice, machine(alice, hello(launches=False)):
        refused = alice.get("/api/machines/m1/folders", params={"path": ROOT})
        assert refused.status_code == 409 and "launches on" in refused.json()["detail"]


def test_the_owner_reads_files_only_on_a_machine_that_shares_them(tmp_path):
    app = create_app(org_settings(tmp_path))
    alice, dave = signed_in(app, "alice"), signed_in(app, "dave")
    with alice, machine(alice):
        assert alice.get("/api/machines/m1").json()["files"] is False
        refused = alice.get("/api/machines/m1/files", params={"path": ROOT})
        assert refused.status_code == 409 and "files on" in refused.json()["detail"]
    with alice, dave, machine(alice, hello(files=True, launches=False)) as socket:
        assert alice.get("/api/machines/m1").json()["files"] is True
        # Without a path, the launch folders themselves, from the hello: nothing to ask the machine.
        assert alice.get("/api/machines/m1/files").json()["entries"] == [{"name": ROOT, "path": ROOT, "kind": "folder"}]
        answer: dict = {}
        thread = threading.Thread(target=lambda: answer.update(response=alice.get("/api/machines/m1/files", params={"path": ROOT})))
        thread.start()
        request = json.loads(socket.receive_text())
        assert (request["type"], request["op"], request["path"]) == ("files", "list", ROOT)
        listed = [{"name": "api", "path": ROOT + "\\api", "kind": "folder", "modified": 1}, {"name": "a.txt", "path": ROOT + "\\a.txt", "kind": "file", "size": 2, "modified": 1}]
        socket.send_text(json.dumps({"type": "files_result", "request": request["request"], "ok": True, "parent": None, "entries": listed}))
        thread.join(timeout=10)
        assert answer["response"].json() == {"path": ROOT, "parent": None, "entries": listed, "truncated": False}

        thread = threading.Thread(target=lambda: answer.update(response=alice.get("/api/machines/m1/files/content", params={"path": ROOT + "\\a.txt"})))
        thread.start()
        request = json.loads(socket.receive_text())
        assert (request["type"], request["op"], request["path"]) == ("files", "read", ROOT + "\\a.txt")
        socket.send_text(json.dumps({"type": "files_result", "request": request["request"], "ok": True, "name": "a.txt", "size": 2, "modified": 1, "binary": False, "truncated": False, "content": "hi"}))
        thread.join(timeout=10)
        assert answer["response"].status_code == 200, answer["response"].text
        assert answer["response"].json() == {"path": ROOT + "\\a.txt", "name": "a.txt", "size": 2, "modified": 1, "binary": False, "truncated": False, "content": "hi"}

        # Checked on the server before anything reaches the machine.
        for path in ("C:\\Windows\\win.ini", ROOT + "\\..\\secrets.txt"):
            assert alice.get("/api/machines/m1/files/content", params={"path": path}).status_code == 400, path
        assert alice.get("/api/machines/m1/files/content").status_code == 400
        # Only the owner (and whom it grants files to): an admin sees the machine but not its files.
        assert dave.get("/api/machines/m1/files", params={"path": ROOT}).status_code == 403
        assert dave.get("/api/machines/m1/files/content", params={"path": ROOT + "\\a.txt"}).status_code == 403
        assert dave.get("/api/machines/m1").json()["you"]["files"] is False


def test_the_owner_opens_a_shell_only_on_a_machine_that_takes_commands(tmp_path):
    app = create_app(org_settings(tmp_path))
    alice, dave = signed_in(app, "alice"), signed_in(app, "dave")
    with alice, dave, machine(alice) as socket:
        assert alice.get("/api/machines/m1").json()["commands"] is False
        refused = alice.post("/api/machines/m1/shell", json={"project": ROOT})
        assert refused.status_code == 409 and "commands on" in refused.json()["detail"]
    with alice, dave, machine(alice, hello(commands=True, launches=False)) as socket:
        assert alice.get("/api/machines/m1").json()["commands"] is True
        assert dave.post("/api/machines/m1/shell", json={"project": ROOT}).status_code == 403
        assert alice.post("/api/machines/m1/shell", json={"project": "C:\\Windows"}).status_code == 400
        assert alice.post("/api/machines/m1/shell", json={"project": ROOT}, headers={"origin": "http://evil.example"}).status_code == 403
        answer: dict = {}
        thread = threading.Thread(target=lambda: answer.update(response=alice.post("/api/machines/m1/shell", json={})))
        thread.start()
        request = json.loads(socket.receive_text())
        # No folder named: the first launch folder.
        assert request == {"type": "shell", "launch": request["launch"], "project": ROOT}
        session_for_launch(app, request, "s-shell")
        socket.send_text(json.dumps({"type": "launched", "launch": request["launch"], "session_id": "s-shell"}))
        thread.join(timeout=10)
        assert answer["response"].status_code == 201, answer["response"].text
        [record] = alice.get("/api/machines/m1").json()["history"]
        assert (record["agent"], record["session_id"], record["by"]) == ("shell", "s-shell", "alice")


def test_a_shells_session_stays_private(tmp_path):
    app = create_app(org_settings(tmp_path))
    alice = signed_in(app, "alice")
    with alice:
        created = alice.post("/api/sessions", json={"client": "shell", "backend": "direct", "visibility": "team", "team": "acme/core", "access": "control"})
        assert created.status_code == 201, created.text
        session = app.state.runtime.sessions.get(created.json()["session_id"])
        assert (session.tags["visibility"], session.tags["access"]) == ("private", "view")
        widened = alice.patch(f"/api/sessions/{session.session_id}", json={"visibility": "org"})
        assert widened.status_code == 400 and "private" in widened.json()["detail"]


def test_the_owner_or_an_admin_updates_a_machine_that_takes_updates(tmp_path):
    app = create_app(org_settings(tmp_path))
    alice, bob, dave = signed_in(app, "alice"), signed_in(app, "bob"), signed_in(app, "dave")
    with alice, bob, dave, machine(alice):
        # Off until the machine allows it, whoever asks: an admin gets as far as that (not a 403).
        for client in (alice, dave):
            refused = client.post("/api/machines/m1/update", json={})
            assert refused.status_code == 409 and "updates on" in refused.json()["detail"]
    with alice, bob, dave, machine(alice, hello(updates=True)) as socket:
        assert alice.get("/api/machines/m1").json()["updates"] is True
        # An admin may (it's only ever the server's own build); another user can't even see it.
        assert dave.get("/api/machines/m1").json()["you"] == {"owner": False, "manage": True, "containers": False, "view": True, "launch": False, "terminal": False, "files": False, "models": True, "servers": False, "update": True, "mcp": False}
        assert bob.post("/api/machines/m1/update", json={}).status_code == 404
        assert alice.post("/api/machines/m1/update", json={}, headers={"origin": "http://evil.example"}).status_code == 403
        # The round trip from the owner's client: each test client runs the app on its own loop, and the socket is alice's.
        answer: dict = {}
        thread = threading.Thread(target=lambda: answer.update(response=alice.post("/api/machines/m1/update", json={"agent": True})))
        thread.start()
        request = json.loads(socket.receive_text())
        assert (request["type"], request["op"], request["agent"]) == ("update", "update", True)
        socket.send_text(json.dumps({"type": "update_result", "request": request["request"], "ok": True, "updating": True, "from": "1.0", "to": "1.1"}))
        thread.join(timeout=10)
        assert answer["response"].status_code == 200, answer["response"].text
        assert answer["response"].json() == {"updating": True, "from": "1.0", "to": "1.1", "message": None}


def test_the_owner_lets_teams_use_each_feature_of_a_machine(tmp_path):
    app = create_app(org_settings(tmp_path))
    alice, bob, carol, frank = (signed_in(app, name) for name in ("alice", "bob", "carol", "frank"))
    with alice, bob, carol, frank, machine(alice, hello(commands=True)) as socket:
        # Nobody else sees it until it's shared.
        assert bob.get("/api/machines").json()["machines"] == []
        # Carol's team may start agents; everyone in acme may see it.
        saved = alice.put("/api/machines/m1/access", json={"access": {"launch": {"teams": ["acme/ops"]}, "view": {"orgs": ["acme"]}}})
        assert saved.status_code == 400 and "aren't in the team acme/ops" in saved.json()["detail"]
        saved = alice.put("/api/machines/m1/access", json={"access": {"launch": {"teams": ["acme/core"]}, "view": {"orgs": ["acme"]}, "terminal": {}}})
        assert saved.status_code == 200, saved.text
        assert saved.json()["access"] == {"launch": {"orgs": [], "teams": ["acme/core"]}, "view": {"orgs": ["acme"], "teams": []}}
        assert alice.put("/api/machines/m1/access", json={"access": {"launch": {"teams": ["acme/nope"]}}}).status_code == 400
        assert alice.put("/api/machines/m1/access", json={"access": {"rm-rf": {"orgs": ["acme"]}}}).status_code == 400
        # Bob (acme/core) may start agents; Carol (acme/ops, so acme) may only look; Frank (globex) sees nothing.
        assert bob.get("/api/machines/m1").json()["you"]["launch"] is True
        seen = carol.get("/api/machines/m1").json()
        assert seen["you"]["launch"] is False and seen["you"]["view"] is True and seen["access"] == {}
        assert frank.get("/api/machines/m1").status_code == 404
        assert carol.post("/api/machines/m1/launch", json={"agent": "qwen", "project": ROOT, "model": "m"}).status_code == 403
        assert bob.post("/api/machines/m1/shell", json={"project": ROOT}).status_code == 403
        # Only the owner (or an admin) changes who can use it.
        assert bob.put("/api/machines/m1/access", json={"access": {}}).status_code == 403
        # Bob's launch runs under the machine's sign-in (alice's), so the session it opens is hers; he gets it too.
        # (His request goes through alice's test client with his cookie: each client runs the app on its own
        # loop, and the machine's socket is on hers.)
        as_bob = {"cookie": f"ai_proxy_session={bob.cookies.get('ai_proxy_session')}"}
        alice.cookies.clear()
        answer: dict = {}
        thread = threading.Thread(target=lambda: answer.update(response=alice.post("/api/machines/m1/launch", json={"agent": "qwen", "project": ROOT, "model": "m"}, headers=as_bob)))
        thread.start()
        request = json.loads(socket.receive_text())
        session = session_for_launch(app, request)
        assert session.session_id not in ids(bob)
        socket.send_text(json.dumps({"type": "launched", "launch": request["launch"], "session_id": session.session_id}))
        thread.join(timeout=10)
        assert answer["response"].status_code == 201, answer["response"].text
        assert answer["response"].json()["by"] == "bob" and session.tags["launched_by"] == "bob"
        assert session.session_id in ids(bob)
        assert bob.patch(f"/api/sessions/{session.session_id}", json={"ended": True}).status_code == 200
        # A client can't claim a session that way itself.
        from agent_server import access
        assert "launched_by" not in access.client_tags({"launched_by": "bob", "x": 1})


def test_an_admin_grants_any_team_but_starts_nothing_without_a_grant(tmp_path):
    app = create_app(org_settings(tmp_path))
    alice, dave, carol, frank = signed_in(app, "alice"), signed_in(app, "dave"), signed_in(app, "carol"), signed_in(app, "frank")
    with alice, dave, carol, frank, machine(alice):
        assert dave.get("/api/machines/m1").json()["you"]["launch"] is False
        # Never to another organization.
        assert dave.put("/api/machines/m1/access", json={"access": {"models": {"orgs": ["globex"]}}}).status_code == 400
        saved = dave.put("/api/machines/m1/access", json={"access": {"models": {"teams": ["acme/ops"]}}})
        assert saved.status_code == 200, saved.text
        assert carol.get("/api/machines/m1").json()["you"]["models"] is True
        assert carol.post("/api/machines/m1/servers/ollama/start").status_code == 403
        assert frank.get("/api/machines/m1").status_code == 404


@pytest.mark.parametrize("wrong", ["owner", "machine", "launch", "client", "missing"])
def test_a_launch_result_cannot_grant_an_unrelated_session(tmp_path, wrong):
    app = create_app(org_settings(tmp_path))
    alice, bob = signed_in(app, "alice"), signed_in(app, "bob")
    with alice, bob, machine(alice) as socket:
        alice.put("/api/machines/m1/access", json={"access": {"launch": {"teams": ["acme/core"]}}})
        as_bob = {"cookie": f"ai_proxy_session={bob.cookies.get('ai_proxy_session')}"}
        answer: dict = {}
        thread = threading.Thread(target=lambda: answer.update(response=alice.post(
            "/api/machines/m1/launch", headers=as_bob, json={"agent": "qwen", "model": "m", "project": ROOT},
        )))
        thread.start()
        request = socket.receive_json()
        session = session_for_launch(app, request)
        if wrong == "client":
            session.client = "shell"
        elif wrong != "missing":
            session.tags[wrong] = "carol" if wrong == "owner" else "unrelated"
        socket.send_json({"type": "launched", "launch": request["launch"],
                          "session_id": "missing" if wrong == "missing" else session.session_id})
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert answer["response"].status_code == 502, answer["response"].text
        assert "launched_by" not in session.tags
        assert bob.get(f"/api/sessions/{session.session_id}").status_code == 404
        [record] = app.state.runtime.fleet.machines["m1"].history
        assert record["session_id"] is None and record["error"]


def test_launch_replies_only_resolve_requests_sent_to_that_machine(tmp_path):
    app = create_app(org_settings(tmp_path))
    alice = signed_in(app, "alice")
    with alice, machine(alice) as socket, machine(alice, hello("m2")):
        answer: dict = {}
        thread = threading.Thread(target=lambda: answer.update(response=alice.post(
            "/api/machines/m1/launch", json={"agent": "qwen", "model": "m", "project": ROOT},
        )))
        thread.start()
        request = socket.receive_json()
        fleet = app.state.runtime.fleet
        # A different machine must not consume the pending request, even with its id.
        fleet.message(fleet.machines["m2"], {"type": "launch_failed", "launch": request["launch"], "error": "forged"}, "")
        assert request["launch"] in fleet._waiting and not fleet._waiting[request["launch"]].done()
        session = session_for_launch(app, request)
        socket.send_json({"type": "launched", "launch": request["launch"], "session_id": session.session_id})
        thread.join(timeout=10)
        assert not thread.is_alive() and answer["response"].status_code == 201


def test_machine_history_respects_private_session_access(tmp_path):
    app = create_app(org_settings(tmp_path))
    alice, bob, admin = (signed_in(app, name) for name in ("alice", "bob", "dave"))
    with alice, bob, admin, machine(alice):
        alice.put("/api/machines/m1/access", json={"access": {"view": {"orgs": ["acme"]}}})
        private = alice.post("/api/sessions", json={"client": "qwen", "backend": "test"}).json()["session_id"]
        target = app.state.runtime.fleet.machines["m1"]
        target.history = [{"session_id": private, "project": "private-project", "by": "alice"},
                          {"session_id": None, "project": "failed-project", "by": "alice", "error": "failed"}]
        assert len(alice.get("/api/machines/m1").json()["history"]) == 2
        for viewer in (bob, admin):
            assert viewer.get(f"/api/sessions/{private}").status_code == 404
            assert viewer.get("/api/machines/m1").json()["history"] == []
        alice.patch(f"/api/sessions/{private}", json={"visibility": "team", "team": "core"})
        assert [record["session_id"] for record in bob.get("/api/machines/m1").json()["history"]] == [private]


def test_a_removed_machine_is_disconnected_and_refused_until_allowed_back(tmp_path):
    app = create_app(org_settings(tmp_path))
    alice, bob = signed_in(app, "alice"), signed_in(app, "bob")
    with alice, bob:
        with machine(alice) as socket:
            assert bob.delete("/api/machines/m1").status_code == 404
            assert alice.delete("/api/machines/m1", headers={"origin": "http://evil.example"}).status_code == 403
            assert alice.delete("/api/machines/m1").status_code == 200
            # Its connection is closed, connected or not.
            with pytest.raises(Exception) as closed:
                socket.receive_text()
            assert getattr(closed.value, "code", None) == 4403
        listed = alice.get("/api/machines").json()
        assert listed["machines"] == [] and [item["id"] for item in listed["removed"]] == ["m1"]
        assert bob.get("/api/machines").json()["removed"] == []
        # It can't come back by itself.
        with pytest.raises(Exception), alice.websocket_connect("/api/machines/connect", headers=ws_headers(alice)) as again:
            again.send_text(json.dumps(hello()))
            again.receive_text()
        assert bob.post("/api/machines/m1/restore").status_code == 404
        assert alice.post("/api/machines/m1/restore").status_code == 200
        with machine(alice):
            assert alice.get("/api/machines").json()["machines"][0]["id"] == "m1"


def test_a_machine_renews_its_token_over_its_connection_and_stays_connected(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_server.auth.CREDENTIAL_CHECK_INTERVAL", 0.05)
    app = create_app(org_settings(tmp_path))
    fleet = app.state.runtime.fleet
    alice = signed_in(app, "alice")
    first = alice.cookies.get("ai_proxy_session")
    with alice, machine(alice) as socket:
        # Another user's token doesn't renew alice's machine; nor does nonsense.
        socket.send_text(json.dumps({"type": "token", "token": access_token(app, "bob")}))
        socket.send_text(json.dumps({"type": "token", "token": "not-a-token"}))
        renewed = access_token(app, "alice")
        socket.send_text(json.dumps({"type": "token", "token": renewed}))
        socket.send_text(json.dumps({"type": "ping"}))
        # The token it connected with expires: the connection carries on with the renewed one.
        issuer(app).revoke(first)
        time.sleep(0.4)
        assert fleet.connected("m1")
        # Once that one stops working too, the server closes it for a fresh token.
        issuer(app).revoke(renewed)
        with pytest.raises(Exception) as closed:
            for _ in range(100):
                socket.receive_text()
        assert getattr(closed.value, "code", None) == 4410


def test_a_request_waits_a_moment_for_a_machine_that_just_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_server.fleet.RECONNECT_WAIT", 5.0)
    app = create_app(org_settings(tmp_path))
    fleet = app.state.runtime.fleet
    alice = signed_in(app, "alice")
    with alice:
        with machine(alice):
            pass
        assert not fleet.connected("m1")

        def reconnect():
            time.sleep(0.5)
            with machine(alice, hello(servers={"enabled": True})) as socket:
                request = json.loads(socket.receive_text())
                assert request["type"] == "server"
                socket.send_text(json.dumps({"type": "server_result", "request": request["request"], "ok": True, "servers": {"enabled": True}}))
                time.sleep(0.3)

        thread = threading.Thread(target=reconnect)
        thread.start()
        try:
            result = alice.post("/api/machines/m1/servers/ollama/start")
        finally:
            thread.join()
        assert result.status_code == 200, result.text
        # One gone for longer is refused straight away.
        fleet.machines["m1"].last_seen -= 3600
        started = time.monotonic()
        assert alice.post("/api/machines/m1/servers/ollama/start").status_code == 409
        assert time.monotonic() - started < 2
