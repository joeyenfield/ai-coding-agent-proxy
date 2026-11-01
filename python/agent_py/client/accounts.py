"""`agent accounts`: your AI subscriptions (provider accounts), the provider CLIs on this computer, and
registering it as a machine a subscription runs on. Mirrors client/internal/accounts.

Detecting a sign-in never reads a credential: it runs the CLI's own status command where the provider
documents one (`codex login status`), or checks that a sign-in file exists without opening it (Claude
Code's .credentials.json), and otherwise says it can't tell. agent-machine runs `agent accounts detect
--json` and reports the result in its hello: booleans and versions.
"""

from __future__ import annotations

import getpass
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from .. import common

PROVIDERS = ("claude", "codex", "copilot")

USAGE = """Usage: agent accounts list                 your provider accounts on the server, and whether each can run
       agent accounts detect [--json]     the provider CLIs here and whether each is signed in (no tokens are read)
       agent accounts status              both: what's here, and your accounts
       agent accounts enroll <claude|codex|copilot> [options]
                                          register this computer as a machine the subscription runs on

Your AI subscriptions (Claude, ChatGPT through Codex, GitHub Copilot), apart from your sign-in to
the server (agent login). Sign in with the provider's own CLI first; enrolling tells the server this
computer is signed in, and never sends the sign-in itself.

Enroll options:
  --name NAME       The account's name on the server (enrolling again with the same name finds it)
  --plan PLAN       Its plan (Pro, Max 5x, Plus, Pro+, ...), for you to tell accounts apart
  --machine ID      Which of your machines this is (default: the one with this computer's hostname)
  --force           Enroll even if the CLI doesn't look signed in here
  --token-stdin     Copilot only: store a fine-grained personal access token with the Copilot Requests
                    permission, read from standard input, encrypted on the server, for the machines you
                    assign the account to (instead of signing in on each)
  --yes             Don't ask before storing the token
"""


@dataclass
class System:
    """What detection looks at, so tests can stand in for it."""
    look_path: Callable[[str], Optional[str]] = common.look_path
    # Runs a program with no input: its output and exit status (-1 when it couldn't run).
    run: Callable[..., Tuple[str, int]] = None  # type: ignore[assignment]
    getenv: Callable[[str], str] = lambda name: os.environ.get(name, "")
    exists: Callable[[str], bool] = lambda path: os.path.isfile(path)
    home: str = field(default_factory=lambda: str(Path.home()))
    platform: str = sys.platform

    def __post_init__(self) -> None:
        if self.run is None:
            self.run = run


