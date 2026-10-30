"""Issuer doubles for API tests: production token validation remains in the loop."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import parse_qs

import httpx
from fastapi.testclient import TestClient


def plant_legacy_credentials(user: str) -> tuple[str, str]:
    """A browser sign-in and a launcher token as versions before OIDC made them: (cookie, launcher token).

    Those versions kept them in users.json; the import into the database leaves them behind,
    so they exist nowhere the server looks. They must authenticate nothing.
    """
    return secrets.token_urlsafe(32), "aip_" + secrets.token_urlsafe(32)


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


def stored_backends(settings) -> dict:
    """The backends as the database holds them now (what backends.yaml showed once)."""
    from agent_server import config_io

    settings.open_database().writer.flush()
    return config_io.export(settings)["backends"]


def database(tmp_path, separate: bool = False):
    """The test's database, set up as the server's (stores built directly use it).

    separate: another one beside it (a SQLite file), even when the tests run on PostgreSQL.
    """
    from agent_server.config import Settings
    from agent_server.db import sqlite_url

    url = sqlite_url(tmp_path / "logs" / "agent-server.sqlite3") if separate else None
    return Settings(log_dir=tmp_path / "logs", database_url=url).open_database()


def stored_rows(db, table) -> list:
    """Every row of a table as it's stored, after the queued writes."""
    from sqlalchemy import select

    db.writer.flush()
    with db.read() as connection:
        return [dict(row) for row in connection.execute(select(table)).mappings()]
