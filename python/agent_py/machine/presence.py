"""This machine's membership of the server's fleet: the connection `agent-machine run` keeps open
(/api/machines/connect) to say what the machine is and has (the model servers it runs above
all), and the launches, tunnels and questions about its model servers the server sends over it.

Launches run a program here, so they're off until `agent-machine launches on`, only go to
folders listed with `agent-machine roots add`, and are checked here again whatever the server
says. Containers the server asks for (containers.py) are off the same way, until
`agent-machine containers on`. It starts and stops Ollama (or its systemd service) and Strata's
saved profiles like the Go one (servers.py), once `agent-machine servers on`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
import websockets

from .. import common
from . import containers, mcp, providers, servers, settings

UPDATE_EVERY = 60
PING_EVERY = 25
# How long a launched agent may take to open its session.
LAUNCH_TIMEOUT = 50
RETRY_DELAYS = (2, 5, 10, 30)
CLOSE_UPGRADE_REQUIRED = 4426

log = logging.getLogger("agent-machine")


class Refused(Exception):
    pass


def agent_command() -> List[str]:
    """The agent command launches run: AI_PROXY_AGENT, or the Python agent beside this agent-machine."""
    program = os.environ.get("AI_PROXY_AGENT")
    if program:
        return [program]
    return [sys.executable, "-m", "agent_py.client"]


def visibility(value: str) -> str:
    return value if value in ("team", "org") else "private"


def arguments(request: Dict[str, Any], project: str) -> List[str]:
    """The `agent` command line for a launch, run headless with its terminal shared."""
    args = [str(request.get("agent") or ""), project, "--headless", "--launch-id", str(request.get("launch") or ""), "--share", visibility(str(request.get("visibility") or ""))]
    if request.get("model"):
        args += ["--model", str(request["model"])]
    if request.get("backend"):
        args += ["--backend", str(request["backend"])]
    if request.get("trace"):
        args.append("--trace")
    if request.get("team"):
        args += ["--team", str(request["team"])]
    if request.get("access") == "control":
        args.append("--let-others-type")
    if request.get("mcp"):
        args += ["--mcp", ",".join(str(item) for item in request["mcp"])]
    return args


def check(request: Dict[str, Any], machine: settings.Machine) -> str:
    """The machine's own say over a launch, whatever the server allowed: launches on, and the folder
    inside one of the roots. A new project folder is made, below the part that exists. Returns the folder."""
    if not machine.launches:
        raise Refused("this machine doesn't take launches (agent-machine launches on)")
    folder = str(request.get("project") or "")
    if not folder:
        raise Refused("name a folder")
    current = os.path.abspath(folder)
    missing: List[str] = []
    while not os.path.lexists(current):
        parent, name = os.path.split(current)
        if parent == current or name in ("", ".", ".."):
            raise Refused(f"{folder} isn't inside one of this machine's launch folders")
        missing.insert(0, name)
        current = parent
    base, ok = machine.within(current)
    if not ok:
        raise Refused(f"{folder} isn't inside one of this machine's launch folders")
    project = os.path.join(base, *missing)
    if missing:
        try:
            os.makedirs(project, exist_ok=True)
        except OSError as error:
            raise Refused(f"couldn't create {folder}: {error}")
    if not os.path.isdir(project):
        raise Refused(f"{folder} isn't a folder")
    return project


def check_shell(request: Dict[str, Any], machine: settings.Machine) -> str:
    """The machine's own say over a shell: commands on, and the folder (which has to exist) inside one of the roots."""
    if not machine.commands:
        raise Refused("this machine doesn't take commands (agent-machine commands on)")
    folder = str(request.get("project") or "")
    if not folder or not os.path.isdir(folder):
        raise Refused(f"{folder} isn't a folder here")
    resolved, ok = machine.within(folder)
    if not ok:
        raise Refused(f"{folder} isn't inside one of this machine's launch folders")
    return resolved


def shell_arguments(request: Dict[str, Any], project: str) -> List[str]:
    """The `agent` command line for a shell, headless and private."""
    return ["--shell", project, "--headless", "--launch-id", str(request.get("launch") or "")]


FOLDERS_LIMIT = 500


