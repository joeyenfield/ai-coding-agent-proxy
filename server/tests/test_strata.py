"""Agents' traffic to Strata: passed through with Strata's key in place of the agent's.

Strata's status, models and logs are agent-machine's (machine/internal/providers); here
the key comes from the machine that runs it, as its hello reported it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from oidc_helpers import authenticated_client as TestClient

from agent_server import strata
from agent_server.agents import AgentDefinition
from agent_server.app import create_app
from agent_server.config import Backend, Settings, serves
from agent_server.fleet import Fleet
from agent_server.db import schema
from agent_server.auth import Identity

# Who the tests' machines sign in as: a site admin, so they're the root organization's, as the tests' sessions are.
ROOT_ADMIN = Identity("test-admin", "token", "admin", (), "root")
from oidc_helpers import database, stored_backends, stored_rows


KEY = "s3cret-strata-key"


@pytest.fixture
def install(monkeypatch):
    """No STRATA_API_KEY on the server: the key is the machine's."""
    monkeypatch.delenv("STRATA_API_KEY", raising=False)


def make_app(tmp_path: Path, handler):
    settings = Settings(
        log_dir=tmp_path / "logs", default_backend="laptop",
        backends={"laptop": Backend("laptop", "http://ollama.test"), "strata": Backend("strata", "http://127.0.0.1:18500", type="strata", machine="desk")},
    )
    app = create_app(settings)
    client = TestClient(app)
    client.__enter__()
    # The machine's hello: its Strata on 18500, with the key from Strata's files there.
    app.state.runtime.fleet.connect(object(), ROOT_ADMIN, "127.0.0.1", {"id": "desk", "providers": [{"type": "strata", "port": 18500, "api_key": KEY}]})
    old = app.state.runtime.client
    app.state.runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return app, client, old


def close_app(app, client, old):
    asyncio.run(app.state.runtime.client.aclose())
    app.state.runtime.client = old
    client.__exit__(None, None, None)


def strata_session(client) -> str:
    return client.post("/api/sessions", json={"client": "test", "backend": "strata"}).json()["session_id"]


@pytest.mark.parametrize("value, expected", [
    ("k1", "k1"), (" k1 , k2", "k1"), (["", "k2"], "k2"), (["k1", "k2"], "k1"), ("none", None), ("NONE ", None), ("", None), (None, None),
])
def test_first_key_reads_strata_key_forms(value, expected):
    assert strata.first_key(value) == expected


def test_local_model_agents_can_use_strata_but_native_ollama_agents_cannot():
    backend = Backend("strata", "http://127.0.0.1:18500", type="strata")
    assert serves(backend, "ollama", "openai-chat") and serves(backend, "ollama", "anthropic")
    assert not serves(backend, "ollama", "ollama") and not serves(backend, "anthropic", "anthropic")
    settings = Settings(default_backend="strata", backends={"laptop": Backend("laptop", "http://ollama.test"), "strata": backend})
    assert settings.backend_for("ollama", None, "openai-chat").name == "strata"
    assert settings.backend_for("ollama", None, "ollama").name == "laptop"
    with pytest.raises(ValueError):
        settings.backend_for("ollama", "strata", "ollama")
    # Without a usable default, an exact type match wins over Strata.
    settings.default_backend = "missing"
    assert settings.backend_for("ollama", None, "openai-chat").name == "laptop"


def test_strata_backends_survive_a_settings_save(tmp_path):
    settings = Settings(backends={"strata": Backend("strata", "http://127.0.0.1:18500", type="strata", machine="m1")})
    settings.update_network_config({
        "proxy": {"listen_host": "127.0.0.1", "listen_port": 8181, "url": "http://127.0.0.1:8181"},
        "default_backend": "strata",
        "backends": {"strata": {"type": "strata", "url": "http://127.0.0.1:18500"}},
    })
    assert settings.backend(None).type == "strata" and settings.backend(None).machine == "m1"
    assert "api_key" not in json.dumps(stored_backends(settings))


def test_openai_chat_passes_through_to_strata_with_its_key(tmp_path, install):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(path=request.url.path, auth=request.headers.get("authorization"), body=json.loads(request.content))
        body = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":1}}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    app, client, old = make_app(tmp_path, handler)
    try:
        session = strata_session(client)
        payload = {"model": "qwen-strata", "stream": True, "messages": [{"role": "user", "content": "Hi"}]}
        with client.stream("POST", f"/session/{session}/v1/chat/completions", json=payload, headers={"authorization": "Bearer local-ollama"}) as response:
            assert response.status_code == 200
            assert "hi" in response.read().decode()
        # Not translated to Ollama's /api/chat, and no models.yaml options added.
        assert seen == {"path": "/v1/chat/completions", "auth": f"Bearer {KEY}", "body": payload}
        record = json.loads(client.get(f"/api/sessions/{session}/telemetry").text)
        assert record["backend"] == "strata" and record["input_tokens"] == 5 and record["output_tokens"] == 1
    finally:
        close_app(app, client, old)


