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
from oidc_helpers import OAuthTestClient, access_token, issuer


BASE = "http://127.0.0.1:8181"
HOST = {"host": "127.0.0.1:8181"}
PASSWORD = "correct horse"


def org_settings(tmp_path: Path) -> Settings:
    settings = Settings(
        log_dir=tmp_path / "logs", backends_file=tmp_path / "backends.yaml",
        default_backend="test", backends={"test": Backend("test", "http://ollama.test")},
    )
    store = UserStore(settings.users_path)
    store.add_org("acme", "Acme")
    store.add_org("globex")
    store.add_team("acme", "core")
    store.add_team("acme", "ops")
    store.add_user("dave", PASSWORD, orgs=["acme"])  # the first user is the admin
    store.add_user("alice", PASSWORD, teams=["acme/core"])
    store.add_user("bob", PASSWORD, teams=["acme/core"])
    store.add_user("carol", PASSWORD, teams=["acme/ops"])
    store.add_user("frank", PASSWORD, orgs=["globex"])
    return settings


def signed_in(app, user: str) -> TestClient:
    client = OAuthTestClient(app, base_url=BASE)
    client.cookies.set("ai_proxy_session", access_token(app, user))
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
        assert anonymous.get("/api/auth/status").json()["required"] is True
        assert anonymous.get("/api/auth/status").json()["identity"] is None
        assert anonymous.get("/terminals").status_code in (200, 404)  # the console's page, which shows sign-in
        for path in ("/api/sessions", "/api/status", "/api/terminals", "/api/containers", "/docs", "/v1/models", "/api/tags"):
            assert anonymous.get(path).status_code == 401, path
        assert anonymous.post("/api/auth/login", json={"username": "alice", "password": "nope"}).status_code == 410
        alice = signed_in(app, "alice")
        assert alice.get("/api/auth/status").json()["identity"] == {"user": "alice", "via": "cookie", "role": "member", "orgs": ["acme"], "teams": ["acme/core"]}
        assert alice.get("/api/sessions").status_code == 200
        # A page on another site can't use the cookie to change things.
        assert alice.post("/api/sessions", json={"client": "x", "backend": "test"}, headers={"origin": "http://evil.example"}).status_code == 403
        issuer(app).revoke(alice.cookies.get("ai_proxy_session"))
        assert alice.get("/api/sessions").status_code == 401


def test_loopback_without_accounts_still_requires_oauth(tmp_path):
    app = create_app(Settings(log_dir=tmp_path / "logs"))
    with TestClient(app, base_url=BASE) as client:
        for path in ("/api/status", "/api/sessions", "/v1/models", "/api/terminals"):
            assert client.get(path).status_code == 401
        assert client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}).status_code == 410
        assert client.post("/api/auth/token", json={"username": "alice", "password": PASSWORD}).status_code == 401


def test_separate_console_origin_authenticated_mutations(tmp_path):
    origin = "https://console.example.com"
    settings = org_settings(tmp_path)
    settings.console_origins = [origin]
    app = create_app(settings)
    with TestClient(app, base_url="https://api.example.com") as client:
        preflight = client.options("/api/auth/login", headers={
            "origin": origin, "access-control-request-method": "POST",
            "access-control-request-headers": "content-type",
        })
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == origin
        assert preflight.headers["access-control-allow-credentials"] == "true"
        token = access_token(app, "alice")
        client.cookies.set("ai_proxy_session", token)
        assert client.get("/api/auth/status", headers={"origin": origin}).json()["identity"]["user"] == "alice"
        created = client.post("/api/sessions", json={"client": "x", "backend": "test"}, headers={"origin": origin})
        assert created.status_code == 201
        assert created.headers["access-control-allow-origin"] == origin
        for foreign in ("https://evil.example.com", "http://console.example.com", "https://console.example.com.evil.test", "null"):
            refused = client.post("/api/sessions", json={"client": "x", "backend": "test"}, headers={"origin": foreign})
            assert refused.status_code == 403
            assert "access-control-allow-origin" not in refused.headers
            assert client.post("/api/auth/logout", headers={"origin": foreign}).status_code == 403
        issuer(app).revoke(token)
        assert client.get("/api/sessions", headers={"origin": origin}).status_code == 401
        assert client.post("/api/auth/login", json={"username": "alice", "password": PASSWORD}, headers={"origin": "https://evil.example.com"}).status_code == 403
        assert client.options("/api/sessions", headers={"origin": "https://evil.example.com", "access-control-request-method": "POST"}).status_code == 400


