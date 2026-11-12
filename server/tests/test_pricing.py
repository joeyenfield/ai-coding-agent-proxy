"""Cost estimates (pricing.py): the price table, a request's cost, a session's total, and the routes."""

from __future__ import annotations

import httpx
import pytest

from agent_server import config_io, pricing
from test_api_providers import OPENAI, api, close_app, make_app, session

PRICES = [
    {"match": "gpt-test-mini*", "input": 2.0, "output": 8.0, "cache_read": 0.5},
    {"match": "claude-*", "input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
]


@pytest.fixture(autouse=True)
def provider_key(monkeypatch):
    monkeypatch.setenv("AI_PROXY_API_KEY_TEST", "sk-upstream-secret")


def test_a_requests_cost_prices_cached_tokens_at_the_cache_rate():
    # OpenAI-style usage: the 400,000 cached tokens are inside the million prompt tokens.
    openai = {"model": "gpt-test-mini", "input_tokens": 1_000_000, "output_tokens": 500_000, "cache_read_tokens": 400_000, "cached_in_input": True}
    cost = pricing.estimate(PRICES, openai, "api")
    assert cost == {"usd": 5.4, "input_usd": 1.2, "output_usd": 4.0, "cache_read_usd": 0.2, "cache_write_usd": 0.0, "price": "gpt-test-mini*", "basis": "api"}
    # Anthropic-style usage: cache reads and writes are counted beside the input tokens.
    claude = {"model": "claude-sonnet-4-5", "input_tokens": 1000, "output_tokens": 2000, "cache_read_tokens": 100_000, "cache_creation_tokens": 10_000}
    cost = pricing.estimate(PRICES, claude, "subscription")
    assert cost["usd"] == pytest.approx(0.003 + 0.03 + 0.03 + 0.0375) and cost["basis"] == "subscription"
    # The provider's own model id is the one billed, and a gateway's prefix is ignored.
    assert pricing.estimate(PRICES, {"model": "fast", "upstream_model": "openai/gpt-test-mini", "input_tokens": 1000, "output_tokens": 0}, "api")["usd"] == 0.002
    # No tokens, or no price for the model: no estimate, never a guess.
    assert pricing.estimate(PRICES, {"model": "gpt-test-mini", "input_tokens": 0, "output_tokens": 0}, "api") is None
    assert pricing.estimate(PRICES, {"model": "qwen3:8b", "input_tokens": 10, "output_tokens": 10}, "api") is None


def test_the_price_table_is_checked():
    assert pricing.checked_prices([{"match": " GPT-X* ", "input": 1, "output": 2}]) == [{"match": "gpt-x*", "input": 1.0, "output": 2.0}]
    for bad in ({}, [{"match": "", "input": 1, "output": 2}], [{"match": "m", "input": -1, "output": 2}], [{"match": "m", "input": 1}], [{"match": "m", "input": True, "output": 1}]):
        with pytest.raises(ValueError):
            pricing.checked_prices(bad)
    assert pricing.price_for(pricing.DEFAULT_PRICES, "claude-opus-4-5-20251101")["match"] == "claude-opus-4-5*"
    # A newer version has its own row ahead of its family's, and an older one isn't caught by a newer one's.
    shipped = {name: pricing.price_for(pricing.DEFAULT_PRICES, name) for name in (
        "claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-haiku-5-5",
        "claude-opus-4-8", "claude-opus-4-1-20250805",
    )}
    assert [shipped[name]["input"] for name in shipped] == [8.0, 10.0, 4.0, 5.0, 2.0, 0.1, 5.0, 15.0]
    assert pricing.price_for(pricing.DEFAULT_PRICES, "llama3") is None


def test_a_hosted_providers_calls_are_priced_and_a_machines_arent(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ollama.test":
            return httpx.Response(200, json={"message": {"role": "assistant", "content": "hi"}, "done": True, "prompt_eval_count": 10, "eval_count": 5})
        body = {"id": "c1", "object": "chat.completion", "model": "gpt-test-mini", "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "prompt_tokens_details": {"cached_tokens": 400}}}
        return httpx.Response(200, json=body)

    app, client, old = make_app(tmp_path, handler, openai=api("openai", "https://api.openai.test/v1", **OPENAI))
    try:
        # The server's own table, set by a site admin; a bad one is refused and the shipped one comes back with null.
        assert client.get("/api/config/prices").json()["custom"] is False
        assert client.put("/api/config/prices", json={"prices": [{"match": "x"}]}).status_code == 400
        saved = client.put("/api/config/prices", json={"prices": PRICES}).json()
        assert saved["custom"] is True and saved["prices"][0]["match"] == "gpt-test-mini*"
        assert config_io.export(app.state.runtime.settings)["model_prices"] == saved["prices"]

        paid = session(client, "openai")
        payload = {"model": "fast", "messages": [{"role": "user", "content": "hi"}]}
        assert client.post(f"/session/{paid}/v1/chat/completions", json=payload).status_code == 200
        [record] = app.state.runtime.records(app.state.runtime.sessions.get(paid))
        # 600 fresh at $2, 400 cached at $0.50, 500 out at $8, for a million.
        assert record["cost"]["usd"] == pytest.approx(0.0012 + 0.0002 + 0.004) and record["cost"]["basis"] == "api"
        total = client.get(f"/api/sessions/{paid}/cost").json()
        assert total["usd"] == record["cost"]["usd"] and total["priced"] == 1 and total["unpriced"] == 0
        assert total["models"] == [{"model": "fast", "requests": 1, "usd": record["cost"]["usd"], "basis": "api"}]

        # A model the table doesn't know is counted as unpriced, not as free.
        client.put("/api/config/prices", json={"prices": [PRICES[1]]})
        assert client.post(f"/session/{paid}/v1/chat/completions", json=payload).status_code == 200
        assert client.get(f"/api/sessions/{paid}/cost").json()["unpriced"] == 1

        # A machine's own model server isn't paid for by the token: no cost, and nothing missing.
        local = session(client, "laptop")
        assert client.post(f"/session/{local}/api/chat", json={"model": "m", "messages": [], "stream": False}).status_code == 200
        [record] = app.state.runtime.records(app.state.runtime.sessions.get(local))
        assert "cost" not in record and "unpriced" not in record
        assert client.get(f"/api/sessions/{local}/cost").json() == {"currency": "USD", "usd": 0.0, "subscription_usd": 0.0, "priced": 0, "unpriced": 0, "models": []}

        assert client.put("/api/config/prices", json={"prices": None}).json()["custom"] is False

        # A provider's own prices come before the server's table: what it charges is its to say.
        backend = app.state.runtime.settings.backends["openai"]
        backend.options["prices"] = pricing.checked_prices([{"match": "gpt-test-*", "input": 10, "output": 20}])
        assert client.post(f"/session/{paid}/v1/chat/completions", json=payload).status_code == 200
        latest = app.state.runtime.records(app.state.runtime.sessions.get(paid))[-1]
        # 600 fresh and 400 cached at $10 (no cache price of its own), 500 out at $20.
        assert latest["cost"]["usd"] == pytest.approx(0.01 + 0.01) and latest["cost"]["price"] == "gpt-test-*"
    finally:
        close_app(app, client, old)
