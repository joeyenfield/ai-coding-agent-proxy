"""agent-machine's command line: signing in, running in the fleet (in the foreground, or started
in the background), and the machine's settings."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

from .. import common, login
from .. import update as updates
from . import containers, mcp, presence, providers, servers, settings

USAGE = """agent-machine puts this computer in an agent-server's fleet, so the server can use the
LLM providers it runs (Ollama, Strata, Docker Model Runner, containers) and start agents on it.

Usage: agent-machine                           this machine as the fleet sees it
       agent-machine login [url]                 (OIDC device sign-in)
       agent-machine logout [url]
       agent-machine start                     join the fleet in the background
       agent-machine stop
       agent-machine run [--server url]        join the fleet in the foreground (Ctrl+C leaves)
       agent-machine containers on|off         let the server run Strata or Ollama here in containers (off by default)
       agent-machine containers cli auto|docker|podman  which runs them (auto: whichever is installed and answering)
       agent-machine servers on|off            let the server manage the model servers here: start and stop them, their settings and models (off by default)
       agent-machine launches on|off           let the server start agents here (off by default)
       agent-machine commands on|off           let you open a terminal here from the console, in a launch folder (off by default)
       agent-machine roots add <dir>           a folder agents may be started in (and any folder inside it)
       agent-machine roots remove <dir>
       agent-machine mcp                       the MCP servers this machine shares with the server's agents
       agent-machine mcp add <name> [--env NAME=VALUE]... [--dir <dir>] [--description <text>] -- <command> [args...]
       agent-machine mcp add <name> --url <url> [--header "Name: value"]... [--description <text>]
       agent-machine mcp remove <name>
       agent-machine name <name>|-             the name the console shows (default: the hostname)
       agent-machine address <host>|-          how the server reaches this machine's model servers
       agent-machine update [--force]          install the build the server offers (its Python wheel, with pip), and restart it
       agent-machine updates on|off            let the console update agent-machine and agent here to the server's build (off by default)
       agent-machine version