def test_strata_timings_set_the_recorded_speeds(tmp_path, install):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 19, "completion_tokens": 2},
            "timings": {"prompt_n": 19, "prompt_ms": 500.0, "predicted_n": 2, "predicted_ms": 100.0},
        })

    app, client, old = make_app(tmp_path, handler)
    try:
        session = strata_session(client)
        assert client.post(f"/session/{session}/v1/chat/completions", json={"model": "m", "messages": []}).status_code == 200
        record = json.loads(client.get(f"/api/sessions/{session}/telemetry").text)
        # From Strata's own timings, not the wall clock of a reply that arrived in one piece.
        assert record["generation_tps"] == 20.0 and record["prompt_tps"] == 38.0
    finally:
        close_app(app, client, old)


def test_anthropic_messages_and_token_counts_pass_through_to_strata(tmp_path, install):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("authorization"), request.headers.get("x-api-key")))
        if request.url.path == "/v1/messages/count_tokens":
            return httpx.Response(200, json={"input_tokens": 42})
        return httpx.Response(200, json={"type": "message", "content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 3, "output_tokens": 1}})

    app, client, old = make_app(tmp_path, handler)
    try:
        session = strata_session(client)
        headers = {"x-api-key": "local-ollama", "anthropic-version": "2023-06-01"}
        reply = client.post(f"/session/{session}/anthropic/v1/messages?beta=true", json={"model": "m", "max_tokens": 5, "messages": []}, headers=headers)
        assert reply.status_code == 200 and reply.json()["content"][0]["text"] == "ok"
        assert client.post(f"/session/{session}/anthropic/v1/messages/count_tokens", json={"messages": []}, headers=headers).json() == {"input_tokens": 42}
        assert seen == [("/v1/messages", f"Bearer {KEY}", None), ("/v1/messages/count_tokens", f"Bearer {KEY}", None)]
    finally:
        close_app(app, client, old)


def test_strata_rejects_native_ollama_api_and_lists_its_own_models(tmp_path, install):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models" and request.headers["authorization"] == f"Bearer {KEY}"
        return httpx.Response(200, json={"object": "list", "data": [{"id": "qwen-strata", "object": "model"}]})

    app, client, old = make_app(tmp_path, handler)
    try:
        session = strata_session(client)
        response = client.post(f"/session/{session}/api/chat", json={"model": "m", "messages": []})
        assert response.status_code == 400 and "/v1 or /anthropic" in response.json()["detail"]
        assert client.get(f"/session/{session}/v1/models").json()["data"][0]["id"] == "qwen-strata"
    finally:
        close_app(app, client, old)


def test_launcher_puts_a_local_agent_on_strata_when_asked(tmp_path, install):
    agent = AgentDefinition(id="qwen", name="Qwen", executable="qwen", protocol="openai-chat", env={"OPENAI_BASE_URL": "{openai_url}"})
    app, client, old = make_app(tmp_path, lambda request: httpx.Response(404))
    try:
        app.state.runtime.agents = lambda: {"qwen": agent}
        prepared = client.post("/api/agents/qwen/sessions", json={"model": "qwen-strata", "backend": "strata"}).json()
        assert prepared["session"]["backend"] == "strata"
        assert prepared["env"]["OPENAI_BASE_URL"].endswith(f"/session/{prepared['session']['session_id']}/v1")
    finally:
        close_app(app, client, old)


def test_key_comes_from_the_container_the_environment_or_the_machine(tmp_path, install, monkeypatch):
    fleet = Fleet(database(tmp_path))
    monkeypatch.setattr(strata, "fleet", fleet)
    fleet.connect(object(), ROOT_ADMIN, "10.0.0.5", {"id": "desk", "providers": [{"type": "strata", "port": 18500, "api_key": KEY}, {"type": "ollama", "port": 11434, "api_key": "x"}]})
    assert strata.key_for(Backend("s", "http://10.0.0.5:18500", type="strata", machine="desk")) == (KEY, "machine")
    assert strata.key_for(Backend("s", "http://10.0.0.5:18501", type="strata", machine="desk")) == (None, None)
    assert strata.key_for(Backend("s", "http://10.0.0.5:18500", type="strata")) == (None, None)
    # Kept in memory only: never in the database.
    assert KEY not in json.dumps(stored_rows(fleet.db, schema.machines))
    assert fleet.machines["desk"].keys == {18500: KEY}
    monkeypatch.setenv("STRATA_API_KEY", "from-env")
    assert strata.key_for(Backend("s", "http://10.0.0.5:18500", type="strata", machine="desk")) == ("from-env", "environment")
    # A machine that drops keeps its key while it's on its way back (requests wait for it), and its next
    # hello says what it has now.
    fleet.disconnect("desk", fleet.sockets["desk"])
    monkeypatch.delenv("STRATA_API_KEY")
    assert strata.key_for(Backend("s", "http://10.0.0.5:18500", type="strata", machine="desk")) == (KEY, "machine")
    fleet.connect(object(), ROOT_ADMIN, "10.0.0.5", {"id": "desk", "providers": [{"type": "strata", "port": 18500}]})
    assert strata.key_for(Backend("s", "http://10.0.0.5:18500", type="strata", machine="desk")) == (None, None)


def test_an_unreachable_machine_backend_says_why(tmp_path, install):
    def handler(request):
        raise httpx.ConnectTimeout("", request=request)

    app, client, old = make_app(tmp_path, handler)
    try:
        fleet = app.state.runtime.fleet
        # On another network, with an agent-machine from before tunnels: the server can only try the address.
        machine = fleet.machines["desk"]
        machine.name, machine.address, machine.tunnel = "gamepc", "203.0.113.5", False
        session = strata_session(client)
        response = client.post(f"/session/{session}/v1/chat/completions", json={"model": "m", "messages": []})
        assert response.status_code == 502
        message = response.json()["error"]["message"]
        assert "strata (http://127.0.0.1:18500): timed out connecting" in message
        assert "gamepc's agent-machine is too old" in message
        record = client.get("/api/requests", params={"session_id": session}).json()[-1]
        assert record["error_type"] == "ConnectTimeout"

        del fleet.sockets["desk"]
        message = client.post(f"/session/{session}/v1/chat/completions", json={"model": "m", "messages": []}).json()["error"]["message"]
        assert "isn't connected" in message
    finally:
        close_app(app, client, old)


def test_the_consoles_chat_is_read_to_the_end_and_its_header_stays_here(tmp_path, install):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = {name.lower() for name in request.headers}
        body = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":1}}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    app, client, old = make_app(tmp_path, handler)
    try:
        session = strata_session(client)
        payload = {"model": "qwen-strata", "stream": True, "messages": [{"role": "user", "content": "Hi"}]}
        with client.stream("POST", f"/session/{session}/v1/chat/completions", json=payload, headers={"x-ai-proxy-finish": "1"}) as response:
            assert response.status_code == 200 and "hi" in response.read().decode()
        assert "x-ai-proxy-finish" not in seen["headers"]
        record = json.loads(client.get(f"/api/sessions/{session}/telemetry").text)
        assert record["error_type"] is None
        assert client.post(f"/api/sessions/{session}/stop").json() == {"stopped": 0}
    finally:
        close_app(app, client, old)


def test_a_reply_read_to_the_end_outlives_its_client_until_stopped():
    from types import SimpleNamespace

    from agent_server.routes.compat import _outliving
    from agent_server.runtime import Runtime

    async def scenario():
        runtime = SimpleNamespace(finishing={})
        runtime.finish_later = lambda session_id, work: Runtime.finish_later(runtime, session_id, work)
        runtime.stop_finishing = lambda session_id: Runtime.stop_finishing(runtime, session_id)
        read, finished = [], asyncio.Event()

        async def reply(parts):
            try:
                for part in parts:
                    read.append(part)
                    yield part
                    await asyncio.sleep(0.01)
            finally:
                finished.set()

        # The client takes one chunk and goes: the rest is still read, by the task.
        stream = _outliving(runtime, "s1", reply([b"a", b"b", b"c", b"d"]))
        assert await stream.__anext__() == b"a"
        await stream.aclose()
        await asyncio.wait_for(finished.wait(), 5)
        await asyncio.sleep(0.05)
        assert read == [b"a", b"b", b"c", b"d"] and runtime.finishing == {}

        # Stopping it ends the reading too.
        finished.clear()
        read.clear()
        stream = _outliving(runtime, "s2", reply([b"x"] * 1000))
        assert await stream.__anext__() == b"x"
        await stream.aclose()
        assert runtime.stop_finishing("s2") == 1
        await asyncio.wait_for(finished.wait(), 5)
        assert len(read) < 1000

    asyncio.run(scenario())
