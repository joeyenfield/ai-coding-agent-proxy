"""`login` and `logout` for both programs: trading a username and password for a token,
kept with the server's URL in the program's own credentials file. The password isn't stored.
A server without accounts is remembered too, with its access key when one is given (--key).
"""

from __future__ import annotations

import argparse
import getpass
import os
import socket
import sys
from pathlib import Path
from typing import Callable, List

import httpx

from . import common


def login(args: List[str], *, program: str, credentials_path: Callable[[], Path], default_server: str, label: str, done: str) -> int:
    parser = argparse.ArgumentParser(prog=f"{program} login", description=f"Sign this machine in to an agent-server. url defaults to {default_server}.")
    parser.add_argument("url", nargs="?", default=default_server)
    parser.add_argument("-u", "--user", default="", help="Your username (asked for when omitted)")
    parser.add_argument("--key", default="", help="For a server without accounts: its access key, to use it from another machine")
    options = parser.parse_args(args)
    server = options.url.strip().rstrip("/")
    if "://" not in server:
        server = "http://" + server
    credentials = common.Credentials.load(credentials_path())
    with common.client(20, program) as http:
        try:
            response = http.get(server + "/api/auth/status")
            if response.status_code >= 400:
                raise RuntimeError(common.detail(response.content))
            required = bool(response.json().get("required"))
        except (httpx.HTTPError, ValueError, RuntimeError) as error:
            print(f"{program} login: couldn't reach {server}: {error}", file=sys.stderr)
            return 1
        if not required:
            credentials.servers[common.normalize(server)] = common.ServerEntry(server, token=options.key)
            credentials.default = common.normalize(server)
            credentials.save()
            print(f"{server} doesn't use accounts, so there's no sign-in; {done} from now on.")
            return 0
        if not server.startswith("https://") and not common.is_loopback_url(server):
            print("Warning: this connection isn't encrypted; your password crosses the network in the clear unless you're on a VPN such as Tailscale.", file=sys.stderr)
        name = options.user.strip()
        if not name:
            print("Username: ", end="", flush=True)
            name = sys.stdin.readline().strip()
        password = os.environ.get("AI_PROXY_PASSWORD_INPUT", "")
        if not password:
            if sys.stdin.isatty():
                password = getpass.getpass("Password: ")
            else:
                print("Password: ", end="", flush=True)
                password = sys.stdin.readline().rstrip("\r\n")
        try:
            response = http.post(server + "/api/auth/token", json={"username": name, "password": password, "label": f"{label} on {socket.gethostname()}"})
        except httpx.HTTPError as error:
            print(f"{program} login: {error}", file=sys.stderr)
            return 1
    if response.status_code >= 400:
        print(f"{program} login: {common.detail(response.content)}", file=sys.stderr)
        return 1
    token = response.json()
    credentials.servers[common.normalize(server)] = common.ServerEntry(server, str(token.get("user") or ""), str(token.get("token") or ""), str(token.get("id") or ""))
    credentials.default = common.normalize(server)
    credentials.save()
    print(f"Signed in to {server} as {token.get('user')}. {done[0].upper() + done[1:]} from now on ({program} logout to stop).")
    return 0


def logout(args: List[str], *, program: str, credentials_path: Callable[[], Path]) -> int:
    parser = argparse.ArgumentParser(prog=f"{program} logout", description="Forget this machine's sign-in to an agent-server (default: the one signed in to last).")
    parser.add_argument("url", nargs="?")
    options = parser.parse_args(args)
    credentials = common.Credentials.load(credentials_path())
    key = common.normalize(options.url) if options.url else credentials.default
    entry = credentials.servers.pop(key, None) if key else None
    if entry is None:
        print("Not signed in to that server.")
        return 0
    if entry.token and entry.token_id:
        # Best effort: the server may be gone.
        try:
            with common.client(10, program) as http:
                http.delete(entry.url.rstrip("/") + "/api/auth/tokens/" + entry.token_id, headers={common.TOKEN_HEADER: entry.token})
        except httpx.HTTPError:
            pass
    if credentials.default == key:
        credentials.default = next(iter(credentials.servers), "")
    credentials.save()
    print(f"Signed out of {entry.url}.")
    return 0
