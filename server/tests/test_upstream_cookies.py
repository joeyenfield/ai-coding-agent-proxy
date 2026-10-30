"""The server's shared HTTP client carries every user's traffic, so it must never keep a cookie.

Before, httpx's default cookie jar kept what an upstream set (a sign-in cookie, say) and sent it with
the next request to that host from any session: one user's account cookie reached another's agent.
"""

from __future__ import annotations

import asyncio

import httpx
from fastapi.testclient import TestClient

from agent_server.app import create_app
from agent_server.config import Backend, Settings
from agent_server.runtime import upstream_client


def test_a_cookie_an_upstream_sets_is_never_sent_with_another_request():
    seen: list[str | None] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie"))
        return httpx.Response(200, headers={"set-cookie": "session=alice-secret; Path=/"})

    async def run() -> None:
        async with upstream_client(transport=httpx.MockTransport(upstream)) as client:
            await client.get("https://chatgpt.com/login")
            await client.get("https://chatgpt.com/backend-api/me")
            # An agent's own cookie still goes through, as it sent it.
            await client.get("https://chatgpt.com/backend-api/me", headers={"cookie": "session=bob"})
            assert len(client.cookies.jar) == 0

    asyncio.run(run())
    assert seen == [None, None, "session=bob"]


def test_the_servers_own_client_keeps_no_cookies(tmp_path):
    app = create_app(Settings(log_dir=tmp_path / "logs", default_backend="t", backends={"t": Backend("t", "http://x")}))
    with TestClient(app):
        client = app.state.runtime.client
        response = httpx.Response(200, headers={"set-cookie": "ai_proxy_session=someone; Path=/"}, request=httpx.Request("GET", "https://aiproxy.example/api/auth/login"))
        client.cookies.extract_cookies(response)
        assert len(client.cookies.jar) == 0
