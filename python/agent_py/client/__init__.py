"""The agent command: launches coding agents through this machine's agent-proxy, signs the
machine in to an agent-server, and runs the agent-proxy itself (`agent proxy`)."""

from __future__ import annotations

import sys
from typing import List, Optional

from .. import common, login
from . import launcher, localproxy, settings


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        if args[0] == "login":
            return login.login(args[1:], program="agent", credentials_path=settings.credentials_path, default_server=settings.upstream().server(),
                               label="agent-proxy", done="this machine's agent-proxy sends everything there")
        if args[0] == "logout":
            return login.logout(args[1:], program="agent", credentials_path=settings.credentials_path)
        if args[0] == "machine":
            # The fleet is its own command, so a machine can supply models without running agents.
            print("Machines join the fleet with their own command: agent-machine (agent-machine --help).", file=sys.stderr)
            return 2
        if args[0] == "proxy":
            return localproxy.main("agent proxy", args[1:])
        if args[0] == "update":
            print("agent update: this is the Python agent; update it with pip (pip install --upgrade ai-proxy-agent-py).", file=sys.stderr)
            return 2
        if args[0] in ("version", "--version"):
            print(f"agent {common.VERSION} (protocol {common.PROTOCOL})")
            return 0
    return launcher.main(args)


def proxy_main() -> int:
    """agent-proxy on its own, for running it as a service."""
    return localproxy.main("agent-proxy", sys.argv[1:])
