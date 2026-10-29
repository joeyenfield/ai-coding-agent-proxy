"""The agent command: launches coding agents through this machine's agent-proxy, signs the
machine in to an agent-server, and runs the agent-proxy itself (`agent proxy`)."""

from __future__ import annotations

import sys
from typing import List, Optional

from .. import common, login
from .. import update as updates
from . import launcher, localproxy, settings


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        if args[0] == "login":
            return login.login(args[1:], program="agent", credentials_path=settings.credentials_path, default_server=settings.upstream().server(),
                               label="agent-proxy", done="this machine's agent-proxy uses it",
                               signed_in="This machine's agent-proxy sends everything there from now on (agent logout to stop).")
        if args[0] == "logout":
            return login.logout(args[1:], program="agent", credentials_path=settings.credentials_path)
        if args[0] == "machine":
            # The fleet is its own command, so a machine can supply models without running agents.
            print("Machines join the fleet with their own command now: agent-machine (agent-machine --help).", file=sys.stderr)
            return 2
        if args[0] == "proxy":
            return localproxy.main("agent proxy", args[1:])
        if args[0] == "update":
            return update_command(args[1:])
        if args[0] in ("version", "--version"):
            print(f"agent {common.VERSION} (protocol {common.PROTOCOL})")
            return 0
    return launcher.main(args)


def update_command(args: List[str]) -> int:
    """`agent update [--force] [--server URL]`: the server's Python build installed here with pip, then the
    running agent-proxy stopped, so the next `agent` starts the new one."""
    import argparse

    parser = argparse.ArgumentParser(prog="agent update", description="Install the build the agent-server offers (its Python wheel, checked against the server's SHA-256), and stop the running agent-proxy.")
    parser.add_argument("--force", action="store_true", help="Install it even when the server's version is this one")
    parser.add_argument("--server", default="", help="The agent-server to update from (default: the one this machine's agent-proxy uses)")
    options = parser.parse_args(args)
    server = update_server(options.server)
    try:
        index = updates.fetch(server, settings.PROGRAM)
        before = updates.current()
        if index.get("version") == before and not options.force and not updates.checkout():
            print(f"agent {before} is the version the agent-server at {server} offers; nothing to do (--force installs it anyway).")
            return 0
        installed = updates.install(server, index, settings.PROGRAM)
    except updates.UpdateError as error:
        print(f"agent update: {error}", file=sys.stderr)
        return 1
    print(f"Updated agent for {sys.executable}: {before or common.VERSION} -> {installed}.")
    print(updates.notes(index), end="")
    # The running agent-proxy is the old code: stop it, and the next agent starts the new one.
    try:
        if localproxy.stop(settings.proxy_url()):
            print("Stopped the running agent-proxy; the next agent starts the new one.")
    except Exception as error:  # an old or foreign process on the port: say so, the update itself is done
        print(f"Stop the running agent-proxy so the new one takes over: {error}")
    return 0


def update_server(fixed: str = "") -> str:
    """The server `agent update` takes its build from: --server, else the one the running agent-proxy
    carries traffic to, else AI_PROXY_URL or the sign-in's."""
    if fixed:
        return fixed.rstrip("/")
    running = common.health(settings.proxy_url())
    if running and running.get("role") == "agent-proxy" and running.get("server"):
        return str(running["server"])
    return settings.upstream().server()


def proxy_main() -> int:
    """agent-proxy on its own, for running it as a service."""
    # How it names itself to the server, in its User-Agent.
    settings.PROGRAM = "agent-proxy"
    return localproxy.main("agent-proxy", sys.argv[1:])
