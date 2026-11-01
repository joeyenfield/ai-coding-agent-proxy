"""agent accounts and agent-machine's report of provider sign-ins: mirrors client/internal/accounts and presence/accounts_test.go."""

from __future__ import annotations

import time
from typing import Dict, List, Tuple

import pytest

from agent_py.client import accounts, launcher, localproxy
from agent_py.machine import presence


class Fake:
    def __init__(self, paths: Dict[str, str], output: Dict[str, Tuple[str, int]], env: Dict[str, str], files: set) -> None:
        self.paths, self.output, self.env, self.files = paths, output, env, files
        self.ran: List[str] = []

    def system(self) -> accounts.System:
        def run(path: str, *args: str) -> Tuple[str, int]:
            line = " ".join([path, *args])
            self.ran.append(line)
            return self.output.get(line, ("", 1))

        return accounts.System(
            look_path=lambda name: self.paths.get(name), run=run, getenv=lambda name: self.env.get(name, ""),
            exists=lambda path: path.replace("\\", "/") in self.files, home="/home/me", platform="linux",
        )


def find(report, provider):
    return next(item for item in report["clis"] if item["provider"] == provider)


def test_detect_reads_no_credentials_and_reports_each_sign_in():
    fake = Fake(
        {"claude": "/bin/claude", "codex": "/bin/codex", "copilot": "/bin/copilot"},
        {"/bin/claude --version": ("2.1.0 (Claude Code)\n", 0), "/bin/codex --version": ("codex-cli 0.50.0", 0),
         "/bin/codex login status": ("Logged in using ChatGPT\n", 0), "/bin/copilot --version": ("1.0.0", 0)},
        {}, {"/home/me/.claude/.credentials.json"},
    )
    report = accounts.detect(fake.system())
    claude, codex, copilot = find(report, "claude"), find(report, "codex"), find(report, "copilot")
    assert claude["installed"] and claude["auth"] == "signed_in" and claude["method"] == "claude.ai" and claude["version"] == "2.1.0 (Claude Code)"
    assert codex["auth"] == "signed_in" and codex["method"] == "chatgpt" and codex["subscription"] is True
    assert copilot["auth"] == "unknown" and copilot["modes"] == ["runner", "token"]
    # Only version and status commands run; nothing reads a sign-in.
    assert all(line.endswith("--version") or line.endswith("login status") for line in fake.ran)


def test_detect_tells_signed_out_and_api_keys_from_subscriptions():
    fake = Fake({"claude": "/bin/claude", "codex": "/bin/codex", "copilot": "/bin/copilot"},
                {"/bin/codex login status": ("Logged in using an API key - sk-proj-***ABCD", 0)}, {"GH_TOKEN": "x"}, set())
    report = accounts.detect(fake.system())
    claude = find(report, "claude")
    assert claude["auth"] == "signed_out" and claude["reauthenticate"]
    codex = find(report, "codex")
    assert codex["method"] == "api_key" and codex["subscription"] is False and "ABCD" not in codex.get("note", "")
    assert find(report, "copilot")["method"] == "token:GH_TOKEN"
    fake.env = {"ANTHROPIC_API_KEY": "sk"}
    fake.output["/bin/codex login status"] = ("Not logged in", 1)
    report = accounts.detect(fake.system())
    assert find(report, "claude")["method"] == "api_key" and find(report, "claude")["subscription"] is False
    assert find(report, "codex")["auth"] == "signed_out"
    fake.paths = {}
    missing = find(accounts.detect(fake.system()), "claude")
    assert not missing["installed"] and missing["auth"] == "unknown"


def test_enroll_arguments():
    options = accounts.parse_enroll(["copilot", "--name=Copilot Pro", "--plan", "Pro", "--token-stdin", "--yes", "--machine", "m1"])
    assert (options.provider, options.name, options.plan, options.token_stdin, options.yes, options.machine) == ("copilot", "Copilot Pro", "Pro", True, True, "m1")
    with pytest.raises(ValueError):
        accounts.parse_enroll(["claude", "--token-stdin"])
    for args in ([], ["gemini"], ["codex", "--nope"], ["codex", "--name"]):
        with pytest.raises(ValueError):
            accounts.parse_enroll(args)


def test_a_launch_names_its_provider_account_and_the_proxy_lets_the_launcher_through():
    assert launcher.parse_args(["copilot-account", ".", "--account", "alice-copilot"]).account == "alice-copilot"
    args = presence.arguments({"launch": "l2", "agent": "copilot-account", "account": "alice-copilot"}, "/w")
    assert args[-2:] == ["--account", "alice-copilot"]
    for method, path in (("GET", "/api/provider-accounts"), ("POST", "/api/provider-accounts"), ("POST", "/api/provider-accounts/a/enrollments"),
                         ("PUT", "/api/provider-accounts/a/credential"), ("POST", "/api/sessions/s1/provider-credential")):
        assert localproxy.allowed(method, path), path
    # Nothing else of the accounts API goes through a computer's agent-proxy.
    for method, path in (("DELETE", "/api/provider-accounts/a"), ("PATCH", "/api/provider-accounts/a"), ("PUT", "/api/provider-accounts/a/assignments/m1")):
        assert not localproxy.allowed(method, path), path


def test_provider_sign_ins_are_looked_at_in_the_background_and_kept():
    looks = []
    cache = presence.ProviderCLIs(lambda: looks.append(1) or {"clis": [{"provider": "codex", "installed": True, "auth": "signed_in"}], "at": 1.0})
    assert cache.current() == {"clis": []} or len(cache.current()["clis"]) == 1
    deadline = time.monotonic() + 5
    while not cache.current()["clis"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(cache.current()["clis"]) == 1
    cache.current()
    assert len(looks) == 1
