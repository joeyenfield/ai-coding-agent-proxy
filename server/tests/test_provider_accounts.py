"""Provider accounts: people's own subscriptions, where they run, the vault, launches with them, usage and scheduling."""

from __future__ import annotations

import base64
import json
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect, select, text

from agent_server import access, config_io
from agent_server.app import create_app
from agent_server.config import Backend, InterceptSettings, Settings
from agent_server.auth import UserStore
from agent_server.db import migrate, schema
from agent_server.provider_accounts import ProviderAccounts, Vault, VaultError, new_key, parse_keys, retry_after
from oidc_helpers import stored_rows
from test_auth import PASSWORD, signed_in
from test_fleet import ROOT, hello, machine


TOKEN = "github_pat_" + "A1b2C3d4" * 8


def account_settings(tmp_path) -> Settings:
    """acme (dave its admin; alice, bob in core; carol in ops), globex (frank), with HTTPS capture for account agents."""
    settings = Settings(
        log_dir=tmp_path / "logs", intercept=InterceptSettings(ca_dir=tmp_path / "certs"),
        default_backend="test", backends={"test": Backend("test", "http://ollama.test", machine="m1")},
    )
    store = UserStore(settings.open_database())
    store.add_org("acme", "Acme")
    store.add_org("globex")
    store.add_team("acme", "core")
    store.add_team("acme", "ops")
    store.add_user("dave", PASSWORD, orgs=["acme"])
    store.add_user("alice", PASSWORD, teams=["acme/core"])
    store.add_user("bob", PASSWORD, teams=["acme/core"])
    store.add_user("carol", PASSWORD, teams=["acme/ops"])
    store.add_user("frank", PASSWORD, orgs=["globex"])
    store.update_user("dave", role="admin")
    return settings


CLIS = {"clis": [
    {"provider": "claude", "installed": True, "version": "2.1.0", "auth": "signed_in", "method": "claude.ai", "modes": ["runner"]},
    {"provider": "codex", "installed": True, "version": "0.50.0", "auth": "signed_in", "method": "chatgpt", "subscription": True, "modes": ["runner"]},
    {"provider": "copilot", "installed": True, "version": "1.0.0", "auth": "unknown", "modes": ["runner", "token"]},
], "at": 1.0}


def runner_hello(**extra) -> dict:
    return hello(**{"agents": ["qwen", "claude-account", "codex-account", "copilot-account", "claude-subscription"], "accounts": CLIS, **extra})


def launch_in_thread(client, body, headers=None):
    answer: dict = {}
    thread = threading.Thread(target=lambda: answer.update(response=client.post("/api/machines/m1/launch", json=body, headers=headers or {})))
    thread.start()
    return thread, answer


