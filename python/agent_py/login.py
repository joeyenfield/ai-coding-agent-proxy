"""OIDC device sign-in and refresh-credential revocation for both programs."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

import httpx

from . import common, oauth

# The toggles to switch on after a sign-in, as agent-machine login --enable does: with
# oauth.SETUP_CODE_ENV, what sets up a container that nobody configures by hand.
ENABLE_ENV = "AI_PROXY_MACHINE_ENABLE"


@dataclass
class Setup:
    """What agent-machine's login adds to the agent's: a setup code instead of a browser (--code) and
    toggles to switch on once signed in (--enable). The machine supplies both, as it owns its toggles."""

    names: List[str]
    parse: Callable[[str], List[str]]  # raises ValueError for an unknown name
    enable: Callable[[List[str]], None]


def login(args: List[str], *, program: str, credentials_path: Callable[[], Path], default_server: str, label: str, done: str, signed_in: str = "",
          setup: Optional[Setup] = None) -> int:
    token_env = "AI_PROXY_MACHINE_TOKEN" if program == "agent-machine" else "AI_PROXY_TOKEN"
    # agent and agent-machine each take a setup code from their own variable (Go's SetupCodeEnvironment).
    code_env = oauth.SETUP_CODE_ENV if program == "agent-machine" else oauth.AGENT_SETUP_CODE_ENV
    for arg in args:
        if arg == "-u" or arg.startswith(("--user", "--key")):
            print(f"Password/key login was removed. Use login [server] for OIDC device sign-in, or {token_env} for an OAuth service-account access token.", file=sys.stderr)
            return 2
    if setup is None:
        parser = argparse.ArgumentParser(prog=f"{program} login", description="Sign in using a browser and a device code, or with a setup code created on the console's Machines page. "
                                         f"For automation inject an OAuth access token with {token_env}.")
        parser.add_argument("--code", default=None, help=f"A setup code from the console's Machines page, used instead of approving in a browser (default: ${code_env})")
    else:
        parser = argparse.ArgumentParser(prog=f"{program} login", description="Sign in using a browser and a device code, or with a setup code created on the console's Machines page "
                                         f"(Add a machine, Create a setup code). For automation inject an OAuth access token with {token_env}.")
        parser.add_argument("--code", default=None, help=f"A setup code from the console's Machines page, used instead of approving in a browser (default: ${oauth.SETUP_CODE_ENV})")
        parser.add_argument("--enable", default=None, help=f"Toggles to switch on once signed in, comma-separated: {', '.join(setup.names)} (default: ${ENABLE_ENV})")
    parser.add_argument("url", nargs="?", default=default_server)
    options = parser.parse_args(args)
    setup_code = (options.code if options.code is not None else os.environ.get(code_env, "")).strip()
    names: List[str] = []
    if setup is not None:
        # Checked before signing in: a typo mustn't spend a setup code's use on a half-done setup.
        try:
            names = setup.parse(options.enable if options.enable is not None else os.environ.get(ENABLE_ENV, ""))
        except ValueError as error:
            print(f"{program} login: {error}", file=sys.stderr)
            return 2
    try:
        sign_in(server_url(options.url), setup_code, program=program, credentials_path=credentials_path)
        if names and setup is not None:
            setup.enable(names)
    except (httpx.HTTPError, ValueError, OSError, RuntimeError) as error:
        print(f"{program} login: {error}", file=sys.stderr)
        return 1
    return 0


def server_url(server: str) -> str:
    """A server as given, https:// when it has no scheme."""
    server = server.strip().rstrip("/")
    return server if "://" in server else "https://" + server


def sign_in(server: str, setup_code: str, *, program: str, credentials_path: Callable[[], Path]) -> common.ServerEntry:
    """Signs in to server (approved by setup_code, or in a browser when it's empty) and saves the
    credentials as the default server's."""
    entry = oauth.device_login(server, sys.stdout, program, setup_code)
    with oauth.credential_lock(credentials_path()):
        credentials = common.Credentials.load(credentials_path())
        credentials.servers[common.normalize(server)] = entry
        credentials.default = common.normalize(server)
        credentials.save()
    print(f"Signed in to {server} as {entry.user}.")
    return entry


def logout(args: List[str], *, program: str, credentials_path: Callable[[], Path]) -> int:
    parser = argparse.ArgumentParser(prog=f"{program} logout", description="Forget this machine's OAuth sign-in.")
    parser.add_argument("url", nargs="?")
    options = parser.parse_args(args)
    revoke_error = None
    try:
        with oauth.credential_lock(credentials_path()):
            credentials = common.Credentials.load(credentials_path())
            key = common.normalize(options.url) if options.url else credentials.default
            entry = credentials.servers.pop(key, None) if key else None
            if entry is None:
                print("Not signed in to that server.")
                return 0
            try:
                oauth.revoke(entry)
            except (httpx.HTTPError, ValueError, OSError, RuntimeError) as error:
                revoke_error = error
            if credentials.default == key:
                credentials.default = next(iter(credentials.servers), "")
            credentials.save()
    except (OSError, RuntimeError) as error:
        print(f"{program} logout: {error}", file=sys.stderr)
        return 1
    print(f"Signed out of {entry.url}.")
    if revoke_error:
        print(f"{program} logout: local credentials forgotten, but issuer revocation failed: {revoke_error}", file=sys.stderr)
        return 1
    return 0