def test_separate_console_origin_terminal_websocket(tmp_path):
    origin = "https://console.example.com"
    settings = org_settings(tmp_path)
    settings.console_origins = [origin]
    app = create_app(settings)
    with TestClient(app, base_url="https://api.example.com") as client:
        client.cookies.set("ai_proxy_session", access_token(app, "alice"))
        session_id = client.post("/api/sessions", json={"client": "x", "backend": "test"}).json()["session_id"]
        headers = {"host": "api.example.com", "cookie": f"ai_proxy_session={client.cookies.get('ai_proxy_session')}"}
        # A trusted console reaches the session lookup; other origins fail the terminal's own gate.
        for candidate, code in ((origin, 4404), ("https://evil.example.com", 4401)):
            with pytest.raises(WebSocketDisconnect) as closed:
                with client.websocket_connect(f"/api/terminals/{session_id}/view", headers={**headers, "origin": candidate}) as ws:
                    ws.receive_json()
            assert closed.value.code == code


def test_only_admins_change_the_server_organizations_and_users(tmp_path):
    app = create_app(org_settings(tmp_path))
    with TestClient(app, base_url=BASE):
        alice, dave = signed_in(app, "alice"), signed_in(app, "dave")
        config = dave.get("/api/config").json()["config"]
        assert alice.put("/api/config", json=config).status_code == 403
        assert dave.put("/api/config", json=config).status_code == 200
        assert alice.post("/api/containers", json={"name": "x", "engine": "ollama"}).status_code == 403
        assert alice.post("/api/orgs", json={"id": "initech"}).status_code == 403
        assert alice.post("/api/orgs/acme/teams", json={"id": "qa"}).status_code == 403
        assert alice.post("/api/users", json={"name": "erin", "password": PASSWORD}).status_code == 403
        assert dave.post("/api/orgs", json={"id": "initech", "name": "Initech"}).status_code == 201
        assert dave.post("/api/orgs", json={"id": "initech"}).status_code == 400
        assert dave.post("/api/orgs/acme/teams", json={"id": "qa", "name": "Quality"}).status_code == 201
        assert dave.post("/api/orgs/nowhere/teams", json={"id": "qa"}).status_code == 404
        directory = dave.post("/api/users", json={"name": "erin", "password": PASSWORD, "orgs": ["initech"], "teams": ["acme/qa"]}).json()
        erin = next(user for user in directory["users"] if user["name"] == "erin")
        # Joining a team joins its organization.
        assert {"name": "erin", "role": "member", "orgs": ["acme", "initech"], "teams": ["acme/qa"]}.items() <= erin.items()
        assert dave.put("/api/users/erin", json={"teams": ["acme/nope"]}).status_code == 400
        assert dave.put("/api/users/dave", json={"role": "member"}).status_code == 400  # the last admin
        assert dave.delete("/api/users/dave").status_code == 400
        assert dave.put("/api/orgs/initech", json={"name": "Initech Ltd"}).json()["orgs"][2]["name"] == "Initech Ltd"


def test_members_see_their_own_organizations_and_the_people_in_them(tmp_path):
    app = create_app(org_settings(tmp_path))
    with TestClient(app, base_url=BASE):
        alice, frank, dave = signed_in(app, "alice"), signed_in(app, "frank"), signed_in(app, "dave")
        orgs = alice.get("/api/orgs").json()["orgs"]
        assert [org["id"] for org in orgs] == ["acme"]
        assert orgs[0]["members"] == ["alice", "bob", "carol", "dave"]
        assert [(team["key"], team["members"]) for team in orgs[0]["teams"]] == [("acme/core", ["alice", "bob"]), ("acme/ops", ["carol"])]
        assert [user["name"] for user in alice.get("/api/users").json()["users"]] == ["alice", "bob", "carol", "dave"]
        assert [user["name"] for user in frank.get("/api/users").json()["users"]] == ["frank"]
        assert [org["id"] for org in dave.get("/api/orgs").json()["orgs"]] == ["acme", "globex"]
        assert len(dave.get("/api/users").json()["users"]) == 5


