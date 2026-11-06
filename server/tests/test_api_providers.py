"""Generic API providers (api_providers.py): OpenAI Chat Completions, OpenAI Responses and Anthropic Messages
passed through to a hosted API, a gateway or a machine's OpenAI-compatible server, with the server's key.

Every upstream is a mock (httpx.MockTransport): no request leaves the test.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from agent_server import api_providers, config_io
from agent_server.api_providers import PrivateHosts, ProviderError, ProviderNetworkBackend, validate
from agent_server.app import create_app
from agent_server.auth import UserStore
from agent_server.config import Backend, Settings, serves
from agent_server.provider_accounts import Vault
from oidc_helpers import authenticated_client as TestClient
from oidc_helpers import stored_backends
from test_auth import org_settings, signed_in
from test_fleet import hello, machine

OPENAI = {"protocols": ["openai-chat", "openai-responses"], "auth": "bearer", "key_env": "AI_PROXY_API_KEY_TEST", "models": {"fast": "gpt-test-mini"},
          "headers": {"X-Title": "Mesh"}, "orgs": ["*"], "discover": False}
CLAUDE = {"protocols": ["anthropic"], "auth": "x-api-key", "key_env": "AI_PROXY_API_KEY_TEST", "anthropic_url": "https://claude.test/anthropic", "orgs": ["*"],
          "discover": False}


def api(name: str, url: str, **options) -> Backend:
    return Backend(name, url, type="api", options=options)


@pytest.fixture(autouse=True)
def provider_key(monkeypatch):
    monkeypatch.setenv("AI_PROXY_API_KEY_TEST", "sk-upstream-secret")


def make_app(tmp_path: Path, handler, default: str = "laptop", **backends: Backend):
    settings = Settings(
        log_dir=tmp_path / "logs", default_backend=default,
        backends={"laptop": Backend("laptop", "http://ollama.test"), **backends},
    )
    app = create_app(settings)
    client = TestClient(app)
    client.__enter__()
    runtime = app.state.runtime
    old = runtime.client, runtime.api_clients
    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    runtime.api_clients = {True: runtime.client, False: runtime.client}
    return app, client, old


def close_app(app, client, old):
    runtime = app.state.runtime
    asyncio.run(runtime.client.aclose())
    for item in old[1].values():
        asyncio.run(item.aclose())
    runtime.client, runtime.api_clients = old[0], {}
    client.__exit__(None, None, None)


def session(client, backend: str) -> str:
    return client.post("/api/sessions", json={"client": "test", "backend": backend}).json()["session_id"]


def records(app, session_id: str) -> list[dict]:
    sessions = app.state.runtime.sessions
    return sessions.records(sessions.get(session_id))


def sse(*events: dict) -> bytes:
    return b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events) + b"data: [DONE]\n\n"


# Forwarding


def test_chat_completions_go_to_the_provider_with_its_key_alias_and_headers(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = {"id": "c1", "object": "chat.completion", "model": "gpt-test-mini",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "t1", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\": \"a.py\"}"}}]}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 4}}}
        return httpx.Response(200, json=body, headers={"x-request-id": "up-1", "x-ratelimit-remaining-requests": "99"})

    app, client, old = make_app(tmp_path, handler, openai=api("openai", "https://api.openai.test/v1", **OPENAI))
    try:
        session_id = session(client, "openai")
        payload = {"model": "fast", "messages": [{"role": "user", "content": "hi"}], "tools": [{"type": "function", "function": {"name": "read_file"}}]}
        response = client.post(f"/session/{session_id}/v1/chat/completions", json=payload, headers={"authorization": "Bearer local-ollama"})
        assert response.status_code == 200, response.text
        assert response.json()["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "read_file"
        assert response.headers["upstream-request-id"] == "up-1" and response.headers["x-ratelimit-remaining-requests"] == "99"
        [upstream] = seen
        assert str(upstream.url) == "https://api.openai.test/v1/chat/completions"
        # The server's key, never the agent's placeholder, and never the platform's own token.
        assert upstream.headers["authorization"] == "Bearer sk-upstream-secret"
        assert "x-ai-proxy-token" not in upstream.headers and upstream.headers["x-title"] == "Mesh"
        sent = json.loads(upstream.content)
        assert sent["model"] == "gpt-test-mini" and sent["tools"] == payload["tools"]
        [record] = records(app, session_id)
        assert record["backend"] == "openai" and record["protocol"] == "openai-chat" and record["upstream_model"] == "gpt-test-mini"
        assert (record["input_tokens"], record["output_tokens"], record["cache_read_tokens"]) == (12, 5, 4)
        assert record["tokens_estimated"] is False
        # The key is in no record or trace.
        assert "sk-upstream-secret" not in json.dumps(record)
    finally:
        close_app(app, client, old)


def test_streamed_chat_is_passed_through_and_counted(tmp_path):
    chunks = sse(
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hel"}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "t1", "function": {"name": "ls", "arguments": "{}"}}]}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 7}},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=chunks, headers={"content-type": "text/event-stream"})

    app, client, old = make_app(tmp_path, handler, openai=api("openai", "https://api.openai.test/v1", **OPENAI))
    try:
        session_id = session(client, "openai")
        response = client.post(f"/session/{session_id}/v1/chat/completions", json={"model": "fast", "stream": True, "messages": []})
        assert response.status_code == 200 and response.content == chunks
        [record] = records(app, session_id)
        assert (record["input_tokens"], record["output_tokens"], record["error_type"]) == (30, 7, None)
        assert record["ttft_ms"] is not None
    finally:
        close_app(app, client, old)


def test_responses_need_a_provider_that_speaks_them(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=sse(
            {"type": "response.output_text.delta", "delta": "ok"},
            {"type": "response.completed", "response": {"usage": {"input_tokens": 9, "output_tokens": 2, "input_tokens_details": {"cached_tokens": 3}}}},
        ), headers={"content-type": "text/event-stream"})

    chat_only = {**OPENAI, "protocols": ["openai-chat"]}
    app, client, old = make_app(tmp_path, handler, openai=api("openai", "https://api.openai.test/v1", **OPENAI),
                                groq=api("groq", "https://api.groq.test/openai/v1", **chat_only))
    try:
        refused = client.post(f"/session/{session(client, 'groq')}/v1/responses", json={"model": "x", "input": "hi"})
        assert refused.status_code == 400 and "openai-responses" in refused.json()["detail"] and not seen
        session_id = session(client, "openai")
        response = client.post(f"/session/{session_id}/v1/responses", json={"model": "gpt-5", "input": "hi", "stream": True})
        assert response.status_code == 200
        assert str(seen[0].url) == "https://api.openai.test/v1/responses"
        [record] = records(app, session_id)
        assert record["protocol"] == "openai-responses" and (record["input_tokens"], record["output_tokens"], record["cache_read_tokens"]) == (9, 2, 3)
        # Native Ollama calls are never a provider's.
        assert client.post(f"/session/{session_id}/api/chat", json={"model": "x"}).status_code == 400
    finally:
        close_app(app, client, old)


def test_anthropic_messages_keep_their_headers_and_count_cache_tokens(tmp_path):
    seen = []
    events = (
        b"event: message_start\ndata: " + json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 20, "output_tokens": 1, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 8}}}).encode() + b"\n\n"
        b"event: content_block_delta\ndata: " + json.dumps({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hmm"}}).encode() + b"\n\n"
        b"event: content_block_delta\ndata: " + json.dumps({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hi"}}).encode() + b"\n\n"
        b"event: message_delta\ndata: " + json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 15}}).encode() + b"\n\n"
        b"event: message_stop\ndata: {\"type\": \"message_stop\"}\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=events, headers={"content-type": "text/event-stream"})

    app, client, old = make_app(tmp_path, handler, claude=api("claude", "https://claude.test/v1", **CLAUDE))
    try:
        session_id = session(client, "claude")
        payload = {"model": "claude-x", "stream": True, "max_tokens": 100, "thinking": {"type": "enabled", "budget_tokens": 50}, "messages": [{"role": "user", "content": "hi"}]}
        response = client.post(f"/session/{session_id}/anthropic/v1/messages", json=payload,
                               headers={"x-api-key": "agent-placeholder", "anthropic-beta": "interleaved-thinking-2025-05-14"})
        assert response.status_code == 200 and response.content == events
        [upstream] = seen
        assert str(upstream.url) == "https://claude.test/anthropic/v1/messages"
        assert upstream.headers["x-api-key"] == "sk-upstream-secret" and "authorization" not in upstream.headers
        assert upstream.headers["anthropic-version"] == api_providers.ANTHROPIC_VERSION
        assert upstream.headers["anthropic-beta"] == "interleaved-thinking-2025-05-14"
        assert json.loads(upstream.content)["thinking"] == payload["thinking"]
        [record] = records(app, session_id)
        assert (record["input_tokens"], record["output_tokens"]) == (20, 15)
        assert (record["cache_read_tokens"], record["cache_creation_tokens"]) == (100, 8) and record["error_type"] is None
        # OpenAI paths aren't this provider's.
        assert client.post(f"/session/{session_id}/v1/chat/completions", json={"model": "x"}).status_code == 400
    finally:
        close_app(app, client, old)


def test_token_counts_without_messages_support_are_estimates(tmp_path):
    app, client, old = make_app(tmp_path, lambda request: httpx.Response(500), openai=api("openai", "https://api.openai.test/v1", **OPENAI))
    try:
        session_id = session(client, "openai")
        response = client.post(f"/session/{session_id}/anthropic/v1/messages/count_tokens", json={"messages": [{"role": "user", "content": "hello there"}]})
        assert response.status_code == 200 and response.json()["input_tokens"] > 0
    finally:
        close_app(app, client, old)


def test_agent_mode_passes_the_agents_own_key(tmp_path):
    seen = []
    app, client, old = make_app(tmp_path, lambda request: seen.append(request) or httpx.Response(200, json={}),
                                byok=api("byok", "https://gateway.test/v1", protocols=["openai-chat"], auth="agent", orgs=["*"]))
    try:
        client.post(f"/session/{session(client, 'byok')}/v1/chat/completions", json={"model": "m"}, headers={"authorization": "Bearer users-own"})
        assert seen[0].headers["authorization"] == "Bearer users-own"
    finally:
        close_app(app, client, old)


def test_azure_api_version_and_api_key_header(tmp_path):
    seen = []
    app, client, old = make_app(tmp_path, lambda request: seen.append(request) or httpx.Response(200, json={}),
                                azure=api("azure", "https://res.openai.azure.test/openai/deployments/gpt4", protocols=["openai-chat"], auth="api-key",
                                          key_env="AI_PROXY_API_KEY_TEST", api_version="2024-10-21", orgs=["*"]))
    try:
        client.post(f"/session/{session(client, 'azure')}/v1/chat/completions?api-version=evil", json={"model": "gpt4"})
        assert str(seen[0].url) == "https://res.openai.azure.test/openai/deployments/gpt4/chat/completions?api-version=2024-10-21"
        assert seen[0].headers["api-key"] == "sk-upstream-secret"
    finally:
        close_app(app, client, old)


# Errors, timeouts, fallback


def test_upstream_errors_and_rate_limits_come_back_as_they_are(tmp_path):
    body = {"error": {"message": "Rate limit reached", "type": "rate_limit_error"}}
    app, client, old = make_app(tmp_path, lambda request: httpx.Response(429, json=body, headers={"retry-after": "7"}),
                                openai=api("openai", "https://api.openai.test/v1", **OPENAI))
    try:
        session_id = session(client, "openai")
        response = client.post(f"/session/{session_id}/v1/chat/completions", json={"model": "fast", "stream": True})
        assert response.status_code == 429 and response.json() == body and response.headers["retry-after"] == "7"
        [record] = records(app, session_id)
        assert record["rate_limited"] is True and record["error_type"] == "upstream_error"
        health = app.state.runtime.api_health.get("openai")
        assert health["ok"] is False and health["status"] == 429
    finally:
        close_app(app, client, old)


def test_a_missing_key_is_refused_before_anything_goes_out(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_PROXY_API_KEY_TEST")
    seen = []
    app, client, old = make_app(tmp_path, lambda request: seen.append(request) or httpx.Response(200), openai=api("openai", "https://api.openai.test/v1", **OPENAI))
    try:
        response = client.post(f"/session/{session(client, 'openai')}/v1/chat/completions", json={"model": "fast"})
        assert response.status_code == 503 and "no key" in response.json()["detail"] and not seen
    finally:
        close_app(app, client, old)


def test_fallback_only_after_a_connection_failure_and_only_to_listed_providers(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.host == "down.test":
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.host == "slow.test":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    chat = {**OPENAI, "protocols": ["openai-chat"]}
    app, client, old = make_app(
        tmp_path, handler,
        primary=api("primary", "https://down.test/v1", **{**chat, "fallback": ["spare"]}),
        spare=api("spare", "https://spare.test/v1", **chat),
        lonely=api("lonely", "https://down.test/v1", **chat),
        slow=api("slow", "https://slow.test/v1", **{**chat, "fallback": ["spare"]}),
    )
    try:
        session_id = session(client, "primary")
        response = client.post(f"/session/{session_id}/v1/chat/completions", json={"model": "fast"})
        assert response.status_code == 200 and response.headers["x-ai-proxy-backend"] == "spare"
        assert seen == ["down.test", "spare.test"]
        [record] = records(app, session_id)
        assert record["backend"] == "spare" and record["fallback_from"] == "primary" and record["routing"][0]["next"] == "spare"
        # The session stays on its own provider: the next request tries it first again.
        assert app.state.runtime.sessions.get(session_id).backend == "primary"
        # No fallback listed: the failure is the agent's.
        seen.clear()
        assert client.post(f"/session/{session(client, 'lonely')}/v1/chat/completions", json={"model": "fast"}).status_code == 502
        assert seen == ["down.test"]
        # The request may have reached it (a read timeout): never sent again.
        seen.clear()
        assert client.post(f"/session/{session(client, 'slow')}/v1/chat/completions", json={"model": "fast"}).status_code == 502
        assert seen == ["slow.test"]
    finally:
        close_app(app, client, old)


# Choosing a provider


def test_agents_match_providers_by_protocol_and_never_move_to_a_hosted_one_silently():
    openai = api("openai", "https://api.openai.test/v1", protocols=["openai-chat", "openai-responses"])
    claude = api("claude", "https://claude.test/v1", protocols=["anthropic"])
    lmstudio = Backend("lmstudio", "http://10.0.0.5:1234/v1", type="api", machine="m1", options={"protocols": ["openai-chat"], "priority": 5})
    off = api("off", "https://off.test/v1", protocols=["openai-chat"], enabled=False)
    assert serves(openai, "ollama", "openai-responses") and serves(openai, "ollama", "openai-chat")
    assert not serves(openai, "ollama", "anthropic") and serves(claude, "ollama", "anthropic")
    assert not serves(openai, "ollama", "ollama") and not serves(off, "ollama", "openai-chat")
    # Agents on their own Anthropic accounts keep the anthropic pass-through.
    assert not serves(claude, "anthropic", "anthropic")
    settings = Settings(default_backend="openai", backends={"laptop": Backend("laptop", "http://ollama.test"), "openai": openai, "claude": claude, "lmstudio": lmstudio, "off": off})
    assert settings.backend_for("ollama", None, "openai-chat").name == "openai"  # the default, chosen by an admin
    settings.default_backend = "laptop"
    # Local first, higher priority first; a hosted provider only by name.
    assert settings.backend_for("ollama", None, "openai-chat").name == "laptop"
    assert settings.backend_for("ollama", None, "anthropic").name == "laptop"
    with pytest.raises(ValueError, match="name a hosted provider"):
        settings.backend_for("ollama", None, "openai-responses", usable=lambda backend: backend.type == "api")
    assert settings.backend_for("ollama", None, "openai-chat", usable=lambda backend: backend.name != "laptop").name == "lmstudio"
    assert settings.backend_for("ollama", "claude", "anthropic").name == "claude"
    with pytest.raises(ValueError, match="turned off"):
        settings.backend("off")


def test_providers_are_only_for_the_organizations_they_name(tmp_path):
    settings = org_settings(tmp_path)
    settings.save_backend("acme-only", {"type": "api", "url": "https://api.openai.test/v1", "options": {"protocols": ["openai-chat"], "orgs": ["acme"]}})
    settings.save_backend("site-only", {"type": "api", "url": "https://api.openai.test/v1", "options": {"protocols": ["openai-chat"]}})
    app = create_app(settings)
    alice, gina = signed_in(app, "alice"), signed_in(app, "frank")
    with alice, gina:
        assert alice.post("/api/sessions", json={"client": "t", "backend": "acme-only"}).status_code == 201
        assert gina.post("/api/sessions", json={"client": "t", "backend": "acme-only"}).status_code == 400
        assert alice.post("/api/sessions", json={"client": "t", "backend": "site-only"}).status_code == 400
        listed = {item["name"] for item in gina.get("/api/providers").json().get("providers", []) if isinstance(item, dict) and "name" in item}
        assert "acme-only" not in listed and "site-only" not in listed
        # Managing them is a site admin's.
        assert alice.get("/api/config/providers").status_code == 403


def test_an_organizations_admins_set_up_its_providers_for_its_teams(tmp_path):
    settings = org_settings(tmp_path)
    UserStore(settings.open_database()).add_user("gwen", "correct horse battery", role="admin", orgs=["globex"])
    app = create_app(settings)
    runtime = app.state.runtime
    dave, alice, carol, frank, gwen, sally = (signed_in(app, user) for user in ("dave", "alice", "carol", "frank", "gwen", "sally"))
    gateway = {"url": "https://litellm.example.com/v1", "protocols": ["openai-chat"], "auth": "agent", "teams": ["acme/core"]}
    with dave, alice, carol, frank, gwen, sally:
        # Only its admins (and site admins) manage them; to anyone else the organization isn't there.
        assert alice.put("/api/orgs/acme/providers/acme-gw", json=gateway).status_code == 403
        assert alice.get("/api/orgs/acme/providers").status_code == 404
        assert gwen.get("/api/orgs/acme/providers").status_code == 404
        assert dave.put("/api/orgs/acme/providers/acme-gw", json={**gateway, "teams": ["globex/core"]}).status_code == 400
        assert dave.put("/api/orgs/acme/providers/acme-gw", json={**gateway, "teams": ["acme/nope"]}).status_code == 400
        # An organization's admin can't share it with other organizations, nor give it to another.
        saved = dave.put("/api/orgs/acme/providers/acme-gw", json={**gateway, "orgs": ["*"], "org": "globex"})
        assert saved.status_code == 200, saved.text
        assert saved.json()["org"] == "acme" and saved.json()["orgs"] == [] and saved.json()["tenant"] == "acme"
        assert [item["name"] for item in dave.get("/api/orgs/acme/providers").json()["providers"]] == ["acme-gw"]
        assert dave.get("/api/orgs/acme/providers").json()["share"] is False
        assert gwen.get("/api/orgs/globex/providers").json()["providers"] == []
        # Another organization's provider is a name that's taken, nothing more.
        taken = gwen.put("/api/orgs/globex/providers/acme-gw", json=gateway)
        assert taken.status_code == 409 and "api" not in taken.json()["detail"]
        assert gwen.delete("/api/orgs/globex/providers/acme-gw").status_code == 404
        assert gwen.post("/api/orgs/globex/providers/acme-gw/test").status_code == 404
        # Its teams' people, its admins and site admins may use it; nobody else.
        for client, expected in ((alice, 201), (dave, 201), (sally, 201), (carol, 400), (frank, 400), (gwen, 400)):
            assert client.post("/api/sessions", json={"client": "t", "backend": "acme-gw"}).status_code == expected
        def mine(client):
            return {item["name"] for item in client.get("/api/providers").json()["providers"]}
        assert "acme-gw" in mine(alice) and "acme-gw" not in mine(carol) and "acme-gw" not in mine(frank)
        assert "backend:acme-gw" in {item["id"] for item in dave.get("/api/orgs/acme/usage").json()["providers"]}
        # Everyone in it, once it names no team.
        assert dave.put("/api/orgs/acme/providers/acme-gw", json={**gateway, "teams": []}).status_code == 200
        assert carol.post("/api/sessions", json={"client": "t", "backend": "acme-gw"}).status_code == 201
        # Fallbacks are the organization's own providers.
        sally.put("/api/config/providers/site-gw", json={"url": "https://openrouter.ai/api/v1", "auth": "agent", "orgs": ["*"]})
        assert dave.put("/api/orgs/acme/providers/acme-gw", json={**gateway, "fallback": ["site-gw"]}).status_code == 400
        # Site admins see every organization's, and whose each is.
        listed = {item["name"]: item for item in sally.get("/api/config/providers").json()["providers"]}
        assert listed["acme-gw"]["tenant"] == "acme" and listed["site-gw"]["tenant"] == "root" and listed["site-gw"]["org"] is None
        assert sally.get("/api/orgs/acme/providers").json()["share"] is True
        # Removing an organization removes its providers: one made again with its name gets none of them.
        assert sally.post("/api/orgs", json={"id": "temp"}).status_code == 201
        assert sally.put("/api/orgs/temp/providers/temp-gw", json={**gateway, "teams": []}).status_code == 200
        assert sally.delete("/api/orgs/temp").status_code == 200
        assert "temp-gw" not in runtime.settings.backends
        assert dave.delete("/api/orgs/acme/providers/acme-gw").json() == {"ok": True}
        assert "acme-gw" not in runtime.settings.backends


# Managing providers


def test_site_admins_add_providers_and_keys_are_never_shown(tmp_path, monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"object": "list", "data": [{"id": "llama-3.3-70b"}, {"id": "qwen3-32b"}]})

    app, client, old = make_app(tmp_path, handler)
    runtime = app.state.runtime
    try:
        new = {"url": "https://api.groq.test/openai/v1", "protocols": ["openai-chat"], "auth": "bearer", "models": {"llama": "llama-3.3-70b"}}
        refused = client.put("/api/config/providers/groq", json={**new, "key": "gsk-secret"})
        assert refused.status_code == 400 and "AI_PROXY_VAULT_KEYS" in refused.json()["detail"]
        vault = Vault({1: os.urandom(32)})
        runtime.provider_accounts.vault = runtime.api_keys.vault = vault
        saved = client.put("/api/config/providers/groq", json={**new, "key": "gsk-secret"})
        assert saved.status_code == 200, saved.text
        assert saved.json()["has_key"] is True and saved.json()["key_source"] == "vault" and "gsk-secret" not in saved.text
        listing = client.get("/api/config/providers")
        assert "gsk-secret" not in listing.text and listing.json()["vault"] is True
        assert {preset["id"] for preset in listing.json()["presets"]} >= {"openai", "anthropic", "openrouter", "litellm"}
        # Saved again without a key, the key stays.
        assert client.put("/api/config/providers/groq", json={**new, "priority": 3}).json()["has_key"] is True
        # Test: the key goes to the provider, the models come back and are remembered.
        tested = client.post("/api/config/providers/groq/test").json()
        assert tested["ok"] and tested["models"] == ["llama-3.3-70b", "qwen3-32b"]
        assert str(seen[-1].url) == "https://api.groq.test/openai/v1/models" and seen[-1].headers["authorization"] == "Bearer gsk-secret"
        assert runtime.api_health.get("groq")["ok"] is True
        listed = client.get("/api/models", params={"backend": "groq"}).json()["backends"]["groq"]["models"]
        assert [item["name"] for item in listed] == ["llama", "llama-3.3-70b", "qwen3-32b"]
        # Agents' model list: aliases, then what it lists.
        ids = [item["id"] for item in client.get("/v1/models", headers={"x-ai-proxy-backend": "groq"}).json()["data"]]
        assert ids == ["llama", "llama-3.3-70b", "qwen3-32b"]
        # The key isn't in the settings export, the stored settings or the request log.
        exported = client.get("/api/config/export").text
        assert "gsk-secret" not in exported and "groq" in exported
        # Moved to another address, the key isn't sent there.
        moved = client.put("/api/config/providers/groq", json={**new, "url": "https://other.test/v1"}).json()
        assert moved["has_key"] is False
        runtime.api_keys.reload()
        assert runtime.api_keys.get(runtime.settings.backends["groq"], runtime.settings.backends) is None
        assert client.delete("/api/config/providers/groq").json() == {"ok": True}
        assert "groq" not in runtime.settings.backends
    finally:
        close_app(app, client, old)


def test_a_stored_key_survives_a_restart_and_is_bound_to_its_provider(tmp_path):
    settings = Settings(log_dir=tmp_path / "logs", backends={})
    vault = Vault({1: os.urandom(32)})
    keys = api_providers.ApiKeys(settings.open_database(), vault)
    settings.save_backend("one", {"type": "api", "url": "https://one.test/v1", "options": {"protocols": ["openai-chat"]}})
    settings.save_backend("two", {"type": "api", "url": "https://one.test/v1", "options": {"protocols": ["openai-chat"]}})
    keys.set("one", "https://one.test/v1", "k-one")
    fresh = api_providers.ApiKeys(settings.open_database(), vault)
    assert fresh.get(settings.backends["one"], settings.backends) == "k-one"
    assert fresh.get(settings.backends["two"], settings.backends) is None
    newer = Vault({**vault.keys, 2: os.urandom(32)})
    assert api_providers.ApiKeys(settings.open_database(), newer).rotate(settings.backends) == 1
    assert api_providers.ApiKeys(settings.open_database(), Vault({2: newer.keys[2]})).get(settings.backends["one"], settings.backends) == "k-one"


@pytest.mark.parametrize("value, message", [
    ({"url": "http://api.example.com/v1"}, "https"),
    ({"url": "https://10.0.0.8/v1"}, "private address"),
    ({"url": "https://169.254.169.254/latest"}, "private address"),
    ({"url": "https://user:pw@api.example.com/v1"}, "credentials"),
    ({"url": "https://api.example.com/v1?key=1"}, "credentials"),
    ({"url": "https://api.example.com/v1", "headers": {"Authorization": "Bearer x"}}, "credential"),
    ({"url": "https://api.example.com/v1", "headers": {"X-Api-Key": "x"}}, "credential"),
    ({"url": "https://api.example.com/v1", "headers": {"Host": "internal"}}, "can't be set"),
    ({"url": "https://api.example.com/v1", "key_env": "AI_PROXY_OIDC_INTROSPECTION_SECRET"}, "AI_PROXY_API_KEY_"),
    ({"url": "https://api.example.com/v1", "protocols": ["ollama"]}, "protocols"),
    ({"url": "https://api.example.com/v1", "anthropic_url": "https://evil.example.net"}, "same scheme"),
    ({"url": "https://api.example.com/v1", "category": "machine"}, "from the machine"),
    ({"url": "https://api.example.com/v1", "surprise": 1}, "no surprise"),
])
def test_provider_settings_are_checked(value, message):
    with pytest.raises(ProviderError, match=message):
        validate("p", value, None)


def test_private_hosts_need_the_operators_say(monkeypatch):
    with pytest.raises(ProviderError):
        validate("p", {"url": "http://litellm.internal:4000/v1"}, None)
    monkeypatch.setenv("AI_PROXY_API_PRIVATE_HOSTS", "litellm.internal, 10.1.0.0/16")
    assert validate("p", {"url": "http://litellm.internal:4000/v1"}, None)[0] == "http://litellm.internal:4000/v1"
    assert validate("p", {"url": "https://10.1.2.3/v1"}, None)[0] == "https://10.1.2.3/v1"


def test_external_providers_never_connect_to_private_addresses():
    async def connect(host: str, allowed: PrivateHosts):
        return await ProviderNetworkBackend(allowed).connect_tcp(host, 9, timeout=2)

    # localhost resolves to loopback: refused before any connection is tried.
    with pytest.raises(api_providers.DestinationDenied):
        asyncio.run(connect("localhost", PrivateHosts()))
    with pytest.raises(api_providers.DestinationDenied):
        asyncio.run(connect("169.254.169.254", PrivateHosts()))
    # Allowed by the operator, it's only the connection that fails (nothing listens on port 9).
    with pytest.raises(Exception) as raised:
        asyncio.run(connect("127.0.0.1", PrivateHosts.parse("127.0.0.0/8")))
    assert not isinstance(raised.value, api_providers.DestinationDenied)


def test_settings_page_keeps_providers_and_cant_add_them(tmp_path):
    settings = Settings(log_dir=tmp_path / "logs", default_backend="openai", backends={"openai": api("openai", "https://api.openai.test/v1", **OPENAI)})
    proxy = {"listen_host": "127.0.0.1", "listen_port": 8181, "url": "http://127.0.0.1:8181"}
    settings.update_network_config({"proxy": proxy, "default_backend": "openai", "backends": {"openai": {"type": "api", "url": "https://api.openai.test/v1"}}})
    assert settings.backends["openai"].options["models"] == {"fast": "gpt-test-mini"}
    assert stored_backends(settings)["openai"]["options"]["protocols"] == ["openai-chat", "openai-responses"]
    with pytest.raises(ValueError, match="API providers page"):
        settings.update_network_config({"proxy": proxy, "default_backend": "openai", "backends": {
            "openai": {"type": "api", "url": "https://api.openai.test/v1"}, "new": {"type": "api", "url": "https://x.test/v1"}}})


def test_settings_import_checks_providers(tmp_path):
    settings = Settings(log_dir=tmp_path / "logs", backends={})
    document = config_io.export(settings)
    document["backends"] = {"or": {"type": "api", "url": "https://openrouter.test/api/v1", "options": {"protocols": ["openai-chat"], "headers": {"X-Title": "Mesh"}}}}
    config_io.apply(settings, config_io.parse(config_io.dump(document)))
    assert settings.backends["or"].options["headers"] == {"X-Title": "Mesh"}
    document["backends"]["or"]["options"]["headers"] = {"Authorization": "Bearer leaked"}
    with pytest.raises(ValueError, match="credential"):
        config_io.apply(settings, config_io.parse(config_io.dump(document)))


# Machine-hosted


def test_a_machines_openai_compatible_server_becomes_a_tunnelled_provider(tmp_path):
    settings = org_settings(tmp_path)
    app = create_app(settings)
    alice, dave = signed_in(app, "alice"), signed_in(app, "dave")
    providers = [{"type": "api", "port": 1234, "flavor": "lmstudio", "models": 2}, {"type": "api", "port": 8000, "flavor": "vllm", "models": 1}]
    with alice, dave, machine(alice, hello(providers=providers)):
        [listed] = [item for item in dave.get("/api/machines").json()["machines"] if item["id"] == "m1"]
        assert [(item["flavor"], item["port"]) for item in listed["providers"]] == [("lmstudio", 1234), ("vllm", 8000)]
        response = dave.post("/api/machines/m1/providers/adopt", json={"type": "api", "port": 8000})
        assert response.status_code == 201, response.text
        backend = settings.backends[response.json()["backend"]]
        assert backend.type == "api" and backend.machine == "m1" and backend.url.endswith(":8000/v1")
        assert api_providers.options(backend)["auth"] == "none" and api_providers.options(backend)["protocols"] == ["openai-chat"]
        [listed] = [item for item in dave.get("/api/machines").json()["machines"] if item["id"] == "m1"]
        assert {item["port"]: item["backend"] for item in listed["providers"]} == {1234: None, 8000: backend.name}
        assert dave.post("/api/machines/m1/providers/adopt", json={"type": "api", "port": 8000}).status_code == 409
        # Its models are the server's own business, not the console's.
        assert dave.post(f"/api/machines/m1/providers/api-8000/models/pull", json={"name": "x"}).status_code in (403, 404)
        # Its address comes from the machine; the rest is a site admin's to edit (dave isn't one).
        assert dave.put(f"/api/config/providers/{backend.name}", json={"protocols": ["openai-chat", "anthropic"]}).status_code == 403


def test_a_machine_hosted_provider_keeps_its_address(tmp_path):
    current = Backend("box-api", "http://10.0.0.5:1234/v1", type="api", machine="m1", options={"category": "machine", "auth": "none"})
    url, options = validate("box-api", {"protocols": ["openai-chat", "openai-responses"], "anthropic_url": "http://10.0.0.5:1234"}, current)
    assert url == "http://10.0.0.5:1234/v1" and options["category"] == "machine" and options["protocols"] == ["openai-chat", "openai-responses"]
    with pytest.raises(ProviderError, match="on a machine"):
        validate("box-api", {"url": "https://elsewhere.test/v1"}, current)


@pytest.mark.parametrize("base, url, anthropic, path, expected", [
    ("https://api.test/v1", "https://api.test/v1", None, "/v1/chat/completions", "https://api.test/v1/chat/completions"),
    ("https://api.test/v1", "https://api.test/v1", None, "/v1/messages", "https://api.test/v1/messages"),
    ("https://openrouter.test/api/v1", "https://openrouter.test/api/v1", None, "/v1/responses", "https://openrouter.test/api/v1/responses"),
    ("https://ds.test/v1", "https://ds.test/v1", "https://ds.test/anthropic", "/v1/messages", "https://ds.test/anthropic/v1/messages"),
    # Through a machine's tunnel: the same path on the tunnel's address.
    ("http://127.0.0.1:5555/v1", "http://10.0.0.5:1234/v1", "http://10.0.0.5:1234", "/v1/messages/count_tokens", "http://127.0.0.1:5555/v1/messages/count_tokens"),
])
def test_paths_follow_the_providers_urls(base, url, anthropic, path, expected):
    assert api_providers.target(base, api("p", url, anthropic_url=anthropic), path) == expected
