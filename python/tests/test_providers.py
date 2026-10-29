"""The provider requests (agent_py/machine/providers.py), as the Go agent-machine's providers_test.go
and presence's detection check them: fake model servers on loopback, the real Manager."""

from __future__ import annotations

import json
import os
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

import pytest

from agent_py.machine import providers, settings


def on() -> settings.Machine:
    return settings.Machine(servers=True)


def off() -> settings.Machine:
    return settings.Machine()


class Upstream:
    """A fake model server on loopback; it records the requests it got."""

    def __init__(self, routes: Dict[str, Callable[[BaseHTTPRequestHandler], None]]) -> None:
        self.routes = routes
        self.calls: List[str] = []
        self.bodies: Dict[str, Any] = {}
        self.headers: Dict[str, Dict[str, str]] = {}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def handle_one(self) -> None:
                key = f"{self.command} {self.path.split('?')[0]}"
                data = self.rfile.read(int(self.headers.get("content-length") or 0))
                try:
                    body = json.loads(data) if data else None
                except ValueError:
                    body = None
                fake.calls.append(key)
                fake.bodies[key] = body
                fake.headers[key] = {name.lower(): value for name, value in self.headers.items()}
                route = fake.routes.get(key)
                if route is None:
                    send(self, 404, b"404 page not found\n")
                else:
                    route(self)

            do_GET = do_POST = do_DELETE = handle_one

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def send(handler: BaseHTTPRequestHandler, status: int, data: bytes, kind: str = "text/plain") -> None:
    handler.send_response(status)
    handler.send_header("content-type", kind)
    handler.send_header("content-length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def reply(value: Any, status: int = 200) -> Callable[[BaseHTTPRequestHandler], None]:
    return lambda handler: send(handler, status, json.dumps(value).encode(), "application/json")


def status(code: int) -> Callable[[BaseHTTPRequestHandler], None]:
    return lambda handler: send(handler, code, b"")


def lines(*values: Any) -> Callable[[BaseHTTPRequestHandler], None]:
    return lambda handler: send(handler, 200, "".join(json.dumps(value) + "\n" for value in values).encode(), "application/x-ndjson")


@pytest.fixture
def serve():
    made: List[Upstream] = []

    def start(routes: Optional[Dict[str, Callable[[BaseHTTPRequestHandler], None]]] = None) -> Upstream:
        fake = Upstream(routes or {})
        made.append(fake)
        return fake

    yield start
    for fake in made:
        fake.close()


def handle(manager: providers.Manager, progress=None, **request: Any) -> Dict[str, Any]:
    return manager.handle(request, progress)


def test_ollama_status_merges_loaded_models(serve):
    fake = serve({
        "GET /api/tags": reply({"models": [
            {"name": "qwen3:8b", "size": 5e9, "details": {"family": "qwen3", "parameter_size": "8B", "quantization_level": "Q4_K_M"}},
            {"name": "a:1b", "size": 1e9},
        ]}),
        "GET /api/ps": reply({"models": [{"name": "qwen3:8b", "size": 6e9, "size_vram": 3e9, "context_length": 16384}]}),
        "GET /api/version": reply({"version": "0.12.3"}),
    })
    answer = handle(providers.Manager(off), request="r1", op="status", kind="ollama", port=fake.port)
    assert answer["ok"] is True and answer["type"] == "provider_result" and answer["request"] == "r1", answer
    state = answer["state"]
    models = state["models"]
    assert state["online"] and state["version"] == "0.12.3" and len(models) == 2 and models[0]["name"] == "a:1b", state
    loaded = models[1]
    assert loaded["loaded"] and loaded["gpu_percent"] == 50 and loaded["quantization"] == "Q4_K_M" and loaded["context_length"] == 16384, loaded


def test_ollama_offline(serve):
    fake = serve()
    port = fake.port
    fake.close()
    state = handle(providers.Manager(off), op="status", kind="ollama", port=port)["state"]
    assert state["online"] is False and state["error"], state


def test_changes_need_servers_on(serve):
    fake = serve({"DELETE /api/delete": reply({})})
    for op in ("pull", "delete", "load", "unload"):
        answer = handle(providers.Manager(off), request="x", op=op, kind="ollama", port=fake.port, model="m")
        assert answer["ok"] is False and "servers on" in answer["error"], (op, answer)
    assert fake.calls == []
    answer = handle(providers.Manager(on), op="delete", kind="ollama", port=fake.port, model="m")
    assert answer["ok"] is True and fake.bodies["DELETE /api/delete"]["model"] == "m", answer


def test_requests_are_checked():
    manager = providers.Manager(on)
    for request in (
        {"op": "status", "kind": "vllm", "port": 1234},
        {"op": "status", "kind": "ollama", "port": 0},
        {"op": "status", "kind": "ollama", "port": 70000},
        {"op": "status", "kind": "ollama", "port": True},
        {"op": "pull", "kind": "ollama", "port": 1234, "model": " "},
        {"op": "rm", "kind": "ollama", "port": 1234},
        {"op": "search", "catalog": "pypi"},
    ):
        assert manager.handle(request)["ok"] is False, request


def test_ollama_pull_relays_progress(serve):
    fake = serve({"POST /api/pull": lines({"status": "pulling manifest"}, {"status": "downloading", "digest": "sha", "total": 10, "completed": 5}, {"status": "success"})})
    got: List[Dict[str, Any]] = []
    answer = handle(providers.Manager(on), got.append, op="pull", kind="ollama", port=fake.port, model="qwen3")
    assert answer["ok"] and len(got) == 3 and got[1]["completed"] == 5 and fake.bodies["POST /api/pull"]["model"] == "qwen3", (answer, got)
    failing = serve({"POST /api/pull": lines({"error": "pull model manifest: file does not exist"})})
    got = []
    answer = handle(providers.Manager(on), got.append, op="pull", kind="ollama", port=failing.port, model="nope")
    assert answer["ok"] is False and "does not exist" in answer["error"] and got[0]["status"] == "error", (answer, got)
    refused = serve({"POST /api/pull": reply({"error": "no space left"}, 500)})
    answer = handle(providers.Manager(on), op="pull", kind="ollama", port=refused.port, model="m")
    assert answer == {"ok": False, "error": "no space left", "type": "provider_result", "request": None}


def test_ollama_load_and_unload(serve):
    fake = serve({"POST /api/generate": reply({"done": True})})
    manager = providers.Manager(on)
    assert handle(manager, op="unload", kind="ollama", port=fake.port, model="m")["ok"]
    body = fake.bodies["POST /api/generate"]
    assert body["keep_alive"] == 0 and body["model"] == "m", body
    assert handle(manager, op="load", kind="ollama", port=fake.port, model="m")["result"]["loaded"] == "m"


def test_ollama_errors_are_readable(serve):
    port = serve().port
    answer = handle(providers.Manager(on), op="delete", kind="ollama", port=port, model="m")
    assert answer["error"] == "404 page not found", answer
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    free = closed.getsockname()[1]
    closed.close()
    answer = handle(providers.Manager(on), op="load", kind="ollama", port=free, model="m")
    assert answer["error"].startswith("Ollama unavailable: "), answer


def test_ollama_log_is_agent_machines(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PROXY_MACHINE_HOME", str(tmp_path))
    manager = providers.Manager(off)
    answer = handle(manager, op="log", kind="ollama", port=providers.OLLAMA_PORT)
    assert answer["ok"] and "No log yet" in answer["text"], answer
    (tmp_path / "ollama.log").write_bytes(b"listening\n")
    assert handle(manager, op="log", kind="ollama", port=providers.OLLAMA_PORT)["text"] == "listening\n"
    assert handle(manager, op="log", kind="ollama", port=11500)["ok"] is False


def test_tail_keeps_the_end(tmp_path):
    path = tmp_path / "log"
    path.write_bytes(b"a" * 5000 + b"end")
    assert providers.tail(path, 1) == "a" * 1021 + "end"
    assert len(providers.tail(path, -1)) == 5003


def test_docker_status_and_models(serve):
    fake = serve({
        "GET /engines/status": reply({"llama.cpp": "running"}),
        "GET /models": reply([
            {"id": "sha256:1", "tags": ["docker.io/ai/qwen3:latest", "ai/qwen3:8B"], "created": 2, "config": {"quantization": "Q4_K_M", "size": "5GB"}},
            {"id": "sha256:2", "tags": ["ai/smollm2"], "created": 1},
        ]),
        "GET /engines/ps": reply([{"model_name": "ai/qwen3:8B", "backend_name": "llama.cpp", "in_use": True}]),
        "GET /engines/df": reply({"models_disk_usage": 5}),
    })
    state = handle(providers.Manager(off), op="status", kind="docker", port=fake.port)["state"]
    models = state["models"]
    assert state["online"] and len(models) == 2 and models[0]["name"] == "ai/qwen3" and models[0]["loaded"] and models[0]["in_use"] and not models[1]["loaded"], state
    assert models[0]["engine"] == "llama.cpp" and models[0]["tags"] == ["ai/qwen3", "ai/qwen3:8B"] and "context_size" in models[0], models[0]
    assert state["disk"]["models_disk_usage"] == 5
    not_docker = serve({"GET /engines/status": reply({"x": 1})})
    state = handle(providers.Manager(off), op="status", kind="docker", port=not_docker.port)["state"]
    assert state["online"] is False and state["engines"] == {} and "not as Docker Model Runner" in state["error"], state
    no_list = serve({"GET /engines/status": reply({"llama.cpp": "running"}), "GET /models": status(500)})
    state = handle(providers.Manager(off), op="status", kind="docker", port=no_list.port)["state"]
    assert state["online"] and state["error"] == "Model Runner couldn't list its models: status 500" and state["engines"] == {"llama.cpp": "running"}, state


def test_docker_pull_unload_delete(serve):
    fake = serve({
        "POST /models/create": lines(
            {"type": "progress", "total": 100, "layer": {"id": "a", "current": 30}},
            {"type": "progress", "total": 100, "layer": {"id": "b", "current": 20}},
            {"type": "success"},
        ),
        "GET /models": reply([{"id": "sha256:1", "tags": ["ai/qwen3:latest", "ai/qwen3:8B"]}]),
        "GET /engines/ps": reply([{"model_name": "ai/qwen3:8B"}]),
        "POST /engines/unload": reply({"unloaded_runners": 1}),
        "DELETE /models/ai/qwen3:8B": reply({}),
    })
    manager = providers.Manager(on)
    got: List[Dict[str, Any]] = []
    answer = handle(manager, got.append, op="pull", kind="docker", port=fake.port, model="ai/qwen3")
    assert answer["ok"] and len(got) == 3 and got[1]["completed"] == 50 and got[1]["total"] == 100 and got[2]["status"] == "success", (answer, got)
    assert fake.bodies["POST /models/create"] == {"from": "ai/qwen3"} and fake.headers["POST /models/create"]["accept"] == "application/json"
    answer = handle(manager, op="unload", kind="docker", port=fake.port, model="ai/qwen3")
    assert answer["ok"] and fake.bodies["POST /engines/unload"]["models"] == ["ai/qwen3:8B"] and answer["result"] == {"unloaded": "ai/qwen3", "runners": 1}, answer
    assert handle(manager, op="delete", kind="docker", port=fake.port, model="ai/qwen3:8B")["ok"]
    assert handle(manager, op="load", kind="docker", port=fake.port, model="ai/x")["error"] == "404 page not found"
    assert fake.headers["POST /engines/v1/chat/completions"]["x-preload-only"] == "true"


def test_pull_progress_messages():
    layers: Dict[str, float] = {}
    assert providers.pull_progress({"type": "error", "message": "denied"}, layers)["error"] == "denied"
    assert providers.pull_progress({"type": "other"}, layers) is None
    assert providers.pull_progress({"type": "warning"}, layers) == {"status": "warning"}
    assert providers.pull_progress({"type": "progress", "layer": {"id": "a", "current": 7}}, layers) == {"status": "downloading", "total": None, "completed": 7}
    assert providers.model_path("hf.co/user/repo:Q4 K") == "hf.co/user/repo:Q4%20K"
    assert providers.model_path("a/b;c,d?e") == "a/b%3Bc%2Cd%3Fe"


def strata_files(tmp_path, monkeypatch, port: int, key: str, alive: bool):
    """A profile, its config and (when alive) a ready file, like Strata writes them."""
    profiles, runtime_dir = tmp_path / f"profiles-{port}", tmp_path / f"runtime-{port}"
    profiles.mkdir()
    runtime_dir.mkdir()
    monkeypatch.setenv("STRATA_PROFILE_DIR", str(profiles))
    monkeypatch.setenv("STRATA_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("STRATA_API_KEY", "")
    engine_log = runtime_dir / "engine.log"
    engine_log.write_text(f"engine with {key} inside\n")
    config = profiles / "run.json"
    config.write_text(json.dumps({"host": "0.0.0.0", "port": port, "model_name": "qwen", "api_key": "none", "log": str(engine_log)}))
    (profiles / "coder.json").write_text(json.dumps({"id": "coder", "source_config": str(config), "overrides": {"api_key": key + ",second"}}))
    if alive:
        (runtime_dir / "coder-ab12.ready.json").write_text(json.dumps({"api_url": f"http://127.0.0.1:{port}", "model_id": "qwen-live", "model_state": "loaded", "pid": os.getpid(), "created_at": 1}))
        (runtime_dir / "coder-dead.ready.json").write_text(json.dumps({"api_url": "http://127.0.0.1:1", "pid": 999999999, "created_at": 2}))
        (runtime_dir / "coder.server.log").write_text(f"POST /v1/chat key={key}\n")


def test_strata_discovery(tmp_path, monkeypatch):
    strata_files(tmp_path, monkeypatch, 18601, "sk-one", True)
    [instance] = providers.discover()
    assert (instance.profile, instance.running, instance.model, instance.api_key, instance.port()) == ("coder", True, "qwen-live", "sk-one", 18601)
    assert instance.server_log and instance.engine_log
    public = instance.public()
    assert "api_key" not in public and public["has_key"] is True and public["logs"] == ["server", "engine"], public
    strata_files(tmp_path, monkeypatch, 18602, "sk-two", False)
    stopped = providers.instance_on(18602)
    assert stopped is not None and not stopped.running and stopped.url == "http://127.0.0.1:18602" and stopped.model == "qwen", stopped
    assert providers.first_key(["", " k "]) == "k" and providers.first_key("none") == "" and providers.first_key(None) == ""


def test_strata_status_key_and_log(tmp_path, monkeypatch, serve):
    saw: List[str] = []

    def v1_status(handler: BaseHTTPRequestHandler) -> None:
        saw.append(handler.headers.get("authorization") or "")
        reply({"engine": "strata 1.2"})(handler)

    fake = serve({
        "GET /health": reply({"service": "strata", "api_key": True, "model": "qwen", "loaded": True}),
        "GET /v1/status": v1_status,
        "GET /v1/models": reply({"data": [{"id": "qwen", "status": {"value": "loaded"}}, {"x": 1}]}),
        "POST /v1/unload": reply({"status": "unloading"}),
    })
    strata_files(tmp_path, monkeypatch, fake.port, "sk-one", True)
    manager = providers.Manager(on)
    state = handle(manager, op="status", kind="strata", port=fake.port)["state"]
    assert state["online"] and state["key"] == {"state": "ok", "source": "profile coder"} and saw[-1] == "Bearer sk-one", state
    assert len(state["models"]) == 1 and state["instance"]["profile"] == "coder" and state["status"] == {"engine": "strata 1.2"}, state
    # The server's key for its containers comes first.
    state = handle(manager, op="status", kind="strata", port=fake.port, key="sk-container")["state"]
    assert state["key"]["source"] == "container" and saw[-1] == "Bearer sk-container", state
    monkeypatch.setenv("STRATA_API_KEY", " sk-env ")
    state = handle(manager, op="status", kind="strata", port=fake.port)["state"]
    assert state["key"]["source"] == "environment" and saw[-1] == "Bearer sk-env", state
    monkeypatch.setenv("STRATA_API_KEY", "")
    answer = handle(manager, op="unload", kind="strata", port=fake.port)
    assert answer["ok"] and answer["result"]["status"] == "unloading", answer
    assert fake.headers["POST /v1/unload"]["authorization"] == "Bearer sk-one"
    assert handle(manager, op="load", kind="strata", port=fake.port)["error"] == "404 page not found"
    assert handle(providers.Manager(off), op="load", kind="strata", port=fake.port)["ok"] is False
    text = handle(manager, op="log", kind="strata", port=fake.port)["text"]
    assert "sk-one" not in text and "<api key>" in text, text
    assert "engine with <api key>" in handle(manager, op="log", kind="strata", port=fake.port, source="engine")["text"]
    assert handle(manager, op="log", kind="strata", port=fake.port, source="x")["ok"] is False
    assert handle(manager, op="log", kind="strata", port=1)["ok"] is False


def test_strata_missing_key(tmp_path, monkeypatch, serve):
    fake = serve({
        "GET /health": reply({"service": "strata", "api_key": True, "model": "qwen", "max_context": 8192}),
        "GET /v1/status": status(401),
        "GET /v1/models": status(401),
    })
    monkeypatch.setenv("STRATA_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("STRATA_API_KEY", "")
    state = handle(providers.Manager(off), op="status", kind="strata", port=fake.port)["state"]
    models = state["models"]
    assert state["key"] == {"state": "missing", "source": None} and len(models) == 1 and models[0]["id"] == "qwen", state
    assert models[0]["meta"]["n_ctx"] == 8192 and models[0]["status"]["value"] == "unloaded" and state["status"] is None
    not_strata = serve({"GET /health": reply({"status": "ok"})})
    state = handle(providers.Manager(off), op="status", kind="strata", port=not_strata.port)["state"]
    assert state["online"] is False and "not as a Strata server" in state["error"], state


def test_metrics():
    answer = handle(providers.Manager(off), op="metrics")
    metrics = answer["metrics"]
    assert answer["ok"] and metrics["cpu"]["count"] >= 1 and metrics["hostname"] and metrics["gpus"] is not None, answer
    assert metrics["memory"]["total"] > 0 and metrics["platform"]
    json.dumps(answer)
    [gpu] = providers.parse_nvidia("0, NVIDIA RTX 4090, 37, 1024, 24564, 51, [N/A]\nbad line\n")
    assert gpu["memory_used"] == 1024 ** 3 and gpu["power_watts"] is None and gpu["utilization"] == 37.0, gpu


def test_detection_finds_the_model_servers_and_strata_keys(tmp_path, monkeypatch, serve):
    ollama = serve({"GET /api/version": reply({"version": "0.12.3"}), "GET /api/tags": reply({"models": [{}, {}]})})
    docker = serve({"GET /engines/v1/models": reply({"object": "list", "data": [{}]})})
    strata = serve({"GET /v1/models": status(401)})
    nothing = serve()
    monkeypatch.setattr(providers, "PROBES", (
        ("ollama", "/api/version", ollama.port), ("docker", "/engines/v1/models", docker.port),
        ("strata", "/v1/models", strata.port), ("docker", "/engines/v1/models", nothing.port),
    ))
    found = providers.detect()
    assert found == [
        {"type": "ollama", "port": ollama.port, "version": "0.12.3", "models": 2},
        {"type": "docker", "port": docker.port, "models": 1},
        {"type": "strata", "port": strata.port},
    ], found
    # A profile serving elsewhere is added from the servers' state, and the profile's key goes with it.
    strata_files(tmp_path, monkeypatch, 18603, "sk-three", True)
    serving = {"strata": {"profiles": [
        {"id": "coder", "url": "http://127.0.0.1:18603"}, {"id": "far", "url": "http://10.0.0.5:18604"}, {"id": "same", "url": f"http://localhost:{strata.port}"},
    ]}}
    found = providers.with_keys(providers.with_strata(found, serving))
    assert found[3:] == [{"type": "strata", "port": 18603, "api_key": "sk-three"}] and "api_key" not in found[2], found