def test_leaving_or_deleting_an_organization_leaves_its_teams(tmp_path):
    store = UserStore(org_settings(tmp_path).users_path)
    assert store.update_user("alice", orgs=["acme", "globex"])["teams"] == ["acme/core"]
    assert store.update_user("alice", orgs=["globex"])["teams"] == []
    store.update_user("bob", orgs=["acme", "globex"])
    store.remove_org("acme")
    assert (store.user("bob")["orgs"], store.user("bob")["teams"]) == (["globex"], [])
    assert (store.user("carol")["orgs"], store.user("carol")["teams"]) == ([], [])
    with pytest.raises(KeyError):
        store.remove_team("acme", "core")


def test_scoped_memberships_preserve_other_organizations_and_persist(tmp_path):
    settings = org_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app, base_url=BASE):
        admin = signed_in(app, "dave")
        assert admin.post("/api/orgs/globex/teams", json={"id": "core"}).status_code == 201
        path = "/api/orgs/globex/teams/core/members/alice"
        assert admin.put(path).status_code == 200
        assert admin.put(path).status_code == 200
        store = app.state.runtime.auth.store
        assert store.user("alice")["teams"] == ["acme/core", "globex/core"]
        assert admin.delete("/api/orgs/acme/teams/core/members/alice").status_code == 200
        assert store.user("alice")["orgs"] == ["acme", "globex"]
        assert store.user("alice")["teams"] == ["globex/core"]
        assert admin.put("/api/orgs/acme/teams/ops/members/alice").status_code == 200
        assert admin.delete("/api/orgs/acme/members/alice").status_code == 200
        assert admin.delete("/api/orgs/acme/members/alice").status_code == 200
        assert store.user("alice")["orgs"] == ["globex"]
        assert store.user("alice")["teams"] == ["globex/core"]
        directory = admin.put("/api/orgs/acme/members/alice").json()
        alice = next(user for user in directory["users"] if user["name"] == "alice")
        assert alice["orgs"] == ["acme", "globex"]
        assert alice["teams"] == ["globex/core"]
    reloaded = UserStore(settings.users_path)
    assert reloaded.user("alice")["orgs"] == ["acme", "globex"]
    assert reloaded.user("alice")["teams"] == ["globex/core"]


def test_team_rename_keeps_memberships_and_sharing_key(tmp_path):
    settings = org_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app, base_url=BASE):
        admin = signed_in(app, "dave")
        response = admin.put("/api/orgs/acme/teams/core", json={"name": "Engineering"})
        assert response.status_code == 200
        team = response.json()["orgs"][0]["teams"][0]
        assert team == {"id": "core", "key": "acme/core", "name": "Engineering", "members": ["alice", "bob"]}
        assert admin.delete("/api/orgs/acme/teams/core").status_code == 200
    store = UserStore(settings.users_path)
    assert store.user("alice")["orgs"] == ["acme"]
    assert store.user("alice")["teams"] == []


@pytest.mark.parametrize(("method", "path", "body"), [
    ("PUT", "/api/orgs/acme/members/bob", None),
    ("DELETE", "/api/orgs/acme/members/bob", None),
    ("PUT", "/api/orgs/acme/teams/core", {"name": "Engineering"}),
    ("PUT", "/api/orgs/acme/teams/core/members/bob", None),
    ("DELETE", "/api/orgs/acme/teams/core/members/bob", None),
])
def test_hierarchy_changes_are_admin_only(tmp_path, method, path, body):
    app = create_app(org_settings(tmp_path))
    with TestClient(app, base_url=BASE) as anonymous:
        alice, frank, admin = signed_in(app, "alice"), signed_in(app, "frank"), signed_in(app, "dave")
        assert anonymous.request(method, path, json=body).status_code == 401
        for member in (alice, frank):
            assert member.request(method, path, json=body).status_code == 403
        assert admin.request(method, path, json=body, headers={"origin": "http://evil.example"}).status_code == 403