def run(path: str, *args: str) -> Tuple[str, int]:
    """A CLI given 10 seconds and no standard input; only what it prints is kept."""
    try:
        done = subprocess.run([path, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "", -1
    return done.stdout.decode(errors="replace"), done.returncode


def detect(system: Optional[System] = None) -> Dict[str, Any]:
    system = system or System()
    return {"clis": [_detect(system, provider) for provider in PROVIDERS], "at": time.time()}


def _detect(system: System, provider: str) -> Dict[str, Any]:
    cli: Dict[str, Any] = {"provider": provider, "installed": False, "auth": "unknown", "modes": ["runner", "token"] if provider == "copilot" else ["runner"], "reauthenticate": False}
    path = system.look_path(provider)
    if not path:
        cli["note"] = f"{provider} isn't installed here"
        return cli
    cli["installed"] = True
    output, status = system.run(path, "--version")
    if status == 0 and first_line(output):
        cli["version"] = first_line(output)
    if provider == "claude":
        # Claude Code keeps its sign-in in .credentials.json (Linux, Windows) or the macOS Keychain; only whether the file is there is checked.
        if system.getenv("ANTHROPIC_API_KEY") or system.getenv("ANTHROPIC_AUTH_TOKEN"):
            cli.update(auth="signed_in", method="api_key", subscription=False, note="ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN is set, so Claude Code uses it rather than a Claude subscription")
        else:
            folder = system.getenv("CLAUDE_CONFIG_DIR") or os.path.join(system.home, ".claude")
            if system.exists(os.path.join(folder, ".credentials.json")):
                cli.update(auth="signed_in", method="claude.ai", subscription=True)
            elif system.platform == "darwin":
                cli["note"] = "Claude Code keeps its sign-in in the macOS Keychain, which isn't read: run claude to check"
            else:
                cli.update(auth="signed_out", reauthenticate=True, note="Run claude and sign in with your Claude account")
    elif provider == "codex":
        # Codex's documented status command; its output may name a masked API key, so only its kind is kept.
        output, status = system.run(path, "login", "status")
        lowered = output.lower()
        if status == 0 and "chatgpt" in lowered:
            cli.update(auth="signed_in", method="chatgpt", subscription=True)
        elif status == 0 and "api key" in lowered:
            cli.update(auth="signed_in", method="api_key", subscription=False, note="Codex is signed in with an API key, not a ChatGPT plan: run codex login")
        elif status == 0:
            cli["auth"] = "signed_in"
        elif status > 0:
            cli.update(auth="signed_out", reauthenticate=True, note="Run codex login (codex login --device-auth without a browser)")
    elif provider == "copilot":
        # Copilot CLI has no status command; a token in its documented variables is a sign-in.
        for name in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
            if system.getenv(name):
                cli.update(auth="signed_in", method="token:" + name)
                break
        else:
            cli["note"] = "Copilot CLI can't say whether it's signed in: run copilot and /login if it asks"
    return cli


def first_line(text: str, size: int = 60) -> str:
    return text.strip().split("\n", 1)[0].strip()[:size]


def main(args: List[str]) -> int:
    if not args or args[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 2 if not args else 0
    from .launcher import APIError, LaunchError

    try:
        if args[0] == "detect":
            detect_command(args[1:])
        elif args[0] == "list":
            list_command()
        elif args[0] == "status":
            detect_command([])
            print()
            list_command()
        elif args[0] == "enroll":
            enroll_command(args[1:])
        else:
            print(f"agent accounts: no command {args[0]}\n\n{USAGE}", end="", file=sys.stderr)
            return 2
    except (ValueError, APIError, LaunchError, OSError) as error:
        print(f"agent accounts: {error}", file=sys.stderr)
        return 1
    return 0


def detect_command(args: List[str]) -> None:
    report = detect()
    if args[:1] == ["--json"]:
        print(json.dumps(report, separators=(",", ":")))
        return
    print("Provider CLIs on this computer:")
    for cli in report["clis"]:
        state = "not installed"
        if cli["installed"]:
            state = cli["auth"].replace("_", " ") + (f" ({cli['method']})" if cli.get("method") else "")
        print(f"  {cli['provider']:<8} {cli.get('version') or '-':<26} {state:<28} {', '.join(cli['modes'])}")
        if cli.get("note") and cli["installed"]:
            print(f"           {cli['note']}")


def list_command() -> None:
    from .launcher import connect

    api, server = connect()
    accounts = (api.call("GET", "/api/provider-accounts") or {}).get("accounts") or []
    if not accounts:
        print(f"No provider accounts on {server} yet: agent accounts enroll <claude|codex|copilot>")
        return
    print(f"Provider accounts on {server}:")
    for account in accounts:
        machines = []
        for item in account.get("assignments") or []:
            health = item.get("health") or {}
            machines.append((health.get("machine_name") or item.get("machine_id")) + ("" if health.get("available") else " (not ready)"))
        off = " [turned off]" if account.get("status") == "disabled" else ""
        print(f"  {account['id']}  {account['display_name']} ({account['provider']}, {account['authentication_mode']}){off}\n"
              f"      {account['state']}: {account['state_reason']}\n      runs on: {', '.join(machines) or 'no machines yet'}")


@dataclass
class EnrollOptions:
    provider: str = ""
    name: str = ""
    plan: str = ""
    machine: str = ""
    force: bool = False
    token_stdin: bool = False
    yes: bool = False


def parse_enroll(args: List[str]) -> EnrollOptions:
    options = EnrollOptions()
    index = 0
    while index < len(args):
        arg = args[index]
        name, has_value, value = arg.partition("=")
        if name in ("--name", "--plan", "--machine"):
            if not has_value:
                if index + 1 >= len(args):
                    raise ValueError(f"{name} needs a value")
                index += 1
                value = args[index]
            setattr(options, name[2:], value)
        elif arg == "--force":
            options.force = True
        elif arg == "--token-stdin":
            options.token_stdin = True
        elif arg in ("--yes", "-y"):
            options.yes = True
        elif arg.startswith("-") or options.provider:
            raise ValueError(f"unknown argument {arg}")
        else:
            options.provider = arg
        index += 1
    if options.provider not in PROVIDERS:
        raise ValueError(f"enroll which provider: {', '.join(PROVIDERS)}")
    if options.token_stdin and options.provider != "copilot":
        raise ValueError("only Copilot takes a stored token: Claude Code and Codex stay signed in on each machine")
    return options


def enroll_command(args: List[str]) -> None:
    from .launcher import APIError, connect

    options = parse_enroll(args)
    cli = next(item for item in detect()["clis"] if item["provider"] == options.provider)
    if not options.token_stdin and not options.force:
        if not cli["installed"]:
            raise ValueError(f"{options.provider} isn't installed here; install it, sign in, then enroll (or --force)")
        if cli["auth"] == "signed_out" or cli.get("subscription") is False:
            raise ValueError(f"{options.provider} doesn't look signed in to a subscription here: {cli.get('note')} (or --force)")
    token = read_token() if options.token_stdin else ""
    api, server = connect()
    mode = "token" if options.token_stdin else "runner"
    body: Dict[str, Any] = {"provider": options.provider, "authentication_mode": mode}
    if options.name:
        body["display_name"] = options.name
    if options.plan:
        body["plan_name"] = options.plan
    account = api.call("POST", "/api/provider-accounts", body)
    if account.get("authentication_mode") != mode:
        raise ValueError(f"you already have {account.get('display_name')} as a {account.get('authentication_mode')} account; use another --name")
    print(f"Account : {account['display_name']} ({account['id']})")
    if options.token_stdin:
        if not options.yes:
            if not sys.stderr.isatty():
                raise ValueError("pass --yes to store the token without being asked")
            print(f"Store this token on {server}, encrypted, for the machines you assign {account['display_name']} to? "
                  "Anyone controlling those machines can use your Copilot subscription. [y/N] ", end="", file=sys.stderr, flush=True)
            if not ask().lower().startswith("y"):
                raise ValueError("not stored")
        api.call("PUT", f"/api/provider-accounts/{quote(account['id'], safe='')}/credential", {"token": token})
        print("Token   : stored, encrypted (it can't be read back)")
    try:
        enrolled = api.call("POST", f"/api/provider-accounts/{quote(account['id'], safe='')}/enrollments", {
            "hostname": socket.gethostname(), "machine": options.machine, "detected": {"auth": cli["auth"], "version": cli.get("version", "")},
        })
    except APIError as error:
        if options.token_stdin and error.status == 404:
            print("Machine : this computer isn't one of your fleet machines; assign the account to machines on the Subscriptions page")
            return
        raise
    print(f"Machine : {enrolled['machine']['name']} ({enrolled['machine']['id']}) runs it now")


def read_token() -> str:
    """One line from standard input, without echo when it's a terminal."""
    if sys.stdin.isatty():
        token = getpass.getpass("Fine-grained personal access token (Copilot Requests permission): ")
    else:
        token = sys.stdin.readline(8192)
    token = token.strip()
    if not token:
        raise ValueError("no token on standard input")
    return token


def ask() -> str:
    """An answer from the terminal, when standard input carried the token."""
    if sys.stdin.isatty():
        return input()
    for name in ("/dev/tty", "CONIN$"):
        try:
            with open(name, encoding="utf-8") as terminal:
                return terminal.readline().strip()
        except OSError:
            continue
    return ""
