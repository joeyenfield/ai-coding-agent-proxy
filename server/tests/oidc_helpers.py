"""Issuer doubles for API tests: production token validation remains in the loop."""

from __future__ import annotations

import base64
import time
from urllib.parse import parse_qs

import httpx
from fastapi.testclient import TestClient


class Issuer:
    def __init__(self, auth):
        self.auth = auth
        self.tokens: dict[str, tuple[str, str | None]] = {}
        self.sequence = 0
        self.auth.settings.oidc_introspection_secret = "test-introspection-secret"
        self.auth.client.close()
        self.auth.client = httpx.Client(transport=httpx.MockTransport(self.introspect), trust_env=False)

    def issue(self, user: str) -> str:
        self.sequence += 1
        token = f"oauth-test-{user}-{self.sequence}"
        self.tokens[token] = (user, self.auth.account_version(user))
        return token

    def revoke(self, token: str) -> None:
        self.tokens.pop(token, None)

    def introspect(self, request: httpx.Request) -> httpx.Response:
        settings = self.auth.settings
        assert str(request.url) == settings.oidc_issuer + "/introspect"
        expected = base64.b64encode(f"{settings.oidc_introspection_client}:{settings.oidc_introspection_secret}".encode()).decode()
        assert request.headers["authorization"] == "Basic " + expected
        token = parse_qs(request.content.decode()).get("token", [""])[0]
        record = self.tokens.get(token)
        if not record or record[1] is None or self.auth.account_version(record[0]) != record[1]:
            return httpx.Response(200, json={"active": False})
        return httpx.Response(200, json={
            "active": True, "username": record[0], "iss": settings.oidc_issuer,
            "aud": settings.oidc_audience, "scope": "mesh-api", "token_type": "Bearer",
            "exp": int(time.time()) + 3600,
        })


def issuer(app) -> Issuer:
    if not hasattr(app.state, "test_issuer"):
        app.state.test_issuer = Issuer(app.state.runtime.auth)
    elif app.state.runtime.auth.client.is_closed:
        app.state.runtime.auth.client = httpx.Client(
            transport=httpx.MockTransport(app.state.test_issuer.introspect), trust_env=False,
        )
    return app.state.test_issuer


def access_token(app, user: str = "test-admin") -> str:
    auth = app.state.runtime.auth
    if user == "test-admin" and not auth.store.user(user):
        auth.store.add_user(user, "test-only-password", role="admin")
    return issuer(app).issue(user)


class OAuthTestClient(TestClient):
    def __enter__(self):
        issuer(self.app)
        return super().__enter__()


def authenticated_client(app, **kwargs) -> TestClient:
    """Explicit fixture for tests unrelated to signing in, using a real OAuth identity."""
    headers = {"x-ai-proxy-token": access_token(app), **kwargs.pop("headers", {})}
    return OAuthTestClient(app, headers=headers, **kwargs)
