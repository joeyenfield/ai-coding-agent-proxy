"""MCP servers machines share: choosing them for a launch, the agent's config, the relay and who may use them.

The machine is in the fleet with a socket that answers nothing (the relay doesn't ask it for
anything); it's on this computer, so the relay goes to its bridge's loopback port directly, which
the mocked HTTP client plays (machine/internal/mcp is the real one, tested in Go). test_client.py
runs the real agent-machine's bridge through a real tunnel.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from agent_server.app import create_app
from agent_server.config import Backend, Settings
from test_auth import org_settings, signed_in


LOCAL = {"base_url": "http://127.0.0.1:8181", "client": ("127.0.0.1", 50000)}
KEY = "bridge-key"
SERVERS = [
    {"name": "github", "transport": "stdio", "description": "GitHub", "found": True},
    {"name": "docs", "transport": "http", "found": True},
]


class QuietSocket:
    async def send_text(self, text: str) -> None:
        raise AssertionError(f"the relay asked the machine for something: {text}")

    async def close(self, **_: Any) -> None:
        pass


class Bridge:
    """agent-machine's MCP bridge, as the mocked HTTP client sees it."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "127.0.0.1" and request.url.port == 41000
        if request.headers.get("x-ai-proxy-mcp-key") != KEY:
            return httpx.Response(401, json={"error": "not the server's key"})
        if request.method == "GET":
            return httpx.Response(200, text=": ping\n\n", headers={"content-type": "text/event-stream"})
        if request.method == "DELETE":
            return httpx.Response(200)
        message = json.loads(request.content)
        if message.get("method") == "initialize":
            body = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"serverInfo":{"name":"github"}}}\n\n'
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream", "mcp-session-id": "s1"})
        if message.get("method") == "tools/call":
            body = (
                'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress","params":{"progress":1}}\n\n'
                'event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"3 repos"}]}}\n\n'
            )
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": -32601, "message": "no such method"}})


def join(app: Any, owner: str | None = None, machine_id: str = "desk") -> None:
    runtime = app.state.runtime
    identity = runtime.auth.identity_for(owner, "token") if owner else None
    runtime.fleet.connect(QuietSocket(), identity, "127.0.0.1", {
        "id": machine_id, "name": machine_id, "tunnel": 1, "mcp": {"port": 41000, "key": KEY, "servers": SERVERS},
    })


def local_app(tmp_path: Path) -> Any:
    return create_app(Settings(
        log_dir=tmp_path / "logs", backends_file=tmp_path / "backends.yaml", default_backend="test",
        backends={"test": Backend("test", "http://ollama.test")},
    ))


def prepare(client: TestClient, agent: str = "claude", **fields: Any) -> httpx.Response:
    return client.post(f"/api/agents/{agent}/sessions", json={"model": "m", "files_dir": "/tmp/f", "launcher": True, **fields})