def test_missing_hierarchy_targets_do_not_change_memberships(tmp_path):
    app = create_app(org_settings(tmp_path))
    with TestClient(app, base_url=BASE):
        admin = signed_in(app, "dave")
        before = admin.get("/api/users").json()
        for path in (
            "/api/orgs/missing/members/alice", "/api/orgs/acme/members/missing",
            "/api/orgs/globex/teams/core/members/alice", "/api/orgs/acme/teams/core/members/missing",
        ):
            assert admin.put(path).status_code == 404
            assert admin.delete(path).status_code == 404
        assert admin.put("/api/orgs/globex/teams/core", json={"name": "No"}).status_code == 404
        assert admin.get("/api/users").json() == before


def test_combined_user_edit_is_atomic_and_password_reset_revokes_access_tokens(tmp_path):
    settings = org_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app, base_url=BASE):
        admin, alice = signed_in(app, "dave"), signed_in(app, "alice")
        before = admin.get("/api/users").json()
        response = admin.put("/api/users/alice", json={"role": "admin", "orgs": ["globex"], "password": "short"})
        assert response.status_code == 400
        assert admin.get("/api/users").json() == before
        assert alice.get("/api/auth/status").json()["identity"]["orgs"] == ["acme"]
        response = admin.put("/api/users/alice", json={"role": "admin", "orgs": ["globex"], "password": "new password"})
        assert response.status_code == 200
        assert alice.get("/api/users").status_code == 401
    store = UserStore(settings.users_path)
    assert store.verify("alice", "new password")
    assert not store.verify("alice", PASSWORD)
    assert store.user("alice")["role"] == "admin"
    assert store.user("alice")["orgs"] == ["globex"]
    assert store.user("alice")["teams"] == []


def test_scoped_removal_revokes_shared_session_access(tmp_path):
    app = create_app(org_settings(tmp_path))
    with TestClient(app, base_url=BASE):
        admin, alice, bob = signed_in(app, "dave"), signed_in(app, "alice"), signed_in(app, "bob")
        session = alice.post("/api/sessions", json={"client": "x", "backend": "test", "visibility": "team"}).json()["session_id"]
        path = f"/api/sessions/{session}"
        assert bob.get(path).status_code == 200
        assert admin.delete("/api/orgs/acme/teams/core/members/bob").status_code == 200
        assert bob.get(path).status_code == 404
        assert alice.patch(path, json={"visibility": "org"}).status_code == 200
        assert bob.get(path).status_code == 200
        assert admin.delete("/api/orgs/acme/members/bob").status_code == 200
        assert bob.get(path).status_code == 404


def test_a_users_file_from_before_organizations_becomes_one_organization(tmp_path):
    path = tmp_path / "users.json"
    path.write_text(json.dumps({
        "org": {"name": "Empty Pockets"}, "teams": {"core": {"name": "Core", "created_at": 1}},
        "users": {"joey": {"password": "x", "role": "admin", "teams": ["core"]}, "sam": {"password": "x", "role": "member", "teams": []}},
        "sessions": {}, "tokens": {},
    }))
    store = UserStore(path)
    assert [(org["id"], org["name"], org["members"]) for org in store.orgs()] == [("empty-pockets", "Empty Pockets", ["joey", "sam"])]
    assert store.user("joey")["teams"] == ["empty-pockets/core"]


def test_oauth_access_tokens_can_be_revoked_and_legacy_tokens_are_rejected(tmp_path):
    app = create_app(org_settings(tmp_path))
    with TestClient(app, base_url=BASE) as client:
        issued = access_token(app, "alice")
        token = {"x-ai-proxy-token": issued}
        assert client.get("/api/sessions", headers=token).status_code == 200
        issuer(app).revoke(issued)
        assert client.get("/api/sessions", headers=token).status_code == 401
        _, legacy = app.state.runtime.auth.store.create_token("alice", "old laptop")
        assert client.get("/api/sessions", headers={"x-ai-proxy-token": legacy}).status_code == 401


