"""Focused OAuth lifecycle tests, mirroring settings/oauth_test.go in both Go modules."""

import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs

import httpx
import pytest

from agent_py import common, login, oauth


def mock_http(monkeypatch, handler):
    monkeypatch.setattr(common, "client", lambda *args, **kwargs: httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=False, trust_env=False,
    ))


def test_device_pending_slowdown_then_success(monkeypatch):
    issuer = "https://auth.example"
    waits, polls = [], []
    monkeypatch.setattr(oauth.time, "sleep", waits.append)

    def handler(request):
        path = request.url.path
        if path == "/api/auth/status":
            value = {"required": True, "issuer": issuer, "client_id": "mesh-cli"}
        elif path == "/.well-known/openid-configuration":
            value = {"issuer": issuer, **{key + "_endpoint": issuer + "/" + endpoint for key, endpoint in (
                ("device_authorization", "device_authorization"), ("token", "token"), ("userinfo", "userinfo"), ("revocation", "revoke"),
            )}}
        elif path == "/device_authorization":
            form = parse_qs(request.content.decode())
            assert form == {"client_id": ["mesh-cli"], "scope": ["openid profile offline_access mesh-api"]}
            value = {"device_code": "device", "user_code": "CODE", "verification_uri": issuer + "/verify", "expires_in": 120, "interval": 1}
        elif path == "/token":
            polls.append(1)
            if len(polls) <= 2:
                return httpx.Response(400, json={"error": "authorization_pending" if len(polls) == 1 else "slow_down"})
            value = {"access_token": "access", "refresh_token": "refresh", "token_type": "Bearer", "expires_in": 3600}
        elif path == "/userinfo":
            assert request.headers["authorization"] == "Bearer access"
            value = {"sub": "u1", "preferred_username": "alice"}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=value)

    mock_http(monkeypatch, handler)
    output = io.StringIO()
    entry = oauth.device_login("https://api.example", output)
    assert entry.user == "alice" and entry.refresh_token == "refresh"
    assert waits == [1, 1, 6] and "CODE" in output.getvalue() and "device" not in output.getvalue()


def test_refresh_is_serialized_and_revocation_stops_reuse(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_PROXY_TOKEN", raising=False)
    path = tmp_path / "credentials.json"
    url = "https://api.example"
    entry = common.ServerEntry(url, access_token="old", refresh_token="refresh-one", expires_at=1,
                               issuer="https://auth.example", client_id="mesh-cli", token_endpoint="https://auth.example/token")
    credentials = common.Credentials(path, {common.normalize(url): entry})
    credentials.save()
    exchanges = []
    rejected = threading.Event()

    def handler(request):
        exchanges.append(1)
        form = parse_qs(request.content.decode())
        assert form["client_id"] == ["mesh-cli"] and form["refresh_token"] in (["refresh-one"], ["refresh-two"])
        if rejected.is_set():
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(200, json={"access_token": "new", "refresh_token": "refresh-two", "expires_in": 3600, "token_type": "Bearer"})

    mock_http(monkeypatch, handler)
    upstream = common.Upstream(lambda: path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda _: upstream.token(url), range(8))) == ["new"] * 8
    assert len(exchanges) == 1
    assert common.Credentials.load(path).servers[common.normalize(url)].refresh_token == "refresh-two"
    upstream.reject_access_token(url)
    assert upstream.token(url) == "new" and len(exchanges) == 2
    rejected.set()
    credentials.save()
    with pytest.raises(oauth.OAuthError, match="refresh failed"):
        upstream.token(url)
    failed = common.Credentials.load(path).servers[common.normalize(url)]
    assert not failed.access_token and not failed.refresh_token


def test_logout_forgets_locally_but_reports_failed_revocation(tmp_path, monkeypatch, capsys):
    path = tmp_path / "credentials.json"
    entry = common.ServerEntry("https://api.example", refresh_token="refresh", issuer="https://auth.example",
                               client_id="mesh-cli", revocation_endpoint="https://auth.example/revoke")
    common.Credentials(path, {"api": entry}, "api").save()
    mock_http(monkeypatch, lambda request: httpx.Response(503))
    assert login.logout([], program="agent", credentials_path=lambda: path) == 1
    assert not common.Credentials.load(path).servers
    assert "revocation failed" in capsys.readouterr().err


def test_untrusted_endpoints_and_redirects_never_receive_credentials(monkeypatch):
    for endpoint in ("https://attacker.example/token", "http://auth.example/token"):
        with pytest.raises(oauth.OAuthError):
            oauth.issuer_endpoint("https://auth.example", endpoint)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://attacker.example/token"})

    mock_http(monkeypatch, handler)
    with common.client() as http, pytest.raises(oauth.OAuthError):
        oauth.token_request(http, "https://auth.example/token", {"refresh_token": "secret"})
    assert len(requests) == 1


def test_expired_service_token_and_legacy_credentials_are_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_PROXY_TOKEN", raising=False)
    path = tmp_path / "credentials.json"
    entry = common.ServerEntry("https://api.example", token="legacy-password-token")
    common.Credentials(path, {common.normalize(entry.url): entry}).save()
    upstream = common.Upstream(lambda: path)
    with pytest.raises(oauth.OAuthError, match="sign-in required"):
        upstream.token(entry.url)
    entry.access_token, entry.issuer, entry.client_id = "expired", "https://auth.example", "mesh-cli"
    entry.expires_at = int(time.time()) - 1
    common.Credentials(path, {common.normalize(entry.url): entry}).save()
    with pytest.raises(oauth.OAuthError, match="expired"):
        upstream.token(entry.url)


def test_missing_login_does_not_forward_a_callers_token(tmp_path, monkeypatch):
    import asyncio
    from agent_py.client.localproxy import AgentProxy

    monkeypatch.delenv("AI_PROXY_TOKEN", raising=False)
    proxy = AgentProxy(common.Upstream(lambda: tmp_path / "credentials.json", fixed="https://api.example"), 18183)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy), base_url="http://127.0.0.1:18084") as http:
            for path in ("/session/s1/v1/models", "/api/agents"):
                response = await http.get(path, headers={common.TOKEN_HEADER: "caller-controlled"})
                assert response.status_code == 401 and "sign-in required" in response.text
        await proxy.aclose()

    asyncio.run(run())


def test_missing_oauth_login_answers_connect_with_407(tmp_path, monkeypatch):
    import asyncio
    from agent_py.client.localproxy import TunnelServer

    monkeypatch.delenv("AI_PROXY_TOKEN", raising=False)
    proxy = TunnelServer(common.Upstream(lambda: tmp_path / "credentials.json", fixed="http://127.0.0.1:1"))

    async def run():
        server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
            writer.write(b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n\r\n")
            await writer.drain()
            status = await asyncio.wait_for(reader.readline(), 5)
            assert status.startswith(b"HTTP/1.1 407")
            writer.close()
            await writer.wait_closed()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())
