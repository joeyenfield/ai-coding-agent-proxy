"""`agent`: start a coding agent on this machine, registered with this machine's agent-proxy.

The launcher makes sure the agent-proxy runs (and, when this computer is the server too, the
agent-server), asks the server for a session and the agent's environment, and runs the agent
so every request goes to the agent-proxy, which carries it to the agent-server. The server
owns the agent definitions and renders them; the launcher only knows how to run what it's
given. With --control the agent runs in a terminal shared with the server.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse

import httpx

from .. import common
from . import settings, terminal

USAGE = """Usage: agent <agent> [project] [options] [-- agent arguments]
       agent --list
       agent login [url] [--user name]    sign this machine in to an agent-server
       agent logout [url]
       agent proxy [--server url]         run this machine's agent-proxy in the foreground
       agent proxy stop                   stop the agent-proxy running here (agent starts it again)
       agent version

Launch a coding agent through this machine's agent-proxy (the Python version of the agent
command). To put this machine in the server's fleet, use agent-machine.

Options:
  -m, --model NAME        Model name or alias from the server's models.yaml (optional for account agents)
      --backend NAME      One of the server's backends (default: the one the server picks for the agent)
      --trace             Capture full request and response payloads
      --list              List the server's agents and whether they're installed here
      --control           Share the agent's terminal with the server, to watch and type into it from
                          the console or a phone (or set AI_PROXY_CONTROL=1)
      --switch            If the backend is a container a machine runs, stop other containers using
                          its GPU to start it

