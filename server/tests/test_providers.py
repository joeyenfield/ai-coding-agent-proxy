"""LLM providers are managed through the machine that runs them, never by the server itself.

The machine is played by FakeSocket, put in the fleet as the machine's connection: it
answers each request Fleet.ask sends the way agent-machine does (machine/internal/providers),
so the real relay runs, pull progress included. The server's HTTP client fails the test
if anything but agents' traffic reaches a model server. test_client.py runs the real
agent-machine.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
from oidc_helpers import authenticated_client as TestClient

from agent_server.app import create_app
from agent_server.config import Backend, Settings
from test_auth import org_settings, signed_in


LOCAL = {"base_url": "http://127.0.0.1:8181", "client": ("127.0.0.1", 50000)}
OLLAMA_STATE = {"online": True, "version": "0.12.3", "models": [
    {"name": "qwen3:8b", "size": 5, "loaded": True, "context_length": 32768, "size_vram": 5, "gpu_percent": 100},
    {"name": "gemma3:4b", "size": 3, "loaded": False},
]}


class FakeSocket:
    """A machine's fleet connection: records the requests and answers like agent-machine."""

    def __init__(self, fleet: Any, machine_id: str):
        self.fleet = fleet
        self.machine_id = machine_id
        self.requests: list[dict[str, Any]] = []
        self.servers_on = True

    async def send_text(self, text: str) -> None:
        request = json.loads(text)
        self.requests.append(request)
        machine = self.fleet.machines[self.machine_id]
        answer = {"type": f"{request['type']}_result", "request": request["request"], "ok": True}
        op = request["op"]
        if request["type"] == "provider":
            if op in {"pull", "delete", "load", "unload"} and not self.servers_on:
                answer.update(ok=False, error="Managing model servers is off on this machine (agent-machine servers on)")
            elif op == "status":
                answer["state"] = OLLAMA_STATE if request["kind"] == "ollama" else {"online": True, "models": [{"id": "qwen-strata", "status": {"value": "loaded"}, "meta": {"n_ctx": 8192}}], "status": {"engine": "0.1.48"}}
            elif op == "pull":
                for done in (10, 100):
                    self.fleet.message(machine, {"type": "provider_progress", "request": request["request"], "progress": {"status": "downloading", "total": 100, "completed": done}}, "127.0.0.1")
                    await asyncio.sleep(0)
                if request["model"] == "missing":
                    answer.update(ok=False, error="pull model manifest: file does not exist")
            elif op in {"delete", "load", "unload"}:
                answer["result"] = {{"delete": "deleted", "load": "loaded", "unload": "unloaded"}[op]: request["model"]}
            elif op == "log":
                answer["text"] = "[strata] done\n"
            elif op == "metrics":
                answer["metrics"] = {"cpu": {"percent": 12.5, "count": 8}, "memory": {"used": 1, "total": 2, "percent": 50}, "gpus": []}
            elif op == "search":
                answer["result"] = {"query": request["query"], "models": [{"name": "qwen3"}], "note": None}
        elif request["type"] == "server":
            if op == "settings" or op == "configure":
                answer["ollama_settings"] = {"config": request.get("config") or {"num_parallel": 1}, "warnings": []}
            answer["servers"] = machine.servers
        self.fleet.message(machine, answer, "127.0.0.1")

    async def close(self, **_: Any) -> None:
        pass


def refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"the server called a model server itself: {request.method} {request.url}")


def start(app: Any, machine_id: str = "desk", **hello: Any) -> FakeSocket:
    fleet = app.state.runtime.fleet
    socket = FakeSocket(fleet, machine_id)
    fleet.connect(socket, app.state.runtime.auth.identity_for("test-admin", "token"), "127.0.0.1", {
        "id": machine_id, "name": machine_id, "providers": [{"type": "ollama", "port": 11434}, {"type": "strata", "port": 18500}],
        "servers": {"enabled": True, "ollama": {"found": True, "running": True}}, **hello,
    })
    return socket


def local_app(tmp_path: Path) -> Any:
    settings = Settings(
        log_dir=tmp_path / "logs", backends_file=tmp_path / "backends.yaml", default_backend="desk",
        backends={
            "desk": Backend("desk", "http://127.0.0.1:11434", machine="desk"),
            "desk-strata": Backend("desk-strata", "http://127.0.0.1:18500", type="strata", machine="desk"),
            "old": Backend("old", "http://10.0.0.9:11434"),
            "anthropic": Backend("anthropic", "https://api.anthropic.com", type="anthropic"),
        },
    )
    return create_app(settings)


