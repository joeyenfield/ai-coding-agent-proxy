"""OIDC device sign-in and refresh-credential revocation for both programs."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, List

import httpx

from . import common, oauth


def login(args: List[str], *, program: str, credentials_path: Callable[[], Path], default_server: str, label: str, done: str, signed_in: str = "") -> int:
    token_env = "AI_PROXY_MACHINE_TOKEN" if program == "agent-machine" else "AI_PROXY_TOKEN"
    for arg in args:
        if arg == "-u" or arg.startswith(("--user", "--key")):
            print(f"Password/key login was removed. Use login [server] for OIDC device sign-in, or {token_env} for an OAuth service-account access token.", file=sys.stderr)
            return 2
    parser = argparse.ArgumentParser(prog=f"{program} login", description=f"Sign in using a browser and a device code. For automation inject an OAuth access token with {token_env}.")
    parser.add_argument("url", nargs="?", default=default_server)
    options = parser.parse_args(args)
    server = options.url.strip().rstrip("/")
    if "://" not in server:
        server = "https://" + server
    try:
        entry = oauth.device_login(server, sys.stdout, program)
        with oauth.credential_lock(credentials_path()):
            credentials = common.Credentials.load(credentials_path())
            credentials.servers[common.normalize(server)] = entry
            credentials.default = common.normalize(server)
            credentials.save()
    except (httpx.HTTPError, ValueError, OSError, RuntimeError) as error:
        print(f"{program} login: {error}", file=sys.stderr)
        return 1
    print(f"Signed in to {server} as {entry.user}.")
    return 0


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