def folders(request: Dict[str, Any], machine: settings.Machine) -> Dict[str, Any]:
    """The folders inside one folder, as the folders_result to send back: only inside the roots (checked
    here after resolving links), only while launches or commands are on, and only folders, no file names.
    Hidden folders and links that lead out of the roots are left out."""
    answer: Dict[str, Any] = {"type": "folders_result", "request": request.get("request")}
    try:
        listed, parent, truncated = list_folders(str(request.get("path") or ""), machine)
    except Refused as error:
        answer.update(ok=False, error=str(error))
        return answer
    answer.update(ok=True, folders=listed, parent=parent, truncated=truncated)
    return answer


def list_folders(folder: str, machine: settings.Machine) -> Tuple[List[Dict[str, str]], Optional[str], bool]:
    if not (machine.launches or machine.commands):
        raise Refused("this machine doesn't take launches (agent-machine launches on)")
    if not folder or not os.path.isabs(folder):
        raise Refused("name a folder inside one of this machine's launch folders")
    if not machine.within(folder)[1]:
        raise Refused(f"{folder} isn't inside one of this machine's launch folders")
    try:
        # In name order, as Go's ReadDir gives them, so the same 500 are kept; shown case-insensitively.
        entries = sorted(os.scandir(folder), key=lambda entry: entry.name)
    except OSError as error:
        raise Refused(f"couldn't read {folder}: {error}")
    listed: List[Dict[str, str]] = []
    truncated = False
    for entry in entries:
        if entry.name.startswith("."):
            continue
        path = os.path.join(folder, entry.name)
        try:
            if entry.is_symlink():
                # A link counts as a folder only when it leads to one inside the roots.
                if not os.path.isdir(path) or not machine.within(path)[1]:
                    continue
            elif not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        if len(listed) == FOLDERS_LIMIT:
            truncated = True
            break
        listed.append({"name": entry.name, "path": path})
    listed.sort(key=lambda item: item["name"].lower())
    # Up goes no further than a root.
    parent = None
    is_root = any(settings.fold(os.path.normpath(root)) == settings.fold(os.path.normpath(folder)) for root in machine.roots)
    up = os.path.dirname(folder)
    if not is_root and up != folder and machine.within(up)[1]:
        parent = up
    return listed, parent, truncated


def agent_proxy_server() -> str:
    """The server this computer's agent-proxy carries traffic to, when it runs."""
    port = os.environ.get("AI_PROXY_LOCAL_PORT") or "8184"
    running = common.health(f"http://127.0.0.1:{port}", timeout=2)
    return str(running.get("server") or "") if running and running.get("role") == "agent-proxy" else ""


def detach_flags() -> Dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"start_new_session": True}


def launch_headless(request: Dict[str, Any], server: str) -> str:
    """Runs `agent ... --headless` detached, and reads the session it opens from its first line of
    output. The agent goes through this computer's agent-proxy, so that has to be the same server."""
    machine = settings.Machine.load()
    project = check(request, machine)
    return run_headless(machine, project, arguments(request, project), server)


def shell_headless(request: Dict[str, Any], server: str) -> str:
    """Runs `agent --shell <folder> --headless`: the user's shell in a terminal only the machine's owner sees."""
    machine = settings.Machine.load()
    project = check_shell(request, machine)
    return run_headless(machine, project, shell_arguments(request, project), server)


def run_headless(machine: settings.Machine, project: str, args: List[str], server: str) -> str:
    running = agent_proxy_server()
    if running and common.normalize(running) != common.normalize(server):
        raise Refused(f"the agent-proxy here carries traffic to {running}, not {server}; sign the agent command in there (agent login {server})")
    # AI_PROXY_URL points a newly started agent-proxy at this machine's server; the machine variables tag the session.
    env = {**os.environ, "AI_PROXY_URL": server, "AI_PROXY_MACHINE_ID": machine.id, "AI_PROXY_MACHINE_NAME": machine.display_name()}
    try:
        process = subprocess.Popen([*agent_command(), *args], cwd=project, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, **detach_flags())
    except OSError as error:
        raise Refused(f"couldn't run the agent command: {error}")
    errors = bytearray()
    first: List[bytes] = []
    got_line = threading.Event()

    def read_stdout() -> None:
        assert process.stdout is not None
        first.append(process.stdout.readline())
        got_line.set()
        # Keep draining, so the agent never blocks on a full pipe.
        for _ in process.stdout:
            pass

    def read_stderr() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            if len(errors) < 4096:
                errors.extend(line[:4096 - len(errors)])

    threading.Thread(target=read_stdout, daemon=True).start()
    threading.Thread(target=read_stderr, daemon=True).start()
    if not got_line.wait(LAUNCH_TIMEOUT):
        process.kill()
        raise Refused("the agent didn't open a session in time")
    try:
        session_id = str(json.loads(first[0] or b"{}").get("session_id") or "")
    except ValueError:
        session_id = ""
    if session_id:
        return session_id
    try:
        process.wait(5)
    except subprocess.TimeoutExpired:
        pass
    raise Refused(f"the agent didn't start: {errors.decode('utf-8', 'replace').strip()}")


