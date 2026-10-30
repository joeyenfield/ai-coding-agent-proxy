"""The database: migrations, settings stored without YAML, the stores, retention, and importing an older install.

Runs on SQLite, and on PostgreSQL too with AI_PROXY_TEST_DATABASE_URL (see conftest.py).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from agent_server import config_io, defaults
from agent_server.agents import stored_agents
from agent_server.auth import UserStore
from agent_server.config import Backend, Settings
from agent_server.db import Database, legacy, migrate, schema, sqlite_url
from agent_server.fleet import Fleet
from agent_server.issuer import State
from agent_server.sessions import SessionStore
from agent_server.terminals import TerminalHub
from oidc_helpers import authenticated_client, database, stored_rows


def loaded(monkeypatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("AI_PROXY_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("AI_PROXY_REMOTE_KEY_FILE", str(tmp_path / "certs" / "remote-access-key"))
    for name in ("AI_PROXY_HOST", "AI_PROXY_PORT", "AI_PROXY_URL", "AI_PROXY_DEFAULT_BACKEND"):
        monkeypatch.delenv(name, raising=False)
    return Settings.load()


def test_migrations_run_once_and_record_their_version(tmp_path):
    db = database(tmp_path)
    assert migrate.version(db) == migrate.latest() == migrate.migrate(db)
    assert migrate.migrate(db, "auth") == migrate.latest("auth")
    db.close()


def test_a_new_database_starts_with_the_shipped_settings(monkeypatch, tmp_path):
    settings = loaded(monkeypatch, tmp_path)
    assert (settings.host, settings.port, settings.proxy_url) == ("0.0.0.0", 9999, "http://127.0.0.1:9999")
    assert settings.default_backend == "anthropic" and settings.backend(None).type == "anthropic"
    assert settings.intercept.enabled and settings.model_defaults == defaults.models()["defaults"]
    assert set(config_io.export(settings)["agents"]) == set(defaults.agents())
    # The environment still wins over what's stored.
    monkeypatch.setenv("AI_PROXY_PORT", "9191")
    settings.db.close()
    assert Settings.load().port == 9191


def test_shipped_agents_follow_releases_until_an_admin_changes_them(monkeypatch, tmp_path):
    settings = loaded(monkeypatch, tmp_path)
    qwen = defaults.agents()["qwen"]
    changed = config_io.put_agent(settings, "qwen", {**qwen, "description": "Ours"})
    assert changed["builtin"] and changed["edited"]
    assert config_io.delete_agent(settings, "aider")
    assert config_io.put_agent(settings, "mine", {"name": "Mine", "executable": "mine", "protocol": "openai-chat"})["builtin"] is False
    with pytest.raises(ValueError, match="protocol"):
        config_io.put_agent(settings, "broken", {"protocol": "smoke-signals"})
    settings.db.close()
    # A release changes what it ships: unchanged agents follow, changed and deleted ones don't, new ones arrive.
    shipped = defaults.agents()
    shipped["codex"] = {**shipped["codex"], "description": "Newer"}
    shipped["fresh"] = {"name": "Fresh", "executable": "fresh", "protocol": "anthropic"}
    monkeypatch.setattr(defaults, "agents", lambda: dict(shipped))
    settings = Settings.load()
    agents = {item["id"]: item for item in stored_agents(settings.db)}
    assert agents["codex"]["definition"]["description"] == "Newer"
    assert agents["qwen"]["definition"]["description"] == "Ours"
    assert "aider" not in agents and "fresh" in agents and "mine" in agents
    assert config_io.reset_agent(settings, "qwen")["definition"] == shipped["qwen"]
    config_io.reset_agent(settings, "aider")
    with pytest.raises(KeyError):
        config_io.reset_agent(settings, "mine")
    settings.db.close()


def test_settings_export_and_import_as_one_document(monkeypatch, tmp_path):
    settings = loaded(monkeypatch, tmp_path)
    document = config_io.export(settings)
    document["backends"]["hosted"] = {"type": "anthropic", "url": "https://example.com"}
    document["models"]["aliases"]["fast"] = {"ollama_model": "qwen3:8b", "think": False}
    document["retention_days"] = 30
    text = config_io.dump(document)
    assert config_io.apply(settings, config_io.parse(text)) == sorted(config_io.KEYS)
    assert settings.backends["hosted"].url == "https://example.com" and settings.profile("fast").think is False
    assert settings.retention_days == 30
    with pytest.raises(ValueError, match="Unknown settings"):
        config_io.apply(settings, {"ollama": {}})
    with pytest.raises(ValueError, match="type"):
        config_io.apply(settings, {"backends": {"x": {"type": "telnet", "url": "http://x"}}})
    # Only the parts a document has, unless it replaces everything.
    config_io.apply(settings, {"default_backend": "hosted"})
    assert settings.default_backend == "hosted" and "anthropic" in settings.backends
    settings.db.close()


def test_the_config_routes_are_for_admins(tmp_path):
    from agent_server.app import create_app

    settings = Settings(log_dir=tmp_path / "logs", default_backend="test", backends={"test": Backend("test", "http://ollama.test")})
    app = create_app(settings)
    with authenticated_client(app) as admin:
        assert {item["id"] for item in admin.get("/api/config/agents").json()} == set(defaults.agents())
        put = admin.put("/api/config/agents/mine", content="name: Mine\nexecutable: mine\nprotocol: anthropic\n")
        assert put.status_code == 200 and put.json()["builtin"] is False
        assert "mine" in {agent["id"] for agent in admin.get("/api/agents").json()}
        assert admin.put("/api/config/agents/mine", content="protocol: carrier-pigeon\n").status_code == 400
        assert admin.delete("/api/config/agents/mine").status_code == 204
        assert admin.post("/api/config/agents/mine/reset").status_code == 404
        models = admin.get("/api/config/models").json()
        models["aliases"]["fast"] = {"ollama_model": "qwen3:8b"}
        assert admin.put("/api/config/models", content=json.dumps(models)).status_code == 200
        assert settings.model_name("fast") == "qwen3:8b"
        exported = admin.get("/api/config/export")
        assert exported.status_code == 200 and "fast" in yaml.safe_load(exported.text)["models"]["aliases"]
        imported = admin.post("/api/config/import", content="retention_days: 7\n")
        assert imported.status_code == 200 and imported.json()["changed"] == ["retention_days"]
        storage = admin.get("/api/config/storage").json()
        assert storage["retention_days"] == 7 and storage["database"]["kind"] in {"sqlite", "postgresql"}
        assert admin.put("/api/config/storage", json={"retention_days": 0}).json()["retention_days"] is None
        assert admin.put("/api/config/storage", json={"retention_days": -3}).status_code == 400
    from oidc_helpers import access_token
    from fastapi.testclient import TestClient

    app.state.runtime.auth.store.add_user("bob", "bob-password", role="member")
    with TestClient(app, headers={"x-ai-proxy-token": access_token(app, "bob")}) as member:
        assert member.get("/api/config/agents").status_code == 403
        assert member.get("/api/config/export").status_code == 403
        assert member.get("/api/config").status_code == 200


def test_requests_page_newest_first_and_old_recordings_go(tmp_path):
    db = database(tmp_path)
    store = SessionStore(db)
    old = store.create({"client": "a", "backend": "x", "trace": True})
    new = store.create({"client": "b", "backend": "x"})
    for index in range(5):
        store.append_request(old, {"request_id": f"{index:06d}", "timestamp": f"2020-01-01T00:00:0{index}Z", "model": "m", "input_tokens": 2})
        store.write_trace(old, f"{index:06d}", {"request": {"n": index}})
    store.append_request(new, {"request_id": "000001", "timestamp": "2030-01-01T00:00:00Z", "model": "other", "output_tokens": 5})
    page = store.query_requests(limit=2)
    assert [(item["session_id"], item["request_id"]) for item in page] == [(new.session_id, "000001"), (old.session_id, "000004")]
    after = store.query_requests([old.session_id, new.session_id], limit=2, before=page[-1]["timestamp"])
    assert [item["request_id"] for item in after] == ["000003", "000002"]
    assert len(store.query_requests(model="m")) == 5 and store.query_requests([]) == []
    assert store.totals() == (10, 5)
    assert store.read_trace(old, "000003") == {"request": {"n": 3}}
    old.last_activity_at = "2020-01-01T00:00:05Z"
    new.last_activity_at = "2030-01-01T00:00:00Z"
    assert store.prune("2025-01-01T00:00:00Z") == 1
    assert store.traces(old) == [] and len(store.records(old)) == 5
    # Sessions come back as they were.
    store.update_tags(new, {"owner": "joey"})
    db.writer.flush()
    again = SessionStore(db)
    assert again.get(new.session_id).tags == {"owner": "joey"}
    store.delete(old)
    assert SessionStore(db).get(old.session_id) is None
    assert not [row for row in stored_rows(db, schema.requests) if row["session_id"] == old.session_id]
    db.close()


def test_terminal_output_is_recorded_in_chunks_and_read_back(tmp_path, monkeypatch):
    import agent_server.terminals as terminals

    monkeypatch.setattr(terminals, "CHUNK", 8)
    monkeypatch.setattr(terminals, "HISTORY_LIMIT", 20)
    db = database(tmp_path)
    store = SessionStore(db)
    session = store.create({"client": "a", "backend": "x"})
    hub = TerminalHub(store)
    terminal = hub.attach(session, None, {"cols": 100, "rows": 30})
    output = b"".join(f"line {index}\n".encode() for index in range(10))
    for start in range(0, len(output), 5):
        hub.output(terminal, output[start:start + 5])
    hub.exited(terminal, 0)
    reloaded = TerminalHub(SessionStore(db)).get(session)
    assert reloaded.total == len(output) and reloaded.history == output[-20:]
    assert (reloaded.cols, reloaded.exit_status) == (100, 0)
    assert [item["session_id"] for item in TerminalHub(SessionStore(db)).summaries()] == [session.session_id]
    db.close()


def test_the_issuer_reads_accounts_but_never_writes_them(tmp_path):
    settings = Settings(log_dir=tmp_path / "logs")
    store = UserStore(settings.open_database())
    store.add_user("alice", "alice-password")
    readonly = UserStore(settings.open_database(), readonly=True)
    assert readonly.account_version("alice") == store.account_version("alice")
    with pytest.raises(PermissionError):
        readonly.add_user("eve", "eve-password")
    # Another process's change shows at once.
    store.update_user("alice", role="admin")
    assert readonly.user("alice")["role"] == "admin"


def test_an_older_install_is_imported_once_and_its_tokens_stay_valid(monkeypatch, tmp_path):
    old = tmp_path / "old"
    (old / "config").mkdir(parents=True)
    (old / "certs" / "oidc").mkdir(parents=True)
    (old / "config" / "backends.yaml").write_text(yaml.safe_dump({
        "proxy": {"listen_host": "0.0.0.0", "listen_port": 9393, "url": "http://10.0.0.5:9393", "public_url": "https://mesh.example.com",
                  "tls": {"cert": "tls/cert.pem", "key": "tls/key.pem"}},
        "default_backend": "desk",
        "backends": {"desk": {"type": "ollama", "url": "http://10.0.0.6:11434", "machine": "m1"}},
        "intercept": {"enabled": True, "passthrough": ["*.example.com"]},
    }))
    (old / "config" / "models.yaml").write_text(yaml.safe_dump({"defaults": {"keep_alive": "5m"}, "models": {"fast": {"ollama_model": "qwen3:8b"}}}))
    (old / "config" / "agents.yaml").write_text(yaml.safe_dump({"agents": {"qwen": defaults.agents()["qwen"], "ours": {"name": "Ours", "executable": "ours"}}}))
    users = {"users": {"joey": {"password": "scrypt$16384$8$1$c2FsdA==$aGFzaA==", "role": "admin", "orgs": ["pockets"], "teams": [], "created_at": 1761234567.123456}},
             "orgs": {"pockets": {"name": "Empty Pockets", "created_at": 1.5, "teams": {}}}, "sessions": {"x": {"user": "joey"}}, "tokens": {}}
    (old / "certs" / "users.json").write_text(json.dumps(users))
    (old / "certs" / "oidc-browser-key").write_text("browser-key\n")
    (old / "certs" / "container-keys.json").write_text(json.dumps({"strata-iq3": "container-key"}))
    from agent_server.certs import CertificateAuthority, load_pair

    ca_cert, ca_key = CertificateAuthority._create()
    (old / "certs" / "ca.pem").write_bytes(ca_cert)
    (old / "certs" / "ca-key.pem").write_bytes(ca_key)
    (old / "logs" / "sessions" / "s1" / "traces").mkdir(parents=True)
    (old / "logs" / "machines.json").write_text(json.dumps({"machines": [{"id": "m1", "name": "desk", "owner": "joey"}], "removed": {"m9": {"name": "gone"}}}))
    session = old / "logs" / "sessions" / "s1"
    (session / "session.json").write_text(json.dumps({"session_id": "s1", "client": "qwen", "project": None, "model": "m", "backend": "desk", "started_at": "2026-01-01T00:00:00Z", "request_count": 2}))
    (session / "requests.jsonl").write_text("".join(json.dumps({"request_id": f"00000{n}", "timestamp": f"2026-01-01T00:00:0{n}Z", "input_tokens": n}) + "\n" for n in (1, 2)))
    (session / "traces" / "000002.json").write_text(json.dumps({"request": {"messages": []}}))
    (session / "terminal.json").write_text(json.dumps({"cols": 90, "rows": 20, "total": 6}))
    (session / "terminal.log").write_bytes(b"hello\n")
    state = sqlite3.connect(old / "certs" / "oidc" / "issuer.sqlite3")
    state.execute("CREATE TABLE records(kind TEXT, key TEXT, data TEXT NOT NULL, PRIMARY KEY(kind,key))")
    state.execute("INSERT INTO records VALUES ('access', 'abc', ?)", (json.dumps({"user": "joey", "exp": 9e9}),))
    state.commit()
    state.close()
    (old / "certs" / "oidc" / "signing-key.pem").write_bytes(_rsa_pem())

    settings = Settings(log_dir=tmp_path / "logs")
    db = settings.open_database()
    with db.write() as connection:
        done = legacy.import_settings(connection, settings, old)
    assert done == ["backends.yaml", "models.yaml", "agents.yaml", "users.json", "oidc-browser-key", "container-keys.json", "the intercept CA", "machines.json"]
    assert load_pair(db) == (ca_cert, ca_key)
    assert legacy.import_sessions(db, old / "logs" / "sessions") == 1
    assert legacy.import_sessions(db, old / "logs" / "sessions") == 0
    document = config_io.export(settings)
    assert document["proxy"]["public_url"] == "https://mesh.example.com" and document["proxy"]["tls"]["cert"].endswith("cert.pem")
    assert document["backends"]["desk"]["machine"] == "m1" and document["default_backend"] == "desk"
    assert document["models"]["aliases"] == {"fast": {"ollama_model": "qwen3:8b"}} and set(document["agents"]) == {"qwen", "ours"}
    store = UserStore(db)
    # The account version every issued token is bound to is unchanged.
    expected = hashlib.sha256(json.dumps([1761234567.123456, users["users"]["joey"]["password"]]).encode()).hexdigest()
    assert store.account_version("joey") == expected and store.org("pockets")["members"] == ["joey"]
    fleet = Fleet(db)
    assert fleet.machines["m1"].owner == "joey" and "m9" in fleet.removed
    sessions = SessionStore(db)
    imported = sessions.get("s1")
    assert imported.last_activity_at == "2026-01-01T00:00:02Z" and len(sessions.records(imported)) == 2
    assert sessions.read_trace(imported, "000002") == {"request": {"messages": []}}
    assert TerminalHub(sessions).get(imported).history == b"hello\n"
    issuer_state = State(Database(sqlite_url(tmp_path / "issuer.sqlite3")), old / "certs" / "oidc")
    assert issuer_state.get("access", "abc")["user"] == "joey"
    assert issuer_state.signing_key.as_dict(private=False)["kty"] == "RSA"
    db.close()


def _rsa_pem() -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