def with_refusing_client(app: Any) -> None:
    app.state.runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))


def test_models_agents_can_use_come_from_each_backends_machine(tmp_path):
    app = local_app(tmp_path)
    with TestClient(app, **LOCAL) as client:
        with_refusing_client(app)
        socket = start(app)
        backends = client.get("/api/models").json()["backends"]
        assert backends["desk"]["online"] and backends["desk"]["version"] == "0.12.3"
        assert [model["name"] for model in backends["desk"]["models"]] == ["gemma3:4b", "qwen3:8b"]
        assert backends["desk"]["models"][1]["loaded"] and "profile" in backends["desk"]["models"][1]
        assert backends["desk-strata"]["online"] and backends["desk-strata"]["models"][0]["name"] == "qwen-strata"
        assert backends["desk-strata"]["models"][0]["profile"]["options"] == {}
        assert not backends["old"]["online"] and "agent-machine" in backends["old"]["error"]
        assert not backends["anthropic"]["online"] and "Hosted" in backends["anthropic"]["error"]
        assert {(request["kind"], request["port"]) for request in socket.requests} == {("ollama", 11434), ("strata", 18500)}
        # The console finds each backend's model server from the status.
        status = client.get("/api/status").json()["backends"]
        assert status["desk"]["provider"] == "ollama-11434" and status["old"]["provider"] is None
        # Once the machine goes, its backends say so rather than the server trying them.
        app.state.runtime.fleet.disconnect("desk", socket)
        assert "isn't connected" in client.get("/api/models").json()["backends"]["desk"]["error"]


def test_agents_are_told_the_context_size_the_machine_reports(tmp_path):
    app = local_app(tmp_path)
    with TestClient(app, **LOCAL) as client:
        with_refusing_client(app)
        start(app)
        # Strata runs one model whatever the agent calls it; Qwen Code would take any qwen3.x for 1M.
        launched = client.post("/api/agents/qwen/sessions", json={"model": "qwen3.8-flash", "backend": "desk-strata", "files_dir": "/tmp/s"}).json()
        assert json.loads(launched["files"]["qwen-settings.json"])["model"]["generationConfig"] == {"contextWindowSize": 8192}
        app.state.runtime.fleet.disconnect("desk", app.state.runtime.fleet.sockets["desk"])
        # Without the machine the size isn't known, and Qwen Code keeps its own guess.
        launched = client.post("/api/agents/qwen/sessions", json={"model": "qwen3.8-flash", "backend": "desk-strata", "files_dir": "/tmp/s"}).json()
        assert json.loads(launched["files"]["qwen-settings.json"])["model"]["generationConfig"] == {}


def test_provider_status_model_actions_logs_metrics_and_search_go_to_the_machine(tmp_path):
    app = local_app(tmp_path)
    with TestClient(app, **LOCAL) as client:
        with_refusing_client(app)
        socket = start(app)
        state = client.get("/api/machines/desk/providers/ollama-11434").json()
        assert state["backend"] == "desk" and state["kind"] == "ollama" and state["port"] == 11434
        assert state["models"][0]["name"] == "qwen3:8b" and state["traffic"]["in_flight"] == 0
        assert client.post("/api/machines/desk/providers/ollama-11434/models/delete", json={"name": "gemma3:4b"}).json() == {"deleted": "gemma3:4b"}
        assert client.post("/api/machines/desk/providers/ollama-11434/models/load", json={"name": "qwen3:8b"}).json() == {"loaded": "qwen3:8b"}
        assert client.post("/api/machines/desk/providers/strata-18500/models/unload").json() == {"unloaded": ""}
        assert client.post("/api/machines/desk/providers/ollama-11434/models/load", json={}).status_code == 400
        assert client.post("/api/machines/desk/providers/strata-18500/models/pull", json={"name": "x"}).status_code == 404
        assert client.get("/api/machines/desk/providers/strata-18500/log?source=engine").text == "[strata] done\n"
        assert client.get("/api/machines/desk/metrics").json()["cpu"]["percent"] == 12.5
        assert client.get("/api/machines/desk/catalog/ollama?q=qwen").json()["models"] == [{"name": "qwen3"}]
        assert client.get("/api/machines/desk/catalog/elsewhere").status_code == 404
        assert client.get("/api/machines/desk/providers/vllm-1").status_code == 404
        assert client.get("/api/machines/nowhere/metrics").status_code == 404
        sent = [(request["op"], request.get("kind"), request.get("port"), request.get("model")) for request in socket.requests]
        assert ("delete", "ollama", 11434, "gemma3:4b") in sent and ("log", "strata", 18500, None) in sent
        assert {"op": "search", "catalog": "ollama", "query": "qwen", "repo": ""}.items() <= socket.requests[-1].items()
        # The machine has the last word on changes.
        socket.servers_on = False
        refused = client.post("/api/machines/desk/providers/ollama-11434/models/load", json={"name": "qwen3:8b"})
        assert refused.status_code == 409 and "agent-machine servers on" in refused.json()["detail"]