Sharing, on a server with accounts:
      --share WHO         private, team or org: who else can see this session (default: private)
      --team NAME         Which of your teams (org/team) with --share team, or which of your
                          organizations with --share org (needed if you're in several)
      --let-others-type   With --control: people it's shared with can type into its terminal and end
                          it, not only watch

Unknown options, and everything after --, are passed to the agent.
"""

VALUED = {"--model", "-m", "--backend", "--share", "--team", "--proxy", "--launch-id"}


class UsageError(Exception):
    pass


class LaunchError(Exception):
    pass


@dataclass
class Options:
    agent: str = ""
    project: str = "."
    model: str = ""
    backend: str = ""
    trace: bool = False
    list: bool = False
    control: bool = False
    switch: bool = False
    share: str = "private"
    team: str = ""
    let_others_type: bool = False
    help: bool = False
    # A launch from the console, run by agent-machine: no local window, its terminal shared with the server.
    headless: bool = False
    launch_id: str = ""
    extra: List[str] = field(default_factory=lambda: [])
    proxy: str = ""

    def sharing(self) -> Dict[str, Any]:
        sharing: Dict[str, Any] = {"visibility": self.share, "access": "control" if self.let_others_type else "view"}
        if self.team:
            sharing["team"] = self.team
        return sharing


def parse_args(args: List[str]) -> Options:
    """Options anywhere, the first two plain arguments as the agent and project, everything else for the agent."""
    options = Options()
    positional = 0
    index = 0
    while index < len(args):
        arg = args[index]
        index += 1
        if arg == "--":
            options.extra.extend(args[index:])
            break
        name, has_value, value = arg.partition("=")
        if not arg.startswith("-") or arg == "-":
            if positional == 0:
                options.agent = arg
            elif positional == 1:
                options.project = arg
            else:
                options.extra.append(arg)
            positional += 1
            continue
        if name in VALUED:
            if not has_value:
                if index >= len(args):
                    raise UsageError(f"{name} needs a value")
                value = args[index]
                index += 1
            if name in ("--model", "-m"):
                options.model = value
            elif name == "--backend":
                options.backend = value
            elif name == "--share":
                if value not in ("private", "team", "org"):
                    raise UsageError("--share must be private, team or org")
                options.share = value
            elif name == "--team":
                options.team = value
            elif name == "--proxy":
                options.proxy = value
            elif name == "--launch-id":
                options.launch_id = value
            continue
        flags = {"--trace": "trace", "--list": "list", "--control": "control", "--switch": "switch",
                 "--let-others-type": "let_others_type", "--headless": "headless", "-h": "help", "--help": "help"}
        if arg in flags:
            setattr(options, flags[arg], True)
        elif arg == "--no-remote":
            pass  # Old flag, kept so it isn't passed on to the agent.
        else:
            options.extra.append(arg)
    return options


class API:
    """The launcher's JSON calls to the server, through this machine's agent-proxy (which signs them)."""

    def __init__(self, base: str, timeout: float = 30) -> None:
        self.base = base
        self.http = common.client(timeout, settings.PROGRAM)

    def call(self, method: str, path: str, body: Any = None) -> Any:
        response = self.http.request(method, self.base + path, json=body) if body is not None else self.http.request(method, self.base + path)
        if response.status_code >= 400:
            raise APIError(response.status_code, common.detail(response.content))
        return response.json() if response.content else None

    def text(self, path: str) -> str:
        response = self.http.get(self.base + path)
        if response.status_code >= 400:
            raise APIError(response.status_code, common.detail(response.content))
        return response.text


class APIError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status


def self_command(*args: str) -> List[str]:
    """This program, as a command line: the same Python running the same package."""
    return [sys.executable, "-m", "agent_py.client", *args]


def detach_flags() -> Dict[str, Any]:
    """Popen arguments that start a program detached from this terminal, so it outlives it."""
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return {"creationflags": flags, "close_fds": True}
    return {"start_new_session": True, "close_fds": True}


def start(command: List[str], url: str, name: str, manual: str, timeout: float) -> None:
    """Runs command in the background, detached, and waits until url/health answers."""
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **detach_flags())
    except OSError as error:
        raise LaunchError(f"couldn't start {name}: {error}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LaunchError(f"{name} exited while starting. Run `{manual}` in a terminal to see why")
        if common.health(url) is not None:
            return
        time.sleep(0.2)
    process.kill()
    raise LaunchError(f"{name} didn't become ready at {url}")


def ensure_proxy() -> Dict[str, Any]:
    """Starts this machine's agent-proxy if it isn't running, and the agent-server too when it's this computer and down."""
    local = settings.proxy_url()
    status = common.health(local)
    if status is None:
        start(self_command("proxy", "--port", str(settings.proxy_port())), local, "agent-proxy", "agent proxy", 30)
        status = common.health(local)
        if status is None:
            raise LaunchError(f"agent-proxy didn't answer at {local}")
    if status.get("role") != "agent-proxy":
        raise LaunchError(f"something other than agent-proxy answers on {local}; set AI_PROXY_LOCAL_PORT to a free port")
    server = str(status.get("server") or "")
    if common.is_loopback_url(server) and common.health(server) is None:
        # One computer for everything: start the agent-server as well, if it's installed here.
        program = shutil.which("agent-server")
        if not program:
            raise LaunchError(f"the agent-server at {server} isn't running; start it (agent-server, or its Docker container), or sign in to another with `agent login <server>`")
        parsed = urlparse(server)
        port = str(parsed.port or (443 if parsed.scheme == "https" else 80))
        # Generous: a fresh agent-server rebuilds the web UI first when its sources changed.
        start([program, "--port", port], server, "agent-server", "agent-server", 120)
    for line in version_notices(status, common.health(server) or {}):
        print("agent:", line, file=sys.stderr)
    return status


def version_notices(proxy: Dict[str, Any], server: Dict[str, Any]) -> List[str]:
    own = common.VERSION
    if not own or own == "dev":
        return []
    lines = []
    running = proxy.get("version") or "an older version"
    if running != own:
        lines.append(f"the running agent-proxy is {running}; this agent is {own}. Run `agent proxy stop` so the next agent starts this one.")
    theirs = server.get("version")
    if theirs and theirs != own and not own.startswith(f"{theirs}+"):
        lines.append(f"agent-server is {theirs}; this agent is {own}.")
    return lines


def environment() -> str:
    """Which install command applies here: linux, macos, windows or wsl."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "linux" and "microsoft" in os.uname().release.lower():
        return "wsl"
    return "linux"


def install_command(agent: Dict[str, Any]) -> str:
    return str((agent.get("installs") or {}).get(environment()) or agent.get("install") or "")


def agents(api: API) -> List[Dict[str, Any]]:
    try:
        return api.call("GET", "/api/agents")
    except APIError as error:
        if error.status == 401:
            raise LaunchError("the agent-server needs this machine to sign in: run `agent login`")
        raise


def find_agent(api: API, agent_id: str) -> Dict[str, Any]:
    listed = agents(api)
    for agent in listed:
        if agent.get("id") == agent_id:
            return agent
    available = ", ".join(sorted(str(agent.get("id")) for agent in listed)) or "none"
    raise LaunchError(f"unknown agent '{agent_id}'. Available: {available}")


def check_signed_in(api: API, server: str) -> None:
    try:
        status = api.call("GET", "/api/auth/status")
    except (APIError, httpx.HTTPError) as error:
        raise LaunchError(f"couldn't reach the agent-server at {server}: {error}")
    if status.get("required") and status.get("identity") is None:
        raise LaunchError(f"{server} needs this machine to sign in: run `agent login {server}`")


def write_file(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8", newline="")
    os.replace(temporary, path)


def intercept_files(api: API) -> Dict[str, Any]:
    """The HTTPS proxy account agents use (this machine's agent-proxy) and local copies of the
    server's CA, kept per fingerprint so a server with a new CA gets a fresh copy."""
    info = api.call("GET", "/api/intercept")
    if not info.get("running"):
        raise LaunchError(f"the agent-server's HTTPS interception is off: {info.get('error') or 'unknown error'}")
    fingerprint = str(info.get("ca_fingerprint") or "").replace(":", "")[:16]
    directory = Path.home() / ".cache" / "agent-proxy" / fingerprint
    ca_path, bundle_path = directory / "ca.pem", directory / "bundle.pem"
    if not bundle_path.exists():
        directory.mkdir(parents=True, exist_ok=True)
        for path, source in ((ca_path, "/api/intercept/ca.pem"), (bundle_path, "/api/intercept/bundle.pem")):
            try:
                write_file(path, api.text(source))
            except (APIError, httpx.HTTPError) as error:
                raise LaunchError(f"couldn't fetch the server's CA ({source}): {error}")
    return {"url": info.get("url"), "ca_path": str(ca_path), "bundle_path": str(bundle_path)}


def write_files(directory: Path, files: Dict[str, str]) -> None:
    """The server's files, refusing any name that isn't a plain file name."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        if not name or name in (".", "..") or any(char in name for char in "/\\:") or Path(name).name != name:
            raise LaunchError(f"the server sent a file with an unsafe name: {name!r}")
        write_file(directory / name, text)


def merge_env(overrides: Dict[str, str]) -> Dict[str, str]:
    """This environment with overrides applied; names compare case-insensitively on Windows."""
    fold = str.upper if sys.platform == "win32" else (lambda name: name)
    replaced = {fold(name) for name in overrides}
    merged = {name: value for name, value in os.environ.items() if fold(name) not in replaced}
    merged.update(overrides)
    return merged


PHASES = {"pulling": "pulling its image", "starting": "starting the container", "loading": "loading the model (the first start also downloads it)", "ready": "ready"}


def ensure_container(proxy_url: str, name: str, switch_others: bool, wait: float = 4 * 3600) -> None:
    """Starts a backend's container if it isn't serving, and waits until it is."""
    api = API(proxy_url)
    path = "/api/containers/" + quote(name, safe="")
    try:
        item = api.call("GET", path)
    except APIError as error:
        if error.status == 401:
            raise LaunchError(f"the agent-server won't start {name} for this machine; sign it in with `agent login`, or start {name} on its Containers page")
        raise
    if item.get("ready"):
        return
    if item.get("phase") not in ("pulling", "starting", "loading"):
        try:
            api.call("POST", path + "/start", {"switch": switch_others})
        except APIError as error:
            if error.status == 409:
                raise LaunchError(f"{error} Run again with --switch, or switch on the Containers page")
            raise
    print(f"Starting {name} ({item.get('engine_label')} container). Ctrl+C stops waiting; the container keeps starting.")
    began = said = time.monotonic()
    last = ""
    while time.monotonic() - began < wait:
        item = api.call("GET", path)
        phase = str(item.get("phase") or "")
        if phase != last:
            print(f"  {name}: {PHASES.get(phase, phase)}")
            last, said = phase, time.monotonic()
        elif time.monotonic() - said > 60:
            print(f"  {name}: still {PHASES.get(phase, phase)} ({int((time.monotonic() - began) / 60)} min)")
            said = time.monotonic()
        if item.get("ready"):
            return
        job = item.get("job") or {}
        if phase in ("failed", "stopped", "unhealthy", "not_created") and job.get("state") != "running":
            reason = job.get("error") or (item.get("state") or {}).get("error") or phase
            raise LaunchError(f"{name} didn't start: {reason}. Its log is on the Containers page")
        time.sleep(2)
    raise LaunchError(f"{name} still isn't ready after {int(wait)}s")


def run_plain(command: List[str], cwd: str, env: Dict[str, str]) -> int:
    # Ctrl+C belongs to the agent; the launcher stays to close the session afterwards.
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        return subprocess.call(command, cwd=cwd, env=env)
    except OSError as error:
        print(f"agent: {error}", file=sys.stderr)
        return 127
    finally:
        signal.signal(signal.SIGINT, previous)


def share_for(local: str, server: str, session_id: str, agent: Dict[str, Any], wanted: bool):
    if not wanted:
        return None, "off (add --control to watch and type into this agent from the console)"
    reason = terminal.unavailable()
    if reason:
        return None, "off: " + reason
    problem = terminal.probe(local)
    if "agent login" in problem:
        return None, "off: " + problem
    share = terminal.Share(terminal.host_url(local, session_id), str(agent.get("executable") or ""))
    view = terminal.view_url(server, session_id)
    return share, f"{view} ({problem})" if problem else f"{view} (watch and type into this terminal from the console)"


def launch(options: Options) -> int:
    project = Path(options.project).resolve()
    if not project.is_dir():
        raise LaunchError(f"project directory does not exist: {project}")
    status = ensure_proxy()
    server = str(status.get("server") or "")
    local = settings.proxy_url()
    # Everything goes through this machine's agent-proxy, which signs it as this machine.
    api = API(local)
    check_signed_in(api, server)
    agent = find_agent(api, options.agent)
    executable = common.look_path(str(agent.get("executable") or ""))
    if not executable:
        install = install_command(agent)
        raise LaunchError(f"{agent.get('name')} ({agent.get('executable')}) is not on PATH." + (f" Install it with: {install}" if install else ""))
    if agent.get("needs_model") and not options.model:
        raise LaunchError(f"{agent.get('name')} needs --model")
    request: Dict[str, Any] = {
        "model": options.model or None, "backend": options.backend or None, "trace": options.trace,
        "project": project.name, "project_path": str(project), "proxy_url": local, "launcher": True, **options.sharing(),
    }
    # Where it runs, and for a launch from the console the fleet machine and the launch that started it.
    hostname = socket.gethostname()
    tags: Dict[str, Any] = {"machine_name": hostname, "hostname": hostname}
    if os.environ.get("AI_PROXY_MACHINE_ID"):
        tags["machine"] = os.environ["AI_PROXY_MACHINE_ID"]
    if os.environ.get("AI_PROXY_MACHINE_NAME"):
        tags["machine_name"] = os.environ["AI_PROXY_MACHINE_NAME"]
    if options.launch_id:
        tags["launch"] = options.launch_id
    request["tags"] = tags
    files_dir = Path.home() / ".cache" / "agent-proxy" / "sessions" / secrets.token_hex(8)
    request["files_dir"] = str(files_dir)
    intercept = None
    if agent.get("intercept"):
        intercept = intercept_files(api)
        request["intercept"] = intercept
    prepared = api.call("POST", f"/api/agents/{quote(str(agent['id']), safe='')}/sessions", request)
    session = prepared.get("session") or {}
    session_id = str(session.get("session_id") or "")

    def end(exit_status: int) -> None:
        try:
            api.call("PATCH", "/api/sessions/" + quote(session_id, safe=""), {"ended": True, "exit_status": exit_status})
        except (APIError, httpx.HTTPError) as error:
            print(f"Warning: could not close the session: {error}", file=sys.stderr)

    try:
        if prepared.get("files"):
            write_files(files_dir, prepared["files"])
        if prepared.get("container"):
            ensure_container(local, str(session.get("backend") or ""), options.switch)
        command = list(prepared.get("command") or [])
        if not command:
            raise LaunchError("the server sent no command for this agent")
    except (LaunchError, APIError, httpx.HTTPError):
        end(1)
        shutil.rmtree(files_dir, ignore_errors=True)
        raise
    # The resolved path, so npm .cmd shims work on Windows.
    command = [executable, *command[1:], *options.extra]
    env = merge_env({str(key): str(value) for key, value in (prepared.get("env") or {}).items()})
    try:
        if options.headless:
            return run_headless(local, session_id, agent, command, str(project), env, end)
        route = f"all HTTPS through {intercept['url']} (traffic goes to the agent's own service)" if intercept else session.get("backend")
        share, control = share_for(local, server, session_id, agent, options.control)
        print(f"Session : {session_id}\nAgent   : {agent.get('name')}\nModel   : {options.model or 'agent default'}\n"
              f"Backend : {route}\nServer  : {server}/live\nControl : {control}", flush=True)
        if share:
            try:
                exit_status = terminal.run(command, str(project), env, share)
            except OSError as error:
                print(f"agent: {error}", file=sys.stderr)
                exit_status = 127
            if share.problem:
                print(f"Control : {share.problem}", file=sys.stderr)
        else:
            exit_status = run_plain(command, str(project), env)
        end(exit_status)
        return exit_status
    finally:
        shutil.rmtree(files_dir, ignore_errors=True)


def run_headless(local: str, session_id: str, agent: Dict[str, Any], command: List[str], cwd: str, env: Dict[str, str], end) -> int:
    """A launch the server asked for: report the session on the one line of output (agent-machine
    reads it), then run the agent with only the server's viewers watching and typing."""
    problem = terminal.probe(local)
    if "agent login" in problem:
        print(problem, file=sys.stderr)
        end(1)
        return 1
    share = terminal.Share(terminal.host_url(local, session_id), str(agent.get("executable") or ""))
    print(json.dumps({"session_id": session_id}), flush=True)
    # Nothing else goes to standard output: agent-machine may stop reading it.
    try:
        sys.stdout = open(os.devnull, "w")
    except OSError:
        pass
    try:
        status = terminal.run_headless(command, cwd, env, share)
    except Exception as error:
        print(f"agent: {error}", file=sys.stderr)
        status = 127
    end(status)
    return status


def list_agents() -> None:
    """The server's agents, whether each is installed here, and the agent-proxy's state."""
    try:
        status = ensure_proxy()
    except LaunchError as error:
        print(f"agent-proxy: not running ({error})")
        return
    server = status.get("server")
    try:
        listed = agents(API(settings.proxy_url(), 15))
    except (LaunchError, APIError, httpx.HTTPError) as error:
        print(f"Couldn't list the agents of {server}: {error}")
    else:
        width = max([5, *(len(str(agent.get("id"))) for agent in listed)])
        for agent in listed:
            missing = common.look_path(str(agent.get("executable") or "")) is None
            route = "account, captured via HTTPS intercept" if agent.get("intercept") else agent.get("protocol")
            print(f"{str(agent.get('id')):<{width}}  {'missing  ' if missing else 'installed'}  {agent.get('name')} ({route})")
            install = install_command(agent)
            if missing and install:
                print(f"{'':<{width}}             install: {install}")
    who = f"signed in as {status['user']}" if status.get("user") else "no sign-in stored (agent login <server>)"
    print(f"\nagent-proxy: carrying traffic to {server}, {who}")


def main(args: List[str]) -> int:
    try:
        options = parse_args(args)
    except UsageError as error:
        print(f"agent: {error}\n\n{USAGE}", file=sys.stderr, end="")
        return 2
    if options.help:
        print(USAGE, end="")
        return 0
    if options.proxy:
        print("agent: --proxy is gone: sign this machine in to a server with `agent login <server>`, and its agent-proxy uses it", file=sys.stderr)
        return 2
    if options.list or not options.agent:
        list_agents()
        return 0 if options.list else 2
    options.control = options.control or options.headless or os.environ.get("AI_PROXY_CONTROL") == "1"
    if options.let_others_type and not options.control:
        print("agent: --let-others-type has no effect without --control", file=sys.stderr)
    try:
        return launch(options)
    except (LaunchError, APIError, httpx.HTTPError, OSError) as error:
        print(f"agent: {error}", file=sys.stderr)
        return 1
