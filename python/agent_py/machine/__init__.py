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
from . import presence, providers, settings

USAGE = """agent-machine puts this computer in an agent-server's fleet, so the server can use the
model servers it runs (Ollama, Strata, Docker Model Runner) and start agents on it.
This is the Python agent-machine; containers and starting model servers are the Go one's only.

Usage: agent-machine                           this machine as the fleet sees it
       agent-machine login [url] [--user name] [--key access-key]
       agent-machine logout [url]
       agent-machine start                     join the fleet in the background
       agent-machine stop
       agent-machine run [--server url]        join the fleet in the foreground (Ctrl+C leaves)
       agent-machine servers on|off            let the server pull, delete, load and unload models here (off by default)
       agent-machine launches on|off           let the server start agents here (off by default)
       agent-machine commands on|off           let you open a terminal here from the console, in a launch folder (off by default)
       agent-machine roots add <dir>           a folder agents may be started in (and any folder inside it)
       agent-machine roots remove <dir>
       agent-machine name <name>|-             the name the console shows (default: the hostname)
       agent-machine address <host>|-          how the server reaches this machine's model servers
       agent-machine update [--force]          install the build the server offers (its Python wheel, with pip), and restart it
       agent-machine version

Launches run the agent command (AI_PROXY_AGENT, else the Python agent), signed in to the same
server with agent login. Only you, signed in as this machine's owner, can start agents or
terminals here; a terminal from the console (commands on) is a shell as this user that only you see.
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
                           label="agent-machine", done="agent-machine supplies it")
    if command == "logout":
        return login.logout(args[1:], program="agent-machine", credentials_path=settings.credentials_path)
    if command == "run":
        return run(args[1:])
    if command == "start":
        return start(args[1:])
    if command == "stop":
        return stop()
    if command == "update":
        return update_command(args[1:])
    return configure(args)


def fail(message: str) -> int:
    print(f"agent-machine: {message}\n\n{USAGE}", file=sys.stderr, end="")
    return 2


def configure(args: List[str]) -> int:
    """Changes one of machine.json's settings, then shows the machine."""
    machine = settings.Machine.load()
    command = args[0]
    if command in ("launches", "servers", "commands"):
        if len(args) != 2 or args[1] not in ("on", "off"):
            return fail(f"{command} takes on or off")
        setattr(machine, command, args[1] == "on")
        if command == "launches" and machine.launches and not machine.roots:
            print("Launches are on, but there's no folder to start agents in yet: agent-machine roots add <dir>")
        if command == "commands" and machine.commands:
            print("Commands are on: signed in as this machine's owner, you can open a terminal here from the console, as this user.")
            if not machine.roots:
                print("Terminals start in a launch folder, and there's none yet: agent-machine roots add <dir>")
    elif command == "roots":
        if len(args) != 3 or args[1] not in ("add", "remove"):
            return fail("roots takes add or remove, and a folder")
        folder = os.path.abspath(args[2])
        same = lambda root: os.path.normcase(os.path.normpath(root)) == os.path.normcase(os.path.normpath(folder))  # noqa: E731
        if args[1] == "add":
            if not os.path.isdir(folder):
                return fail(f"{folder} isn't a folder")
            if not any(same(root) for root in machine.roots):
                machine.roots.append(folder)
        else:
            machine.roots = [root for root in machine.roots if not same(root)]
    elif command == "containers":
        return fail("containers are the Go agent-machine's only")
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
    log.info("agent-machine %s (python) joining %s", common.VERSION, status.server)

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
    options = parser.parse_args(args)
    other = settings.running()
    server = options.server or (other.server if other and other.server else settings.upstream().server())
    try:
        index = updates.fetch(server, settings.PROGRAM)
        if index.get("version") == common.VERSION and not options.force:
            print(f"agent-machine {common.VERSION} is the version the agent-server at {server} offers; nothing to do (--force installs it anyway).")
            return 0
        installed = updates.install(server, index, settings.PROGRAM)
    except updates.UpdateError as error:
        print(f"agent-machine update: {error}", file=sys.stderr)
        return 1
    print(f"Updated agent-machine for {sys.executable}: {common.VERSION} -> {installed}.")
    print(updates.notes(index), end="")
    if updates.checkout():
        print(f"This copy ran from the checkout at {updates.checkout()}; agent-machine now runs the installed one.")
    return restart_after_update()


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
    elif entry.user:
        signed_in = f"as {entry.user}"
    elif entry.token:
        signed_in = "with an access key"
    else:
        signed_in = "no accounts there"
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
    print("Containers: not in the Python agent-machine")
    print("Servers   : on: the server can change the models here (pull, delete, load, unload)" if machine.servers else "Servers   : off (agent-machine servers on)")
    print("Launches  :", "on" if machine.launches else "off (agent-machine launches on)")
    print("Commands  : on: you can open a terminal here from the console" if machine.commands else "Commands  : off (agent-machine commands on)")
    if machine.launches and not machine.roots:
        print("Folders   : none (agent-machine roots add <dir>)")
    for index, root in enumerate(machine.roots):
        print("Folders   :" if index == 0 else "           ", root)