Ollama, Strata and Docker Model Runner running here are found by themselves, and the console
sees their models, logs and this machine's load through agent-machine. With servers on, it can
also pull, delete, load and unload models, start Ollama (ollama serve, with the settings saved
from the console) and Strata's saved profiles here, and stop them.
Containers run Strata or Ollama images only, with their own settings; docker run is root here.
Launches run the agent command (AI_PROXY_AGENT, else the Python agent), signed in to the same server with agent login.
Only you, signed in as this machine's owner, can start agents, containers or terminals here.
A terminal from the console (commands on) is a shell as this user: only you see it.
MCP servers run here, as this user, for the agents you choose and the teams you let use them
(the machine's Access tab). Their commands, environments and URLs stay here.
Settings changes reach the server within a minute.
"""

STARTED_ENV = "AI_PROXY_MACHINE_STARTED"


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] == "status":
        show(settings.Machine.load())
        return 0
    command = args[0]
    if command in ("-h", "--help", "help"):
        print(USAGE, end="")
        return 0
    if command in ("version", "--version"):
        print(f"agent-machine {common.VERSION} (protocol {common.PROTOCOL})")
        return 0
    if command == "login":
        return login.login(args[1:], program="agent-machine", credentials_path=settings.credentials_path, default_server=settings.upstream().server(),
                           label="agent-machine", done="agent-machine supplies it",
                           signed_in="agent-machine supplies this machine's providers there from now on (agent-machine logout to stop). Start it with: agent-machine start")
    if command == "logout":
        return login.logout(args[1:], program="agent-machine", credentials_path=settings.credentials_path)
    if command == "run":
        return run(args[1:])
    if command == "start":
        return start(args[1:])
    if command == "stop":
        return stop()
    if command == "mcp":
        return mcp_command(args[1:])
    if command == "update":
        return update_command(args[1:])
    return configure(args)


def fail(message: str) -> int:
    print(f"agent-machine: {message}\n\n{USAGE}", file=sys.stderr, end="")
    return 2


def mcp_command(args: List[str]) -> int:
    """Lists, adds and removes the MCP servers this machine shares (mcp.json)."""
    if not args:
        return list_mcp()
    if args[0] == "add":
        try:
            server = parse_mcp(args[1:])
            mcp.add(server)
        except ValueError as problem:
            return fail(str(problem))
        print(f"Sharing {server.name}. Agents launched with --mcp {server.name} (or chosen on Start an agent) can use it, within a minute.")
        if not server.found():
            print(f"Note: {server.command[0]} isn't on this machine's PATH yet.")
        return list_mcp()
    if args[0] == "remove":
        if len(args) != 2:
            return fail("mcp remove takes a name")
        try:
            removed = mcp.remove(args[1])
        except ValueError as problem:
            return fail(str(problem))
        if not removed:
            return fail(f"this machine shares no MCP server called {args[1]}")
        return list_mcp()
    return fail("mcp takes add or remove")


def parse_mcp(args: List[str]) -> mcp.Server:
    if not args or args[0].startswith("-"):
        raise ValueError("mcp add takes a name first")
    server = mcp.Server(name=args[0])
    index = 1
    while index < len(args):
        arg = args[index]
        if arg == "--":
            server.command = args[index + 1:]
            break
        # --name value or --name=value (--env=NAME=VALUE included).
        name, has_value, value = arg.partition("=")
        if not has_value:
            if index + 1 >= len(args):
                raise ValueError(f"{arg} needs a value")
            index += 1
            value = args[index]
        if name == "--url":
            server.url = value
        elif name == "--dir":
            folder = os.path.abspath(value)
            if not os.path.isdir(folder):
                raise ValueError(f"{folder} isn't a folder")
            server.dir = folder
        elif name == "--description":
            server.description = value
        elif name == "--env":
            key, has_setting, setting = value.partition("=")
            if not has_setting or not key:
                raise ValueError("--env takes NAME=VALUE")
            server.env[key] = setting
        elif name == "--header":
            key, has_setting, setting = value.partition(":")
            if not has_setting or not key.strip():
                raise ValueError('--header takes "Name: value"')
            server.headers[key.strip()] = setting.strip()
        else:
            raise ValueError(f"unknown option {arg} (the command goes after --)")
        index += 1
    server.check()
    return server


def describe_mcp(server: mcp.Server) -> str:
    how = server.url or " ".join(server.command) + ("" if server.found() else " (not installed)")
    return f"{server.name} ({server.transport}): {how}"


def list_mcp() -> int:
    try:
        servers = mcp.load()
    except ValueError as problem:
        print(f"agent-machine: {problem}", file=sys.stderr)
        return 1
    if not servers:
        print("MCP       : none shared (agent-machine mcp add <name> -- <command>)")
    for index, server in enumerate(servers):
        print("MCP       :" if index == 0 else "           ", describe_mcp(server))
    return 0


def service_command(args: List[str]) -> int:
    """sudo agent-machine servers service [off]: lets this user restart Ollama's systemd service."""
    off = len(args) == 3 and args[2] == "off"
    user = os.environ.get("SUDO_USER", "")
    try:
        servers.set_up_service(user, off)
    except (servers.Refused, OSError) as problem:
        return fail(str(problem))
    if off:
        print("agent-machine can no longer restart ollama.service or change its settings.")
    else:
        print(f"{user}'s agent-machine may now start, stop and restart ollama.service, and set its OLLAMA_* settings ({servers.SERVICE_ENV_PATH}).")
        print("Undo it with: sudo agent-machine servers service off")
    return 0


def configure(args: List[str]) -> int:
    """Changes one of machine.json's settings, then shows the machine."""
    machine = settings.Machine.load()
    command = args[0]
    if command == "servers" and len(args) >= 2 and args[1] == "service":
        return service_command(args)
    if command in ("launches", "servers", "commands", "updates"):
        if len(args) != 2 or args[1] not in ("on", "off"):
            return fail("servers takes on, off, or service [off] (with sudo)" if command == "servers" else f"{command} takes on or off")
        setattr(machine, command, args[1] == "on")
        if command == "launches" and machine.launches and not machine.roots:
            print("Launches are on, but there's no folder to start agents in yet: agent-machine roots add <dir>")
        if command == "updates" and machine.updates:
            print("Updates are on: the machine's owner or an admin can update agent-machine and agent here to the server's build from the console.")
        if command == "commands" and machine.commands:
            print("Commands are on: signed in as this machine's owner, you can open a terminal here from the console, as this user.")
            if not machine.roots:
                print("Terminals start in a launch folder, and there's none yet: agent-machine roots add <dir>")
    elif command == "roots":
        if len(args) != 3 or args[1] not in ("add", "remove"):
            return fail("roots takes add or remove, and a folder")
        folder = os.path.abspath(args[2])
        same = lambda root: os.path.normpath(root).lower() == os.path.normpath(folder).lower()  # noqa: E731  (as the Go one: case-insensitive everywhere)
        if args[1] == "add":
            if not os.path.isdir(folder):
                return fail(f"{folder} isn't a folder")
            if not any(same(root) for root in machine.roots):
                machine.roots.append(folder)
        else:
            machine.roots = [root for root in machine.roots if not same(root)]
    elif command == "containers":
        if len(args) == 2 and args[1] in ("on", "off"):
            machine.containers = args[1] == "on"
        elif len(args) == 3 and args[1] == "cli" and args[2] in ("auto", "docker", "podman"):
            machine.container_cli = "" if args[2] == "auto" else args[2]
        else:
            return fail("containers takes on, off, or cli auto|docker|podman")
    elif command in ("name", "address"):
        if len(args) != 2:
            return fail(f"{command} takes one value, or - to clear it")
        setattr(machine, command, "" if args[1] == "-" else args[1])
    else:
        return fail(f"unknown command {command}")
    machine.save()
    show(machine)
    return 0


def started_by() -> str:
    """How this run began, for status.json."""
    if os.environ.get(STARTED_ENV) == "background":
        return "background"
    return "terminal" if sys.stdin is not None and sys.stdin.isatty() else "service"


def open_log() -> None:
    log = logging.getLogger("agent-machine")
    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(message)s", "%Y/%m/%d %H:%M:%S")
    try:
        settings.home().mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(settings.log_path(), encoding="utf-8")
        handler.setFormatter(formatter)
        log.addHandler(handler)
    except OSError:
        pass
    if sys.stderr is not None and sys.stderr.isatty():
        terminal = logging.StreamHandler(sys.stderr)
        terminal.setFormatter(formatter)
        log.addHandler(terminal)


def run(args: List[str]) -> int:
    """Stays in the fleet until interrupted, reconnecting whenever the connection drops."""
    parser = argparse.ArgumentParser(prog="agent-machine run")
    parser.add_argument("--server", default="", help=f"The agent-server to join (default: the one from agent-machine login, then {common.DEFAULT_SERVER})")
    options = parser.parse_args(args)
    other = settings.running()
    if other:
        print(f"agent-machine is already running (process {other.pid}, {other.state}). Stop it first: agent-machine stop", file=sys.stderr)
        return 1
    open_log()
    upstream = settings.upstream(options.server)
    status = settings.Status(pid=os.getpid(), server=upstream.server(), state="connecting", started=started_by())
    status.save()
    log = logging.getLogger("agent-machine")
    log.info("agent-machine %s joining %s", common.VERSION, status.server)

    def on_status(state: str) -> None:
        status.state = state
        status.save()

    def on_server_version(version: str) -> None:
        status.server_version = version
        status.save()

    member = presence.Presence(upstream, on_status, on_server_version)

    async def serve() -> None:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for name in ("SIGINT", "SIGTERM"):
            try:
                loop.add_signal_handler(getattr(signal, name), stop_event.set)
            except (NotImplementedError, AttributeError, ValueError):
                pass  # Windows: Ctrl+C arrives as KeyboardInterrupt, and stop terminates the process.
        await member.run(stop_event)

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
    status.pid, status.state = 0, "stopped"
    status.save()
    log.info("agent-machine left the fleet")
    return 0


def update_command(args: List[str]) -> int:
    """`agent-machine update [--force] [--server URL]`: the server's Python build installed here with pip,
    then the running agent-machine restarted on it."""
    parser = argparse.ArgumentParser(prog="agent-machine update", description="Install the build the agent-server offers (its Python wheel, checked against the server's SHA-256), and restart the running agent-machine.")
    parser.add_argument("--force", action="store_true", help="Install it even when the server's version is this one")
    parser.add_argument("--server", default="", help="The agent-server to update from (default: the one from agent-machine login)")
    parser.add_argument("--with-agent", action="store_true", help="Update the agent command first (agent update), as a remote update from the console asks")
    options = parser.parse_args(args)
    other = settings.running()
    server = options.server or (other.server if other and other.server else settings.upstream().server())
    # Read before the agent's update: an AI_PROXY_AGENT on this same Python installs the same wheel.
    before = updates.current()
    if options.with_agent and os.environ.get("AI_PROXY_AGENT"):
        update_agent(os.environ["AI_PROXY_AGENT"], server)
    try:
        index = updates.fetch(server, settings.PROGRAM)
        if index.get("version") == before and not options.force and not updates.checkout():
            print(f"agent-machine {before} is the version the agent-server at {server} offers; nothing to do (--force installs it anyway).")
            return 0
        installed = updates.install(server, index, settings.PROGRAM)
    except updates.UpdateError as error:
        print(f"agent-machine update: {error}", file=sys.stderr)
        return 1
    print(f"Updated agent-machine for {sys.executable}: {before or common.VERSION} -> {installed}.")
    print(updates.notes(index), end="")
    if options.with_agent and not os.environ.get("AI_PROXY_AGENT"):
        # The agent command is this same package, now updated: only its running agent-proxy is still the old code.
        subprocess.run([sys.executable, "-m", "agent_py.client", "proxy", "stop"], cwd=str(Path.home()))
    if updates.checkout():
        print(f"This copy ran from the checkout at {updates.checkout()}; agent-machine now runs the installed one.")
    return restart_after_update()


def update_agent(program: str, server: str) -> None:
    """`agent update --server <server>` with the agent command launches run, as the Go agent-machine does."""
    flags = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}
    try:
        done = subprocess.run([program, "update", "--server", server], **flags)
    except OSError as error:
        print(f"agent update: {error}", file=sys.stderr)
        return
    if done.returncode != 0:
        print(f"agent update: exit status {done.returncode}", file=sys.stderr)


