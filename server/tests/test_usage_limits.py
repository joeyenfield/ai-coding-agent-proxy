"""Usage by person and provider, and the token limits an organization's admins set on it.

A limit stops its person's model calls to that provider (429, usage_limit) and new sessions there,
before anything reaches the provider; a person's own limit for a window replaces everyone's;
members see only their own usage; nothing crosses organizations.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx

from agent_server.app import create_app
from agent_server.usage_limits import UsageLimits, provider_for, subscription_for_host
from oidc_helpers import database
from test_auth import signed_in
from test_tenancy import tenant_settings

MESSAGE = {"model": "claude-test", "max_tokens": 20, "messages": [{"role": "user", "content": "Hi"}]}


def upstream(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-test",
            "content": [{"type": "text", "text": "Hello"}], "usage": {"input_tokens": 600, "output_tokens": 500},
        })
    return handler


def use_mock(app, calls: list):
    runtime = app.state.runtime
    old = runtime.client
    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream(calls)))
    return old


def call(client, session_id: str):
    return client.post(f"/session/{session_id}/anthropic/v1/messages", json=MESSAGE)


def row(report: dict, user: str, provider: str) -> dict:
    return next(item for item in report["rows"] if item["user"] == user and item["provider"] == provider)


def test_a_limit_blocks_its_person_on_that_provider_until_an_admin_lifts_it(tmp_path):
    app = create_app(tenant_settings(tmp_path))
    alice, dave, frank, gina = (signed_in(app, name) for name in ("alice", "dave", "frank", "gina"))
    calls: list = []
    with alice, dave, frank, gina:
        old = use_mock(app, calls)
        try:
            session = alice.post("/api/sessions", json={"client": "x", "backend": "hosted"}).json()["session_id"]
            assert call(alice, session).status_code == 200
            assert len(calls) == 1

            mine = {item["id"]: item for item in alice.get("/api/providers").json()["providers"]}
            assert {"backend:hosted", "subscription:claude", "subscription:codex", "subscription:copilot"} <= set(mine)
            assert mine["backend:hosted"]["usage"]["24h"] == {"requests": 1, "input_tokens": 600, "output_tokens": 500}
            assert mine["backend:hosted"]["kind"] == "hosted" and mine["backend:hosted"]["blocked"] is None

            # Only the organization's admins set limits, and only for their own organization's people and providers.
            limit = {"user": "alice", "provider": "backend:hosted", "window": "24h", "tokens": 1000}
            assert alice.put("/api/orgs/acme/limits", json=limit).status_code == 403
            assert gina.put("/api/orgs/acme/limits", json=limit).status_code == 404
            assert dave.put("/api/orgs/acme/limits", json={**limit, "user": "frank"}).status_code == 400
            assert dave.put("/api/orgs/acme/limits", json={**limit, "provider": "backend:nowhere"}).status_code == 400
            assert dave.put("/api/orgs/acme/limits", json={**limit, "window": "1y"}).status_code == 400
            assert dave.put("/api/orgs/acme/limits", json={**limit, "tokens": -1}).status_code == 400
            report = dave.put("/api/orgs/acme/limits", json=limit).json()
            assert row(report, "alice", "backend:hosted")["blocked"]["used"] == 1100

            # Alice is stopped before the provider sees the call, and told why in a shape agents read.
            refused = call(alice, session)
            assert refused.status_code == 429
            assert refused.json()["error"]["code"] == "usage_limit"
            assert "1,100 of 1,000 tokens" in refused.json()["error"]["message"]
            assert int(refused.headers["retry-after"]) > 0
            assert len(calls) == 1
            # The refusal is in her requests, and isn't counted as usage.
            records = alice.get("/api/requests", params={"session_id": session}).json()
            records = records["requests"] if isinstance(records, dict) else records
            assert sorted(str(item["error_type"]) for item in records) == ["None", "usage_limit"]
            assert [item["input_tokens"] + item["output_tokens"] for item in records if item["error_type"] == "usage_limit"] == [0]
            assert alice.get("/api/providers").json()["providers"][0]["usage"]["24h"]["requests"] == 1
            # New sessions on that provider are refused too; others stay open.
            agent = alice.post("/api/agents/claude-subscription/sessions", json={"backend": "hosted", "model": "claude-test"})
            assert agent.status_code == 429 and "Usage limit reached" in agent.json()["detail"]

            # Members see only themselves; other organizations nothing.
            own = alice.get("/api/orgs/acme/usage").json()
            assert own["manage"] is False and own["limits"] == [] and {item["user"] for item in own["rows"]} == {"alice"}
            assert frank.get("/api/orgs/acme/usage").status_code == 404
            assert {item["user"] for item in dave.get("/api/orgs/acme/usage").json()["rows"]} >= {"alice"}
            assert not any(item["provider"] == "backend:hosted" for item in gina.get("/api/orgs/globex/usage").json()["rows"])
            # Frank, in another organization, isn't touched by Acme's limits.
            other = frank.post("/api/sessions", json={"client": "x", "backend": "hosted"}).json()["session_id"]
            assert call(frank, other).status_code == 200

            # Everyone's limit applies where a person has none of their own for that window.
            dave.put("/api/orgs/acme/limits", json={**limit, "user": "*", "tokens": 5000})
            report = dave.delete("/api/orgs/acme/limits", params={"user": "alice", "provider": "backend:hosted", "window": "24h"}).json()
            assert row(report, "alice", "backend:hosted")["limits"][0]["inherited"] is True
            assert row(report, "dave", "backend:hosted")["blocked"] is None
            assert call(alice, session).status_code == 200
            # A zero limit turns the provider off for them; limits for different windows all apply.
            dave.put("/api/orgs/acme/limits", json={**limit, "user": "*", "window": "month", "tokens": 0})
            assert call(alice, session).status_code == 429
            assert dave.delete("/api/orgs/acme/limits", params={"user": "*", "provider": "backend:hosted", "window": "nope"}).status_code == 404

            # Limits are kept in the database: a new process applies them.
            stored = UsageLimits(database(tmp_path))
            assert stored.blocked("acme", "alice", "backend:hosted")["window"] == "month"
        finally:
            asyncio.run(app.state.runtime.client.aclose())
            app.state.runtime.client = old


def test_a_rolling_window_frees_up_as_its_oldest_hours_leave_it(tmp_path):
    now = [1_800_000_000.0]
    usage = UsageLimits(database(tmp_path), clock=lambda: now[0])
    session = SimpleNamespace(tags={"owner": "machine-owner", "launched_by": "joey", "tenant": "pockets"})
    record = {"kind": "api", "backend": "ollama-game-pc", "model": "qwen", "input_tokens": 400, "output_tokens": 200}
    usage.observe(session, record)
    usage.set_limit("pockets", "joey", "backend:ollama-game-pc", "5h", 1000, "admin")
    assert usage.blocked_session(session, "backend:ollama-game-pc") is None
    now[0] += 3600
    usage.observe(session, record)
    blocked = usage.blocked_session(session, "backend:ollama-game-pc")
    # Counted against whoever started the session from the console, not the machine it ran under.
    assert blocked["used"] == 1200 and usage.blocked("pockets", "machine-owner", "backend:ollama-game-pc") is None
    # It frees up when the first hour has left the window.
    first = int((now[0] - 3600) // 3600 * 3600)
    assert blocked["until"] == first + 3600 + 5 * 3600
    now[0] = blocked["until"] + 1
    assert usage.blocked_session(session, "backend:ollama-game-pc") is None


def test_which_provider_a_call_counts_against():
    assert provider_for({"kind": "api", "backend": "ollama-fold6"}) == "backend:ollama-fold6"
    assert provider_for({"kind": "api", "backend": "direct"}) is None
    assert provider_for({"kind": "intercept", "host": "api.anthropic.com"}) == "subscription:claude"
    assert provider_for({"kind": "upgrade", "host": "chatgpt.com"}) == "subscription:codex"
    assert provider_for({"kind": "intercept", "host": "statsig.anthropic.com.example"}) is None
    assert provider_for({"kind": "mcp", "backend": "x"}) is None
    assert subscription_for_host("api.githubcopilot.com") == "subscription:copilot"
    assert subscription_for_host("") is None