def test_a_pull_streams_the_machines_progress_and_its_failure(tmp_path):
    app = local_app(tmp_path)
    with TestClient(app, **LOCAL) as client:
        with_refusing_client(app)
        start(app)
        lines = [json.loads(line) for line in client.post("/api/machines/desk/providers/ollama-11434/models/pull", json={"name": "qwen3:8b"}).text.splitlines()]
        assert [line.get("completed") for line in lines] == [10, 100, None] and lines[-1] == {"status": "success"}
        failed = [json.loads(line) for line in client.post("/api/machines/desk/providers/ollama-11434/models/pull", json={"name": "missing"}).text.splitlines()]
        assert failed[-1] == {"status": "error", "error": "pull model manifest: file does not exist"}


def test_a_machine_going_away_ends_its_waiting_requests(tmp_path):
    app = local_app(tmp_path)

    class Silent(FakeSocket):
        async def send_text(self, text: str) -> None:
            self.requests.append(json.loads(text))
            # Gone before answering.
            self.fleet.disconnect(self.machine_id, self)

    with TestClient(app, **LOCAL) as client:
        with_refusing_client(app)
        fleet = app.state.runtime.fleet
        fleet.connect(Silent(fleet, "desk"), None, "127.0.0.1", {"id": "desk", "providers": [{"type": "ollama", "port": 11434}]})
        response = client.get("/api/machines/desk/providers/ollama-11434")
        assert response.status_code == 502 and "lost the connection" in response.json()["detail"]


def test_ollama_settings_and_restarts_are_the_machines_and_its_owners(tmp_path):
    app = create_app(org_settings(tmp_path))
    alice, dave = signed_in(app, "alice"), signed_in(app, "dave")
    with alice, dave:
        with_refusing_client(app)
        fleet = app.state.runtime.fleet
        socket = FakeSocket(fleet, "desk")
        identity = app.state.runtime.auth.identity_for("alice", "token")
        fleet.connect(socket, identity, "127.0.0.1", {"id": "desk", "servers": {"enabled": True, "ollama": {"found": True}}, "providers": [{"type": "ollama", "port": 11434}]})
        got = alice.get("/api/machines/desk/servers/ollama").json()
        assert got["config"] == {"num_parallel": 1} and got["status"]["found"] is True
        saved = alice.put("/api/machines/desk/servers/ollama", json={"num_parallel": 2})
        assert saved.status_code == 200 and saved.json()["config"] == {"num_parallel": 2}
        assert socket.requests[-1]["op"] == "configure" and socket.requests[-1]["config"] == {"num_parallel": 2}
        assert alice.post("/api/machines/desk/servers/ollama/restart").status_code == 200
        assert socket.requests[-1]["op"] == "restart" and socket.requests[-1]["bind"] == "127.0.0.1"
        # An admin sees the machine and its models, but its Ollama is its owner's to change.
        assert dave.get("/api/machines/desk/providers/ollama-11434").status_code == 200
        assert dave.put("/api/machines/desk/servers/ollama", json={"num_parallel": 3}).status_code == 403
        assert dave.post("/api/machines/desk/servers/ollama/restart").status_code == 403


def test_the_server_no_longer_manages_model_servers_itself(tmp_path):
    app = local_app(tmp_path)
    with TestClient(app, **LOCAL) as client:
        with_refusing_client(app)
        for method, path in [
            ("GET", "/api/ollama/server"), ("POST", "/api/ollama/desk/models/pull"), ("GET", "/api/strata"),
            ("GET", "/api/docker"), ("GET", "/api/docker-hub/search"), ("GET", "/api/hosts/desk"), ("GET", "/api/host/metrics"),
        ]:
            assert client.request(method, path).status_code in {404, 405}, path
        # Agents' native Ollama calls still go through; managing calls don't.
        session = client.post("/api/sessions", json={"client": "aider", "backend": "desk"}).json()["session_id"]
        for path in ("pull", "delete", "create", "copy", "push"):
            refused = client.post(f"/session/{session}/api/{path}", json={"model": "qwen3:8b"})
            assert refused.status_code == 404 and "machine" in refused.json()["detail"]