def restart_after_update() -> int:
    """Puts the running agent-machine on the new build, however it was started."""
    other = settings.running()
    if other is None:
        return 0
    if other.started == "terminal":
        print(f"agent-machine runs in a terminal (process {other.pid}): stop it there and run it again for the new version.")
        return 0
    if other.started == "service":
        code = stop()
        if code == 0:
            print("Its service starts it again with the new version within a few seconds (systemd, launchd); if yours doesn't, restart the service.")
        return code
    code = stop()
    if code != 0:
        return code
    # From the home folder, so a checkout in the folder it ran from can't stand in for the installed package.
    return start(["--server", other.server] if other.server else [], cwd=str(Path.home()))


def start(args: List[str], cwd: Optional[str] = None) -> int:
    """Runs `agent-machine run` detached from this terminal, and waits until it has connected or failed to."""
    other = settings.running()
    if other:
        print(f"agent-machine is already running (process {other.pid}): {other.state}")
        return 0
    if sys.platform == "win32":
        flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    else:
        flags = {"start_new_session": True}
    try:
        process = subprocess.Popen([sys.executable, "-m", "agent_py.machine", "run", *args], env={**os.environ, STARTED_ENV: "background"}, cwd=cwd,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **flags)
    except OSError as error:
        print(f"agent-machine: couldn't start: {error}", file=sys.stderr)
        return 1
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = settings.Status.load()
        if status.pid == process.pid and status.state != "connecting":
            print(f"agent-machine started (process {process.pid}): {status.state}\nIts log: {settings.log_path()}")
            return 0
        if process.poll() is not None:
            print(f"agent-machine exited while starting. Run `agent-machine run` to see why (log: {settings.log_path()})", file=sys.stderr)
            return 1
        time.sleep(0.2)
    print(f"agent-machine started (process {process.pid}), still connecting. Its log: {settings.log_path()}")
    return 0


