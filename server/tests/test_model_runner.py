from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from oidc_helpers import authenticated_client as TestClient

from agent_server import model_runner
from agent_server.agents import AgentDefinition
from agent_server.app import create_app
from agent_server.config import Backend, Settings, serves


DMR = "http://127.0.0.1:12434"
QWEN = {
    "id": "sha256:aaa111", "tags": ["docker.io/ai/qwen3:latest", "docker.io/ai/qwen3:8B-Q4_K_M"], "created": 200,
    "config": {"format": "gguf", "quantization": "Q4_K_M", "parameters": "8.19 B", "architecture": "qwen3", "size": "4.68 GiB", "context_size": 40960},
}
SMOL = {"id": "sha256:bbb222", "tags": ["ai/smollm2"], "created": 100, "config": {"format": "gguf", "parameters": "361.82 M"}}


def make_app(tmp_path: Path, handler, **backends: Backend):
    settings = Settings(
        log_dir=tmp_path / "logs", default_backend="laptop",
        backends={"laptop": Backend("laptop", "http://ollama.test"), "docker": Backend("docker", DMR, type="docker"), **backends},
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


def docker_session(client) -> str:
    return client.post("/api/sessions", json={"client": "test", "backend": "docker"}).json()["session_id"]


@pytest.mark.parametrize("path, expected", [
    ("/v1/chat/completions", "/engines/v1/chat/completions"),
    ("/v1/responses", "/engines/v1/responses"),
    ("/v1/models", "/engines/v1/models"),
    ("/v1/messages", "/anthropic/v1/messages"),
    ("/v1/messages/count_tokens", "/anthropic/v1/messages/count_tokens"),
    ("/api/chat", "/api/chat"),
])
def test_agent_paths_map_to_model_runner_prefixes(path, expected):
    assert model_runner.upstream_path(path) == expected


def test_every_local_model_agent_can_use_docker_but_only_some_can_use_strata():
    docker = Backend("docker", DMR, type="docker")
    strata = Backend("strata", "http://127.0.0.1:18500", type="strata")
    for protocol in ("openai-chat", "openai-responses", "anthropic", "ollama"):
        assert serves(docker, "ollama", protocol)
    assert not serves(strata, "ollama", "ollama") and not serves(docker, "anthropic", "anthropic")
    settings = Settings(default_backend="docker", backends={"laptop": Backend("laptop", "http://ollama.test"), "docker": docker, "strata": strata})
    assert settings.backend_for("ollama", None, "ollama").name == "docker"
    with pytest.raises(ValueError, match="Strata doesn't have"):
        settings.backend_for("ollama", "strata", "ollama")


def test_docker_backends_survive_a_settings_save(tmp_path):
    settings = Settings(backends={"docker": Backend("docker", DMR, type="docker", machine="m1")})
    proxy = {"listen_host": "127.0.0.1", "listen_port": 8181, "url": "http://127.0.0.1:8181"}
    settings.update_network_config({"proxy": proxy, "default_backend": "docker", "backends": {"docker": {"type": "docker", "url": DMR}}})
    assert settings.backend(None).type == "docker" and settings.backend(None).machine == "m1"
    # Model servers come from machines: a new one can't be added by its address.
    with pytest.raises(ValueError, match="added from the machine"):
        settings.update_network_config({"proxy": proxy, "default_backend": "docker", "backends": {"docker": {"type": "docker", "url": DMR}, "other": {"type": "docker", "url": "http://other:12434"}}})


def test_openai_chat_passes_through_to_engines_v1_unchanged(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(path=request.url.path, auth=request.headers.get("authorization"), body=json.loads(request.content))
        body = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":1}}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    app, client, old = make_app(tmp_path, handler)
    try:
        session = docker_session(client)
        payload = {"model": "ai/qwen3", "stream": True, "messages": [{"role": "user", "content": "Hi"}]}
        with client.stream("POST", f"/session/{session}/v1/chat/completions", json=payload, headers={"authorization": "Bearer local"}) as response:
            assert response.status_code == 200
            assert "hi" in response.read().decode()
        # Not translated to Ollama's /api/chat, no models.yaml options, and the agent's own header kept.
        assert seen == {"path": "/engines/v1/chat/completions", "auth": "Bearer local", "body": payload}
        record = json.loads(client.get(f"/api/sessions/{session}/telemetry").text)
        assert record["backend"] == "docker" and record["input_tokens"] == 5 and record["output_tokens"] == 1
    finally:
        close_app(app, client, old)


def test_anthropic_responses_native_ollama_and_model_list_reach_model_runner(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        body = json.loads(request.content) if request.content else {}
        if request.url.path == "/anthropic/v1/messages/count_tokens":
            return httpx.Response(200, json={"input_tokens": 42})
        if request.url.path == "/anthropic/v1/messages":
            return httpx.Response(200, json={"type": "message", "content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 3, "output_tokens": 1}})
        if request.url.path == "/engines/v1/responses":
            return httpx.Response(200, json={"output": [], "usage": {"input_tokens": 2, "output_tokens": 1}})
        if request.url.path == "/api/chat":
            # Passed through: the proxy's Ollama profile (num_ctx and so on) isn't added.
            assert "options" not in body
            return httpx.Response(200, json={"message": {"role": "assistant", "content": "ok"}, "done": True, "prompt_eval_count": 4, "eval_count": 1})
        if request.url.path == "/engines/v1/models":
            return httpx.Response(200, json={"object": "list", "data": [{"id": "ai/qwen3", "object": "model"}]})
        return httpx.Response(404)

    app, client, old = make_app(tmp_path, handler)
    try:
        session = docker_session(client)
        message = client.post(f"/session/{session}/anthropic/v1/messages", json={"model": "ai/qwen3", "max_tokens": 5, "messages": []})
        assert message.status_code == 200 and message.json()["content"][0]["text"] == "ok"
        assert client.post(f"/session/{session}/anthropic/v1/messages/count_tokens", json={"messages": []}).json() == {"input_tokens": 42}
        assert client.post(f"/session/{session}/v1/responses", json={"model": "ai/qwen3", "input": "hi"}).status_code == 200
        native = client.post(f"/session/{session}/api/chat", json={"model": "ai/qwen3", "stream": False, "messages": []})
        assert native.status_code == 200 and native.json()["message"]["content"] == "ok"
        assert client.get(f"/session/{session}/v1/models").json()["data"][0]["id"] == "ai/qwen3"
        assert seen == ["/anthropic/v1/messages", "/anthropic/v1/messages/count_tokens", "/engines/v1/responses", "/api/chat", "/engines/v1/models"]
    finally:
        close_app(app, client, old)


def test_agents_list_the_backends_their_protocol_can_use(tmp_path):
    agents = {
        "qwen": AgentDefinition(id="qwen", name="Qwen", executable="qwen", protocol="openai-chat"),
        "aider": AgentDefinition(id="aider", name="Aider", executable="aider", protocol="ollama"),
        "hosted": AgentDefinition(id="hosted", name="Hosted", executable="claude", protocol="anthropic", backend_type="anthropic"),
        "account": AgentDefinition(id="account", name="Account", executable="claude", protocol="anthropic", intercept=True),
    }
    strata = Backend("strata", "http://127.0.0.1:18500", type="strata")
    anthropic = Backend("anthropic", "https://api.anthropic.com", type="anthropic")
    app, client, old = make_app(tmp_path, lambda request: httpx.Response(404), strata=strata, anthropic=anthropic)
    try:
        app.state.runtime.agents = lambda: agents
        listed = {agent["id"]: agent["backends"] for agent in client.get("/api/agents").json()}
        assert listed == {"qwen": ["laptop", "docker", "strata"], "aider": ["laptop", "docker"], "hosted": ["anthropic"], "account": []}
        prepared = client.post("/api/agents/aider/sessions", json={"model": "ai/qwen3", "backend": "docker"}).json()
        assert prepared["session"]["backend"] == "docker"
    finally:
        close_app(app, client, old)