def test_the_openapi_document_needs_a_sign_in_and_names_every_operation_once(tmp_path):
    app = create_app(org_settings(tmp_path))
    with TestClient(app, base_url=BASE) as client:
        assert client.get("/openapi.json").status_code == 401
        assert client.get("/redoc").status_code == 401
        token = {"x-ai-proxy-token": access_token(app, "alice")}
        assert "swagger-ui" in client.get("/docs", headers=token).text
        spec = client.get("/openapi.json", headers=token).json()
    operations = [(path, operation) for path, methods in spec["paths"].items() for operation in methods.values()]
    names = [operation["operationId"] for _, operation in operations]
    assert len(names) == len(set(names))
    tag = {path: operation["tags"] for path, operation in operations}
    assert tag["/api/auth/token"] == ["Accounts"] and tag["/api/machines"] == ["Machines"]
    assert tag["/session/{session_id}/v1/{path}"] == ["Agent APIs"]
    assert {t for tags in tag.values() for t in tags} <= {item["name"] for item in spec["tags"]}
    assert {"accessToken", "proxyAccessToken", "consoleCookie"} == set(spec["components"]["securitySchemes"])
    assert "/api/machines/connect" in spec["info"]["description"]  # the WebSockets, which OpenAPI can't list


def test_agents_need_oauth_and_an_open_controllable_session(tmp_path):
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
            assert agent.post(f"/session/{session}/v1/chat/completions", json=body).status_code == 401
            agent.headers["x-ai-proxy-token"] = access_token(app, "alice")
            # The agent's own Authorization header goes through untouched.
            assert agent.post(f"/session/{session}/v1/chat/completions", json=body, headers={"authorization": "Bearer sk-agent"}).status_code == 200
            assert seen == ["Bearer sk-agent"]
            assert agent.post("/v1/chat/completions", json=body, headers={"x-ai-proxy-session": session}).status_code == 200
            assert agent.post("/session/not-a-session/v1/chat/completions", json=body).status_code == 404
            alice.patch(f"/api/sessions/{session}", json={"ended": True})
            assert agent.post(f"/session/{session}/v1/chat/completions", json=body).status_code == 404
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
        assert created["you"] == {"owner": True, "control": True, "sharing": {"owner": "alice", "visibility": "private", "team": None, "org": None, "access": "view"}}
        # Private means private, admins included.
        frank = signed_in(app, "frank")
        for other in (bob, carol, dave):
            assert session not in ids(other)
            assert other.get(f"/api/sessions/{session}").status_code == 404
            assert other.get("/api/requests", params={"session_id": session}).status_code == 404

        assert alice.patch(f"/api/sessions/{session}", json={"visibility": "team"}).json()["tags"]["team"] == "acme/core"
        assert session in ids(bob) and session not in ids(carol)
        # Bob can watch, not change.
        assert bob.patch(f"/api/sessions/{session}", json={"ended": True}).status_code == 403
        assert bob.patch(f"/api/sessions/{session}", json={"visibility": "org"}).status_code == 403
        assert bob.delete(f"/api/sessions/{session}").status_code == 403
        assert alice.patch(f"/api/sessions/{session}", json={"visibility": "team", "team": "ops"}).status_code == 400

        assert alice.patch(f"/api/sessions/{session}", json={"visibility": "org", "team": "globex"}).status_code == 400
        assert alice.patch(f"/api/sessions/{session}", json={"visibility": "org", "access": "control"}).json()["tags"]["org"] == "acme"
        assert session in ids(carol) and session in ids(dave) and session not in ids(frank)
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
        token = access_token(app, "alice")
        bob_token = access_token(app, "bob")
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


