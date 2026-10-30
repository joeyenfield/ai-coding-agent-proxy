"""The API's browser PKCE flow uses the real issuer, including renewal and revocation."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_server.app import create_app
from agent_server.auth import COOKIE, UserStore
from agent_server.config import Settings
from agent_server.issuer import create_auth_app, digest


@pytest.fixture
def browser(tmp_path, monkeypatch):
    settings = Settings(
        log_dir=tmp_path / "logs", 
        oidc_state_dir=tmp_path / "issuer", oidc_introspection_secret="browser-introspection-secret",
        console_origins=["https://console.aicontrolmesh.com"],
    )
    store = UserStore(settings.open_database())
    store.add_user("admin", "admin-password")
    store.add_user("alice", "alice-password", role="member")
    issuer_app = create_auth_app(settings)
    with TestClient(issuer_app, base_url=settings.oidc_issuer) as issuer:
        def transport(request):
            assert str(request.url).startswith(settings.oidc_issuer + "/")
            result = issuer.request(request.method, str(request.url), headers=request.headers,
                                    content=request.content, follow_redirects=False)
            return httpx.Response(result.status_code, headers=result.headers, content=result.content)

        original_init = httpx.AsyncClient.__init__

        def async_init(self, *args, **kwargs):
            kwargs.setdefault("transport", httpx.MockTransport(transport))
            original_init(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", async_init)
        api_app = create_app(settings)
        api_app.state.runtime.auth.client.close()
        api_app.state.runtime.auth.client = httpx.Client(transport=httpx.MockTransport(transport))
        with TestClient(api_app, base_url="https://api.aicontrolmesh.com", follow_redirects=False) as api:
            yield api, issuer, settings


def authorize(api, issuer, settings):
    login = api.get("/api/auth/login", params={"return_to": settings.oidc_browser_origins[0]})
    assert login.status_code == 302, login.text
    params = parse_qs(urlsplit(login.headers["location"]).query)
    assert params["code_challenge_method"] == ["S256"]
    assert params["nonce"] and params["state"] and params["code_challenge"]
    form = issuer.get(login.headers["location"])
    transaction = re.search(r'name="transaction" value="([^"]+)"', form.text).group(1)
    approved = issuer.post("/authorize", data={
        "transaction": transaction, "username": "alice", "password": "alice-password", "approve": "yes",
    }, headers={"Origin": settings.oidc_issuer}, follow_redirects=False)
    assert approved.status_code == 302, approved.text
    return approved.headers["location"]


def test_real_browser_login_refresh_and_logout(browser):
    api, issuer, settings = browser
    callback = authorize(api, issuer, settings)
    response = api.get(callback)
    assert response.status_code == 307, response.text
    assert response.headers["location"] == settings.oidc_browser_origins[0]
    cookies = response.headers.get_list("set-cookie")
    assert any("ai_proxy_session=" in value and "HttpOnly" in value and "Secure" in value for value in cookies)
    assert any("mesh_refresh=" in value and "Path=/api/auth" in value for value in cookies)
    assert all("Domain=" not in value for value in cookies)
    assert api.get("/api/auth/status").json()["identity"]["user"] == "alice"
    assert api.get("/api/status").status_code == 200
    old_refresh = api.cookies.get("mesh_refresh")
    assert api.post("/api/auth/refresh", headers={"Origin": settings.oidc_browser_origins[0]}).status_code == 200
    assert api.cookies.get("mesh_refresh") != old_refresh
    current_access = api.cookies.get(COOKIE)
    assert api.post("/api/auth/logout", headers={"Origin": settings.oidc_browser_origins[0]}).status_code == 200
    assert api.get("/api/status").status_code == 401
    assert api.get("/api/status", headers={"Authorization": "Bearer " + current_access}).status_code == 401
    # A replayed callback signs nobody in; the browser goes back to the console's sign-in page.
    replayed = api.get(callback, follow_redirects=False)
    assert replayed.status_code == 303 and replayed.headers["location"].endswith("/?sign_in=failed")
    assert COOKIE + "=" not in replayed.headers.get("set-cookie", "")


def test_callback_rejects_unbound_state_and_refresh_rejects_cross_origin(browser):
    api, issuer, settings = browser
    callback = authorize(api, issuer, settings)
    parsed = urlsplit(callback)
    query = parse_qs(parsed.query)
    query["state"] = ["not-the-browser-state"]
    forged = urlunsplit(parsed._replace(query=urlencode(query, doseq=True)))
    refused = api.get(forged, follow_redirects=False)
    assert refused.status_code == 303 and refused.headers["location"].endswith("/?sign_in=failed")
    assert api.get("/api/auth/status").json()["identity"] is None
    assert api.post("/api/auth/refresh", headers={"Origin": "https://evil.test"}).status_code == 403


def test_a_signed_in_console_approves_a_device_code_without_a_password(browser):
    api, issuer, settings = browser
    assert api.get(authorize(api, issuer, settings)).status_code == 307
    asked = issuer.post("/device_authorization", data={"client_id": settings.oidc_cli_client, "scope": "mesh-api offline_access"}).json()
    poll = {"client_id": settings.oidc_cli_client, "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": asked["device_code"]}
    device = lambda code, action: api.post("/api/auth/device", json={"user_code": code, "action": action})
    assert device("BBBB-CCCC", "check").status_code == 404
    # Check says what asked, and decides nothing.
    checked = device(asked["user_code"].lower(), "check")
    assert checked.status_code == 200, checked.text
    assert checked.json()["client"] == "agent or agent-machine" and checked.json()["user"] == "alice"
    assert issuer.post("/token", data=poll).json()["error"] == "authorization_pending"
    assert device(asked["user_code"], "approve").json() == {"approved": True}
    service = issuer.app.state.issuer
    with service.state.transaction():  # the next poll comes as if after the interval
        record = service.state.get("device", digest(asked["device_code"]))
        service.state.put("device", digest(asked["device_code"]), {**record, "last_poll": 0})
    tokens = issuer.post("/token", data=poll)
    assert tokens.status_code == 200 and tokens.json()["refresh_token"], tokens.text
    # A code is decided once, and a command-line token can't approve devices.
    assert device(asked["user_code"], "deny").status_code == 409
    another = issuer.post("/device_authorization", data={"client_id": settings.oidc_cli_client, "scope": "mesh-api offline_access"}).json()
    cli = api.post("/api/auth/device", json={"user_code": another["user_code"], "action": "approve"},
                   headers={"Authorization": "Bearer " + tokens.json()["access_token"]})
    assert cli.status_code == 403
    # Without any sign-in the API refuses before the issuer is asked.
    api.cookies.clear()
    assert device(another["user_code"], "approve").status_code == 401
