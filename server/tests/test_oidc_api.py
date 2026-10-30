"""OIDC is mandatory, including agent paths, without leaking platform credentials upstream."""

from __future__ import annotations

import asyncio
import base64
import time

import httpx
import pytest
from fastapi import WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_server.app import create_app
from agent_server.auth import UserStore
from agent_server.config import Backend, Settings
from agent_server.intercept import InterceptProxy


@pytest.fixture
def api_app(tmp_path):
    settings = Settings(
        log_dir=tmp_path / "logs", backends_file=tmp_path / "backends.yaml",
        oidc_introspection_secret="test-resource-secret",
        default_backend="hosted", backends={"hosted": Backend("hosted", "https://provider.test", type="anthropic")},
    )
    store = UserStore(settings.users_path)
    store.add_user("admin", "test password")
    store.add_user("alice", "test password")
    app = create_app(settings)
    runtime = app.state.runtime
    claims = {
        "active": True, "username": "alice", "iss": settings.oidc_issuer, "aud": settings.oidc_audience,
        "exp": time.time() + 300, "scope": "openid mesh-api", "token_type": "Bearer",
    }

    def introspect(request):
        assert str(request.url) == settings.oidc_issuer + "/introspect"
        assert request.headers["authorization"].startswith("Basic ")
        if request.content != b"token=access":
            return httpx.Response(200, json={"active": False})
        return httpx.Response(200, json=claims)

    runtime.auth.client.close()
    runtime.auth.client = httpx.Client(transport=httpx.MockTransport(introspect))
    with TestClient(app, base_url="http://127.0.0.1:8181") as client:
        yield client, runtime, claims


def test_oidc_is_required_without_any_users_or_local_trust(tmp_path):
    with TestClient(create_app(Settings(log_dir=tmp_path / "logs")), base_url="http://127.0.0.1:8181") as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/auth/status").json()["required"] is True
        for path in ("/api/status", "/api/terminals", "/v1/models", "/session/anything/v1/models"):
            result = client.get(path)
            assert result.status_code == 401
            assert result.headers["www-authenticate"].startswith("Bearer ")


@pytest.mark.parametrize("settings", [
    {"oidc_issuer": "http://auth.example.test"},
    {"oidc_issuer": "https://auth.example.test/prefix"},
    {"oidc_browser_origins": ["https://console.example.test/path"]},
    {"oidc_browser_origins": ["https://*.example.test"]},
    {"oidc_redirect_uris": ["http://api.example.test/api/auth/callback"]},
    {"oidc_cli_client": "mesh-console"},
])
def test_unsafe_oidc_configuration_is_rejected(settings):
    with pytest.raises(ValueError):
        Settings(**settings)


def test_legacy_credentials_and_id_tokens_are_not_access_tokens(api_app):
    client, runtime, _ = api_app
    old = runtime.auth.store.start_session("alice", "old browser")
    _, launcher = runtime.auth.store.create_token("alice", "old launcher")
    for token in (old, launcher, "id-token"):
        assert client.get("/api/status", headers={"authorization": "Bearer " + token}).status_code == 401
        client.cookies.set("ai_proxy_session", token)
        assert client.get("/api/status").status_code == 401
        client.cookies.clear()
    assert client.post("/api/auth/login", json={"username": "alice", "password": "test password"}).status_code == 410
    assert client.post("/api/auth/token", json={"username": "alice", "password": "test password"}).status_code == 401


@pytest.mark.parametrize(("field", "value"), [("active", False), ("exp", 0)])
def test_connected_websockets_require_renewal_and_clean_up(api_app, monkeypatch, field, value):
    client, runtime, claims = api_app
    monkeypatch.setattr("agent_server.auth.CREDENTIAL_CHECK_INTERVAL", 0.01)

    @client.app.websocket("/api/test-oidc-socket")
    async def socket(websocket: WebSocket):
        await websocket.accept()
        await websocket.receive_text()

    with client.websocket_connect("/api/test-oidc-socket", headers={"Authorization": "Bearer access"}) as websocket:
        claims[field] = value
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_text()
        assert closed.value.code == 4410


@pytest.mark.parametrize(("origin", "allowed"), [
    (None, True), ("http://127.0.0.1:8181", True), ("https://console.example.test", True),
    ("https://evil.test", False), ("https://127.0.0.1:8181", False), ("null", False),
])
def test_websocket_origin_is_enforced_before_the_handler(api_app, origin, allowed):
    client, runtime, _ = api_app
    runtime.settings.console_origins = ["https://console.example.test"]
    entered = []

    @client.app.websocket("/api/test-origin-socket")
    async def socket(websocket: WebSocket):
        entered.append(True)
        await websocket.accept()
        await websocket.send_text("connected")
        await websocket.close()

    headers = {"Authorization": "Bearer access"}
    if origin is not None:
        headers["Origin"] = origin
    if allowed:
        with client.websocket_connect("ws://127.0.0.1:8181/api/test-origin-socket", headers=headers) as websocket:
            assert websocket.receive_text() == "connected"
        assert entered == [True]
    else:
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect("ws://127.0.0.1:8181/api/test-origin-socket", headers=headers):
                pytest.fail("A foreign origin reached the WebSocket handler")
        assert closed.value.code == 4403
        assert not entered