@pytest.mark.parametrize("change, activity", [
    ("team", "input"), ("team", "output"), ("team", "idle"), ("org", "idle"),
    ("user", "idle"), ("password", "idle"), ("cookie", "idle"), ("token", "idle"), ("external", "idle"),
])
def test_connected_terminal_viewers_lose_revoked_access(tmp_path, monkeypatch, change, activity):
    from agent_server.routes import terminals

    monkeypatch.setattr(terminals, "ACCESS_POLL_SECONDS", 0.02)
    settings = org_settings(tmp_path)
    app = create_app(settings)
    with TestClient(app, base_url=BASE) as client:
        alice, bob, admin = (signed_in(app, name) for name in ("alice", "bob", "dave"))
        sid = client.post("/api/sessions", headers=ws_headers(alice), json={
            "client": "review", "backend": "test", "visibility": "team", "team": "core", "access": "control",
        }).json()["session_id"]
        headers = ws_headers(bob)
        if change == "token":
            issued = access_token(app, "bob")
            headers = {**HOST, "x-ai-proxy-token": issued}
        with client.websocket_connect(f"/api/terminals/{sid}/host", headers=ws_headers(alice)) as host:
            host.send_json({"type": "hello", "cols": 80, "rows": 24})
            assert host.receive_json()["type"] == "resume"
            with client.websocket_connect(f"/api/terminals/{sid}/view", headers=headers) as viewer:
                assert viewer.receive_json()["type"] == "state"
                assert viewer.receive_json()["type"] == "replayed"
                sent = []

                async def input_spy(*args):
                    sent.append(args)

                monkeypatch.setattr(app.state.runtime.terminals, "send_input", input_spy)
                if change in {"team", "org"}:
                    path = "/api/orgs/acme/teams/core/members/bob" if change == "team" else "/api/orgs/acme/members/bob"
                    response = client.delete(path, headers=ws_headers(admin))
                elif change == "user":
                    response = client.delete("/api/users/bob", headers=ws_headers(admin))
                elif change == "password":
                    response = client.put("/api/users/bob", headers=ws_headers(admin), json={"password": "changed password"})
                elif change == "cookie":
                    issuer(app).revoke(bob.cookies.get("ai_proxy_session"))
                    response = None
                elif change == "token":
                    issuer(app).revoke(issued)
                    response = None
                else:
                    # A CLI edit uses a different UserStore instance; live checks must reload it too.
                    UserStore(settings.users_path).set_membership("acme", "bob", False, team="core")
                    response = None
                assert response is None or response.status_code == 200
                assert client.get(f"/api/sessions/{sid}", headers=headers).status_code in {401, 404}
                if activity == "input":
                    viewer.send_json({"type": "input", "text": "must not reach the host"})
                elif activity == "output":
                    host.send_bytes(b"private output after revocation")
                message = viewer.receive()
                assert message["type"] == "websocket.close"
                assert message["code"] == (4410 if change in {"user", "password", "cookie", "token"} else 4403)
                assert sent == []


def test_account_commands_create_the_first_admin(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AI_PROXY_REMOTE_KEY_FILE", str(tmp_path / "remote-access-key"))
    monkeypatch.setenv("AI_PROXY_NEW_PASSWORD", PASSWORD)
    assert account_command("user", ["add", "joey", "--team", "core"]) == 1  # which organization's?
    assert account_command("user", ["add", "joey", "--org", "pockets", "--team", "core"]) == 0
    assert "Sign-in is on" in capsys.readouterr().out
    assert account_command("user", ["add", "sam", "--team", "other/ops"]) == 0
    assert account_command("org", ["rename", "pockets", "Empty Pockets"]) == 0
    assert account_command("team", ["add", "pockets", "web"]) == 0
    store = UserStore(tmp_path / "users.json")
    assert [(org["id"], org["name"]) for org in store.orgs()] == [("other", "other"), ("pockets", "Empty Pockets")]
    assert [team["key"] for team in store.org("pockets")["teams"]] == ["pockets/core", "pockets/web"]
    assert [(user["name"], user["role"], user["orgs"], user["teams"]) for user in store.user_list()] == [
        ("joey", "admin", ["pockets"], ["pockets/core"]), ("sam", "member", ["other"], ["other/ops"]),
    ]
    assert account_command("org", ["remove", "other"]) == 0 and store.user("sam")["orgs"] == []
    assert store.verify("joey", PASSWORD) and not store.verify("joey", "wrong")
    monkeypatch.setenv("AI_PROXY_NEW_PASSWORD", "short")
    assert account_command("user", ["passwd", "joey"]) == 1


def test_a_first_start_creates_an_admin_even_on_loopback(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AI_PROXY_USERNAME", raising=False)
    settings = Settings(log_dir=tmp_path / "logs")
    saved = ensure_first_user(settings)
    password = saved.read_text().strip()
    assert password in capsys.readouterr().err
    store = UserStore(settings.users_path)
    assert store.verify(FIRST_USER, password) and store.user(FIRST_USER)["role"] == "admin"
    # Once users.json exists it's left alone, even with every user removed.
    store.remove_user(FIRST_USER)
    assert ensure_first_user(settings) is None and store.users() == []