def test_accounts_are_their_owners_and_stay_in_their_tenant(tmp_path):
    app = create_app(account_settings(tmp_path))
    alice, bob, dave, frank = (signed_in(app, name) for name in ("alice", "bob", "dave", "frank"))
    with alice, bob, dave, frank:
        made = alice.post("/api/provider-accounts", json={"provider": "claude", "display_name": "Personal Claude", "plan_name": "Max 5x"})
        assert made.status_code == 201, made.text
        account = made.json()
        assert account["owner"] == "alice" and account["tenant"] == "acme" and account["authentication_mode"] == "runner"
        assert account["state"] == "offline" and account["credential"] == {"stored": False}
        # Enrolling again finds it rather than making another.
        again = alice.post("/api/provider-accounts", json={"provider": "claude", "display_name": "personal claude"})
        assert again.status_code == 200 and again.json()["id"] == account["id"] and again.json()["existing"]
        # Claude never takes a stored credential.
        assert alice.post("/api/provider-accounts", json={"provider": "claude", "authentication_mode": "token"}).status_code == 400
        assert alice.post("/api/provider-accounts", json={"provider": "nope"}).status_code == 400

        url = f"/api/provider-accounts/{account['id']}"
        # Same organization isn't enough; another organization is nothing at all.
        assert bob.get("/api/provider-accounts").json()["accounts"] == []
        assert bob.get(url).status_code == 404 and frank.get(url).status_code == 404
        assert frank.patch(url, json={"status": "disabled"}).status_code == 404
        # The tenant's admin sees its metadata and may turn it off, but not use or change it.
        [seen] = dave.get("/api/provider-accounts").json()["accounts"]
        assert seen["you"] == {"owner": False, "use": False, "manage": False, "suspend": True} and seen["users"] == []
        assert dave.patch(url, json={"display_name": "mine now"}).status_code == 403
        assert dave.delete(url).status_code == 403
        turned_off = dave.patch(url, json={"status": "disabled"})
        assert turned_off.status_code == 200 and turned_off.json()["status"] == "disabled" and turned_off.json()["disabled_by"] == "dave"
        assert alice.patch(url, json={"status": "active"}).json()["status"] == "active"
        # Its owner lets bob launch with it: he sees it, can't change it.
        shared = alice.patch(url, json={"users": ["bob", "alice"], "priority": 5})
        assert shared.json()["users"] == ["bob"] and shared.json()["priority"] == 5
        [bobs] = bob.get("/api/provider-accounts").json()["accounts"]
        assert bobs["you"]["use"] and not bobs["you"]["manage"] and bobs["users"] == []
        assert bob.patch(url, json={"priority": 1}).status_code == 403
        assert bob.get(f"{url}/events").status_code == 403
        kinds = [event["kind"] for event in alice.get(f"{url}/events").json()["events"]]
        assert {"created", "disabled", "enabled"} <= set(kinds)
        assert alice.delete(url).status_code == 200 and alice.get(url).status_code == 404


def test_an_account_is_assigned_only_to_its_owners_machines(tmp_path):
    app = create_app(account_settings(tmp_path))
    alice, bob = signed_in(app, "alice"), signed_in(app, "bob")
    with alice, bob, machine(alice, runner_hello()), machine(bob, hello("m2")):
        account = alice.post("/api/provider-accounts", json={"provider": "claude"}).json()
        url = f"/api/provider-accounts/{account['id']}/assignments"
        assert alice.put(f"{url}/m2", json={}).status_code == 403
        assert alice.put(f"{url}/nowhere", json={}).status_code == 404
        assert bob.put(f"{url}/m1", json={}).status_code == 404
        assigned = alice.put(f"{url}/m1", json={})
        assert assigned.status_code == 200, assigned.text
        assert assigned.json()["health"]["available"] and assigned.json()["health"]["authenticated"] is True
        listed = alice.get("/api/provider-accounts", params={"machine": "m1"}).json()["accounts"]
        assert [item["id"] for item in listed] == [account["id"]] and listed[0]["state"] == "available"
        assert alice.get("/api/provider-accounts", params={"machine": "m2"}).json()["accounts"] == []
        assert alice.delete(f"{url}/m1").status_code == 200
        assert alice.get(f"/api/provider-accounts/{account['id']}").json()["state"] == "offline"


def test_enrolling_registers_this_machine_as_a_runner_without_secrets(tmp_path):
    app = create_app(account_settings(tmp_path))
    alice = signed_in(app, "alice")
    with alice, machine(alice, runner_hello(hostname="Alice-PC")):
        account = alice.post("/api/provider-accounts", json={"provider": "codex", "display_name": "ChatGPT Plus"}).json()
        url = f"/api/provider-accounts/{account['id']}/enrollments"
        assert alice.post(url, json={"hostname": "someone-else"}).status_code == 404
        enrolled = alice.post(url, json={"hostname": "alice-pc", "detected": {"auth": "signed_in", "version": "0.50.0"}})
        assert enrolled.status_code == 201, enrolled.text
        assert enrolled.json()["machine"]["id"] == "m1" and enrolled.json()["assignment"]["source"] == "enrollment"
        health = alice.get(f"/api/provider-accounts/{account['id']}/health").json()
        assert health["state"] == "available" and health["machines"][0]["cli_version"] == "0.50.0"