def test_account_tunnel_cannot_use_another_users_session(api_app):
    client, runtime, _ = api_app
    runtime.intercept = InterceptProxy(runtime)
    session = runtime.sessions.create({"client": "claude", "backend": "direct", "tags": {"owner": "admin"}})
    authorization = base64.b64encode((session.session_id + ":agent-proxy").encode()).decode()
    with client.websocket_connect("/api/proxy/tunnel", headers={"Authorization": "Bearer access"}) as websocket:
        websocket.send_bytes((
            "CONNECT public.example.test:443 HTTP/1.1\r\n"
            "Host: public.example.test:443\r\n"
            f"Proxy-Authorization: Basic {authorization}\r\n\r\n"
        ).encode())
        assert websocket.receive_bytes().startswith(b"HTTP/1.1 407 ")


@pytest.mark.parametrize(("field", "value"), [
    ("active", False), ("aud", "another-api"), ("iss", "https://evil.test"),
    ("scope", "openid profile"), ("exp", 0), ("exp", "future"), ("token_type", "id_token"), ("username", "missing"),
])
def test_access_token_claims_are_enforced(api_app, field, value):
    client, _, claims = api_app
    claims[field] = value
    assert client.get("/api/status", headers={"authorization": "Bearer access"}).status_code == 401


def test_access_tokens_keep_existing_roles_and_session_isolation(api_app):
    client, _, _ = api_app
    header = {"authorization": "Bearer access"}
    assert client.get("/api/auth/status", headers=header).json()["identity"]["user"] == "alice"
    assert client.post("/api/orgs", json={"id": "stolen"}, headers=header).status_code == 403
    session = client.post("/api/sessions", json={"client": "claude", "backend": "hosted"}, headers=header).json()["session_id"]
    assert client.get(f"/session/{session}/v1/models").status_code == 401
    assert client.get("/session/nonexistent/v1/models", headers=header).status_code == 404


def test_browser_origins_must_match_scheme_as_well_as_host(api_app):
    client, _, _ = api_app
    header = {"Authorization": "Bearer access", "Origin": "https://127.0.0.1:8181"}
    assert client.post("/api/sessions", json={"client": "claude"}, headers=header).status_code == 403


@pytest.mark.parametrize("alternate_header", [False, True])
def test_platform_token_is_never_forwarded_to_a_provider(api_app, alternate_header):
    client, runtime, _ = api_app
    seen = []

    def provider(request):
        seen.append(request)
        return httpx.Response(200, json={"type": "message", "content": [], "usage": {"input_tokens": 1, "output_tokens": 0}})

    original = runtime.client
    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    try:
        headers = {"x-ai-proxy-token": "access", "authorization": "Bearer vendor-key"} if alternate_header else {"authorization": "Bearer access"}
        session = client.post("/api/sessions", headers=headers, json={"client": "claude", "backend": "hosted"}).json()["session_id"]
        response = client.post(f"/session/{session}/anthropic/v1/messages", headers=headers, json={"model": "claude", "messages": [], "max_tokens": 1})
        assert response.status_code == 200, response.text
        assert len(seen) == 1
        assert "x-ai-proxy-token" not in seen[0].headers
        assert "cookie" not in seen[0].headers
        assert seen[0].headers.get("authorization") == ("Bearer vendor-key" if alternate_header else None)
    finally:
        asyncio.run(runtime.client.aclose())
        runtime.client = original


def test_authentication_outage_fails_closed(api_app):
    client, runtime, _ = api_app

    def unavailable(request):
        return httpx.Response(503)

    runtime.auth.client.close()
    runtime.auth.client = httpx.Client(transport=httpx.MockTransport(unavailable))
    assert client.get("/api/status", headers={"authorization": "Bearer access"}).status_code == 503
    runtime.auth.settings.oidc_introspection_secret = ""
    assert client.get("/api/status", headers={"authorization": "Bearer access"}).status_code == 503


def test_auth_redirects_and_refresh_reject_foreign_origins(api_app):
    client, _, _ = api_app
    assert client.get("/api/auth/login", params={"return_to": "https://evil.test"}, follow_redirects=False).status_code == 400
    assert client.get("/api/auth/login", follow_redirects=False).status_code == 400  # callback not registered for this host
    for path in ("/api/auth/refresh", "/api/auth/logout"):
        assert client.post(path, headers={"origin": "https://evil.test"}).status_code == 403
    assert client.post("/api/auth/refresh").status_code == 401
