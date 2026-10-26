"""Containers the fleet's machines run: specs, keys, GPU switching and who may manage them.

The machine's side is played by FakeMachine, which answers Fleet.ask the way a
machine's agent-proxy does; test_fleet.py covers the requests going over the machine's
WebSocket, and test_client.py runs the real client's containers against fake_docker.py.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from agent_server import containers, strata
from agent_server.app import create_app
from agent_server.config import Backend, Settings
from agent_server.fleet import FleetError, Machine


LOCAL = {"base_url": "http://127.0.0.1:8181", "client": ("127.0.0.1", 50000)}
OPTIONS = {
    "strata": ["MODEL", "FAMILY", "CONTEXT", "KV", "VISION", "PARALLEL"],
    "ollama": ["OLLAMA_CONTEXT_LENGTH", "OLLAMA_KV_CACHE_TYPE", "OLLAMA_FLASH_ATTENTION", "OLLAMA_NUM_PARALLEL", "OLLAMA_KEEP_ALIVE"],
}
ENGINES = [
    {"id": "strata", "label": "Strata", "backend_type": "strata", "image": "ghcr.io/niko1221/strata:latest-cuda13", "images": [],
     "port": 8080, "data_path": "/data", "volume": "strata-data", "health_path": "/health", "stop_timeout": 120,
     "options": [{"env": env, "label": env, "choices": [], "hint": ""} for env in OPTIONS["strata"]], "summary": ""},
    {"id": "ollama", "label": "Ollama", "backend_type": "ollama", "image": "ollama/ollama:latest", "images": [],
     "port": 11434, "data_path": "/root/.ollama", "volume": "ollama-models", "health_path": "/api/version", "stop_timeout": 30,
     "options": [{"env": env, "label": env, "choices": [], "hint": ""} for env in OPTIONS["ollama"]], "summary": ""},
]


class FakeMachine:
    """A machine that runs containers: keeps them in memory and records every request."""

    def __init__(self, machine: Machine):
        self.machine = machine
        self.calls: list[dict[str, Any]] = []
        self.running: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.health = ""

    async def ask(self, machine: Machine, op: str, timeout: float = 30.0, **fields: Any) -> dict[str, Any]:
        assert machine is self.machine
        self.calls.append({"op": op, **fields})
        if op == "status":
            reports = {}
            for name, request in fields["containers"].items():
                current = self.running.get(name)
                state = {"status": "running" if current["running"] else "exited", "running": current["running"], "health": self.health} if current else None
                reports[name] = {"state": state, "job": self.jobs.get(name), "outdated": bool(current and current["spec"] != request["spec"]),
                                 "command": f"docker run ... {request['spec']['image']}", "problem": None}
            return {"ok": True, "runtime": {"online": True}, "containers": reports}
        name = fields["name"]
        if op in {"start", "restart"}:
            for other in fields.get("stop_first", []):
                self.running[other["name"]]["running"] = False
            self.running[name] = {"running": True, "spec": fields["spec"], "key": fields["key"], "bind": fields["bind"]}
        elif op == "stop":
            self.running[name]["running"] = False
        elif op == "remove":
            self.running.pop(name, None)
            return {"ok": True}
        elif op == "logs":
            return {"ok": True, "text": "loaded\n"}
        action = "switch" if fields.get("stop_first") else op
        self.jobs[name] = {"action": action, "state": "done", "started_at": 1.0, "finished_at": 2.0, "error": None, "lines": ["Started"]}
        return {"ok": True, "job": self.jobs[name]}


def make_settings(tmp_path: Path, **backends: Backend) -> Settings:
    backends_file = tmp_path / "backends.yaml"
    laptop = {"laptop": Backend("laptop", "http://ollama.test")}
    data = {"default_backend": "laptop", "backends": {name: backend.public() for name, backend in {**laptop, **backends}.items()}}
    backends_file.write_text(yaml.safe_dump(data))
    return Settings(log_dir=tmp_path / "logs", backends_file=backends_file, default_backend="laptop", backends={**laptop, **backends})


def join(app: Any, machine_id: str = "m1", address: str = "127.0.0.1", enabled: bool = True) -> FakeMachine:
    """Put a machine that runs containers in the fleet, as if its agent-proxy had connected."""
    fleet = app.state.runtime.fleet
    machine = Machine(id=machine_id, name=f"box-{machine_id}", address=address, providers=[{"type": "ollama", "port": 11434, "version": "", "models": 0}],
                      containers={"enabled": enabled, "cli": "docker", "found": True, "online": True, "version": "27.1", "error": None, "engines": ENGINES})
    fleet.machines[machine_id] = machine
    fleet.sockets[machine_id] = object()
    fake = FakeMachine(machine)
    fleet.ask = fake.ask
    return fake


def test_specs_accept_only_the_engines_settings_and_plain_images():
    strata_engine, ollama_engine = ENGINES
    spec = containers.validate_spec({"engine": "strata", "env": {"MODEL": "IQ3_XXS", "CONTEXT": 32768, "VISION": ""}}, strata_engine)
    assert spec == {"engine": "strata", "image": "ghcr.io/niko1221/strata:latest-cuda13", "port": 8080, "gpus": "all",
                    "env": {"MODEL": "IQ3_XXS", "CONTEXT": "32768"}}
    for bad, message in [
        ({"engine": "ollama"}, "not strata"),
        ({"engine": "strata", "image": "--privileged"}, "Image"),
        ({"engine": "strata", "env": {"LD_PRELOAD": "x"}}, "isn't a Strata setting"),
        ({"engine": "strata", "gpus": "0;rm"}, "GPUs"),
        ({"engine": "strata", "port": 80}, "Port"),
        ({"engine": "strata", "volume": "../etc"}, "Volume"),
    ]:
        with pytest.raises(ValueError, match=message):
            containers.validate_spec(bad, strata_engine)
    assert containers.validate_spec({"engine": "ollama", "volume": "D:\\models", "gpus": "0,1"}, ollama_engine)["volume"] == "D:\\models"


def test_create_start_switch_and_delete_containers_on_a_machine(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app, **LOCAL) as client:
        machine = join(app)
        overview = client.get("/api/containers").json()
        assert overview["containers"] == []
        [host] = overview["machines"]
        assert host["id"] == "m1" and host["enabled"] and [engine["id"] for engine in host["engines"]] == ["strata", "ollama"]

        created = client.post("/api/containers", json={"name": "strata-iq3", "machine": "m1", "engine": "strata", "port": 18080, "env": {"MODEL": "IQ3_XXS", "KV": "q4_0"}})
        assert created.status_code == 201, created.text
        saved = yaml.safe_load((tmp_path / "backends.yaml").read_text())["backends"]["strata-iq3"]
        assert saved == {"type": "strata", "url": "http://127.0.0.1:18080", "machine": "m1", "container": created.json()["spec"]}
        assert client.post("/api/containers", json={"name": "laptop", "machine": "m1", "engine": "ollama"}).status_code == 409
        assert client.post("/api/containers", json={"name": "x", "machine": "m1", "engine": "vllm"}).status_code == 400
        assert client.post("/api/containers", json={"name": "x", "machine": "nope", "engine": "ollama"}).status_code == 404
        # The port the machine's own Ollama uses is skipped.
        assert client.post("/api/containers", json={"name": "ollama-cpu", "machine": "m1", "engine": "ollama", "gpus": "none"}).json()["url"] == "http://127.0.0.1:11435"

        item = client.get("/api/containers/strata-iq3").json()
        assert item["phase"] == "not_created" and item["machine_name"] == "box-m1" and item["machine_online"]
        started = client.post("/api/containers/strata-iq3/start", json={})
        assert started.status_code == 202 and started.json()["job"]["state"] == "done"
        start = machine.calls[-1]
        assert start["op"] == "start" and start["spec"] == created.json()["spec"] and start["bind"] == "127.0.0.1"
        # The server keeps Strata's key, gives it to the machine and uses it for requests.
        assert start["key"] and strata.key_for(app.state.runtime.settings.backends["strata-iq3"]) == (start["key"], "container")
        assert client.get("/api/containers/strata-iq3").json()["phase"] == "loading"
        machine.health = "healthy"
        assert client.get("/api/containers/strata-iq3").json()["phase"] == "ready"
        assert client.get("/api/containers/strata-iq3/log").text == "loaded\n"
        assert machine.calls[-1] == {"op": "logs", "name": "strata-iq3", "key": start["key"], "tail": 400}

        # A second Strata on the same GPU: starting it needs a switch, which the machine does.
        client.post("/api/containers", json={"name": "strata-coder", "machine": "m1", "engine": "strata", "port": 18081, "env": {"FAMILY": "coder"}})
        refused = client.post("/api/containers/strata-coder/start", json={})
        assert refused.status_code == 409 and refused.json()["conflicts"] == ["strata-iq3"]
        assert client.get("/api/containers/strata-coder").json()["conflicts"] == ["strata-iq3"]
        switched = client.post("/api/containers/strata-coder/start", json={"switch": True})
        assert switched.json()["job"]["action"] == "switch"
        assert machine.calls[-1]["stop_first"] == [{"name": "strata-iq3", "engine": "strata"}]
        # A CPU-only Ollama doesn't compete for the GPU.
        assert client.post("/api/containers/ollama-cpu/start", json={}).status_code == 202

        # Changed settings show as outdated until the machine recreates it.
        assert client.put("/api/containers/strata-iq3", json={"port": 18080, "env": {"MODEL": "IQ3_S"}}).status_code == 200
        assert client.get("/api/containers/strata-iq3").json()["outdated"] is True
        assert client.put("/api/containers/strata-iq3", json={"engine": "ollama"}).status_code == 400

        assert client.delete("/api/containers/strata-iq3").status_code == 200
        assert machine.calls[-1]["op"] == "remove" and "strata-iq3" not in machine.running
        assert "strata-iq3" not in yaml.safe_load((tmp_path / "backends.yaml").read_text())["backends"]
        assert "strata-iq3" not in json.loads((tmp_path / "logs" / "remote" / "container-keys.json").read_text())


def test_containers_on_another_machine_listen_where_the_server_reaches_them(tmp_path):
    app = create_app(make_settings(tmp_path))
    with TestClient(app, **LOCAL) as client:
        machine = join(app, address="192.168.1.20")
        created = client.post("/api/containers", json={"name": "box", "machine": "m1", "engine": "ollama", "port": 11500})
        assert created.json()["url"] == "http://192.168.1.20:11500"
        client.post("/api/containers/box/start", json={})
        assert machine.calls[-1]["bind"] == "0.0.0.0"


def test_a_machine_that_is_away_or_has_containers_off_says_so(tmp_path):
    spec = {"engine": "ollama", "image": "ollama/ollama:latest", "port": 11500, "gpus": "all", "env": {}}
    app = create_app(make_settings(tmp_path, box=Backend("box", "http://127.0.0.1:11500", machine="m1", container=spec)))
    with TestClient(app, **LOCAL) as client:
        assert "isn't in the fleet" in client.get("/api/containers/box").json()["problem"]
        join(app, enabled=False)
        assert "containers off" in client.get("/api/containers/box").json()["problem"]
        assert client.post("/api/containers", json={"name": "x", "machine": "m1", "engine": "ollama"}).status_code == 409
        del app.state.runtime.fleet.sockets["m1"]
        item = client.get("/api/containers/box").json()
        assert "isn't connected" in item["problem"] and item["machine_online"] is False


def test_containers_need_a_trusted_device_and_settings_saves_keep_them(tmp_path):
    spec = {"engine": "ollama", "image": "ollama/ollama:latest", "port": 11500, "gpus": "all", "env": {}}
    app = create_app(make_settings(tmp_path, box=Backend("box", "http://127.0.0.1:11500", machine="m1", container=spec)))
    with TestClient(app) as stranger:
        assert stranger.get("/api/containers").status_code == 401
        assert stranger.post("/api/containers/box/start", json={}).status_code == 401
    with TestClient(app, **LOCAL) as client:
        join(app)
        refused = client.post("/api/containers/box/start", json={}, headers={"origin": "http://evil.example"})
        assert refused.status_code == 401
        config = client.get("/api/config").json()["config"]
        assert config["backends"]["box"]["container"] == spec and config["backends"]["box"]["machine"] == "m1"
        # The Settings page can't add or change a container block or a machine, but saving keeps them.
        config["backends"]["laptop"]["container"] = {"engine": "strata", "image": "evil/image"}
        config["backends"]["box"]["container"] = {"engine": "ollama", "image": "evil/image"}
        config["backends"]["box"]["machine"] = "m2"
        assert client.put("/api/config", json=config).status_code == 200
        saved = yaml.safe_load((tmp_path / "backends.yaml").read_text())["backends"]
        assert "container" not in saved["laptop"] and saved["box"]["container"] == spec and saved["box"]["machine"] == "m1"


def test_only_the_machines_owner_creates_containers_on_it(tmp_path):
    from test_auth import org_settings, signed_in

    settings = org_settings(tmp_path)
    app = create_app(settings)
    dave, carol = signed_in(app, "dave"), signed_in(app, "carol")
    with dave, carol:
        machine = join(app)
        machine.machine.owner = "carol"
        # dave is an admin but not the owner; carol owns it but isn't an admin, and a container is a new backend.
        assert dave.post("/api/containers", json={"name": "x", "machine": "m1", "engine": "ollama"}).status_code == 403
        assert carol.post("/api/containers", json={"name": "x", "machine": "m1", "engine": "ollama"}).status_code == 403
        machine.machine.owner = "dave"
        assert dave.post("/api/containers", json={"name": "x", "machine": "m1", "engine": "ollama"}).status_code == 201


def test_strata_requests_carry_the_container_key_and_failures_come_back(tmp_path):
    spec = {"engine": "strata", "image": "ghcr.io/niko1221/strata:latest-cuda13", "port": 18090, "gpus": "all", "env": {}}
    settings = make_settings(tmp_path, s1=Backend("s1", "http://strata.test", type="strata", machine="m1", container=spec))
    app = create_app(settings)
    key = app.state.runtime.containers.keys.get("s1", create=True)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    with TestClient(app, **LOCAL) as client:
        machine = join(app)

        async def busy(*args: Any, **fields: Any) -> dict[str, Any]:
            raise FleetError(409, "s1 is busy with start; wait for it to finish")

        app.state.runtime.fleet.ask = busy
        refused = client.post("/api/containers/s1/stop", json={})
        assert refused.status_code == 409 and "busy" in refused.json()["detail"]
        app.state.runtime.fleet.ask = machine.ask
        old = app.state.runtime.client
        app.state.runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            session = client.post("/api/sessions", json={"client": "test", "backend": "s1"}).json()["session_id"]
            response = client.post(f"/session/{session}/v1/chat/completions", json={"model": "m", "messages": []}, headers={"authorization": "Bearer local-placeholder"})
            assert response.status_code == 200
            assert seen["authorization"] == f"Bearer {key}"
        finally:
            asyncio.run(app.state.runtime.client.aclose())
            app.state.runtime.client = old
