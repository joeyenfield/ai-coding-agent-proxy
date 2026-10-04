from __future__ import annotations

import json
import ssl
from pathlib import Path

import httpx
import pytest

from agent_proxy.agents import AgentDefinition
from agent_proxy.config import InterceptSettings, Settings
from agent_proxy.intercept import InterceptProxy, redact_headers
from agent_proxy.runtime import Runtime
from agent_proxy.websocket_tap import WebSocketTap, frame


ANTHROPIC_STREAM = "".join(
    f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
    for event in [
        {"type": "message_start", "message": {"model": "claude-x", "usage": {"input_tokens": 12, "cache_read_input_tokens": 900, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}},
        {"type": "message_stop"},
    ]
)


@pytest.fixture
async def intercept(tmp_path: Path):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/messages":
            return httpx.Response(200, text=ANTHROPIC_STREAM, headers={"content-type": "text/event-stream"})
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "secret-access", "expires_in": 3600})
        return httpx.Response(404, json={"error": "missing"})

    settings = Settings(log_dir=tmp_path, intercept=InterceptSettings(listen_port=0, ca_dir=tmp_path / "certs"))
    runtime = Runtime(settings)
    runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    proxy = InterceptProxy(runtime)
    await proxy.start()
    port = proxy.server.sockets[0].getsockname()[1]
    session = runtime.sessions.create({"client": "claude-account", "backend": "direct", "trace": True})
    verify = ssl.create_default_context(cafile=str(runtime.ca.cert_path))
    client = httpx.AsyncClient(proxy=f"http://{session.session_id}:x@127.0.0.1:{port}", verify=verify)
    try:
        yield runtime, session, client, seen, port
    finally:
        await client.aclose()
        await proxy.stop()
        await runtime.client.aclose()


async def test_intercepted_stream_is_forwarded_and_recorded(intercept):
    runtime, session, client, seen, _ = intercept
    body = {"model": "claude-x", "stream": True, "max_tokens": 50, "messages": [{"role": "user", "content": "hi"}]}
    async with client.stream(
        "POST", "https://api.anthropic.test/v1/messages?beta=true", json=body,
        headers={"authorization": "Bearer sk-secret", "anthropic-version": "2023-06-01"},
    ) as response:
        text = (await response.aread()).decode()
    assert response.status_code == 200
    assert text == ANTHROPIC_STREAM

    upstream = seen[0]
    assert str(upstream.url) == "https://api.anthropic.test/v1/messages?beta=true"
    assert upstream.headers["authorization"] == "Bearer sk-secret"
    assert "proxy-authorization" not in upstream.headers
    assert json.loads(upstream.content) == body

    [record] = runtime.records(session)
    assert record["kind"] == "intercept"
    assert record["method"] == "POST" and record["host"] == "api.anthropic.test"
    assert record["model"] == "claude-x"
    assert (record["input_tokens"], record["output_tokens"], record["cache_read_tokens"]) == (12, 7, 900)
    assert record["error_type"] is None and record["ttft_ms"] is not None

    trace = runtime.sessions.read_trace(session, record["request_id"])
    assert trace["request"] == body
    headers = dict(trace["http"]["request_headers"])
    assert headers["authorization"] == "Bearer [redacted]"
    assert "sk-secret" not in json.dumps(trace)
    live = runtime.live.details(f"{session.session_id}:{record['request_id']}")
    assert live["content"] == "Hello"
    assert live["http"]["status"] == 200


async def test_token_endpoints_are_redacted_and_errors_pass_through(intercept):
    runtime, session, client, _, _ = intercept
    response = await client.post("https://auth.example.test/oauth/token", json={"refresh_token": "r-secret", "grant_type": "refresh_token"})
    assert response.json()["access_token"] == "secret-access"
    missing = await client.get("https://auth.example.test/nothing")
    assert missing.status_code == 404

    records = {record["endpoint"]: record for record in runtime.records(session)}
    trace = runtime.sessions.read_trace(session, records["/oauth/token"]["request_id"])
    assert trace["request"] == {"refresh_token": "[redacted]", "grant_type": "refresh_token"}
    assert trace["response"]["access_token"] == "[redacted]"
    assert records["/nothing"]["status"] == 404


async def test_unknown_proxy_user_goes_to_unattributed_session(intercept):
    runtime, session, _, _, port = intercept
    verify = ssl.create_default_context(cafile=str(runtime.ca.cert_path))
    async with httpx.AsyncClient(proxy=f"http://127.0.0.1:{port}", verify=verify) as anonymous:
        await anonymous.get("https://auth.example.test/nothing")
    assert runtime.records(session) == []
    unattributed = [item for item in runtime.sessions.list() if item.client == "unattributed"]
    assert len(unattributed) == 1 and unattributed[0].request_count == 1


