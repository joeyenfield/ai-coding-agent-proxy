"""agent-machine login with a setup code and --enable, and the automatic sign-in on start,
mirroring machine/internal/login/login_test.go."""

from __future__ import annotations

import os
from urllib.parse import parse_qs

import httpx
import pytest

from agent_py import common, oauth
from agent_py import machine as agent_machine
from agent_py.machine import settings

SERVER = "https://api.example"
ISSUER = "https://auth.example"


class FakeIssuer:
    """The server and its issuer in one, behind httpx's MockTransport."""

    def __init__(self) -> None:
        self.setups: list = []
        self.tokens = 0
        self.setup = lambda: (200, {"approved": True, "user": "alice"})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        status, value = 200, None
        if path == "/api/auth/status":
            value = {"required": True, "issuer": ISSUER, "client_id": "mesh-cli"}
        elif path == "/.well-known/openid-configuration":
            value = {"issuer": ISSUER, "device_authorization_endpoint": ISSUER + "/device_authorization", "token_endpoint": ISSUER + "/token",
                     "userinfo_endpoint": ISSUER + "/userinfo", "revocation_endpoint": ISSUER + "/revoke"}
        elif path == "/device_authorization":
            value = {"device_code": "secret-device", "user_code": "ABCD-EFGH", "verification_uri": ISSUER + "/verify", "expires_in": 120, "interval": 1}
        elif path == "/device/setup":
            assert str(request.url) == ISSUER + "/device/setup"
            self.setups.append({key: values[0] for key, values in parse_qs(request.content.decode()).items()})
            status, value = self.setup()
        elif path == "/token":
            assert parse_qs(request.content.decode())["device_code"] == ["secret-device"]
            self.tokens += 1
            value = {"access_token": "oauth-access", "refresh_token": "oauth-refresh", "expires_in": 3600, "token_type": "Bearer"}
        elif path == "/userinfo":
            assert request.headers["authorization"] == "Bearer oauth-access"
            value = {"sub": "u1", "preferred_username": "alice"}
        else:
            status = 404
        return httpx.Response(status, json=value)


@pytest.fixture
def issuer(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PROXY_MACHINE_HOME", str(tmp_path / "home"))
    for name in ("AI_PROXY_MACHINE_TOKEN", oauth.SETUP_CODE_ENV, "AI_PROXY_MACHINE_ENABLE", "AI_PROXY_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(oauth.time, "sleep", lambda wait: None)
    fake = FakeIssuer()
    monkeypatch.setattr(common, "client", lambda *args, **kwargs: httpx.Client(
        transport=httpx.MockTransport(fake), follow_redirects=False, trust_env=False,
    ))
    return fake


def signed_in() -> bool:
    entry = common.Credentials.load(settings.credentials_path()).servers.get(common.normalize(SERVER))
    return entry is not None and entry.access_token == "oauth-access"


def test_device_login_without_a_code(issuer, capsys):
    assert agent_machine.main(["login", SERVER, "--enable", "updates"]) == 0
    assert "enter code ABCD-EFGH" in capsys.readouterr().out
    assert issuer.setups == [] and signed_in() and settings.Machine.load().updates


def test_login_with_setup_code(issuer, capsys):
    code = "msc_secret-setup-code"
    assert agent_machine.main(["login", SERVER, "--code", code, "--enable", "launches, terminals"]) == 0
    captured = capsys.readouterr()
    assert code not in captured.out + captured.err and "ABCD-EFGH" not in captured.out + captured.err
    assert issuer.setups == [{"user_code": "ABCD-EFGH", "setup_code": code}] and issuer.tokens == 1
    assert signed_in()
    machine = settings.Machine.load()
    assert machine.launches and machine.commands and not (machine.servers or machine.containers or machine.updates)


def test_login_with_setup_code_from_environment(issuer, monkeypatch, capsys):
    monkeypatch.setenv(oauth.SETUP_CODE_ENV, "msc_from-env")
    monkeypatch.setenv("AI_PROXY_MACHINE_ENABLE", "servers,containers")
    assert agent_machine.main(["login", SERVER]) == 0
    assert "msc_from-env" not in capsys.readouterr().out
    assert issuer.setups[0]["setup_code"] == "msc_from-env"
    machine = settings.Machine.load()
    assert machine.servers and machine.containers and not machine.launches


def test_invalid_setup_code(issuer, capsys):
    issuer.setup = lambda: (400, {"error": "invalid_setup_code", "error_description": "unknown"})
    assert agent_machine.main(["login", SERVER, "--code=msc_wrong", "--enable", "launches"]) == 1
    captured = capsys.readouterr()
    assert oauth.INVALID_SETUP_CODE == "The setup code isn't valid: it may have expired or been used up. Create a new one on the console's Machines page."
    assert oauth.INVALID_SETUP_CODE in captured.err and "msc_wrong" not in captured.out + captured.err
    assert issuer.tokens == 0 and not settings.credentials_path().exists() and not settings.Machine.load().launches
    for status, error, want in ((400, "invalid_user_code", "device code"), (409, "code_already_used", "already approved"), (429, "slow_down", "too many")):
        issuer.setup = lambda: (status, {"error": error})
        assert agent_machine.main(["login", SERVER, "--code", "msc_x"]) == 1
        assert want in capsys.readouterr().err


def test_unknown_toggle_is_refused_before_signing_in(issuer, monkeypatch, capsys):
    assert agent_machine.main(["login", SERVER, "--code", "msc_x", "--enable", "launches,rockets"]) == 2
    assert "launches, commands, files, updates, servers, containers" in capsys.readouterr().err
    monkeypatch.setenv("AI_PROXY_MACHINE_ENABLE", "nope")
    assert agent_machine.main(["login", SERVER]) == 2
    assert issuer.setups == [] and issuer.tokens == 0


def test_automatic_sign_in(issuer, monkeypatch, capsys):
    agent_machine.automatic(SERVER)  # No code: nothing to do.
    assert issuer.setups == []
    monkeypatch.setenv(oauth.SETUP_CODE_ENV, "msc_auto")
    monkeypatch.setenv("AI_PROXY_MACHINE_ENABLE", "launches")
    agent_machine.automatic(SERVER)
    assert "msc_auto" not in capsys.readouterr().out
    assert len(issuer.setups) == 1 and signed_in() and settings.Machine.load().launches
    assert oauth.SETUP_CODE_ENV not in os.environ  # agents started here inherit the environment
    # A restart finds the credentials and leaves the code alone.
    monkeypatch.setenv(oauth.SETUP_CODE_ENV, "msc_auto")
    assert agent_machine.sign_in_first(SERVER) == 0 and len(issuer.setups) == 1
    # A refused code fails start and run, with the message.
    monkeypatch.setenv("AI_PROXY_MACHINE_HOME", str(settings.home().parent / "other"))
    monkeypatch.setenv(oauth.SETUP_CODE_ENV, "msc_auto")
    issuer.setup = lambda: (400, {"error": "invalid_setup_code"})
    assert agent_machine.sign_in_first(SERVER) == 1
    assert "Machines page" in capsys.readouterr().err