class Presence:
    """Keeps this machine connected to the server's fleet while `agent-machine run` runs."""

    def __init__(self, upstream: common.Upstream, on_status: Optional[Callable[[str], None]] = None,
                 on_server_version: Optional[Callable[[str], None]] = None) -> None:
        self.upstream = upstream
        self.on_status = on_status
        self.on_server_version = on_server_version
        self.providers = providers.Manager()
        self.servers = servers.Manager()
        # Runs the server's containers here, when the machine allows it.
        self.containers = containers.Manager()
        self.detect: Callable[[], List[Dict[str, Any]]] = providers.detect
        self.launch: Callable[[Dict[str, Any], str], str] = launch_headless
        self.shell: Callable[[Dict[str, Any], str], str] = shell_headless
        # The ports tunnels may reach: the model servers in the last hello or update. Nothing else here is ever reached.
        self.tunnel_ports: set = set()
        # Shares the MCP servers in mcp.json (agent-machine mcp add) through the tunnel.
        self.mcp = mcp.Bridge()
        self.status = ""

    def set_status(self, status: str) -> None:
        self.status = status
        if self.on_status:
            self.on_status(status)

    async def dial(self, server: str, path: str):
        headers = common.identify({}, settings.PROGRAM)
        token = await asyncio.to_thread(self.upstream.token, server)
        if token:
            headers[common.TOKEN_HEADER] = token
        return await common.signed_connect(common.websocket_url(server, path), additional_headers=headers, proxy=None, open_timeout=10,
                             max_size=None, compression=None, user_agent_header=None, ping_interval=None)

    async def run(self, stop: asyncio.Event) -> None:
        """Connects and reconnects until stop is set; the MCP sessions it runs end with it."""
        try:
            await self.connect(stop)
        finally:
            self.mcp.close()

    async def connect(self, stop: asyncio.Event) -> None:
        attempt = 0
        while not stop.is_set():
            loop = asyncio.get_running_loop()
            started = loop.time()
            session = asyncio.ensure_future(self.session())
            stopping = asyncio.ensure_future(stop.wait())
            await asyncio.wait([session, stopping], return_when=asyncio.FIRST_COMPLETED)
            if stop.is_set():
                session.cancel()
                try:
                    await session
                except (asyncio.CancelledError, Exception):
                    pass
                return
            stopping.cancel()
            error = session.exception() or "the connection ended"
            if loop.time() - started > 60:
                attempt = 0
            self.set_status(f"disconnected: {error}")
            log.info("fleet: %s", error)
            try:
                await asyncio.wait_for(stop.wait(), RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
            except asyncio.TimeoutError:
                pass
            attempt += 1

    async def session(self) -> None:
        server = self.upstream.server()
        try:
            conn = await self.dial(server, "/api/machines/connect")
        except websockets.InvalidStatus as error:
            status = error.response.status_code
            if status == 426:
                raise Refused(f"the agent-server at {server} can't take this agent-machine: {common.detail(error.response.body or b'')}")
            raise Refused(f"the agent-server at {server} answered {status} (run `agent-machine login {server}`)")
        async with conn:
            lock = asyncio.Lock()

            async def send(value: Dict[str, Any]) -> None:
                async with lock:
                    await asyncio.wait_for(conn.send(json.dumps(value)), 15)

            await send(await asyncio.to_thread(self.describe, server, "hello"))
            try:
                welcome = json.loads(await asyncio.wait_for(conn.recv(), 20))
            except websockets.ConnectionClosed as closed:
                if closed.rcvd and closed.rcvd.code == 4410:
                    await asyncio.to_thread(self.upstream.reject_access_token, server)
                reason = closed.rcvd.reason if closed.rcvd else ""
                if closed.rcvd and closed.rcvd.code == CLOSE_UPGRADE_REQUIRED:
                    raise Refused(f"the agent-server at {server} can't take this agent-machine: {reason}")
                raise Refused(f"the agent-server refused this machine: {reason}")
            if not isinstance(welcome, dict) or welcome.get("type") != "welcome":
                raise Refused("unexpected answer from the agent-server")
            self.set_status(f"connected to {server}")
            log.info("fleet: connected to %s", server)
            await asyncio.to_thread(self.check_version, server)
            loop = asyncio.get_running_loop()

            def send_from_thread(value: Dict[str, Any]) -> None:
                asyncio.run_coroutine_threadsafe(send(value), loop).result(30)

            async def keep_up() -> None:
                since_update = 0
                while True:
                    await asyncio.sleep(PING_EVERY)
                    since_update += PING_EVERY
                    await send({"type": "ping"})
                    if since_update >= UPDATE_EVERY:
                        since_update = 0
                        await send(await asyncio.to_thread(self.describe, server, "update"))

            tasks = {asyncio.ensure_future(keep_up())}
            try:
                async for raw in conn:
                    try:
                        message = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(message, dict):
                        task = asyncio.ensure_future(self.dispatch(message, server, send, send_from_thread))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
            except websockets.ConnectionClosed as closed:
                if closed.rcvd and closed.rcvd.code == 4410:
                    await asyncio.to_thread(self.upstream.reject_access_token, server)
                raise
            finally:
                for task in tasks:
                    task.cancel()
            raise Refused(f"the agent-server closed the connection ({conn.close_code})")

    async def dispatch(self, message: Dict[str, Any], server: str, send, send_from_thread) -> None:
        kind = message.get("type")
        try:
            if kind == "provider":
                # A pull streams its progress until the model is in, which can take an hour.
                def progress(update: Dict[str, Any]) -> None:
                    send_from_thread({"type": "provider_progress", "request": message.get("request"), "progress": update})

                await send(await asyncio.to_thread(self.providers.handle, message, progress))
            elif kind == "tunnel":
                await self.tunnel(server, message)
            elif kind == "container":
                # Starts answer at once (the job runs on); removes and logs can take a while.
                await send(await asyncio.to_thread(self.containers.handle, message))
            elif kind == "server":
                await send(await asyncio.to_thread(self.servers.handle, message))
                # A model server started or stopped: the server sees it in the providers at once.
                if message.get("op") != "status":
                    await send(await asyncio.to_thread(self.describe, server, "update"))
            elif kind in ("launch", "shell"):
                await self.launched(message, server, send)
            elif kind == "update":
                await send(await asyncio.to_thread(self.remote_update, server, message))
            elif kind == "folders":
                await send(await asyncio.to_thread(folders, message, settings.Machine.load()))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.warning("fleet: %s request failed: %s", kind, error)

    def remote_update(self, server: str, request: Dict[str, Any]) -> Dict[str, Any]:
        """An update the server asked for, as the update_result to send back: only while the machine allows it
        (agent-machine updates on), only the server's own build. The work is `agent-machine update` run
        detached, so it can stop this process and start on the new code; its output goes to the log."""
        from .. import update as updates

        answer: Dict[str, Any] = {"type": "update_result", "request": request.get("request")}
        if not settings.Machine.load().updates:
            answer.update(ok=False, error="this machine doesn't take updates from the server (agent-machine updates on)")
            return answer
        try:
            index = updates.fetch(server, settings.PROGRAM)
        except updates.UpdateError as error:
            answer.update(ok=False, error=str(error))
            return answer
        before = updates.current()
        answer.update(ok=True, to=index.get("version"))
        answer["from"] = before
        if index.get("version") == before and not request.get("agent") and not updates.checkout():
            answer.update(updating=False, message="agent-machine is already the server's build")
            return answer
        args = ["update", "--server", server] + (["--with-agent"] if request.get("agent") else [])
        try:
            self.spawn_update(args)
        except OSError as error:
            answer.update(ok=False, error=f"couldn't start the update: {error}")
            return answer
        log.info("fleet: updating to %s, as the server asked (agent too: %s)", index.get("version"), bool(request.get("agent")))
        answer["updating"] = True
        return answer

    @staticmethod
    def spawn_update(args: List[str]) -> None:
        """`agent-machine <args>` with this Python, detached so it outlives this process, from the home folder
        (so a checkout there can't stand in for the installed package), writing to the log."""
        settings.home().mkdir(parents=True, exist_ok=True)
        with open(settings.log_path(), "ab") as output:
            subprocess.Popen([sys.executable, "-m", "agent_py.machine", *args], cwd=str(Path.home()), stdin=subprocess.DEVNULL,
                             stdout=output, stderr=output, close_fds=True, **detach_flags())

    async def launched(self, request: Dict[str, Any], server: str, send) -> None:
        shell = request.get("type") == "shell"
        run, what = (self.shell, "a shell") if shell else (self.launch, request.get("agent"))
        try:
            session_id = await asyncio.to_thread(run, request, server)
        except Exception as error:
            log.info("fleet: launch %s of %s in %s failed: %s", request.get("launch"), what, request.get("project"), error)
            await send({"type": "launch_failed", "launch": request.get("launch"), "error": str(error)})
            return
        log.info("fleet: launched %s in %s: session %s", what, request.get("project"), session_id)
        await send({"type": "launched", "launch": request.get("launch"), "session_id": session_id})
        # What's installed may have changed the server's view; send it again soon.
        await send(await asyncio.to_thread(self.describe, server, "update"))

    async def tunnel(self, server: str, request: Dict[str, Any]) -> None:
        """Carries one connection from the server to a model server here, for agents' requests the
        server can't send over the network: the machine dials a WebSocket back for it and passes its
        bytes to and from 127.0.0.1:<port>, so a machine behind NAT or in WSL serves without a port forward."""
        port, tunnel_id = request.get("port"), str(request.get("tunnel") or "")
        # The containers the server has asked about serve here too.
        if (port not in self.tunnel_ports and port not in self.containers.ports()) or not tunnel_id:
            log.info("fleet: tunnel to port %s refused: not one of this machine's model servers", port)
            return
        async with await self.dial(server, f"/api/machines/tunnel/{tunnel_id}") as conn:
            try:
                reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 10)
            except (OSError, asyncio.TimeoutError):
                await conn.close(4502, "the model server isn't answering")
                return

            async def to_model_server() -> None:
                async for message in conn:
                    if isinstance(message, bytes):
                        writer.write(message)
                        await writer.drain()

            async def to_server() -> None:
                while True:
                    data = await reader.read(64 * 1024)
                    if not data:
                        await conn.close()
                        return
                    await conn.send(data)

            tasks = [asyncio.ensure_future(to_model_server()), asyncio.ensure_future(to_server())]
            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                for task in done:
                    task.exception()
            finally:
                writer.close()

    def check_version(self, server: str) -> None:
        """Logs, once per connection, when the server runs another version; keeps it for the status display."""
        theirs = str((common.health(server, timeout=5) or {}).get("version") or "")
        if self.on_server_version:
            self.on_server_version(theirs)
        notice = version_notice(common.VERSION, theirs)
        if notice:
            log.info("fleet: %s", notice)

    def describe(self, server: str, kind: str) -> Dict[str, Any]:
        """The machine's hello or update: what it is, has and allows."""
        machine = settings.Machine.load()
        serving = self.servers.describe()
        found = providers.with_keys(providers.with_strata(self.detect(), serving))
        # The MCP servers it shares, by name; the server reaches them on the bridge's port, through the tunnel.
        shared = self.mcp.describe()
        self.tunnel_ports = {provider["port"] for provider in found} | ({shared["port"]} if shared else set())
        return {
            "type": kind, "id": machine.id, "name": machine.display_name(), "hostname": socket.gethostname(),
            "os": common.goos(), "arch": common.goarch(), "version": common.VERSION, "protocol": common.PROTOCOL,
            "address": machine.address, "agents": self.installed_agents(server), "providers": found,
            "launches": machine.launches, "roots": machine.roots, "commands": machine.commands, "updates": machine.updates,
            # It carries agents' requests to its model servers itself, so the server needn't reach them.
            "tunnel": 1, "launcher": True,
            "containers": self.containers.describe(),
            "servers": serving, "mcp": shared,
        }

    def installed_agents(self, server: str) -> List[str]:
        """Which of the server's agents have their executable on this machine's PATH."""
        headers = {}
        token = self.upstream.token(server)
        if token:
            headers[common.TOKEN_HEADER] = token
        try:
            with common.client(10, settings.PROGRAM) as http:
                response = http.get(server + "/api/agents", headers=headers)
            listed = response.json() if response.status_code == 200 else []
        except (httpx.HTTPError, ValueError):
            return []
        return [str(agent["id"]) for agent in listed if isinstance(agent, dict) and agent.get("id") and common.look_path(str(agent.get("executable") or ""))]


def version_notice(own: str, server: str) -> str:
    """The line to show when the server's version isn't this agent-machine's; a build without a version says nothing."""
    if not own or own == "dev" or not server or server == own:
        return ""
    return f"agent-server is {server}; this agent-machine is {own}. Update with: agent-machine update"
