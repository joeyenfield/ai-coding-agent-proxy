"""End-to-end issuer grants, browser boundaries and credential lifecycle."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import re
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import KeySet
import pytest

from agent_server.auth import UserStore
from agent_server.config import Settings
from agent_server.db import schema
from agent_server.issuer import ACCESS_TTL, DEVICE_GRANT, Issuer, create_auth_app, digest
from oidc_helpers import stored_rows


@pytest.fixture
def issuer(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_PROXY_USERNAME", raising=False)
    monkeypatch.delenv("AI_PROXY_PASSWORD", raising=False)
    settings = Settings(log_dir=tmp_path / "logs", oidc_state_dir=tmp_path / "issuer",
                        oidc_introspection_secret="api-introspection-secret")
    store = UserStore(settings.open_database())
    store.add_user("admin", "admin-password")
    store.add_org("acme")
    store.add_user("alice", "alice-password", role="member", tenant="acme")
    app = create_auth_app(settings)
    with TestClient(app, base_url=settings.oidc_issuer) as client:
        yield client, app.state.issuer, store
    app.state.issuer.state.db.close()


def auth_params(service, **changes):
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    data = {"client_id": service.settings.oidc_console_client, "redirect_uri": service.settings.oidc_redirect_uris[0],
            "response_type": "code", "scope": "openid profile offline_access mesh-api",
            "code_challenge": challenge, "code_challenge_method": "S256", "state": "browser-state", "nonce": "browser-nonce"}
    return {**data, **changes}, verifier


def transaction(response):
    assert response.status_code == 200, response.text
    return re.search(r'name="transaction" value="([^"]+)"', response.text).group(1)


def browser_code(client, service, *, username="alice", password="alice-password", **changes):
    params, verifier = auth_params(service, **changes)
    page = client.get("/authorize", params=params)
    # Under no-referrer, browsers post the form with `Origin: null`, which the issuer refuses.
    assert page.headers["referrer-policy"] == "same-origin"
    # form-action also covers the redirect back to the client's callback.
    callback = urlsplit(params["redirect_uri"])
    assert f"form-action 'self' {callback.scheme}://{callback.netloc}" in page.headers["content-security-policy"]
    # And the console the callback then redirects to, or the browser stays on the signed-in form.
    for console in service.settings.oidc_browser_origins:
        assert console in page.headers["content-security-policy"].split("form-action", 1)[1].split(";", 1)[0]
    tx = transaction(page)
    result = client.post("/authorize", data={"transaction": tx, "username": username, "password": password, "approve": "yes"},
                         headers={"Origin": service.issuer}, follow_redirects=False)
    assert result.status_code == 302, result.text
    query = parse_qs(urlsplit(result.headers["location"]).query)
    assert query["state"] == [params["state"]]
    return query["code"][0], verifier, params


def exchange(client, service, code, verifier, params):
    return client.post("/token", data={"grant_type": "authorization_code", "client_id": params["client_id"],
                                      "code": code, "code_verifier": verifier, "redirect_uri": params["redirect_uri"]})


def introspect(client, service, token, secret=None):
    return client.post("/introspect", data={"token": token},
                       auth=(service.settings.oidc_introspection_client, secret or service.settings.oidc_introspection_secret))


def refresh(client, service, token, client_id=None):
    return client.post("/token", data={"grant_type": "refresh_token", "client_id": client_id or service.settings.oidc_console_client,
                                      "refresh_token": token})


def test_password_limit_follows_account_across_source_addresses(issuer):
    client, service, store = issuer
    params, verifier = auth_params(service)
    tx = transaction(client.get("/authorize", params=params))
    for attempt in range(31):
        with TestClient(client.app, base_url=service.issuer, client=(f"198.51.100.{attempt + 1}", 50000)) as visitor:
            visitor.cookies.update(client.cookies)
            response = visitor.post("/authorize", data={
                "transaction": tx, "username": " ALICE ", "password": "wrong", "approve": "yes",
            }, headers={"Origin": service.issuer})
            assert response.status_code == (401 if attempt < 30 else 429)


def test_browser_pkce_discovery_jwks_and_no_secret_storage(issuer, caplog):
    client, service, store = issuer
    before = stored_rows(store.db, schema.users)
    assert client.get("/health").json() == {"status": "ok"}
    discovery = client.get("/.well-known/openid-configuration").json()
    assert discovery["issuer"] == service.issuer
    assert discovery["code_challenge_methods_supported"] == ["S256"]
    assert discovery["id_token_signing_alg_values_supported"] == ["RS256"]
    code, verifier, params = browser_code(client, service)
    with caplog.at_level(logging.DEBUG):
        response = exchange(client, service, code, verifier, params)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    tokens = response.json()
    assert tokens["expires_in"] == ACCESS_TTL
    keys = KeySet.import_key_set(client.get(discovery["jwks_uri"]).json())
    claims = jwt.decode(tokens["id_token"], keys, algorithms=["RS256"]).claims
    assert claims["iss"] == service.issuer
    assert claims["aud"] == [params["client_id"]]
    assert claims["nonce"] == params["nonce"]
    assert claims["preferred_username"] == "alice"
    result = introspect(client, service, tokens["access_token"]).json()
    assert result["active"] is True and result["username"] == "alice"
    assert result["aud"] == service.settings.oidc_audience and result["token_type"] == "Bearer"
    assert "role" not in result
    info = client.get("/userinfo", headers={"Authorization": "Bearer " + tokens["access_token"]}).json()
    assert info["sub"] == claims["sub"] and info["preferred_username"] == "alice"
    assert exchange(client, service, code, verifier, params).json()["error"] == "invalid_grant"
    assert stored_rows(store.db, schema.users) == before
    stored = json.dumps(stored_rows(service.state.db, schema.records)) + repr(stored_rows(service.state.db, schema.auth_secrets))
    for secret in (tokens["access_token"], tokens["refresh_token"], code, verifier, "alice-password"):
        assert secret not in stored
        assert secret not in caplog.text
    assert "d" not in client.get("/.well-known/jwks.json").json()["keys"][0]


@pytest.mark.parametrize("change", [
    {"client_id": "unknown"}, {"redirect_uri": "https://evil.example/callback"},
    {"redirect_uri": "https://api.aicontrolmesh.com/api/auth/callback?extra=yes"},
    {"response_type": "token"}, {"nonce": ""}, {"state": ""},
    {"code_challenge": ""}, {"code_challenge_method": "plain"}, {"scope": "openid administrator"},
])
def test_invalid_authorization_never_redirects(issuer, change):
    client, service, _ = issuer
    params, _ = auth_params(service, **change)
    response = client.get("/authorize", params=params, follow_redirects=False)
    assert response.status_code in {400, 401}
    assert "location" not in response.headers


def test_code_binding_nonce_replay_and_strict_forms(issuer):
    client, service, _ = issuer
    code, verifier, params = browser_code(client, service)
    wrong = exchange(client, service, code, "w" * 64, params)
    assert wrong.status_code == 400
    assert exchange(client, service, code, verifier, {**params, "redirect_uri": "https://evil.example"}).status_code == 400
    assert exchange(client, service, code, verifier, {**params, "client_id": service.settings.oidc_cli_client}).status_code == 400
    assert exchange(client, service, code, verifier, params).status_code == 200
    assert client.get("/authorize", params=params).status_code == 400
    assert client.post("/token", content="client_id=mesh-cli&client_id=mesh-console",
                       headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 400
    assert client.post("/token", json={"client_id": "mesh-cli"}).status_code == 415
    assert client.post("/token", content="x=" + "a" * 17000,
                       headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 413
    assert client.post("/token", data={"client_id": service.settings.oidc_cli_client, "grant_type": "password"}).json()["error"] == "unsupported_grant_type"


def test_browser_csrf_host_cookie_and_password_is_auth_only(issuer):
    client, service, _ = issuer
    params, _ = auth_params(service)
    response = client.get("/authorize", params=params)
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" in cookie and "Domain=" not in cookie
    tx = transaction(response)
    assert 'action="/authorize"' in response.text
    form = {"transaction": tx, "username": "alice", "password": "alice-password", "approve": "yes"}
    assert client.post("/authorize", data=form).status_code == 403
    assert client.post("/authorize", data=form, headers={"Origin": "https://console.aicontrolmesh.com"}).status_code == 403
    assert client.post("/authorize", data={**form, "transaction": "wrong"}, headers={"Origin": service.issuer}).status_code == 403
    # A second browser cannot borrow an authorization transaction.
    client.cookies.clear()
    assert client.post("/authorize", data=form, headers={"Origin": service.issuer}).status_code == 403
    tx = transaction(client.get("/authorize", params=params))
    denied = client.post("/authorize", data={**form, "transaction": tx, "password": "wrong"},
                         headers={"Origin": service.issuer})
    assert denied.status_code == 401 and "alice-password" not in denied.text and "location" not in denied.headers


def test_sign_in_pages_are_branded_retryable_and_independent(issuer):
    client, service, _ = issuer
    params, _ = auth_params(service)
    origin = {"Origin": service.issuer}
    page = client.get("/authorize", params=params)
    assert "AI Control Mesh" in page.text and 'href="/sign-in.css"' in page.text and "<script" not in page.text
    assert "style-src 'self'" in page.headers["content-security-policy"]
    css = client.get("/sign-in.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css") and "--accent" in css.text
    # Two pages open at once (two tabs, or the console starting over) each keep working.
    other, _ = auth_params(service, state="second-tab", nonce="second-tab-nonce")
    first, second = transaction(page), transaction(client.get("/authorize", params=other))
    form = {"username": "alice", "password": "alice-password", "approve": "yes"}
    # A wrong password shows the same form again, with the name kept and a message, as HTML.
    wrong = client.post("/authorize", data={**form, "transaction": first, "password": "wrong"}, headers=origin)
    assert wrong.status_code == 401 and wrong.headers["content-type"].startswith("text/html")
    assert "Wrong username or password." in wrong.text and 'value="alice"' in wrong.text and first in wrong.text
    signed_in = client.post("/authorize", data={**form, "transaction": first}, headers=origin, follow_redirects=False)
    assert signed_in.status_code == 302 and "code=" in signed_in.headers["location"]
    assert client.post("/authorize", data={**form, "transaction": second}, headers=origin, follow_redirects=False).status_code == 302
    # A spent page explains itself and offers the console, instead of a JSON error.
    spent = client.post("/authorize", data={**form, "transaction": first}, headers=origin)
    assert spent.status_code == 403 and "This page has expired" in spent.text
    assert f'href="{service.settings.oidc_browser_origins[0]}"' in spent.text
    assert not [name for name in client.cookies.keys() if name.startswith(service.cookie)]
    broken = client.get("/authorize", params={**params, "redirect_uri": "https://evil.test/callback"})
    assert broken.status_code == 400 and "This sign-in link isn&#x27;t valid" in broken.text and "evil.test" not in broken.text


def test_device_page_keeps_what_was_typed_after_a_mistake(issuer):
    client, service, _ = issuer
    device = device_issue(client, service)
    tx = transaction(client.get("/device"))
    form = {"transaction": tx, "username": "alice", "password": "alice-password", "approve": "yes"}
    mistyped = client.post("/device", data={**form, "user_code": "BBBB-CCCC"}, headers={"Origin": service.issuer})
    assert mistyped.status_code == 400 and "That code isn&#x27;t right" in mistyped.text and 'value="BBBB-CCCC"' in mistyped.text
    approved = client.post("/device", data={**form, "user_code": device["user_code"]}, headers={"Origin": service.issuer})
    assert approved.status_code == 200 and "Device approved" in approved.text and "AI Control Mesh" in approved.text


def device_issue(client, service):
    result = client.post("/device_authorization", data={"client_id": service.settings.oidc_cli_client,
                                                       "scope": "mesh-api offline_access"})
    assert result.status_code == 200, result.text
    return result.json()


def poll(client, service, device):
    return client.post("/token", data={"client_id": service.settings.oidc_cli_client,
                                      "grant_type": DEVICE_GRANT, "device_code": device["device_code"]})


def advance_poll(service, device):
    key = digest(device["device_code"])
    with service.state.transaction():
        data = service.state.get("device", key)
        data["last_poll"] = 0
        service.state.put("device", key, data)


def test_device_pending_slowdown_explicit_approval_and_single_use(issuer):
    client, service, _ = issuer
    device = device_issue(client, service)
    assert device["verification_uri"] == service.issuer + "/device"
    assert poll(client, service, device).json()["error"] == "authorization_pending"
    assert poll(client, service, device).json()["error"] == "slow_down"
    tx = transaction(client.get("/device", params={"user_code": device["user_code"]}))
    # GET and automatic visits never approve the code.
    advance_poll(service, device)
    assert poll(client, service, device).json()["error"] == "authorization_pending"
    form = {"transaction": tx, "user_code": device["user_code"], "username": "alice", "password": "alice-password"}
    assert client.post("/device", data=form, headers={"Origin": service.issuer}).status_code == 400
    assert client.post("/device", data={**form, "approve": "yes"}, headers={"Origin": service.issuer}).status_code == 200
    advance_poll(service, device)
    tokens = poll(client, service, device)
    assert tokens.status_code == 200, tokens.text
    assert "refresh_token" in tokens.json()
    assert poll(client, service, device).status_code == 400
    assert introspect(client, service, tokens.json()["access_token"]).json()["username"] == "alice"


def test_device_denial_is_authenticated_csrf_bound_and_final(issuer):
    client, service, _ = issuer
    device = device_issue(client, service)
    page = client.get("/device")
    assert 'name="approve" value="no"' in page.text
    tx = transaction(page)
    form = {"transaction": tx, "user_code": device["user_code"],
            "username": "alice", "password": "alice-password", "approve": "no"}
    assert client.post("/device", data=form).status_code == 403
    assert client.post("/device", data={**form, "password": "wrong"},
                       headers={"Origin": service.issuer}).status_code == 401
    assert poll(client, service, device).json()["error"] == "authorization_pending"
    denied = client.post("/device", data=form, headers={"Origin": service.issuer})
    assert denied.status_code == 200 and "Device denied" in denied.text
    assert service.cookie not in client.cookies
    # Neither replaying the transaction nor starting a fresh form can reverse denial.
    assert client.post("/device", data={**form, "approve": "yes"},
                       headers={"Origin": service.issuer}).status_code == 403
    fresh = transaction(client.get("/device"))
    assert client.post("/device", data={**form, "transaction": fresh, "approve": "yes"},
                       headers={"Origin": service.issuer}).status_code == 400
    for _ in range(2):
        advance_poll(service, device)
        response = poll(client, service, device)
        assert response.status_code == 400
        assert response.json()["error"] == "access_denied"
        assert "access_token" not in response.json()
    assert not [row for row in stored_rows(service.state.db, schema.records) if row["kind"] in ("access", "refresh")]


def test_device_expiration_wrong_client_and_guess_limit(issuer):
    client, service, _ = issuer
    device = device_issue(client, service)
    wrong = client.post("/token", data={"client_id": service.settings.oidc_console_client, "grant_type": DEVICE_GRANT,
                                      "device_code": device["device_code"]})
    assert wrong.status_code == 400
    with service.state.transaction():
        data = service.state.get("device", digest(device["device_code"]))
        data["exp"] = 0
        service.state.put("device", digest(device["device_code"]), data)
    assert poll(client, service, device).json()["error"] == "expired_token"
    tx = transaction(client.get("/device"))
    form = {"transaction": tx, "user_code": "XXXX-XXXX", "username": "alice", "password": "alice-password", "approve": "yes"}
    for _ in range(10):
        assert client.post("/device", data=form, headers={"Origin": service.issuer}).status_code == 400
    assert client.post("/device", data=form, headers={"Origin": service.issuer}).status_code == 429


def test_refresh_rotation_replay_revokes_whole_family(issuer):
    client, service, _ = issuer
    code, verifier, params = browser_code(client, service)
    original = exchange(client, service, code, verifier, params).json()
    assert refresh(client, service, original["refresh_token"], service.settings.oidc_cli_client).status_code == 400
    rotated = refresh(client, service, original["refresh_token"])
    assert rotated.status_code == 200, rotated.text
    rotated = rotated.json()
    assert rotated["refresh_token"] != original["refresh_token"]
    assert introspect(client, service, rotated["access_token"]).json()["active"]
    # A retry moments later (its answer was lost on the way back) is allowed once and revokes nothing.
    retried = refresh(client, service, original["refresh_token"])
    assert retried.status_code == 200, retried.text
    assert introspect(client, service, rotated["access_token"]).json()["active"]
    # Any further replay is theft: the whole family goes.
    assert refresh(client, service, original["refresh_token"]).json()["error"] == "invalid_grant"
    assert refresh(client, service, rotated["refresh_token"]).status_code == 400
    for token in (original["access_token"], rotated["access_token"], retried.json()["access_token"]):
        assert introspect(client, service, token).json() == {"active": False}


def test_a_refresh_token_replayed_after_the_grace_revokes_its_family(issuer, monkeypatch):
    client, service, _ = issuer
    monkeypatch.setattr("agent_server.issuer.REFRESH_REUSE_GRACE", -1)
    code, verifier, params = browser_code(client, service)
    original = exchange(client, service, code, verifier, params).json()
    rotated = refresh(client, service, original["refresh_token"]).json()
    assert refresh(client, service, original["refresh_token"]).json()["error"] == "invalid_grant"
    assert introspect(client, service, rotated["access_token"]).json() == {"active": False}


def test_refresh_absolute_expiry_scopes_and_revocation_ownership(issuer):
    client, service, _ = issuer
    code, verifier, params = browser_code(client, service)
    tokens = exchange(client, service, code, verifier, params).json()
    assert client.post("/token", data={"client_id": params["client_id"], "grant_type": "refresh_token",
                                      "refresh_token": tokens["refresh_token"], "scope": "mesh-api admin"}).status_code == 400
    assert client.post("/revoke", data={"client_id": service.settings.oidc_cli_client, "token": tokens["refresh_token"]}).status_code == 200
    assert introspect(client, service, tokens["access_token"]).json()["active"]
    assert client.post("/revoke", data={"client_id": params["client_id"], "token": tokens["refresh_token"]}).status_code == 200
    assert introspect(client, service, tokens["access_token"]).json() == {"active": False}
    code, verifier, params = browser_code(client, service, nonce="second")
    tokens = exchange(client, service, code, verifier, params).json()
    with service.state.transaction():
        record = service.state.get("refresh", digest(tokens["refresh_token"]))
        family = service.state.get("family", record["family"])
        family["exp"] = 0
        service.state.put("family", record["family"], family)
    assert refresh(client, service, tokens["refresh_token"]).status_code == 400


def test_managed_api_credentials_lifecycle_and_tenant_boundary(issuer):
    client, service, store = issuer
    store.add_user("org-admin", "org-admin-password", role="admin", tenant="acme")

    def console(user, password):
        code, verifier, params = browser_code(client, service, username=user, password=password, nonce=user)
        return exchange(client, service, code, verifier, params).json()["access_token"]

    admin_token = console("admin", "admin-password")
    org_token = console("org-admin", "org-admin-password")
    member_token = console("alice", "alice-password")

    def manage(token, org="root", **data):
        return client.post("/api-credentials", data={"token": token, "org": org, **data},
                           auth=(service.settings.oidc_introspection_client, service.settings.oidc_introspection_secret))

    assert manage(member_token, "acme", action="list").status_code == 403
    assert manage(org_token, action="list").status_code == 403
    assert manage(org_token, "acme", action="create", user="admin").status_code == 400
    assert manage(org_token, "acme", action="create", user="org-admin", bootstrap="true").status_code == 400
    created = manage(admin_token, action="create", user="admin", bootstrap="true", label="Initial setup")
    assert created.status_code == 200, created.text
    credential = created.json()
    client_id = credential["client_id"]
    assert credential["bootstrap"] and credential["active"]
    listed = manage(admin_token, action="list").json()["credentials"]
    assert listed[0]["label"] == "Initial setup"
    assert credential["client_secret"] not in json.dumps(listed)
    assert "secret_hash" not in json.dumps(listed)

    def mint(secret):
        return client.post("/token", data={"grant_type": "client_credentials", "scope": "mesh-api"}, auth=(client_id, secret))

    service.jwt = True
    token = mint(credential["client_secret"]).json()["access_token"]
    assert token.count(".") != 2  # Must recheck the client's role and rotation even in a JWT deployment.
    assert introspect(client, service, token).json()["active"]
    assert manage(token, action="create", user="admin").status_code == 403
    assert manage(org_token, "acme", action="revoke", id=client_id).status_code == 404
    rotated = manage(admin_token, action="regenerate", id=client_id).json()
    assert mint(credential["client_secret"]).status_code == 401
    assert introspect(client, service, token).json() == {"active": False}
    current = mint(rotated["client_secret"]).json()["access_token"]
    assert introspect(client, service, current).json()["active"]
    assert manage(admin_token, action="revoke", id=client_id).status_code == 200
    assert introspect(client, service, current).json() == {"active": False}
    assert mint(rotated["client_secret"]).status_code == 400
    assert manage(admin_token, action="regenerate", id=client_id).status_code == 409
    assert manage(admin_token, action="list").json()["credentials"][0]["revoked"]
    assert manage(admin_token, action="create", user="admin").status_code == 200

    made = manage(org_token, "acme", action="create", user="org-admin").json()
    client_id = made["client_id"]
    token = mint(made["client_secret"]).json()["access_token"]
    store.add_user("other-admin", "other-admin-password", role="admin", tenant="acme")
    store.update_user("org-admin", role="member")
    assert introspect(client, service, token).json() == {"active": False}
    assert mint(made["client_secret"]).status_code == 400


def test_environment_admin_can_own_management_credentials(issuer, monkeypatch):
    client, service, _ = issuer
    monkeypatch.setenv("AI_PROXY_USERNAME", "operator")
    monkeypatch.setenv("AI_PROXY_PASSWORD", "operator-password")
    # It's a site admin the directory lists, but only inside the root organization.
    identity = service.auth.identity_for("operator", "token")
    assert identity is not None and identity.site_admin
    listed = {user["name"]: user for user in service.auth.user_list(identity)}
    assert listed["operator"]["environment"] is True and listed["operator"]["tenant"] == "root"
    alice = service.auth.identity_for("alice", "token")
    assert "operator" not in {user["name"] for user in service.auth.user_list(alice)}
    # Never a plain service client: the member-only rule still holds.
    with service.state.transaction():
        with pytest.raises(ValueError, match="member account"):
            service.provision("automation", "operator")
        secret = service.provision("initial-setup", "operator", management=True, bootstrap=True, label="Setup")
    minted = client.post("/token", data={"grant_type": "client_credentials", "scope": "mesh-api"},
                         auth=("initial-setup", secret))
    assert minted.status_code == 200, minted.text
    record = introspect(client, service, minted.json()["access_token"]).json()
    assert record["active"] and record["username"] == "operator"
    # The version every token and client record carries never reveals a fast digest of the password.
    import hashlib

    version = service.auth.account_version("operator")
    assert version and version != hashlib.sha256(b"operator-password").hexdigest()
    assert service.client("initial-setup").version == version
    # Signed in as that account, the console can create one in the root organization too.
    code, verifier, params = browser_code(client, service, username="operator", password="operator-password", nonce="operator")
    console = exchange(client, service, code, verifier, params).json()["access_token"]
    created = client.post("/api-credentials", data={"token": console, "org": "root", "action": "create",
                                                    "user": "operator", "bootstrap": "true", "label": "Console"},
                          auth=(service.settings.oidc_introspection_client, service.settings.oidc_introspection_secret))
    assert created.status_code == 200, created.text
    assert created.json()["tenant"] == "root" and created.json()["active"]


def test_api_credential_can_provision_api_data_and_revoke_ends_access(issuer, monkeypatch):
    import httpx

    from agent_server.app import create_app

    client, service, store = issuer
    code, verifier, params = browser_code(client, service, username="admin", password="admin-password")
    console_token = exchange(client, service, code, verifier, params).json()["access_token"]
    api_app = create_app(service.settings)
    resource_auth = api_app.state.runtime.auth
    resource_auth.client.close()

    def relay_request(request):
        response = client.request(request.method, request.url.path, content=request.content, headers=request.headers)
        return httpx.Response(response.status_code, content=response.content, headers=response.headers)

    resource_auth.client = httpx.Client(transport=httpx.MockTransport(relay_request), trust_env=False)
    with TestClient(api_app) as api:
        console_headers = {"Authorization": "Bearer " + console_token}
        path = "/api/orgs/root/api-credentials"
        assert api.get(path).status_code == 401
        created = api.post(path, json={"user": "admin", "label": "Bootstrap", "bootstrap": True}, headers=console_headers)
        assert created.status_code == 200, created.text
        assert created.headers["cache-control"] == "no-store"
        credential = created.json()
        token = client.post("/token", data={"grant_type": "client_credentials"},
                            auth=(credential["client_id"], credential["client_secret"])).json()["access_token"]
        headers = {"Authorization": "Bearer " + token}
        assert api.post("/api/orgs", json={"id": "test-data", "name": "Test data"}, headers=headers).status_code == 201
        assert api.post("/api/users", json={"name": "test-user", "password": "test-password", "tenant": "test-data"}, headers=headers).status_code == 201
        assert store.user("test-user")["tenant"] == "test-data"
        assert api.get(path, headers=headers).status_code == 403
        assert api.get(path, headers=console_headers).json()["credentials"][0]["bootstrap"]
        assert api.delete(path + "/" + credential["client_id"], headers=console_headers).status_code == 200
        assert api.get("/api/orgs", headers=headers).status_code == 401
        assert api.post(path, content=b"not json", headers={**console_headers, "content-type": "application/json"}).status_code == 400
        # The environment account joins the site-admin directory, and can own a credential for automation.
        monkeypatch.setenv("AI_PROXY_USERNAME", "operator")
        monkeypatch.setenv("AI_PROXY_PASSWORD", "operator-password")
        listed = {item["name"]: item for item in api.get("/api/users", headers=console_headers).json()["users"]}
        assert listed["operator"]["environment"] is True and listed["operator"]["tenant"] == "root"
        environment = api.post(path, json={"user": "operator", "label": "Environment", "bootstrap": True}, headers=console_headers)
        assert environment.status_code == 200, environment.text
        assert environment.json()["user"] == "operator" and environment.json()["tenant"] == "root"


def test_service_clients_introspection_auth_audience_and_no_admin_grant(issuer):
    client, service, _ = issuer
    with service.state.transaction():
        secret = service.provision("automation", "alice")
        with pytest.raises(ValueError):
            service.provision("admin-automation", "admin")
        with pytest.raises(ValueError):
            service.provision("bad-scope", "alice", "admin")
    assert client.post("/token", data={"grant_type": "client_credentials"}, auth=("automation", "wrong")).status_code == 401
    assert client.post("/token", data={"grant_type": "client_credentials", "client_id": "automation", "client_secret": secret}).status_code == 401
    assert client.post("/token", data={"grant_type": "client_credentials"}, auth=(service.settings.oidc_introspection_client,
                                                                                                service.settings.oidc_introspection_secret)).status_code == 400
    assert client.post("/token", data={"grant_type": "client_credentials", "audience": "https://evil.example"},
                       auth=("automation", secret)).json()["error"] == "invalid_target"
    result = client.post("/token", data={"grant_type": "client_credentials", "scope": "mesh-api"}, auth=("automation", secret))
    assert result.status_code == 200, result.text
    tokens = result.json()
    assert "refresh_token" not in tokens and "id_token" not in tokens
    assert introspect(client, service, tokens["access_token"], secret="wrong").status_code == 401
    assert client.post("/introspect", data={"token": tokens["access_token"]}, auth=("automation", secret)).status_code == 401
    record = introspect(client, service, tokens["access_token"]).json()
    assert record["active"] and record["username"] == "alice" and record["scope"] == "mesh-api"
    assert client.post("/revoke", data={"token": tokens["access_token"]}, auth=("automation", secret)).status_code == 200
    assert introspect(client, service, tokens["access_token"]).json() == {"active": False}


def test_password_changes_deleted_accounts_and_access_expiry(issuer):
    client, service, store = issuer
    code, verifier, params = browser_code(client, service)
    tokens = exchange(client, service, code, verifier, params).json()
    store.set_password("alice", "new-alice-password")
    assert introspect(client, service, tokens["access_token"]).json() == {"active": False}
    assert refresh(client, service, tokens["refresh_token"]).status_code == 400
    with service.state.transaction():
        secret = service.provision("automation", "alice")
    response = client.post("/token", data={"grant_type": "client_credentials"}, auth=("automation", secret))
    token = response.json()["access_token"]
    assert introspect(client, service, token).json()["active"]
    with service.state.transaction():
        data = service.state.get("access", digest(token))
        data["exp"] = 0
        service.state.put("access", digest(token), data)
    assert introspect(client, service, token).json() == {"active": False}
    store.remove_user("alice")
    store.add_user("alice", "new-alice-password", role="member", tenant="acme")
    assert client.post("/token", data={"grant_type": "client_credentials"}, auth=("automation", secret)).status_code == 400


def test_persistent_key_and_configuration_fail_closed(issuer):
    client, service, _ = issuer
    original = client.get("/.well-known/jwks.json").json()
    another = create_auth_app(service.settings)
    with TestClient(another, base_url=service.issuer) as other:
        assert other.get("/.well-known/jwks.json").json() == original
    another.state.issuer.state.db.close()
    settings = service.settings
    settings.oidc_introspection_secret = ""
    with pytest.raises(ValueError, match="required"):
        Issuer(settings)
    settings.oidc_introspection_secret = "secret"
    settings.oidc_issuer = "http://public.example"
    with pytest.raises(ValueError, match="HTTPS"):
        Issuer(settings)


def test_code_consumption_is_atomic_across_issuer_process_connections(issuer):
    client, service, _ = issuer
    code, verifier, params = browser_code(client, service)
    other_app = create_auth_app(service.settings)
    with TestClient(other_app, base_url=service.issuer) as other, ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(exchange, endpoint, service, code, verifier, params) for endpoint in (client, other)]
        results = [future.result() for future in futures]
    assert sorted(result.status_code for result in results) == [200, 400]
    denied = next(result for result in results if result.status_code == 400)
    assert denied.json()["error"] == "invalid_grant"


def test_browser_origins_cors_and_loopback_development(issuer):
    client, service, _ = issuer
    origin = service.settings.oidc_browser_origins[0]
    headers = {"Origin": origin, "Access-Control-Request-Method": "GET",
               "Access-Control-Request-Headers": "authorization"}
    response = client.options("/userinfo", headers=headers)
    assert response.status_code == 204 and response.headers["Access-Control-Allow-Origin"] == origin
    assert "Access-Control-Allow-Credentials" not in response.headers
    assert client.options("/userinfo", headers={**headers, "Origin": "https://evil.example"}).status_code == 403
    assert client.options("/userinfo", headers={**headers, "Access-Control-Request-Headers": "x-unknown"}).status_code == 403
    assert client.post("/token", data={"client_id": "mesh-cli"}, headers={"Origin": "https://evil.example"}).status_code == 403
    service.settings.oidc_issuer = "http://127.0.0.1:10000"
    dev_app = create_auth_app(service.settings)
    dev_service = dev_app.state.issuer
    with TestClient(dev_app, base_url=dev_service.issuer) as dev:
        code, verifier, params = browser_code(dev, dev_service, nonce="loopback-development")
        assert exchange(dev, dev_service, code, verifier, params).status_code == 200


def test_service_client_does_not_inherit_later_admin_promotion(issuer):
    client, service, store = issuer
    with service.state.transaction():
        secret = service.provision("automation", "alice")
    response = client.post("/token", data={"grant_type": "client_credentials"}, auth=("automation", secret))
    token = response.json()["access_token"]
    store.update_user("alice", role="admin")
    assert introspect(client, service, token).json() == {"active": False}
    assert client.post("/token", data={"grant_type": "client_credentials"}, auth=("automation", secret)).status_code == 400


def test_service_client_recreation_and_changed_audience_never_reactivate_tokens(issuer):
    client, service, _ = issuer
    with service.state.transaction():
        secret = service.provision("automation", "alice")
    tokens = client.post("/token", data={"grant_type": "client_credentials"}, auth=("automation", secret)).json()
    with service.state.transaction():
        service.state.delete("client", "automation")
        service.provision("automation", "alice")
    assert introspect(client, service, tokens["access_token"]).json() == {"active": False}
    code, verifier, params = browser_code(client, service)
    tokens = exchange(client, service, code, verifier, params).json()
    service.settings.oidc_audience = "https://another-api.example"
    assert introspect(client, service, tokens["access_token"]).json() == {"active": False}
    assert refresh(client, service, tokens["refresh_token"]).status_code == 400