@pytest.mark.parametrize(("report", "state"), [
    ({"provider": "codex", "installed": True, "auth": "signed_out"}, "auth_expired"),
    ({"provider": "codex", "installed": True, "auth": "signed_in", "method": "api_key", "subscription": False}, "auth_expired"),
    ({"provider": "codex", "installed": True, "auth": "unknown"}, "unknown"),
    ({"provider": "codex", "installed": False}, "offline"),
])
def test_what_the_machine_reports_decides_the_accounts_state(tmp_path, report, state):
    app = create_app(account_settings(tmp_path))
    alice = signed_in(app, "alice")
    with alice, machine(alice, runner_hello(accounts={"clis": [report]})):
        account = alice.post("/api/provider-accounts", json={"provider": "codex"}).json()
        alice.put(f"/api/provider-accounts/{account['id']}/assignments/m1", json={})
        assert alice.get(f"/api/provider-accounts/{account['id']}").json()["state"] == state


def test_an_older_machine_and_a_disconnected_one_arent_available(tmp_path):
    app = create_app(account_settings(tmp_path))
    alice = signed_in(app, "alice")
    with alice:
        with machine(alice, hello()):
            account = alice.post("/api/provider-accounts", json={"provider": "claude"}).json()
            alice.put(f"/api/provider-accounts/{account['id']}/assignments/m1", json={})
            [health] = alice.get(f"/api/provider-accounts/{account['id']}/health").json()["machines"]
            assert health["error_code"] == "machine_too_old" and not health["available"]
            # It can't start agents with an account either.
            assert alice.post("/api/machines/m1/launch", json={"agent": "claude-account", "project": ROOT, "account_id": account["id"]}).status_code == 409
        with machine(alice, runner_hello()):
            assert alice.get(f"/api/provider-accounts/{account['id']}").json()["state"] == "available"
        assert alice.get(f"/api/provider-accounts/{account['id']}").json()["state"] == "offline"