def test_intercept_agents_get_proxy_and_ca_environment():
    agent = AgentDefinition(id="claude-account", name="Claude", executable="claude", protocol="anthropic", intercept=True)
    info = {"url": "http://127.0.0.1:8183", "ca_path": "/ca.pem", "bundle_path": "/bundle.pem"}
    env = agent.render("http://proxy", "s1", "", intercept=info)["env"]
    assert env["HTTPS_PROXY"] == "http://s1:agent-proxy@127.0.0.1:8183"
    assert env["NODE_EXTRA_CA_CERTS"] == "/ca.pem"
    assert env["SSL_CERT_FILE"] == env["GIT_SSL_CAINFO"] == "/bundle.pem"
    assert "localhost" in env["NO_PROXY"]
    assert not agent.needs_model
    with pytest.raises(ValueError):
        agent.render("http://proxy", "s1", "", intercept=None)


def test_header_redaction_and_passthrough_patterns():
    assert redact_headers([("Cookie", "a=b"), ("x-api-key", "k"), ("accept", "x")]) == [
        ["Cookie", "[redacted]"], ["x-api-key", "[redacted]"], ["accept", "x"],
    ]
    assert redact_headers([("set-cookie", "__oailb=eyJsecret; Path=/"), ("authorization", "Bearer abc"), ("x-auth", "tok=abc def")]) == [
        ["set-cookie", "[redacted]"], ["authorization", "Bearer [redacted]"], ["x-auth", "[redacted]"],
    ]
    settings = InterceptSettings(passthrough=["*.internal.test"])
    assert settings.tunnelled("git.internal.test") and not settings.tunnelled("api.github.com")


def test_websocket_tap_reassembles_masked_fragmented_frames():
    tap = WebSocketTap()
    message = json.dumps({"type": "response.create", "input": "x" * 300}).encode()
    first = bytearray(frame(message[:100], mask=b"abcd"))
    first[0] &= 0x7F  # not final
    rest = bytearray(frame(message[100:], mask=b"wxyz"))
    rest[0] = 0x80  # final continuation frame
    data = bytes(first + rest)
    assert tap.feed(data[:50]) == []
    assert tap.feed(data[50:]) == [(1, message)]


async def test_websocket_responses_turns_are_recorded(tmp_path):
    import asyncio

    events = [
        {"type": "response.created", "response": {"model": "gpt-x"}},
        {"type": "response.output_text.delta", "delta": "ok"},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 40, "output_tokens": 2, "input_tokens_details": {"cached_tokens": 30}}}},
    ]
    seen_headers = []

    async def upstream(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        seen_headers.append(head.decode())
        writer.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
        tap = WebSocketTap()
        while data := await reader.read(65536):
            for _, payload in tap.feed(data):
                if json.loads(payload)["type"] == "response.create":
                    for event in events:
                        writer.write(frame(json.dumps(event).encode()))
                    await writer.drain()
        writer.close()

    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    upstream_port = server.sockets[0].getsockname()[1]
    settings = Settings(log_dir=tmp_path, intercept=InterceptSettings(listen_port=0, ca_dir=tmp_path / "certs"))
    runtime = Runtime(settings)
    proxy = InterceptProxy(runtime)
    await proxy.start()
    session = runtime.sessions.create({"client": "codex-account", "backend": "direct", "trace": True})
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.server.sockets[0].getsockname()[1])
        writer.write(
            f"GET http://127.0.0.1:{upstream_port}/backend-api/codex/responses HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{upstream_port}\r\nProxy-Authorization: Basic {__import__('base64').b64encode(f'{session.session_id}:x'.encode()).decode()}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: a2V5\r\nSec-WebSocket-Version: 13\r\n"
            "Sec-WebSocket-Extensions: permessage-deflate\r\n\r\n".encode()
        )
        await reader.readuntil(b"\r\n\r\n")
        writer.write(frame(json.dumps({"type": "response.create", "model": "gpt-x", "input": [{"role": "user", "content": "hi"}]}).encode(), mask=b"1234"))
        await writer.drain()
        client_tap = WebSocketTap()
        received = []
        while len(received) < 3:
            received += client_tap.feed(await reader.read(65536))
        writer.close()
        for _ in range(50):
            if len(runtime.records(session)) == 2:
                break
            await asyncio.sleep(0.05)
    finally:
        await proxy.stop()
        server.close()

    assert "permessage-deflate" not in seen_headers[0]
    records = {record["kind"]: record for record in runtime.records(session)}
    turn = records["intercept"]
    assert (turn["method"], turn["model"], turn["endpoint"]) == ("WS", "gpt-x", "/backend-api/codex/responses")
    assert (turn["input_tokens"], turn["output_tokens"], turn["cache_read_tokens"]) == (40, 2, 30)
    assert turn["error_type"] is None and turn["ttft_ms"] is not None
    assert runtime.sessions.read_trace(session, turn["request_id"])["request"]["model"] == "gpt-x"
    assert records["upgrade"]["status"] == 101
