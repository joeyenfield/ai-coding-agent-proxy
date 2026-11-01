"""Signed access tokens: issued by agent-auth (AI_PROXY_AUTH_JWT_ACCESS_TOKENS=1) and checked by the API itself.

A signature alone is never enough: the issuer, audience, expiry, token type and scope, revocations the
issuer published (sign-out, a reused refresh token, a removed service client) and the account's version
(a new password) are all checked, and permissions are read fresh. While revocations can't be known to be
current, tokens are introspected as before.
"""

from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient
from joserfc import jwt as jose_jwt

from agent_server.auth import Auth, Revocations, UserStore
from agent_server.cluster import RedisCluster
from agent_server.config import Settings
from agent_server.issuer import create_auth_app
from test_oidc_issuer import browser_code, exchange, refresh


@pytest.fixture
def signed(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_PROXY_USERNAME", raising=False)
    monkeypatch.delenv("AI_PROXY_PASSWORD", raising=False)
    url = f"fakeredis://{uuid.uuid4()}"
    monkeypatch.setenv("AI_PROXY_AUTH_JWT_ACCESS_TOKENS", "1")
    monkeypatch.setenv("AI_PROXY_REDIS_URL", url)
    settings = Settings(log_dir=tmp_path / "logs", oidc_state_dir=tmp_path / "issuer", oidc_introspection_secret="api-introspection-secret")
    store = UserStore(settings.open_database())
    store.add_user("admin", "admin-password")
    store.add_org("acme")
    store.add_user("alice", "alice-password", role="member", tenant="acme")
    app = create_auth_app(settings)
    with TestClient(app, base_url=settings.oidc_issuer) as client:
        api = Auth(UserStore(settings.open_database()), settings)
        api.client = client
        asked: list[str] = []
        client.event_hooks["request"].append(lambda request: asked.append(request.url.path))
        api.revocations = Revocations(RedisCluster(url, "api-1", "internal-test-token-0123456789"))
        yield client, app.state.issuer, store, api, asked
    app.state.issuer.state.db.close()


def identify(api: Auth, token: str):
    class Carrier:
        headers = {"x-ai-proxy-token": token}
        cookies: dict[str, str] = {}

    return api.identify(Carrier())  # type: ignore[arg-type]


def forged(service, **claims):
    now = int(time.time())
    user = service.bound_user("alice")
    base = {"iss": service.issuer, "aud": service.settings.oidc_audience, "sub": user.sub, "username": "alice", "client_id": "mesh-console",
            "scope": "openid mesh-api", "iat": now, "nbf": now, "exp": now + 300, "jti": "x", "acct": user.version, "fam": "f"}
    return jose_jwt.encode({"alg": "RS256", "kid": service.state.signing_key.kid, "typ": "at+jwt"}, {**base, **claims}, service.state.signing_key)


def test_a_signed_token_is_checked_without_asking_the_issuer(signed):
    client, service, _, api, asked = signed
    code, verifier, params = browser_code(client, service)
    tokens = exchange(client, service, code, verifier, params).json()
    access = tokens["access_token"]
    assert access.count(".") == 2 and "_family" not in tokens
    header = jose_jwt.decode(access, service.state.signing_key).header
    assert header["typ"] == "at+jwt" and header["alg"] == "RS256"
    asked.clear()
    identity = identify(api, access)
    assert identity is not None and identity.user == "alice" and identity.tenant == "acme"
    assert "/introspect" not in asked
    # The issuer still answers for it (other clients, or while revocations aren't current).
    assert client.post("/introspect", data={"token": access}, auth=("mesh-api", "api-introspection-secret")).json()["active"]


def test_a_valid_signature_is_not_enough(signed):
    client, service, _, api, asked = signed
    good = forged(service)
    assert identify(api, good).user == "alice"
    head, body, signature = good.split(".")
    tampered = ".".join([head, body, signature[::-1]])
    for token in (
        tampered, forged(service, aud="https://another.example"), forged(service, iss="https://evil.example"),
        forged(service, exp=int(time.time()) - 120), forged(service, nbf=int(time.time()) + 600), forged(service, scope="openid"),
        forged(service, acct="an-older-password"), forged(service, username="nobody"),
    ):
        assert identify(api, token) is None
    assert "/introspect" not in asked
    # Another algorithm, or none, is refused before any key is looked at.
    unsigned = jose_jwt.encode({"alg": "HS256", "typ": "at+jwt"}, {"username": "alice"}, __import__("joserfc.jwk", fromlist=["OctKey"]).OctKey.import_key("k" * 32))
    assert identify(api, unsigned) is None


def test_sign_out_a_reused_refresh_token_and_a_new_password_end_tokens_at_once(signed):
    client, service, store, api, _ = signed
    code, verifier, params = browser_code(client, service, nonce="nonce-1")
    tokens = exchange(client, service, code, verifier, params).json()
    assert identify(api, tokens["access_token"]).user == "alice"
    assert client.post("/revoke", data={"token": tokens["refresh_token"], "client_id": params["client_id"]}).status_code == 200
    api.revocations.refreshed = 0  # (a message would get there first; the shared keys are read every few seconds too)
    assert identify(api, tokens["access_token"]) is None

    code, verifier, params = browser_code(client, service, nonce="nonce-2")
    tokens = exchange(client, service, code, verifier, params).json()
    rotated = refresh(client, service, tokens["refresh_token"]).json()
    refresh(client, service, tokens["refresh_token"])
    refresh(client, service, tokens["refresh_token"])  # past the grace: theft, the family goes
    api.revocations.refreshed = 0
    assert identify(api, rotated["access_token"]) is None

    code, verifier, params = browser_code(client, service, nonce="nonce-3")
    tokens = exchange(client, service, code, verifier, params).json()
    store.set_password("alice", "a-new-password")
    assert identify(api, tokens["access_token"]) is None


def test_without_current_revocations_tokens_are_introspected(signed):
    client, service, _, api, asked = signed
    code, verifier, params = browser_code(client, service)
    access = exchange(client, service, code, verifier, params).json()["access_token"]

    class Down:
        def keys(self, prefix):
            raise ConnectionError("redis is down")

        def on(self, *args):
            pass

    api.revocations = Revocations(Down())
    asked.clear()
    assert identify(api, access).user == "alice" and "/introspect" in asked