def test_a_launch_gives_the_agent_the_mcp_servers_it_chose_and_the_server_records_their_calls(tmp_path):
    app = local_app(tmp_path)
    bridge = Bridge()
    with TestClient(app, **LOCAL) as client:
        app.state.runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(bridge))
        join(app)
        listed = client.get("/api/mcp").json()
        assert [(item["id"], item["transport"], item["connected"]) for item in listed] == [("desk/github", "stdio", True), ("desk/docs", "http", True)]
        # The key is the machine's and the server's only.
        assert KEY not in json.dumps(client.get("/api/machines/desk").json())
        assert KEY not in (tmp_path / "logs" / "machines.json").read_text()

        prepared = prepare(client, mcp=["desk/github"])
        assert prepared.status_code == 201, prepared.text
        launch = prepared.json()
        session_id = launch["session"]["session_id"]
        url = f"http://127.0.0.1:8184/session/{session_id}/mcp/desk/github"
        assert launch["mcp"] == [{"id": "desk/github", "name": "github", "url": url}]
        assert launch["command"][:3] == ["claude", "--mcp-config", "/tmp/f/claude-mcp.json"]
        assert json.loads(launch["files"]["claude-mcp.json"]) == {"mcpServers": {"github": {"type": "http", "url": url}}}
        # A machine's name stands for all of its servers; all for every one.
        assert [item["id"] for item in prepare(client, "opencode", mcp="desk").json()["mcp"]] == ["desk/github", "desk/docs"]
        assert len(prepare(client, "codex", mcp=["all"]).json()["mcp"]) == 2
        # None chosen, nothing added.
        assert prepare(client).json()["command"] == ["claude", "--model", "m"]
        for wrong in (["desk/nope"], ["elsewhere"], ["elsewhere/github"]):
            refused = prepare(client, mcp=wrong)
            assert refused.status_code == 400, wrong
        assert "can't take MCP" in prepare(client, "aider", mcp=["desk/github"]).json()["detail"]
        # Clients can't hand a session servers by its tags.
        tagged = prepare(client, tags={"mcp": ["desk/docs"]}).json()["session"]["session_id"]
        assert client.post(f"/session/{tagged}/mcp/desk/docs", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}).status_code == 404

        base = f"/session/{session_id}/mcp/desk/github"
        accept = {"accept": "application/json, text/event-stream", "authorization": "Bearer agent-secret"}
        initialized = client.post(base, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, headers=accept)
        assert initialized.status_code == 200 and initialized.headers["mcp-session-id"] == "s1"
        assert '"serverInfo"' in initialized.text
        called = client.post(base, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "search_repos", "arguments": {"q": "x"}}}, headers={**accept, "mcp-session-id": "s1"})
        assert "3 repos" in called.text and "notifications/progress" in called.text
        assert client.get(base, headers={"accept": "text/event-stream", "mcp-session-id": "s1"}).status_code == 200
        assert client.delete(base, headers={"mcp-session-id": "s1"}).status_code == 200
        # The bridge got the MCP headers and its key, and never the agent's own credentials.
        sent = bridge.requests[1]
        assert sent.url.path == "/mcp/github" and sent.headers["mcp-session-id"] == "s1" and "authorization" not in sent.headers
        # Only the servers it was given.
        assert client.post(f"/session/{session_id}/mcp/desk/docs", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}).status_code == 404

        records = [json.loads(line) for line in client.get(f"/api/sessions/{session_id}/telemetry").text.splitlines()]
        assert [(record["kind"], record["endpoint"], record["host"]) for record in records] == [
            ("mcp", "initialize", "desk/github"), ("mcp", "tools/call search_repos", "desk/github"),
        ]
        assert records[1]["status"] == 200 and records[1]["error_type"] is None

        # When the machine goes, its servers can't be reached.
        app.state.runtime.fleet.disconnect("desk", app.state.runtime.fleet.sockets["desk"])
        assert client.post(base, json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"}).status_code == 503
        assert client.get("/api/mcp").json()[0]["connected"] is False


def test_only_those_a_machine_lets_use_its_mcp_servers_get_them(tmp_path):
    app = create_app(org_settings(tmp_path))
    alice, bob, dave = signed_in(app, "alice"), signed_in(app, "bob"), signed_in(app, "dave")
    with alice, bob, dave:
        app.state.runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(Bridge()))
        join(app, owner="alice")
        assert len(alice.get("/api/mcp").json()) == 2
        # Not bob's, and not an admin's either without a grant: they often run with the owner's tokens.
        assert bob.get("/api/mcp").json() == [] and dave.get("/api/mcp").json() == []
        assert prepare(bob, mcp=["desk/github"]).status_code == 400
        assert dave.get("/api/machines/desk").json()["you"]["mcp"] is False

        assert alice.put("/api/machines/desk/access", json={"access": {"mcp": {"teams": ["acme/core"]}}}).status_code == 200
        assert [item["id"] for item in bob.get("/api/mcp").json()] == ["desk/github", "desk/docs"]
        prepared = prepare(bob, mcp=["desk/github"])
        assert prepared.status_code == 201, prepared.text
        base = f"/session/{prepared.json()['session']['session_id']}/mcp/desk/github"
        message = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
        # The session id is the credential, as for model traffic.
        with TestClient(app, base_url="http://127.0.0.1:8181") as agent:
            app.state.runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(Bridge()))
            assert agent.post(base, json=message).status_code == 200
            # Grants are checked on every request.
            assert alice.put("/api/machines/desk/access", json={"access": {}}).status_code == 200
            refused = agent.post(base, json=message)
            assert refused.status_code == 403 and "no longer" in refused.json()["detail"]
