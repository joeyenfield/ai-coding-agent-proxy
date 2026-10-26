"""The fleet: machines connecting, who sees them, launches and adopting their model servers.

The machine's side is played over the same WebSocket the client's agent-proxy uses
(/api/machines/connect); test_client.py runs the real client against it.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager

import pytest
import yaml
from fastapi.testclient import TestClient

from agent_server.app import create_app
from agent_server.config import Backend
from agent_server.fleet import FleetError, within_roots
from test_auth import BASE, HOST, org_settings, signed_in, ws_headers


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
        assert listed["you"] == {"owner": True, "launch": True}
        # The machine's address as the server sees it, for its model servers.
        assert listed["providers"][0]["url"].endswith(":11434") and listed["providers"][0]["backend"] is None
        assert bob.get("/api/machines").json()["machines"] == []
        assert bob.get("/api/machines/m1").status_code == 404
        # An admin sees it, but can't start programs on it.
        [seen] = dave.get("/api/machines").json()["machines"]
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
        saved = yaml.safe_load(settings.backends_file.read_text())["backends"][name]
        assert saved["url"].endswith(":11434") and saved["machine"] == "m1"
        [listed] = dave.get("/api/machines").json()["machines"]
        assert listed["providers"][0]["backend"] == "laptop" and listed["backends"] == ["laptop"]
        assert dave.post("/api/machines/m1/providers/adopt", json={"type": "ollama"}).status_code == 409
        assert dave.post("/api/machines/m1/providers/adopt", json={"type": "strata"}).status_code == 404


def test_backends_from_before_machines_join_the_machine_that_runs_them(tmp_path):
    settings = org_settings(tmp_path)
    settings.backends = {**settings.backends, "old": Backend("old", "http://localhost:11434/")}
    settings.backends_file.write_text(yaml.safe_dump({"default_backend": "test", "backends": {name: backend.public() for name, backend in settings.backends.items()}}))
    app = create_app(settings)
    alice = signed_in(app, "alice")
    # The machine is this computer, so its Ollama is http://127.0.0.1:11434: the same server.
    with alice, machine(alice, hello(address="127.0.0.1")):
        assert settings.backends["old"].machine == "m1" and settings.backends["test"].machine is None
        assert yaml.safe_load(settings.backends_file.read_text())["backends"]["old"]["machine"] == "m1"
        assert alice.get("/api/machines/m1").json()["backends"] == ["old"]


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
    settings.backends_file.write_text(yaml.safe_dump({"default_backend": "test", "backends": {name: backend.public() for name, backend in settings.backends.items()}}))
    app = create_app(settings)
    alice = signed_in(app, "alice")
    # The machine says it's this computer (agent-machine address 127.0.0.1): its providers move there.
    with alice, machine(alice, hello(address="127.0.0.1")):
        assert settings.backends["gamepc"].url == "http://127.0.0.1:11434"
        assert settings.backends["box"].url == "http://127.0.0.1:11500" and settings.backends["box"].container == spec
        assert settings.backends["elsewhere"].url == "http://10.0.0.9:11434"
        saved = yaml.safe_load(settings.backends_file.read_text())["backends"]
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
