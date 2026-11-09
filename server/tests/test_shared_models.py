"""Models site admins share with every organization (Settings.shared_models, fleet.shared_with_all).

A share names one of the site's own providers and the models every organization's agents may use there.
Other organizations reach that provider only for those models: requests, launches and model lists are
held to them, and they never see where the provider runs. Every upstream is a mock.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agent_server import config_io
from agent_server.app import create_app
from agent_server.config import checked_shared_models, shared_model
from test_auth import org_settings, signed_in

SHARED = "qwen3:0.6b"


def ollama(seen: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": SHARED}, {"name": "llama3:70b"}]})
        if request.url.path == "/api/chat":
            body = json.loads(request.content)
            seen.append(body)
            return httpx.Response(200, json={"model": body["model"], "message": {"role": "assistant", "content": "hi"}, "done": True})
        return httpx.Response(404)
    return handler


def shared_app(tmp_path):
    settings = org_settings(tmp_path)
    # The site's own model server (the root organization's: no machine), and acme's on acme-box ("test").
    settings.save_backend("site-ollama", {"type": "ollama", "url": "http://site-ollama.test"})
    return create_app(settings)


def chat(client, model: str, backend: str = "site-ollama") -> httpx.Response:
    return client.post("/api/chat", headers={"x-ai-proxy-backend": backend}, json={"model": model, "messages": [{"role": "user", "content": "hello"}], "stream": False})


def test_a_share_lets_every_organization_use_only_its_models(tmp_path):
    app = shared_app(tmp_path)
    seen: list[dict] = []
    sally, dave, frank = (signed_in(app, name) for name in ("sally", "dave", "frank"))
    with sally, dave, frank:
        runtime = app.state.runtime
        old = runtime.client
        runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(ollama(seen)))
        try:
            # Before it's shared, the site's model server is nobody else's.
            assert "site-ollama" not in frank.get("/api/status").json()["backends"]
            assert chat(frank, SHARED).status_code == 400 and not seen

            # Only site admins share (SITE_ADMIN_ONLY, as all of /api/config/), and only the site's own providers.
            assert dave.put("/api/config/shared-models", json={"shared": {"site-ollama": [SHARED]}}).status_code in (403, 404)
            assert frank.get("/api/config/shared-models").status_code in (403, 404)
            refused = sally.put("/api/config/shared-models", json={"shared": {"test": ["*"]}})
            assert refused.status_code == 400 and "site's own" in refused.text
            assert sally.put("/api/config/shared-models", json={"shared": {"nothing": ["m"]}}).status_code == 400
            assert sally.put("/api/config/shared-models", json={"shared": {"site-ollama": "m"}}).status_code == 400
            listed = sally.get("/api/config/shared-models").json()
            assert listed["shared"] == {} and [item["name"] for item in listed["backends"]] == ["site-ollama"]
            saved = sally.put("/api/config/shared-models", json={"shared": {"site-ollama": [SHARED, " "]}})
            assert saved.status_code == 400  # a blank model name
            saved = sally.put("/api/config/shared-models", json={"shared": {"site-ollama": [SHARED]}})
            assert saved.status_code == 200, saved.text
            assert saved.json()["shared"] == {"site-ollama": [SHARED]}

            # Every organization's people now see it, but not where it runs, and only the shared model.
            status = frank.get("/api/status").json()["backends"]["site-ollama"]
            assert status == {"type": "ollama", "shared": True}
            assert any("site-ollama" in agent["backends"] for agent in frank.get("/api/agents").json())
            assert [item["name"] for item in frank.get("/api/tags", headers={"x-ai-proxy-backend": "site-ollama"}).json()["models"]] == [SHARED]
            assert [item["id"] for item in frank.get("/v1/models", headers={"x-ai-proxy-backend": "site-ollama"}).json()["data"]] == [SHARED]
            assert "site-ollama" in {item["name"] for item in frank.get("/api/providers").json()["providers"]}

            # The shared model goes through; any other, or none, is refused before anything goes upstream.
            assert chat(frank, SHARED).status_code == 200 and seen[-1]["model"] == SHARED
            count = len(seen)
            other = chat(frank, "llama3:70b")
            assert other.status_code == 403 and "isn't shared" in other.text and len(seen) == count
            assert chat(frank, "").status_code == 403 and len(seen) == count
            # So are a launch and a prepared session naming another model.
            prepared = frank.post("/api/agents/qwen/sessions", json={"model": "llama3:70b", "backend": "site-ollama"})
            assert prepared.status_code == 403
            prepared = frank.post("/api/agents/qwen/sessions", json={"model": SHARED, "backend": "site-ollama"})
            assert prepared.status_code == 201, prepared.text

            # The site's own people, and acme on its own provider, keep every model.
            assert chat(sally, "llama3:70b").status_code == 200
            assert chat(dave, "llama3:70b", backend="test").status_code == 200
            assert "url" in sally.get("/api/status").json()["backends"]["site-ollama"]
            # Acme's provider is still acme's alone.
            assert chat(frank, SHARED, backend="test").status_code == 400

            # The share is a setting: exported, and gone with its provider.
            assert config_io.export(runtime.settings)["shared_models"] == {"site-ollama": [SHARED]}
            runtime.settings.remove_backend("site-ollama")
            assert runtime.settings.shared_models == {}
            assert config_io.export(runtime.settings)["shared_models"] == {}
        finally:
            asyncio.run(runtime.client.aclose())
            runtime.client = old


def test_an_import_keeps_only_shares_of_providers_it_has(tmp_path):
    app = shared_app(tmp_path)
    settings = app.state.runtime.settings
    config_io.apply(settings, {"shared_models": {"site-ollama": ["*"], "gone": ["m"]}})
    assert settings.shared_models == {"site-ollama": ["*"]}
    with pytest.raises(ValueError):
        config_io.apply(settings, {"shared_models": {"site-ollama": [""]}})


def test_shared_models_match_their_usual_spellings():
    assert shared_model(["qwen3"], "qwen3:latest") and shared_model(["qwen3:latest"], "qwen3")
    assert shared_model(["deepseek-chat"], "deepseek-chat[1m]")
    assert shared_model(["*"], "anything") and not shared_model(["*"], "")
    assert not shared_model(["qwen3"], "qwen3:7b")
    assert checked_shared_models({"a": ["m", "m", "*"], "b": []}) == {"a": ["*"]}
