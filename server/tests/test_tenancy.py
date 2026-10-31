"""Tenants: every organization is one, under the root, and nothing crosses between them.

Users, machines, providers (backends), sessions and their recordings each belong to one tenant.
Site admins (the root organization's) run the server and set organizations up, and see and control
everything in every one, to look after the server; an organization's admins manage only their own.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agent_server.app import create_app
from agent_server.auth import ROOT_TENANT, UserStore
from agent_server.config import Backend, Settings
from agent_server.db import migrate, schema
from test_auth import PASSWORD, signed_in, ws_headers
from test_fleet import hello


def tenant_settings(tmp_path: Path) -> Settings:
    settings = Settings(
        log_dir=tmp_path / "logs", default_backend="hosted",
        backends={"hosted": Backend("hosted", "http://anthropic.test", "anthropic")},
    )
    store = UserStore(settings.open_database())
    store.add_user("site", PASSWORD)  # the first user: an admin of the root organization
    store.add_org("acme", "Acme")
    store.add_org("globex")
    store.add_team("acme", "core")
    store.add_team("globex", "core")
    store.add_user("dave", PASSWORD, role="admin", tenant="acme")
    store.add_user("alice", PASSWORD, teams=["acme/core"])
    store.add_user("gina", PASSWORD, role="admin", tenant="globex")
    store.add_user("frank", PASSWORD, teams=["globex/core"])
    return settings


def connect(client: TestClient):
    """A machine's connection, signed in as client's user."""
    return client.websocket_connect("/api/machines/connect", headers=ws_headers(client))


def welcome(socket, message: dict) -> None:
    socket.send_text(json.dumps(message))
    assert json.loads(socket.receive_text())["type"] == "welcome"


def test_identities_carry_their_one_tenant(tmp_path):
    app = create_app(tenant_settings(tmp_path))
    site, alice = signed_in(app, "site"), signed_in(app, "alice")
    with site, alice:
        assert site.get("/api/auth/status").json()["identity"] == {
            "user": "site", "via": "cookie", "role": "admin", "tenant": ROOT_TENANT, "site_admin": True, "orgs": [ROOT_TENANT], "teams": [],
        }
        identity = alice.get("/api/auth/status").json()["identity"]
        assert (identity["tenant"], identity["site_admin"], identity["orgs"], identity["teams"]) == ("acme", False, ["acme"], ["acme/core"])


def test_users_belong_to_one_organization_for_good(tmp_path):
    store = UserStore(tenant_settings(tmp_path).open_database())
    with pytest.raises(ValueError, match="organization they were made in|one organization"):
        store.add_user("two", PASSWORD, orgs=["acme", "globex"])
    with pytest.raises(ValueError, match="site admins only"):
        store.add_user("lost", PASSWORD, role="member")
    with pytest.raises(ValueError, match="stays in the organization"):
        store.update_user("alice", orgs=["globex"])
    with pytest.raises(ValueError, match="No team"):
        store.update_user("alice", teams=["globex/core"])
    with pytest.raises(ValueError, match="organization they were made in|one organization"):
        store.set_membership("globex", "alice", True)
    with pytest.raises(ValueError, match="site admins only"):
        store.update_user("site", role="member")
    with pytest.raises(ValueError, match="users first"):
        store.remove_org("acme")
    with pytest.raises(ValueError, match="root"):
        store.remove_org(ROOT_TENANT)
    orgs = {org["id"]: org for org in store.orgs()}
    assert orgs[ROOT_TENANT]["root"] and orgs[ROOT_TENANT]["parent"] is None and orgs[ROOT_TENANT]["members"] == ["site"]
    assert orgs["acme"]["parent"] == ROOT_TENANT and orgs["acme"]["members"] == ["alice", "dave"]


def test_sessions_never_cross_tenants(tmp_path):
    app = create_app(tenant_settings(tmp_path))
    alice, dave, frank, gina, site = (signed_in(app, name) for name in ("alice", "dave", "frank", "gina", "site"))
    with alice, dave, frank, gina, site:
        created = alice.post("/api/sessions", json={"client": "x", "visibility": "org"})
        assert created.status_code == 201, created.text
        session = created.json()
        assert session["tags"]["tenant"] == "acme" and session["tags"]["org"] == "acme"
        session_id = session["session_id"]
        # Everyone in acme sees an organization-wide session; no other organization's people do.
        assert dave.get(f"/api/sessions/{session_id}").status_code == 200
        for outsider in (frank, gina):
            assert outsider.get(f"/api/sessions/{session_id}").status_code == 404
            assert session_id not in {item["session_id"] for item in outsider.get("/api/sessions").json()}
            assert outsider.get("/api/requests", params={"session_id": session_id}).status_code == 404
            assert outsider.delete(f"/api/sessions/{session_id}").status_code == 404
            assert outsider.get(f"/session/{session_id}/v1/models").status_code == 404
        # Sharing with another organization isn't possible, and clients can't pick their tenant.
        assert alice.patch(f"/api/sessions/{session_id}", json={"visibility": "org", "org": "globex"}).status_code == 400
        planted = alice.post("/api/sessions", json={"client": "x", "tags": {"tenant": "globex", "owner": "frank"}}).json()
        assert planted["tags"]["tenant"] == "acme" and planted["tags"]["owner"] == "alice"
        # Delete all only reaches the caller's own sessions, a site admin's too.
        site.delete("/api/sessions")
        assert dave.get(f"/api/sessions/{session_id}").status_code == 200
        # Site admins see and control every session, private ones in other organizations included.
        private = alice.post("/api/sessions", json={"client": "x"}).json()["session_id"]
        assert dave.get(f"/api/sessions/{private}").status_code == 404
        assert site.get(f"/api/sessions/{private}").status_code == 200
        assert {session_id, private} <= {item["session_id"] for item in site.get("/api/sessions").json()}
        assert site.get(f"/api/sessions/{private}").json()["you"]["control"] is True
        assert site.delete(f"/api/sessions/{private}").status_code == 200
        assert alice.get(f"/api/sessions/{private}").status_code == 404


def test_a_session_without_a_tenant_is_nobodys(tmp_path):
    app = create_app(tenant_settings(tmp_path))
    site, dave = signed_in(app, "site"), signed_in(app, "dave")
    with site, dave:
        stray = app.state.runtime.sessions.create({"client": "x", "backend": "hosted", "tags": {"owner": "dave", "visibility": "org"}})
        assert dave.get(f"/api/sessions/{stray.session_id}").status_code == 404
        # Only a site admin reaches it, to clean it up.
        assert site.get(f"/api/sessions/{stray.session_id}").status_code == 200


def test_machines_and_their_providers_stay_in_their_tenant(tmp_path):
    app = create_app(tenant_settings(tmp_path))
    alice, dave, frank, gina, site = (signed_in(app, name) for name in ("alice", "dave", "frank", "gina", "site"))
    with alice, dave, frank, gina, site, connect(alice) as socket:
        welcome(socket, hello())
        assert app.state.runtime.fleet.machines["m1"].tenant == "acme"
        assert dave.get("/api/machines/m1").json()["tenant"] == "acme"
        for outsider in (frank, gina):
            assert outsider.get("/api/machines").json()["machines"] == []
            assert outsider.get("/api/machines/m1").status_code == 404
            assert outsider.post("/api/machines/m1/providers/adopt", json={"type": "ollama"}).status_code in (403, 404)
        # Another tenant's user can't take the machine's id over.
        with pytest.raises(Exception), connect(frank) as other:
            other.send_text(json.dumps(hello()))
            other.receive_text()
        # Grants only ever name the machine's own organization and its teams.
        assert alice.put("/api/machines/m1/access", json={"access": {"view": {"orgs": ["globex"]}}}).status_code == 400
        assert alice.put("/api/machines/m1/access", json={"access": {"view": {"teams": ["globex/core"]}}}).status_code == 400
        assert alice.put("/api/machines/m1/access", json={"access": {"launch": {"orgs": ["acme"]}}}).status_code == 200
        assert frank.get("/api/machines/m1").status_code == 404

        # Its model server, added as a provider by acme's admin, is only for acme's agents.
        adopted = dave.post("/api/machines/m1/providers/adopt", json={"type": "ollama"})
        assert adopted.status_code == 201, adopted.text
        name = adopted.json()["backend"]
        assert name in alice.get("/api/status").json()["backends"]
        assert "hosted" in frank.get("/api/status").json()["backends"]
        assert name not in frank.get("/api/status").json()["backends"]
        assert name in site.get("/api/status").json()["backends"]
        assert all(name not in agent["backends"] for agent in frank.get("/api/agents").json())
        assert any(name in agent["backends"] for agent in alice.get("/api/agents").json())
        assert name not in frank.get("/api/models").json()["backends"]
        assert frank.get("/api/models", params={"backend": name}).status_code == 404
        assert frank.post("/api/sessions", json={"client": "x", "backend": name}).status_code == 400
        assert frank.post("/api/agents/qwen/sessions", json={"model": "m", "backend": name}).status_code == 400
        # Naming another tenant's backend on an agent request reaches nothing.
        own = frank.post("/api/sessions", json={"client": "x"}).json()["session_id"]
        refused = frank.post(f"/session/{own}/v1/chat/completions", headers={"x-ai-proxy-backend": name}, json={"model": "m", "messages": []})
        assert refused.status_code == 400 and "Unknown backend" in refused.text
        refused = frank.post("/v1/chat/completions", headers={"x-ai-proxy-backend": name}, json={"model": "m", "messages": []})
        assert refused.status_code == 400
        # Its containers are acme's too.
        assert frank.get("/api/containers").json() == {"machines": [], "containers": []}
        # A site admin sees the machine, may use all of it, and may remove it.
        you = site.get("/api/machines/m1").json()["you"]
        assert you["manage"] and you["containers"] and all(you[feature] for feature in ("view", "launch", "terminal", "models", "servers", "update", "mcp"))
        assert [item["id"] for item in site.get("/api/containers").json()["machines"]] == ["m1"]
        assert site.delete("/api/machines/m1").status_code == 200
        assert [item["id"] for item in site.get("/api/machines").json()["removed"]] == ["m1"]


def test_removed_machines_are_restored_only_inside_their_tenant(tmp_path):
    app = create_app(tenant_settings(tmp_path))
    alice, dave, gina = signed_in(app, "alice"), signed_in(app, "dave"), signed_in(app, "gina")
    with alice, dave, gina:
        with connect(alice) as socket:
            welcome(socket, hello())
        assert gina.delete("/api/machines/m1").status_code == 404
        assert dave.delete("/api/machines/m1").status_code == 200
        assert gina.get("/api/machines").json()["removed"] == []
        assert gina.post("/api/machines/m1/restore").status_code == 404
        assert [item["id"] for item in dave.get("/api/machines").json()["removed"]] == ["m1"]


def test_organization_admins_manage_only_their_own_organization(tmp_path):
    app = create_app(tenant_settings(tmp_path))
    alice, dave, gina, site = signed_in(app, "alice"), signed_in(app, "dave"), signed_in(app, "gina"), signed_in(app, "site")
    with alice, dave, gina, site:
        assert [org["id"] for org in dave.get("/api/orgs").json()["orgs"]] == ["acme"]
        assert {user["name"] for user in dave.get("/api/users").json()["users"]} == {"alice", "dave"}
        assert [org["id"] for org in site.get("/api/orgs").json()["orgs"]] == [ROOT_TENANT, "acme", "globex"]
        assert len(site.get("/api/users").json()["users"]) == 5
        # An organization admin adds people to their own organization only.
        made = dave.post("/api/users", json={"name": "erin", "password": PASSWORD, "teams": ["acme/core"]})
        assert made.status_code == 201, made.text
        assert app.state.runtime.auth.store.user("erin")["tenant"] == "acme"
        assert dave.post("/api/users", json={"name": "mallory", "password": PASSWORD, "tenant": "globex"}).status_code == 404
        assert dave.post("/api/users", json={"name": "mallory", "password": PASSWORD, "role": "admin", "tenant": ROOT_TENANT}).status_code == 404
        assert dave.put("/api/users/frank", json={"role": "admin"}).status_code == 404
        assert dave.delete("/api/users/frank").status_code == 404
        assert dave.put("/api/users/site", json={"password": "a-new-password"}).status_code == 404
        assert dave.post("/api/orgs/globex/teams", json={"id": "x"}).status_code == 404
        assert dave.put("/api/orgs/globex/teams/core/members/alice").status_code == 404
        assert dave.put("/api/orgs/acme/teams/core/members/frank").status_code == 404
        assert dave.put("/api/orgs/acme/members/frank").status_code == 404
        assert dave.post("/api/orgs/acme/teams", json={"id": "ops"}).status_code == 201
        # Only site admins make and remove organizations, or change the server.
        assert dave.post("/api/orgs", json={"id": "initech"}).status_code == 403
        assert dave.delete("/api/orgs/acme").status_code == 403
        assert dave.get("/api/config").status_code == 403
        assert dave.get("/api/config/agents").status_code == 403
        assert dave.get("/api/server/stats").status_code == 403
        assert alice.post("/api/users", json={"name": "x1", "password": PASSWORD}).status_code == 403
        assert site.get("/api/config").status_code == 200
        assert site.post("/api/orgs", json={"id": "initech"}).status_code == 201
        assert site.post("/api/users", json={"name": "ivan", "password": PASSWORD, "role": "admin", "tenant": "initech"}).status_code == 201
        assert site.post("/api/users", json={"name": "rooted", "password": PASSWORD}).status_code == 400
        assert site.delete("/api/orgs/initech").status_code == 400
        assert site.delete("/api/users/ivan").status_code == 200
        assert site.delete("/api/orgs/initech").status_code == 200


def test_the_migration_puts_everything_in_one_tenant(tmp_path, monkeypatch):
    settings = Settings(log_dir=tmp_path / "logs", backends={"test": Backend("test", "http://ollama.test")})
    db = settings.open_database()
    with db.write() as connection:
        connection.execute(schema.users.delete())
        connection.execute(schema.orgs.delete())
        connection.execute(schema.orgs.insert().values(id="acme", name="Acme", created_at=1.0, teams={"core": {"name": "core"}}))
        connection.execute(schema.orgs.insert().values(id="globex", name="Globex", created_at=1.0, teams={"ops": {"name": "ops"}}))
        for name, role, orgs, teams in (
            ("boss", "admin", [], []), ("dave", "admin", ["acme"], ["acme/core"]),
            ("both", "member", ["globex", "acme"], ["acme/core", "globex/ops"]), ("loner", "member", [], []),
        ):
            connection.execute(schema.users.insert().values(name=name, password="x", role=role, created_at=1.0, orgs=orgs, teams=teams))
        for session_id, owner in (("s1", "dave"), ("s2", None), ("s3", "both")):
            tags = {"owner": owner} if owner else {}
            connection.execute(schema.sessions.insert().values(id=session_id, started_at="2026-01-01", data={"session_id": session_id, "tags": tags}))
        connection.execute(schema.machines.insert().values(id="m1", data={"id": "m1", "name": "m1", "owner": "dave"}))
        connection.execute(schema.machines.insert().values(id="m2", data={"id": "m2", "name": "m2"}))
        connection.execute(schema.api_versions.update().values(version=1))
    assert migrate.migrate(db) == migrate.latest()
    with db.read() as connection:
        users = {row["name"]: row for row in connection.execute(select(schema.users)).mappings()}
        orgs = {row["id"]: row for row in connection.execute(select(schema.orgs)).mappings()}
        sessions = {row[0]: row[1] for row in connection.execute(select(schema.sessions.c.id, schema.sessions.c.data))}
        machines = {row[0]: row[1] for row in connection.execute(select(schema.machines.c.id, schema.machines.c.data))}
    assert {name: row["tenant"] for name, row in users.items()} == {"boss": ROOT_TENANT, "dave": "acme", "both": "acme", "loner": migrate.UNASSIGNED_TENANT}
    assert users["both"]["teams"] == ["acme/core"] and users["both"]["orgs"] == ["acme"]
    assert orgs[ROOT_TENANT]["parent"] is None and {orgs[key]["parent"] for key in orgs if key != ROOT_TENANT} == {ROOT_TENANT}
    assert {key: data["tags"]["tenant"] for key, data in sessions.items()} == {"s1": "acme", "s2": ROOT_TENANT, "s3": "acme"}
    assert {key: data["tenant"] for key, data in machines.items()} == {"m1": "acme", "m2": ROOT_TENANT}
    # And the server reads them that way.
    store = UserStore(db)
    assert store.user("both")["tenant"] == "acme" and store.user("boss")["role"] == "admin"