def test_a_launch_grant_doesnt_spend_the_machine_owners_subscription(tmp_path):
    app = create_app(account_settings(tmp_path))
    alice, bob = signed_in(app, "alice"), signed_in(app, "bob")
    with alice, bob, machine(alice, runner_hello()) as socket:
        assert alice.put("/api/machines/m1/access", json={"access": {"launch": {"teams": ["acme/core"]}}}).status_code == 200
        account = alice.post("/api/provider-accounts", json={"provider": "claude"}).json()
        alice.put(f"/api/provider-accounts/{account['id']}/assignments/m1", json={})
        # Bob may start agents there, but an account agent would run on alice's own Claude sign-in.
        refused = bob.post("/api/machines/m1/launch", json={"agent": "claude-account", "project": ROOT})
        assert refused.status_code == 403 and "alice's own subscription" in refused.json()["detail"]
        assert bob.post("/api/machines/m1/launch", json={"agent": "claude-account", "project": ROOT, "account_id": account["id"]}).status_code == 404
        assert bob.post("/api/machines/m1/launch", json={"agent": "claude-account", "project": ROOT, "account_id": "auto"}).status_code == 409
        # Local-model agents are as before.
        as_bob = {"cookie": f"ai_proxy_session={bob.cookies.get('ai_proxy_session')}"}

        # Once alice lets him, his launch carries the account (its id, nothing secret) to the machine.
        alice.patch(f"/api/provider-accounts/{account['id']}", json={"users": ["bob"]})
        cookie = alice.cookies.get("ai_proxy_session")
        alice.cookies.clear()
        thread, answer = launch_in_thread(alice, {"agent": "claude-account", "project": ROOT, "account_id": account["id"]}, as_bob)
        request = json.loads(socket.receive_text())
        assert request["type"] == "launch" and request["account"] == account["id"]
        assert set(request) >= {"launch", "agent", "account"} and "token" not in json.dumps(request).lower()
        # The machine's launcher opens the session as alice, through the account's grant.
        session = app.state.runtime.sessions.create({
            "client": "claude-account", "backend": "direct",
            "tags": {"owner": "alice", "tenant": "acme", "machine": "m1", "launch": request["launch"], "provider_account": account["id"]},
        })
        socket.send_text(json.dumps({"type": "launched", "launch": request["launch"], "session_id": session.session_id}))
        thread.join(timeout=10)
        response = answer["response"]
        assert response.status_code == 201, response.text
        assert response.json()["account"]["id"] == account["id"] and session.tags["launched_by"] == "bob"
        alice.cookies.set("ai_proxy_session", cookie)

        # A session without the account is someone else's launch, not this one.
        thread, answer = launch_in_thread(alice, {"agent": "claude-account", "project": ROOT, "account_id": account["id"]})
        request = json.loads(socket.receive_text())
        wrong = app.state.runtime.sessions.create({
            "client": "claude-account", "backend": "direct", "tags": {"owner": "alice", "tenant": "acme", "machine": "m1", "launch": request["launch"]},
        })
        socket.send_text(json.dumps({"type": "launched", "launch": request["launch"], "session_id": wrong.session_id}))
        thread.join(timeout=10)
        assert answer["response"].status_code == 502

        # The owner still starts account agents on the machine's own sign-in, without naming an account.
        thread, answer = launch_in_thread(alice, {"agent": "claude-account", "project": ROOT})
        request = json.loads(socket.receive_text())
        assert "account" not in request
        own = app.state.runtime.sessions.create({"client": "claude-account", "backend": "direct", "tags": {"owner": "alice", "tenant": "acme", "machine": "m1", "launch": request["launch"]}})
        socket.send_text(json.dumps({"type": "launched", "launch": request["launch"], "session_id": own.session_id}))
        thread.join(timeout=10)
        assert answer["response"].status_code == 201

        # Taking the assignment away stops new launches with it.
        alice.delete(f"/api/provider-accounts/{account['id']}/assignments/m1")
        gone = alice.post("/api/machines/m1/launch", json={"agent": "claude-account", "project": ROOT, "account_id": account["id"]})
        assert gone.status_code == 409 and "isn't assigned" in gone.json()["detail"]
        # An account of one kind doesn't run another kind's agent, and others take none.
        alice.put(f"/api/provider-accounts/{account['id']}/assignments/m1", json={})
        assert alice.post("/api/machines/m1/launch", json={"agent": "codex-account", "project": ROOT, "account_id": account["id"]}).status_code == 400
        assert alice.post("/api/machines/m1/launch", json={"agent": "qwen", "project": ROOT, "model": "m", "account_id": account["id"]}).status_code == 400


def test_a_session_takes_an_account_only_its_launcher_may_use(tmp_path):
    app = create_app(account_settings(tmp_path))
    alice, bob = signed_in(app, "alice"), signed_in(app, "bob")
    with alice, bob, machine(alice, runner_hello()):
        account = alice.post("/api/provider-accounts", json={"provider": "claude"}).json()
        alice.put(f"/api/provider-accounts/{account['id']}/assignments/m1", json={})
        body = {"launcher": True, "account": account["id"], "proxy_url": "http://127.0.0.1:8184", "tags": {"provider_account": "forged"}}
        assert bob.post("/api/agents/claude-account/sessions", json=body).status_code == 404
        assert alice.post("/api/agents/codex-account/sessions", json=body).status_code == 400
        made = alice.post("/api/agents/claude-account/sessions", json=body)
        assert made.status_code == 201, made.text
        assert made.json()["account"] == {"id": account["id"], "provider": "claude", "display_name": account["display_name"], "credential": False}
        session = app.state.runtime.sessions.get(made.json()["session"]["session_id"])
        assert session.tags["provider_account"] == account["id"]
        # The tag is the server's: no client sets or changes it.
        assert "provider_account" in access.SERVER_TAGS
        assert alice.patch(f"/api/sessions/{session.session_id}", json={"tags": {"provider_account": "other"}}).status_code in (200, 400)
        assert session.tags["provider_account"] == account["id"]
        plain = alice.post("/api/agents/claude-account/sessions", json={**body, "account": None})
        assert "provider_account" not in app.state.runtime.sessions.get(plain.json()["session"]["session_id"]).tags


