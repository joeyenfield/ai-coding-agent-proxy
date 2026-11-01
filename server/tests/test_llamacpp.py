"""llama.cpp's llama-server as a provider: agents' traffic passes through, the rest is its machine's.

Traffic tests play llama-server with a mock transport; the machine is played by
LlamaSocket, which answers like agent-machine (machine/internal/providers/llamacpp.go and
servers/llamacpp.go). test_client.py runs the real agent-machine against fake_llamacpp.py.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from oidc_helpers import authenticated_client as TestClient

from agent_server import strata
from agent_server.app import create_app
from agent_server.config import Backend, Settings, serves
from agent_server.fleet import _servers, provider_request
from test_providers import LOCAL, ROOT_ADMIN, FakeSocket, refuse

LLAMA = "http://127.0.0.1:9931"
ROUTER_STATE = {"online": True, "router": True, "models": [
    {"name": "ggml-org/qwen-GGUF:Q8_0", "state": "loaded", "loaded": True, "size": 1234, "context_train": 32768},
    {"name": "unsloth/zeta-GGUF:Q4_K_M", "state": "unloaded", "loaded": False, "size": 99},
    {"name": "big/one-GGUF:Q4_K_M", "state": "downloading", "loaded": False, "progress": {"completed": 1, "total": 2}},
]}
LLAMA_STATE = {"known": True, "found": True, "running": True, "managed": True, "port": 9931, "version": "b9999", "source": "github", "variant": "cpu", "release": "b9999"}


def make_app(tmp_path: Path, handler, **backends: Backend):
    settings = Settings(
        log_dir=tmp_path / "logs", default_backend="llama",
        backends={"laptop": Backend("laptop", "http://ollama.test"), "llama": Backend("llama", LLAMA, type="llamacpp"), **backends},
    )
    app = create_app(settings)
    client = TestClient(app)
    client.__enter__()
    old = app.state.runtime.client
    app.state.runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return app, client, old


def close_app(app, client, old):
    asyncio.run(app.state.runtime.client.aclose())
    app.state.runtime.client = old
    client.__exit__(None, None, None)


def test_llamacpp_serves_agents_that_speak_its_apis_but_not_native_ollama():
    llama = Backend("llama", LLAMA, type="llamacpp")
    for protocol in ("openai-chat", "openai-responses", "anthropic"):
        assert serves(llama, "ollama", protocol)
    assert not serves(llama, "ollama", "ollama") and not serves(llama, "anthropic", "anthropic")
    settings = Settings(default_backend="llama", backends={"laptop": Backend("laptop", "http://ollama.test"), "llama": llama})
    with pytest.raises(ValueError, match="llama.cpp doesn't have"):
        settings.backend_for("ollama", "llama", "ollama")


def test_agents_traffic_passes_through_to_llama_server_unchanged(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("authorization"), json.loads(request.content) if request.content else None))
        if request.url.path == "/v1/chat/completions":
            body = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":1}}\n\ndata: [DONE]\n\n'
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
        if request.url.path == "/v1/messages":
            return httpx.Response(200, json={"type": "message", "content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 3, "output_tokens": 1}})
        if request.url.path == "/v1/messages/count_tokens":
            return httpx.Response(200, json={"input_tokens": 42})
        if request.url.path == "/v1/responses":
            return httpx.Response(200, json={"output": [], "usage": {"input_tokens": 2, "output_tokens": 1}})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [{"id": "ggml-org/qwen-GGUF:Q8_0", "owned_by": "llamacpp"}]})
        return httpx.Response(404)

    app, client, old = make_app(tmp_path, handler)
    try:
        session = client.post("/api/sessions", json={"client": "test", "backend": "llama"}).json()["session_id"]
        payload = {"model": "ggml-org/qwen-GGUF:Q8_0", "stream": True, "messages": [{"role": "user", "content": "Hi"}]}
        with client.stream("POST", f"/session/{session}/v1/chat/completions", json=payload, headers={"authorization": "Bearer local"}) as response:
            assert response.status_code == 200 and "hi" in response.read().decode()
        # Not translated to Ollama's /api/chat, no Ollama model tuning, and the agent's own header kept.
        assert seen[-1] == ("/v1/chat/completions", "Bearer local", payload)
        messages = {"model": "ggml-org/qwen-GGUF:Q8_0", "max_tokens": 10, "messages": [{"role": "user", "content": "Hi"}]}
        assert client.post(f"/session/{session}/anthropic/v1/messages", json=messages).json()["content"][0]["text"] == "ok"
        assert client.post(f"/session/{session}/anthropic/v1/messages/count_tokens", json=messages).json() == {"input_tokens": 42}
        assert client.post(f"/session/{session}/v1/responses", json={"model": "m", "input": "hi"}).status_code == 200
        assert client.get(f"/session/{session}/v1/models").json()["data"][0]["owned_by"] == "llamacpp"
        assert [path for path, _, _ in seen] == ["/v1/chat/completions", "/v1/messages", "/v1/messages/count_tokens", "/v1/responses", "/v1/models"]
        # It has no native Ollama API.
        refused = client.post(f"/session/{session}/api/chat", json={"model": "m", "messages": []})
        assert refused.status_code == 400 and "llama.cpp speaks the OpenAI and Anthropic APIs" in refused.json()["detail"]
        record = json.loads(client.get(f"/api/sessions/{session}/telemetry").text.splitlines()[0])
        assert record["backend"] == "llama" and record["input_tokens"] == 5 and record["output_tokens"] == 1
    finally:
        close_app(app, client, old)


def test_a_llamacpp_container_gets_the_servers_key_in_place_of_the_agents(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    box = Backend("box", "http://127.0.0.1:19931", type="llamacpp", machine="desk", container={"engine": "llamacpp", "image": "ghcr.io/ggml-org/llama.cpp:server", "port": 19931})
    app, client, old = make_app(tmp_path, handler, box=box)
    try:
        app.state.runtime.fleet.connect(FakeSocket(app.state.runtime.fleet, "desk"), ROOT_ADMIN, "127.0.0.1", {"id": "desk", "providers": []})
        key = app.state.runtime.containers.keys.get("box", create=True)
        assert key and strata.managed_keys["box"] == key
        # The machine is asked to run it with that key, as Strata's containers are.
        assert app.state.runtime.containers.request("box", box, app.state.runtime.fleet.machines["desk"])["key"] == key
        session = client.post("/api/sessions", json={"client": "test", "backend": "box"}).json()["session_id"]
        client.post(f"/session/{session}/v1/chat/completions", json={"model": "m", "messages": []}, headers={"x-api-key": "sk-placeholder", "authorization": "Bearer placeholder"})
        assert seen == [f"Bearer {key}"]
        # Provider requests to the machine carry it too, so agent-machine can manage the container's models.
        assert provider_request(box, "llamacpp", 19931) == {"kind": "llamacpp", "port": 19931, "key": key}
        # A llama-server the machine runs itself (on loopback, through its tunnel) takes the agent's headers.
        assert provider_request(Backend("own", LLAMA, type="llamacpp", machine="desk"), "llamacpp", 9931) == {"kind": "llamacpp", "port": 9931}
    finally:
        strata.managed_keys.pop("box", None)
        close_app(app, client, old)


class LlamaSocket(FakeSocket):
    """A machine running llama.cpp: answers provider and server requests the way agent-machine does."""

    def __init__(self, fleet: Any, machine_id: str):
        super().__init__(fleet, machine_id)
        self.config: dict[str, Any] = {"ctx_size": 16384}

    async def send_text(self, text: str) -> None:
        request = json.loads(text)
        if request.get("kind") != "llamacpp":
            return await super().send_text(text)
        self.requests.append(request)
        machine = self.fleet.machines[self.machine_id]
        answer: dict[str, Any] = {"type": f"{request['type']}_result", "request": request["request"], "ok": True}
        op = request["op"]
        if request["type"] == "provider":
            if op == "status":
                answer["state"] = ROUTER_STATE
            elif op == "pull":
                for done in (40, 100):
                    self.fleet.message(machine, {"type": "provider_progress", "request": request["request"], "progress": {"status": "downloading", "total": 100, "completed": done}}, "127.0.0.1")
                    await asyncio.sleep(0)
                if request["model"].endswith(":FAIL"):
                    answer.update(ok=False, error="no such quant")
            elif op in {"delete", "load", "unload"}:
                answer["result"] = {{"delete": "deleted", "load": "loaded", "unload": "unloaded"}[op]: request["model"]}
            elif op == "log":
                answer["text"] = "main: server is listening\n"
        else:
            if op == "configure":
                self.config = request["config"]
            if op in {"settings", "configure"}:
                answer["llamacpp_settings"] = {"config": self.config, "warnings": [], "schema": {"groups": [], "settings": []}}
            if op == "install_options":
                answer["llamacpp_install"] = {"source": "github", "release": "b9999", "variants": [{"id": "cpu", "label": "CPU only", "recommended": True}]}
            servers = {"enabled": True, "ollama": {"found": False}, "llamacpp": {**LLAMA_STATE, "install": {"state": "installing", "variant": request.get("variant"), "done": 1, "total": 4}} if op == "install" else LLAMA_STATE}
            answer["servers"] = servers
        self.fleet.message(machine, answer, "127.0.0.1")


def test_a_machines_llamacpp_is_managed_through_the_machine(tmp_path):
    settings = Settings(log_dir=tmp_path / "logs", default_backend="desk-llama", backends={
        "desk-llama": Backend("desk-llama", LLAMA, type="llamacpp", machine="desk"),
    })
    app = create_app(settings)
    with TestClient(app, **LOCAL) as client:
        app.state.runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
        fleet = app.state.runtime.fleet
        socket = LlamaSocket(fleet, "desk")
        fleet.connect(socket, ROOT_ADMIN, "127.0.0.1", {
            "id": "desk", "name": "desk", "providers": [{"type": "llamacpp", "port": 9931, "models": 3}],
            "servers": {"enabled": True, "llamacpp": LLAMA_STATE},
        })
        assert fleet.machines["desk"].servers["llamacpp"]["version"] == "b9999"
        # The agents' model picker: what the router can serve, without what's still downloading.
        models = client.get("/api/models").json()["backends"]["desk-llama"]
        assert models["online"] and [model["name"] for model in models["models"]] == ["ggml-org/qwen-GGUF:Q8_0", "unsloth/zeta-GGUF:Q4_K_M"]
        assert models["models"][0]["quantization"] == "Q8_0" and models["models"][0]["loaded"] and models["models"][0]["context_length"] is None
        assert client.get("/api/status").json()["backends"]["desk-llama"]["provider"] == "llamacpp-9931"

        state = client.get("/api/machines/desk/providers/llamacpp-9931").json()
        assert state["kind"] == "llamacpp" and state["backend"] == "desk-llama" and state["router"] is True and len(state["models"]) == 3
        lines = [json.loads(line) for line in client.post("/api/machines/desk/providers/llamacpp-9931/models/pull", json={"name": "ggml-org/new-GGUF:Q4_K_M"}).text.splitlines()]
        assert [line.get("completed") for line in lines] == [40, 100, None] and lines[-1] == {"status": "success"}
        failed = [json.loads(line) for line in client.post("/api/machines/desk/providers/llamacpp-9931/models/pull", json={"name": "x/y:FAIL"}).text.splitlines()]
        assert failed[-1] == {"status": "error", "error": "no such quant"}
        assert client.post("/api/machines/desk/providers/llamacpp-9931/models/delete", json={"name": "unsloth/zeta-GGUF:Q4_K_M"}).json() == {"deleted": "unsloth/zeta-GGUF:Q4_K_M"}
        assert client.post("/api/machines/desk/providers/llamacpp-9931/models/load", json={"name": "unsloth/zeta-GGUF:Q4_K_M"}).json() == {"loaded": "unsloth/zeta-GGUF:Q4_K_M"}
        assert client.post("/api/machines/desk/providers/llamacpp-9931/models/load", json={}).status_code == 400
        assert client.get("/api/machines/desk/providers/llamacpp-9931/log").text == "main: server is listening\n"

        # Its settings, install options, install, start and stop are the machine's too.
        assert client.get("/api/machines/desk/servers/llamacpp").json()["config"] == {"ctx_size": 16384}
        saved = client.put("/api/machines/desk/servers/llamacpp", json={"ctx_size": 8192})
        assert saved.status_code == 200 and saved.json()["config"] == {"ctx_size": 8192} and saved.json()["status"]["running"]
        options = client.get("/api/machines/desk/servers/llamacpp/install").json()
        assert options["release"] == "b9999" and options["variants"][0]["id"] == "cpu"
        assert client.post("/api/machines/desk/servers/llamacpp/install", json={"variant": "cpu; rm -rf /"}).status_code == 400
        assert client.post("/api/machines/desk/servers/llamacpp/install", json={}).status_code == 400
        installing = client.post("/api/machines/desk/servers/llamacpp/install", json={"variant": "cuda-12.8"})
        assert installing.status_code == 200 and installing.json()["servers"]["llamacpp"]["install"]["variant"] == "cuda-12.8"
        for action in ("start", "restart", "stop", "uninstall"):
            assert client.post(f"/api/machines/desk/servers/llamacpp/{action}").status_code == 200, action
        assert client.post("/api/machines/desk/servers/llamacpp/remove").status_code == 404
        sent = [(request["type"], request["op"], request.get("variant"), request.get("bind")) for request in socket.requests if request["type"] == "server"]
        assert ("server", "install", "cuda-12.8", None) in sent and ("server", "start", None, None) in sent
        # Nothing about llama.cpp reached a model server from here: refuse() would have failed the test.


def test_llamacpp_settings_are_its_owners_to_change(tmp_path):
    from test_auth import org_settings, signed_in

    app = create_app(org_settings(tmp_path))
    alice, dave = signed_in(app, "alice"), signed_in(app, "dave")
    with alice, dave:
        app.state.runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
        fleet = app.state.runtime.fleet
        identity = app.state.runtime.auth.identity_for("alice", "token")
        fleet.connect(LlamaSocket(fleet, "desk"), identity, "127.0.0.1", {"id": "desk", "servers": {"enabled": True, "llamacpp": LLAMA_STATE}, "providers": [{"type": "llamacpp", "port": 9931}]})
        assert alice.put("/api/machines/desk/servers/llamacpp", json={"ctx_size": 8192}).status_code == 200
        assert alice.get("/api/machines/desk/servers/llamacpp/install").status_code == 200
        # An admin sees the machine and its models, but its llama.cpp is its owner's to change and install.
        assert dave.get("/api/machines/desk/providers/llamacpp-9931").status_code == 200
        assert dave.put("/api/machines/desk/servers/llamacpp", json={"ctx_size": 1}).status_code == 403
        assert dave.get("/api/machines/desk/servers/llamacpp/install").status_code == 403
        assert dave.post("/api/machines/desk/servers/llamacpp/install", json={"variant": "cpu"}).status_code == 403
        assert dave.post("/api/machines/desk/servers/llamacpp/start").status_code == 403


def test_a_machines_llamacpp_state_is_kept_to_known_fields():
    kept = _servers({"enabled": True, "llamacpp": {**LLAMA_STATE, "secret": "x", "install": {"state": "failed", "error": "e" * 900, "done": 5, "total": "lots"}}})
    llama = kept["llamacpp"]
    assert "secret" not in llama and llama["version"] == "b9999" and llama["source"] == "github"
    assert llama["install"]["state"] == "failed" and len(llama["install"]["error"]) == 500 and llama["install"]["total"] == 0
    # A machine from before llama.cpp says nothing about it.
    assert _servers({"enabled": True})["llamacpp"] == {"found": False, "running": False, "managed": False, "known": False}