def stop() -> int:
    other = settings.running()
    if other is None:
        print("agent-machine isn't running.")
        return 0
    try:
        settings.stop_process(other.pid)
    except OSError as error:
        print(f"agent-machine: couldn't stop process {other.pid}: {error}", file=sys.stderr)
        return 1
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and settings.alive(other.pid):
        time.sleep(0.1)
    if settings.alive(other.pid):
        print(f"agent-machine (process {other.pid}) is still running", file=sys.stderr)
        return 1
    # A killed process (Windows has no SIGTERM) can't say it stopped; say it for it.
    status = settings.Status.load()
    if status.pid == other.pid:
        status.pid, status.state = 0, "stopped"
        status.save()
    print("agent-machine stopped; this machine has left the fleet.")
    return 0


def show(machine: settings.Machine) -> None:
    upstream = settings.upstream()
    server = upstream.server()
    entry = upstream.credentials().servers.get(common.normalize(server))
    if entry is None:
        signed_in = f"not signed in (agent-machine login {server})"
    elif entry.access_token and entry.user:
        signed_in = f"as {entry.user}"
    elif entry.access_token:
        signed_in = "with OAuth"
    else:
        signed_in = f"OAuth sign-in required (agent-machine login {server})"
    if os.environ.get("AI_PROXY_MACHINE_TOKEN"):
        signed_in = "with an injected OAuth access token"
    print(f"Machine   : {machine.display_name()} ({machine.id})\nServer    : {server}, {signed_in}")
    status = settings.running()
    if status:
        print(f"Fleet     : {status.state} (process {status.pid})")
        notice = presence.version_notice(common.VERSION, status.server_version)
        if notice:
            print("Version   :", notice)
    else:
        print("Fleet     : not running (agent-machine start)")
    if machine.address:
        print("Address   :", machine.address)
    found = providers.detect()
    if not found:
        print("Providers : no Ollama, Strata or Docker Model Runner answering here")
    for index, provider in enumerate(found):
        print(f"{'Providers :' if index == 0 else '           '} {provider['type']} on port {provider['port']}")
    show_containers(machine)
    print("Servers   : on: the server can manage the model servers here (start, stop, settings, models)" if machine.servers else "Servers   : off (agent-machine servers on)")
    print("Launches  :", "on" if machine.launches else "off (agent-machine launches on)")
    print("Updates   : on: the console can update agent-machine and agent here to the server's build" if machine.updates else "Updates   : off (agent-machine updates on)")
    print("Commands  : on: you can open a terminal here from the console" if machine.commands else "Commands  : off (agent-machine commands on)")
    if machine.launches and not machine.roots:
        print("Folders   : none (agent-machine roots add <dir>)")
    try:
        shared = mcp.load()
    except ValueError:
        shared = []
    for index, server in enumerate(shared):
        print("MCP       :" if index == 0 else "           ", describe_mcp(server))
    for index, root in enumerate(machine.roots):
        print("Folders   :" if index == 0 else "           ", root)


def show_containers(machine: settings.Machine) -> None:
    cli = containers.cli_for(machine)
    how = " (found by itself; agent-machine containers cli docker|podman picks one)"
    if machine.container_cli:
        how = " (chosen; agent-machine containers cli auto finds it by itself)"
    if not machine.containers:
        print("Containers: off (agent-machine containers on)")
    elif cli.command() is None and not machine.container_cli:
        print("Containers: on, but neither docker nor podman is installed or on PATH")
    elif cli.command() is None:
        print(f"Containers: on, but {cli.name} isn't installed or isn't on PATH")
    elif not cli.probe()["online"]:
        print(f"Containers: on, with {cli.name}{how}, but its engine isn't answering (start Docker Desktop, or `podman machine start`)")
    else:
        print(f"Containers: on, with {cli.name}{how}")