def test_a_managed_token_is_encrypted_and_handed_to_one_launch_once(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PROXY_VAULT_KEYS", new_key(1))
    app = create_app(account_settings(tmp_path))
    runtime = app.state.runtime
    alice, bob = signed_in(app, "alice"), signed_in(app, "bob")
    with alice, bob, machine(alice, runner_hello()):
        assert alice.get("/api/provider-accounts").json()["vault"] == {"configured": True}
        account = alice.post("/api/provider-accounts", json={"provider": "copilot", "authentication_mode": "token", "display_name": "Copilot Pro"}).json()
        url = f"/api/provider-accounts/{account['id']}"
        assert account["state"] == "auth_expired"
        assert alice.put(f"{url}/credential", json={"token": "ghp_" + "x" * 36}).status_code == 400
        assert alice.put(f"{url}/credential", content="x" * 20_000).status_code == 413
        assert bob.put(f"{url}/credential", json={"token": TOKEN}).status_code == 404
        stored = alice.put(f"{url}/credential", json={"token": TOKEN})
        assert stored.status_code == 200, stored.text
        assert stored.json()["credential"]["stored"] and stored.json()["credential"]["key_version"] == 1
        alice.put(f"{url}/assignments/m1", json={})
        # Nothing the API, the database's plain columns, the events or the settings export say holds it.
        for answer in (alice.get(url), alice.get("/api/provider-accounts"), alice.get(f"{url}/events"), alice.get(f"{url}/usage")):
            assert TOKEN not in answer.text
        [row] = stored_rows(runtime.db, schema.provider_credentials)
        assert TOKEN.encode() not in bytes(row["ciphertext"]) and row["key_version"] == 1
        assert TOKEN not in json.dumps(stored_rows(runtime.db, schema.provider_accounts), default=str)
        assert TOKEN not in json.dumps(config_io.export(runtime.settings), default=str)

        # A launch the server sends gets a grant; the machine's launcher opens its session through it ...
        machine_ = runtime.fleet.machines["m1"]
        holder = runtime.provider_accounts.accounts[account["id"]]
        runtime.provider_accounts.grant("launch-1", holder, machine_, SimpleNamespace(user="alice"))
        body = {"launcher": True, "account": account["id"], "proxy_url": "http://127.0.0.1:8184", "tags": {"machine": "m1", "launch": "launch-1"}}
        made = alice.post("/api/agents/copilot-account/sessions", json=body)
        assert made.status_code == 201, made.text
        assert made.json()["account"]["credential"] is True and TOKEN not in made.text
        session_id = made.json()["session"]["session_id"]
        # ... and its launcher takes the token once. Nobody else, and nothing a second time.
        assert bob.post(f"/api/sessions/{session_id}/provider-credential").status_code == 404
        given = alice.post(f"/api/sessions/{session_id}/provider-credential")
        assert given.status_code == 200 and given.json() == {"env": {"COPILOT_GITHUB_TOKEN": TOKEN}}
        assert given.headers["cache-control"] == "no-store"
        assert alice.post(f"/api/sessions/{session_id}/provider-credential").status_code == 403
        assert "provisioned" in [event["kind"] for event in alice.get(f"{url}/events").json()["events"]]

        # Without a grant (a launch by hand, or someone else's machine), a token account opens no session.
        by_hand = alice.post("/api/agents/copilot-account/sessions", json={**body, "tags": {"machine": "m1", "launch": "made-up"}})
        assert by_hand.status_code == 409
        # Provisioning turned off for the machine: the grant can't hand it over.
        runtime.provider_accounts.grant("launch-2", holder, machine_, SimpleNamespace(user="alice"))
        second = alice.post("/api/agents/copilot-account/sessions", json={**body, "tags": {"machine": "m1", "launch": "launch-2"}}).json()["session"]["session_id"]
        alice.put(f"{url}/assignments/m1", json={"provisioning_allowed": False})
        assert alice.post(f"/api/sessions/{second}/provider-credential").status_code == 403
        # Revoked: forgotten here, and the account can't sign in until a new one is stored.
        revoked = alice.post(f"{url}/revoke")
        assert revoked.json()["credential"] == {"stored": False} and revoked.json()["state"] == "auth_expired"
        assert stored_rows(runtime.db, schema.provider_credentials) == []


def test_without_a_vault_key_tokens_are_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_PROXY_VAULT_KEYS", raising=False)
    monkeypatch.delenv("AI_PROXY_VAULT_KEYS_FILE", raising=False)
    app = create_app(account_settings(tmp_path))
    alice = signed_in(app, "alice")
    with alice:
        account = alice.post("/api/provider-accounts", json={"provider": "copilot", "authentication_mode": "token"}).json()
        refused = alice.put(f"/api/provider-accounts/{account['id']}/credential", json={"token": TOKEN})
        assert refused.status_code == 409 and "AI_PROXY_VAULT_KEYS" in refused.json()["detail"]


def test_the_vault_binds_each_token_to_its_account_and_rotates(tmp_path):
    vault = Vault(parse_keys(new_key(1)))
    version, nonce, ciphertext = vault.encrypt("secret", b"account-a")
    assert vault.decrypt(version, nonce, ciphertext, b"account-a") == "secret"
    with pytest.raises(VaultError):
        vault.decrypt(version, nonce, ciphertext, b"account-b")
    with pytest.raises(VaultError):
        Vault(parse_keys(new_key(1))).decrypt(version, nonce, ciphertext, b"account-a")
    with pytest.raises(VaultError):
        parse_keys("1:" + base64.urlsafe_b64encode(b"short").decode()) and Vault(parse_keys("1:" + base64.urlsafe_b64encode(b"short").decode()))
    # A newer key encrypts; the older one still decrypts until rotate re-encrypts.
    settings = account_settings(tmp_path)
    db = settings.open_database()
    old = Vault(parse_keys(new_key(1)))
    fleet = SimpleNamespace(machines={}, connected=lambda _: False)
    accounts = ProviderAccounts(db, fleet, old)
    alice = SimpleNamespace(user="alice", tenant="acme", admin=False, site_admin=False)
    account, _ = accounts.create(alice, {"provider": "copilot", "authentication_mode": "token"})
    accounts.set_credential(alice, account, TOKEN)
    db.writer.flush()
    both = Vault({**old.keys, 2: parse_keys(new_key(2))[2]})
    rotated = ProviderAccounts(db, fleet, both)
    assert rotated.rotate() == 1 and rotated._credential(rotated.accounts[account.id]) == TOKEN
    db.writer.flush()
    with db.read() as connection:
        assert connection.execute(select(schema.provider_credentials.c.key_version)).scalar() == 2
    assert ProviderAccounts(db, fleet, Vault({2: both.keys[2]}))._credential(account) == TOKEN


def test_agent_server_vault_makes_keys_reports_and_rotates(tmp_path, monkeypatch, capsys):
    from agent_server import commands

    monkeypatch.delenv("AI_PROXY_VAULT_KEYS", raising=False)
    monkeypatch.delenv("AI_PROXY_VAULT_KEYS_FILE", raising=False)
    assert commands.main("vault", ["key"]) == 0
    first = capsys.readouterr().out.strip()
    assert first.startswith("1:") and len(parse_keys(first)[1]) == 32
    monkeypatch.setenv("AI_PROXY_LOG_DIR", str(tmp_path / "logs"))
    assert commands.main("vault", ["rotate"]) == 1
    monkeypatch.setenv("AI_PROXY_VAULT_KEYS", first)
    assert commands.main("vault", ["key"]) == 0 and capsys.readouterr().out.startswith("2:")
    assert commands.main("vault", ["status"]) == 0
    assert "Configured key versions: 1" in capsys.readouterr().out
    assert commands.main("vault", ["rotate"]) == 0 and "Re-encrypted 0 tokens" in capsys.readouterr().out


def test_usage_is_counted_and_the_providers_429s_cool_an_account_down(tmp_path):
    app = create_app(account_settings(tmp_path))
    runtime = app.state.runtime
    accounts = runtime.provider_accounts
    now = [1_800_000_000.0]
    accounts.clock = lambda: now[0]
    alice = signed_in(app, "alice")
    with alice, machine(alice, runner_hello()):
        account = alice.post("/api/provider-accounts", json={"provider": "claude", "limit": {"window": "5h", "unit": "requests", "value": 4}}).json()
        alice.put(f"/api/provider-accounts/{account['id']}/assignments/m1", json={})
        session = runtime.sessions.create({"client": "claude-account", "backend": "direct", "tags": {"owner": "alice", "tenant": "acme", "provider_account": account["id"]}})

        def record(status=200, host="api.anthropic.com", tokens=100, headers=()):
            accounts.observe(session, {"status": status, "host": host, "model": "claude-sonnet", "input_tokens": tokens, "output_tokens": tokens // 2}, {"response_headers": [list(item) for item in headers]})

        record()
        record()
        # Telemetry and other hosts aren't the subscription's.
        record(host="statsig.anthropic.com.evil.test")
        record(status=429, host="sentry.io", headers=[("Retry-After", "99999")])
        usage = alice.get(f"/api/provider-accounts/{account['id']}/usage").json()
        five = next(item for item in usage["observed"] if item["window"] == "5h")
        assert (five["requests"], five["input_tokens"], five["output_tokens"]) == (2, 200, 100)
        assert five["source"] == "observed" and usage["reported"] is None
        estimate = usage["estimate"]
        assert (estimate["used"], estimate["remaining"], estimate["percent_used"], estimate["source"]) == (2, 2, 50, "estimate")
        assert alice.get(f"/api/provider-accounts/{account['id']}").json()["state"] == "available"
        record()
        state = alice.get(f"/api/provider-accounts/{account['id']}").json()
        assert state["state"] == "near_limit" and "estimated" in state["state_reason"]

        # A short Retry-After is a throttle; a long one means the window is used up.
        record(status=429, headers=[("retry-after", "30")])
        assert alice.get(f"/api/provider-accounts/{account['id']}").json()["state"] == "rate_limited"
        assert alice.post("/api/machines/m1/launch", json={"agent": "claude-account", "project": ROOT, "account_id": account["id"]}).status_code == 409
        now[0] += 31
        assert alice.get(f"/api/provider-accounts/{account['id']}").json()["state"] != "rate_limited"
        record(status=429, headers=[("Retry-After", "7200")])
        exhausted = alice.get(f"/api/provider-accounts/{account['id']}").json()
        assert exhausted["state"] == "quota_exhausted"
        assert alice.get(f"/api/provider-accounts/{account['id']}/usage").json()["rate_limits"]["exhausted_until"] == pytest.approx(now[0] + 7200)
        assert alice.patch(f"/api/provider-accounts/{account['id']}", json={"clear_observations": True}).json()["state"] != "quota_exhausted"
        # Without a declared limit the remaining balance is unknown, not a made-up number.
        alice.patch(f"/api/provider-accounts/{account['id']}", json={"limit": None})
        assert alice.get(f"/api/provider-accounts/{account['id']}/usage").json()["remaining"] == "unknown"


def test_retry_after_takes_seconds_or_a_date():
    assert retry_after({"response_headers": [["Retry-After", "12"]]}, 0) == 12
    assert retry_after({"response_headers": [["retry-after", "Thu, 01 Jan 1970 00:01:40 GMT"]]}, 40) == 60
    assert retry_after({"response_headers": [["Retry-After", "soon"]]}, 0) is None
    assert retry_after(None, 0) is None


def test_the_scheduler_picks_an_available_subscription_then_falls_back_to_local_models(tmp_path):
    app = create_app(account_settings(tmp_path))
    runtime = app.state.runtime
    alice = signed_in(app, "alice")
    with alice, machine(alice, runner_hello()) as socket:
        claude = alice.post("/api/provider-accounts", json={"provider": "claude", "priority": 1}).json()
        codex = alice.post("/api/provider-accounts", json={"provider": "codex", "priority": 2}).json()
        for account in (claude, codex):
            alice.put(f"/api/provider-accounts/{account['id']}/assignments/m1", json={})
        plan = alice.post("/api/provider-accounts/plan", json={"agents": ["claude-account", "codex-account"], "project": ROOT}).json()
        assert plan["choice"]["account_id"] == claude["id"] and plan["choice"]["agent"] == "claude-account"
        # Claude is throttled: Codex takes the task.
        runtime.provider_accounts.accounts[claude["id"]].observed = {"cooldown_until": runtime.provider_accounts.clock() + 600}
        plan = alice.post("/api/provider-accounts/plan", json={"agents": ["claude-account", "codex-account"]}).json()
        assert plan["choice"]["account_id"] == codex["id"]
        assert any(item.get("account_id") == claude["id"] and "throttling" in item["reason"] for item in plan["skipped"])
        # Codex used up too: a local-model agent, if asked for; a paid API never.
        runtime.provider_accounts.accounts[codex["id"]].observed = {"exhausted_until": runtime.provider_accounts.clock() + 600}
        plan = alice.post("/api/provider-accounts/plan", json={"agents": ["claude-account", "codex-account"], "fallback": ["claude-subscription", "qwen"]}).json()
        assert plan["choice"]["kind"] == "fallback" and plan["choice"]["agent"] == "qwen" and plan["choice"]["account_id"] is None
        assert any(item.get("agent") == "claude-subscription" and "paid API" in item["reason"] for item in plan["skipped"])
        assert alice.post("/api/provider-accounts/plan", json={"agents": ["claude-account", "codex-account"]}).json()["choice"] is None
        # A launch can ask for the best one itself.
        runtime.provider_accounts.accounts[codex["id"]].observed = {}
        thread, answer = launch_in_thread(alice, {"agent": "codex-account", "project": ROOT, "account_id": "auto"})
        request = json.loads(socket.receive_text())
        assert request["account"] == codex["id"]
        session = runtime.sessions.create({"client": "codex-account", "backend": "direct", "tags": {"owner": "alice", "tenant": "acme", "machine": "m1", "launch": request["launch"], "provider_account": codex["id"]}})
        socket.send_text(json.dumps({"type": "launched", "launch": request["launch"], "session_id": session.session_id}))
        thread.join(timeout=10)
        assert answer["response"].status_code == 201 and answer["response"].json()["account"]["id"] == codex["id"]


def test_the_migration_adds_the_tables_to_an_existing_database(tmp_path):
    settings = account_settings(tmp_path)
    db = settings.open_database()
    assert migrate.version(db) == migrate.latest()
    with db.write() as connection:
        for table in reversed(schema.PROVIDER_ACCOUNT_TABLES):
            table.drop(connection)
        connection.execute(schema.api_versions.update().values(version=2))
    assert migrate.migrate(db) == migrate.latest()
    with db.read() as connection:
        names = set(inspect(connection).get_table_names(schema=None if db.sqlite else "mesh"))
    assert {table.name for table in schema.PROVIDER_ACCOUNT_TABLES} <= names
