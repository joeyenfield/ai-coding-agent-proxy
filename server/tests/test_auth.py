from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_server.app import FIRST_USER, account_command, create_app, ensure_first_user
from agent_server.auth import UserStore
from agent_server.config import Backend, Settings


BASE = "http://127.0.0.1:8181"
HOST = {"host": "127.0.0.1:8181"}
PASSWORD = "correct horse"


def org_settings(tmp_path: Path) -> Settings:
    settings = Settings(
        log_dir=tmp_path / "logs", backends_file=tmp_path / "backends.yaml",
        default_backend="test", backends={"test": Backend("test", "http://ollama.test")},
    )
    store = UserStore(settings.users_path)
    store.add_team("core")
    store.add_team("ops")
    store.add_user("dave", PASSWORD)  # the first user is the admin
    store.add_user("alice", PASSWORD, teams=["core"])
    store.add_user("bob", PASSWORD, teams=["core"])
    store.add_user("carol", PASSWORD, teams=["ops"])
    return settings


def signed_in(app, user: str) -> TestClient:
    client = TestClient(app, base_url=BASE)
    response = client.post("/api/auth/login", json={"username": user, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return client


def ws_headers(client: TestClient) -> dict[str, str]:
    """TestClient opens WebSockets on ws://testserver, so a sign-in cookie for 127.0.0.1 goes by hand."""
    return {**HOST, "cookie": f"ai_proxy_session={client.cookies.get('ai_proxy_session')}"}


def ids(client: TestClient) -> set[str]:
    return {session["session_id"] for session in client.get("/api/sessions").json()}


def test_everything_but_sign_in_and_the_console_files_needs_an_account(tmp_path):
    app = create_app(org_settings(tmp_path))
    with TestClient(app, base_url=BASE) as anonymous:
        assert anonymous.get("/health").status_code == 200
        assert anonymous.get("/api/auth/status").json() == {"required": True, "identity": None, "secure": False}
        assert anonymous.get("/terminals").status_code in (200, 404)  # the console's page, which shows sign-in
        for path in ("/api/sessions", "/api/status", "/api/terminals", "/api/containers", "/docs", "/v1/models", "/api/tags"):
            assert anonymous.get(path).status_code == 401, path
        assert anonymous.post("/api/auth/login", json={"username": "alice", "password": "nope"}).status_code == 401
        alice = signed_in(app, "alice")
        assert alice.get("/api/auth/status").json()["identity"] == {"user": "alice", "via": "cookie", "role": "member", "teams": ["core"]}
        assert alice.get("/api/sessions").status_code == 200
        # A page on another site can't use the cookie to change things.
        assert alice.post("/api/sessions", json={"client": "x", "backend": "test"}, headers={"origin": "http://evil.example"}).status_code == 403
        alice.post("/api/auth/logout")
        assert alice.get("/api/sessions").status_code == 401


def test_failed_sign_ins_are_limited(tmp_path):
    app = create_app(org_settings(tmp_path))
    with TestClient(app, base_url=BASE) as client:
        for _ in range(10):
            assert client.post("/api/auth/login", json={"username": "alice", "password": "wrong"}).status_code == 401
        assert client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 429


def test_only_admins_change_the_server_and_the_organization(tmp_path):
    app = create_app(org_settings(tmp_path))
    with TestClient(app, base_url=BASE):
        alice, dave = signed_in(app, "alice"), signed_in(app, "dave")
        config = dave.get("/api/config").json()["config"]
        assert alice.put("/api/config", json=config).status_code == 403
        assert dave.put("/api/config", json=config).status_code == 200
        assert alice.post("/api/containers", json={"name": "x", "engine": "ollama"}).status_code == 403
        assert alice.post("/api/org/teams", json={"id": "qa"}).status_code == 403
        assert dave.post("/api/org/teams", json={"id": "qa", "name": "Quality"}).status_code == 201
        org = dave.post("/api/org/users", json={"name": "erin", "password": PASSWORD, "teams": ["qa"]}).json()
        assert {"name": "erin", "role": "member", "teams": ["qa"]}.items() <= next(user for user in org["users"] if user["name"] == "erin").items()
        assert dave.put("/api/org/users/dave", json={"role": "member"}).status_code == 400  # the last admin
        # Everyone can see who's in the organization.
        assert [team["id"] for team in alice.get("/api/org").json()["teams"]] == ["core", "ops", "qa"]
        assert dave.delete("/api/org/users/dave").status_code == 400


def test_launcher_tokens_work_and_can_be_revoked(tmp_path):
    app = create_app(org_settings(tmp_path))
    with TestClient(app, base_url=BASE) as client:
        issued = client.post("/api/auth/token", json={"username": "alice", "password": PASSWORD, "label": "laptop"}).json()
        token = {"x-ai-proxy-token": issued["token"]}
        assert client.get("/api/sessions", headers=token).status_code == 200
        alice = signed_in(app, "alice")
        assert [item["label"] for item in alice.get("/api/auth/tokens").json()["tokens"]] == ["laptop"]
        alice.delete(f"/api/auth/tokens/{issued['id']}")
        assert client.get("/api/sessions", headers=token).status_code == 401


def test_the_openapi_document_needs_a_sign_in_and_names_every_operation_once(tmp_path):
    app = create_app(org_settings(tmp_path))
    with TestClient(app, base_url=BASE) as client:
        assert client.get("/openapi.json").status_code == 401
        assert client.get("/redoc").status_code == 401
        token = {"x-ai-proxy-token": client.post("/api/auth/token", json={"username": "alice", "password": PASSWORD}).json()["token"]}
        assert "swagger-ui" in client.get("/docs", headers=token).text
        spec = client.get("/openapi.json", headers=token).json()
    operations = [(path, operation) for path, methods in spec["paths"].items() for operation in methods.values()]
    names = [operation["operationId"] for _, operation in operations]
    assert len(names) == len(set(names))
    tag = {path: operation["tags"] for path, operation in operations}
    assert tag["/api/auth/token"] == ["Accounts"] and tag["/api/machines"] == ["Machines"]
    assert tag["/session/{session_id}/v1/{path}"] == ["Agent APIs"]
    assert {t for tags in tag.values() for t in tags} <= {item["name"] for item in spec["tags"]}
    assert {"launcherToken", "consoleCookie"} == set(spec["components"]["securitySchemes"])
    assert "/api/machines/connect" in spec["info"]["description"]  # the WebSockets, which OpenAPI can't list


def test_agents_use_their_open_session_as_the_credential(tmp_path):
    app = create_app(org_settings(tmp_path))
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    with TestClient(app, base_url=BASE) as agent:
        old = app.state.runtime.client
        app.state.runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            app.state.runtime.settings.model_defaults = {"translate_openai": False}
            alice = signed_in(app, "alice")
            session = alice.post("/api/sessions", json={"client": "qwen", "backend": "test"}).json()["session_id"]
            body = {"model": "m", "messages": []}
            # The agent's own Authorization header goes through untouched.
            assert agent.post(f"/session/{session}/v1/chat/completions", json=body, headers={"authorization": "Bearer sk-agent"}).status_code == 200
            assert seen == ["Bearer sk-agent"]
            assert agent.post("/v1/chat/completions", json=body, headers={"x-ai-proxy-session": session}).status_code == 200
            assert agent.post("/session/not-a-session/v1/chat/completions", json=body).status_code == 401
            alice.patch(f"/api/sessions/{session}", json={"ended": True})
            assert agent.post(f"/session/{session}/v1/chat/completions", json=body).status_code == 401
        finally:
            asyncio.run(app.state.runtime.client.aclose())
            app.state.runtime.client = old


def test_sessions_are_private_until_their_owner_shares_them(tmp_path):
    app = create_app(org_settings(tmp_path))
    with TestClient(app, base_url=BASE):
        alice, bob, carol, dave = (signed_in(app, user) for user in ("alice", "bob", "carol", "dave"))
        created = alice.post("/api/sessions", json={"client": "claude", "backend": "test", "tags": {"owner": "bob", "project_path": "/w"}}).json()
        session = created["session_id"]
        assert created["tags"]["owner"] == "alice" and created["tags"]["visibility"] == "private" and created["tags"]["project_path"] == "/w"
        assert created["you"] == {"owner": True, "control": True, "sharing": {"owner": "alice", "visibility": "private", "team": None, "access": "view"}}
        # Private means private, admins included.
        for other in (bob, carol, dave):
            assert session not in ids(other)
            assert other.get(f"/api/sessions/{session}").status_code == 404
            assert other.get("/api/requests", params={"session_id": session}).status_code == 404

        assert alice.patch(f"/api/sessions/{session}", json={"visibility": "team"}).json()["tags"]["team"] == "core"
        assert session in ids(bob) and session not in ids(carol)
        # Bob can watch, not change.
        assert bob.patch(f"/api/sessions/{session}", json={"ended": True}).status_code == 403
        assert bob.patch(f"/api/sessions/{session}", json={"visibility": "org"}).status_code == 403
        assert bob.delete(f"/api/sessions/{session}").status_code == 403
        assert alice.patch(f"/api/sessions/{session}", json={"visibility": "team", "team": "ops"}).status_code == 400

        alice.patch(f"/api/sessions/{session}", json={"visibility": "org", "access": "control"})
        assert session in ids(carol) and session in ids(dave)
        assert carol.get(f"/api/sessions/{session}").json()["you"]["control"] is True
        assert carol.patch(f"/api/sessions/{session}", json={"ended": True}).status_code == 200

        # "Delete all" only touches your own sessions.
        bobs = bob.post("/api/sessions", json={"client": "x", "backend": "test", "visibility": "org"}).json()["session_id"]
        alice.delete("/api/sessions")
        assert session not in ids(dave) and bobs in ids(dave)
        # Status counts only what you can see.
        assert carol.get("/api/status").json()["total_sessions"] == 1


def test_shared_terminals_follow_the_session_sharing(tmp_path):
    app = create_app(org_settings(tmp_path))
    with TestClient(app, base_url=BASE) as client:
        alice, bob = signed_in(app, "alice"), signed_in(app, "bob")
        session = alice.post("/api/sessions", json={"client": "claude", "backend": "test", "visibility": "team"}).json()["session_id"]
        token = client.post("/api/auth/token", json={"username": "alice", "password": PASSWORD}).json()["token"]
        bob_token = client.post("/api/auth/token", json={"username": "bob", "password": PASSWORD}).json()["token"]
        # Bob may watch it, but can't host (impersonate) its terminal.
        with client.websocket_connect(f"/api/terminals/{session}/host", headers={**HOST, "x-ai-proxy-token": bob_token}) as host:
            with pytest.raises(WebSocketDisconnect):
                host.receive_text()
        with client.websocket_connect(f"/api/terminals/{session}/host", headers={**HOST, "x-ai-proxy-token": token}) as host:
            host.send_text(json.dumps({"type": "hello", "cols": 80, "rows": 24}))
            assert json.loads(host.receive_text())["type"] == "resume"
            host.send_bytes(b"> ready\r\n")
            assert [item["session_id"] for item in bob.get("/api/terminals").json()["terminals"]] == [session]
            assert bob.get("/api/terminals").json()["terminals"][0]["you"]["control"] is False
            with bob.websocket_connect(f"/api/terminals/{session}/view", headers=ws_headers(bob)) as viewer:
                assert json.loads(viewer.receive_text())["type"] == "state"
                assert viewer.receive_bytes() == b"> ready\r\n"
                assert json.loads(viewer.receive_text())["type"] == "replayed"
                viewer.send_text(json.dumps({"type": "input", "text": "rm -rf /", "enter": True}))
                assert json.loads(viewer.receive_text())["message"].startswith("This session is shared with you to watch")
                # Making it private again disconnects Bob straight away.
                alice.patch(f"/api/sessions/{session}", json={"visibility": "private"})
                message = viewer.receive()
                assert message["type"] == "websocket.close"
            with bob.websocket_connect(f"/api/terminals/{session}/view", headers=ws_headers(bob)) as viewer:
                with pytest.raises(WebSocketDisconnect) as closed:
                    viewer.receive_text()
                assert closed.value.code == 4404


def test_account_commands_create_the_first_admin(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AI_PROXY_REMOTE_KEY_FILE", str(tmp_path / "remote-access-key"))
    monkeypatch.setenv("AI_PROXY_NEW_PASSWORD", PASSWORD)
    assert account_command("user", ["add", "joey", "--team", "core"]) == 0
    assert "Sign-in is on" in capsys.readouterr().out
    assert account_command("user", ["add", "sam"]) == 0
    assert account_command("org", ["rename", "Empty Pockets"]) == 0
    store = UserStore(tmp_path / "users.json")
    assert store.org()["name"] == "Empty Pockets"
    assert [(user["name"], user["role"], user["teams"]) for user in store.org()["users"]] == [("joey", "admin", ["core"]), ("sam", "member", [])]
    assert store.verify("joey", PASSWORD) and not store.verify("joey", "wrong")
    monkeypatch.setenv("AI_PROXY_NEW_PASSWORD", "short")
    assert account_command("user", ["passwd", "joey"]) == 1


def test_a_first_start_on_the_network_creates_an_admin(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AI_PROXY_USERNAME", raising=False)
    settings = Settings(log_dir=tmp_path / "logs")
    # On this computer only, it stays a single-user tool without sign-in.
    assert ensure_first_user(settings) is None and not settings.users_path.exists()
    settings.host = "0.0.0.0"
    saved = ensure_first_user(settings)
    password = saved.read_text().strip()
    assert password in capsys.readouterr().err
    store = UserStore(settings.users_path)
    assert store.verify(FIRST_USER, password) and store.user(FIRST_USER)["role"] == "admin"
    # Once users.json exists it's left alone, even with every user removed.
    store.remove_user(FIRST_USER)
    assert ensure_first_user(settings) is None and store.users() == []
