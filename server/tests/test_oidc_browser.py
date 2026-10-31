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
    store.add_org("acme")
    store.add_user("alice", "alice-password", role="member", tenant="acme")
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


def authorize(api, issuer, settings, user: str = "alice", password: str = "alice-password"):
    login = api.get("/api/auth/login", params={"return_to": settings.oidc_browser_origins[0]})
    assert login.status_code == 302, login.text
    params = parse_qs(urlsplit(login.headers["location"]).query)
    assert params["code_challenge_method"] == ["S256"]
    assert params["nonce"] and params["state"] and params["code_challenge"]
    form = issuer.get(login.headers["location"])
    transaction = re.search(r'name="transaction" value="([^"]+)"', form.text).group(1)
    approved = issuer.post("/authorize", data={
        "transaction": transaction, "username": user, "password": password, "approve": "yes",
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
    assert any("mesh_accounts=" in value and "Path=/api/auth" in value and "HttpOnly" in value for value in cookies)
    assert all("Domain=" not in value for value in cookies)
    assert api.get("/api/auth/status").json()["identity"]["user"] == "alice"
    assert api.get("/api/status").status_code == 200
    old_refresh = api.cookies.get("mesh_accounts")
    assert api.post("/api/auth/refresh", headers={"Origin": settings.oidc_browser_origins[0]}).status_code == 200
    assert api.cookies.get("mesh_accounts") != old_refresh
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


def held_refresh_token(api, user: str) -> str:
    """The refresh token the browser holds for user (the cookie is signed, not encrypted: it's the browser's own)."""
    from itsdangerous import URLSafeSerializer

    _, data = URLSafeSerializer("unused").loads_unsafe(api.cookies.get("mesh_accounts"))
    return data["accounts"][user]


def test_one_browser_holds_several_sign_ins_and_switches_between_them(browser):
    api, issuer, settings = browser
    origin = {"Origin": settings.oidc_browser_origins[0]}
    whoami = lambda: (api.get("/api/auth/status").json()["identity"] or {}).get("user")
    listed = lambda: api.get("/api/auth/accounts").json()
    assert listed() == {"active": None, "accounts": [], "max": 5}
    assert api.get(authorize(api, issuer, settings)).status_code == 307
    assert api.get(authorize(api, issuer, settings, "admin", "admin-password")).status_code == 307
    # Signing in again adds an account and makes it the active one; the other stays signed in.
    assert listed()["active"] == "admin" and [item["user"] for item in listed()["accounts"]] == ["admin", "alice"]
    assert whoami() == "admin"
    admin_access = api.cookies.get(COOKIE)

    switched = api.post("/api/auth/switch", json={"user": "alice"}, headers=origin)
    assert switched.status_code == 200 and switched.json()["active"] == "alice"
    assert whoami() == "alice" and api.cookies.get(COOKIE) != admin_access
    assert api.post("/api/auth/switch", json={"user": "mallory"}, headers=origin).status_code == 404
    assert api.post("/api/auth/switch", json={"user": "admin"}, headers={"Origin": "https://evil.test"}).status_code == 403

    # Signing out of one that isn't active leaves the active one alone, and revokes the other's sign-in.
    admin_refresh = held_refresh_token(api, "admin")
    out = api.post("/api/auth/logout", json={"user": "admin"}, headers=origin)
    assert out.status_code == 200 and out.json()["accounts"] == [{"user": "alice", "active": True}]
    assert whoami() == "alice"
    revoked = issuer.post("/token", data={"client_id": settings.oidc_console_client, "grant_type": "refresh_token", "refresh_token": admin_refresh})
    assert revoked.status_code == 400

    # Signing out of the active one makes the next one active.
    assert api.get(authorize(api, issuer, settings, "admin", "admin-password")).status_code == 307
    assert whoami() == "admin"
    out = api.post("/api/auth/logout", headers=origin).json()
    assert out["active"] == "alice" and whoami() == "alice"

    # Signing in to the same account again keeps one entry, and its older sign-in is revoked.
    older = held_refresh_token(api, "alice")
    assert api.get(authorize(api, issuer, settings)).status_code == 307
    assert [item["user"] for item in listed()["accounts"]] == ["alice"]
    assert issuer.post("/token", data={"client_id": settings.oidc_console_client, "grant_type": "refresh_token", "refresh_token": older}).status_code == 400

    # Everyone at once.
    assert api.post("/api/auth/logout", json={"all": True}, headers=origin).json() == {"identity": None, "active": None, "accounts": [], "max": 5}
    assert whoami() is None and api.get("/api/status").status_code == 401
    assert api.post("/api/auth/refresh", headers=origin).status_code == 401


def test_a_tampered_account_cookie_is_no_sign_in_and_an_older_one_is_carried_over(browser):
    api, issuer, settings = browser
    origin = {"Origin": settings.oidc_browser_origins[0]}
    assert api.get(authorize(api, issuer, settings)).status_code == 307
    refresh_token = held_refresh_token(api, "alice")
    api.cookies.clear()
    api.cookies.set("mesh_accounts", "eyJhY3RpdmUiOiJhZG1pbiJ9.forged", domain="api.aicontrolmesh.com", path="/api/auth")
    assert api.get("/api/auth/accounts").json()["accounts"] == []
    assert api.post("/api/auth/refresh", headers=origin).status_code == 401
    # A browser signed in before several sign-ins: its one refresh cookie becomes a named account.
    api.cookies.clear()
    api.cookies.set("mesh_refresh", refresh_token, domain="api.aicontrolmesh.com", path="/api/auth")
    assert api.post("/api/auth/refresh", headers=origin).status_code == 200
    assert api.get("/api/auth/accounts").json()["accounts"] == [{"user": "alice", "active": True}]
    assert api.get("/api/auth/status").json()["identity"]["user"] == "alice"
