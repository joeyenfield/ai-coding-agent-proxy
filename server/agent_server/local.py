"""`agent-server local`: a whole server on this computer, for one person, with one command.

It runs the API and agent-auth as two background processes on loopback, with SQLite and
everything else in a per-user folder (`instance_home`). On first start it makes the secrets they
share (the introspection secret, a vault key) into `secrets.json` in that folder, owner-only, and
hands them to the two processes through their environment, never their command lines. The API
starts first (it creates the database and the first account, `admin`), then the issuer, which
reads accounts from that database. `stop` stops only the processes this command started, checked
by process id, start time and command line, never by name or by port.

Inherited AI_PROXY_* variables are left out of the children, so a shell set up for another
server (a test, a hosted install) can't point the local instance at it.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener

from .provider_accounts import new_key

# The quickstart's ports, so the same addresses work; --port and --auth-port pick others.
DEFAULT_PORT = 19999
DEFAULT_AUTH_PORT = 20000
HOST = "127.0.0.1"
# The one name used for OIDC, callbacks and cookies; it resolves to loopback everywhere.
HOSTNAME = "localhost"
FOLDER_NAME = "AI Control Mesh"
# How long a start waits for each process to answer (the API may rebuild the console first).
READY_SECONDS = 180.0
STOP_SECONDS = 15.0
# Marks the children's command lines, so stop can tell them from any other Python.
MARK = "agent-server-local"
PROGRAMS = ("server", "auth")
ENTRY = {
    "server": "import sys; from agent_server.app import main; sys.argv[0] = 'agent-server'; main()",
    "auth": "import sys; from agent_server.issuer import main; sys.argv[0] = 'agent-auth'; main()",
}


def instance_home(environ: dict[str, str] | None = None, platform: str | None = None) -> Path:
    """The local instance's folder: AI_PROXY_LOCAL_HOME, else the platform's per-user data folder."""
    environ = os.environ if environ is None else environ
    platform = platform or sys.platform
    configured = environ.get("AI_PROXY_LOCAL_HOME")
    if configured:
        return Path(configured).expanduser()
    home = Path(environ.get("HOME") or environ.get("USERPROFILE") or Path.home())
    if platform == "win32":
        base = environ.get("LOCALAPPDATA")
        return (Path(base) if base else home / "AppData" / "Local") / FOLDER_NAME
    if platform == "darwin":
        return home / "Library" / "Application Support" / FOLDER_NAME
    data = environ.get("XDG_DATA_HOME")
    return (Path(data) if data else home / ".local" / "share") / "ai-control-mesh"


@dataclass
class Process:
    pid: int
    # psutil's create_time, so a reused process id is never taken for ours.
    started: float


@dataclass
class Instance:
    """The instance's folder and what's kept in it: ports in local.json, secrets in secrets.json."""

    home: Path
    port: int = DEFAULT_PORT
    auth_port: int = DEFAULT_AUTH_PORT
    processes: dict[str, Process] = field(default_factory=dict)

    @property
    def url(self) -> str:
        return f"http://{HOSTNAME}:{self.port}"

    @property
    def issuer(self) -> str:
        return f"http://{HOSTNAME}:{self.auth_port}"

    @property
    def state_path(self) -> Path:
        return self.home / "local.json"

    @property
    def secrets_path(self) -> Path:
        return self.home / "secrets.json"

    @property
    def password_path(self) -> Path:
        # The server writes first-user-password beside its remote key file (Settings.users_path).
        return self.home / "private" / "first-user-password"

    def log(self, program: str) -> Path:
        return self.home / "logs" / f"{program}.out.log"

    @classmethod
    def load(cls, home: Path) -> "Instance":
        instance = cls(home)
        try:
            data = json.loads(instance.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return instance
        instance.port = int(data.get("port") or DEFAULT_PORT)
        instance.auth_port = int(data.get("auth_port") or DEFAULT_AUTH_PORT)
        for name, item in (data.get("processes") or {}).items():
            if name in PROGRAMS and isinstance(item, dict):
                instance.processes[name] = Process(int(item["pid"]), float(item["started"]))
        return instance

    def save(self) -> None:
        data = {"port": self.port, "auth_port": self.auth_port, "processes": {name: asdict(item) for name, item in self.processes.items()}}
        _write_private(self.state_path, json.dumps(data, indent=2))

    def secrets(self) -> dict[str, str]:
        """The instance's secrets, made once and kept: changing them would sign everyone out and lose stored keys."""
        try:
            stored = json.loads(self.secrets_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = {}
        made = {"introspection_secret": secrets.token_urlsafe(32), "vault_keys": new_key(1)}
        if all(stored.get(key) for key in made):
            return stored
        stored = {**made, **{key: value for key, value in stored.items() if value}}
        _write_private(self.secrets_path, json.dumps(stored, indent=2))
        return stored

    def environment(self, base: dict[str, str] | None = None) -> dict[str, str]:
        """The children's environment: this one without any AI_PROXY_* variable, then the instance's own."""
        base = os.environ if base is None else base
        keep = {key: value for key, value in base.items() if not key.upper().startswith("AI_PROXY_")}
        stored = self.secrets()
        return {
            **keep,
            "AI_PROXY_HOST": HOST,
            "AI_PROXY_PORT": str(self.port),
            "AI_PROXY_URL": self.url,
            "AI_PROXY_LOG_DIR": str(self.home / "logs"),
            "AI_PROXY_REMOTE_KEY_FILE": str(self.home / "private" / "remote-access-key"),
            "AI_PROXY_CA_DIR": str(self.home / "logs" / "ca"),
            "AI_PROXY_OIDC_STATE_DIR": str(self.home / "oidc"),
            "AI_PROXY_OIDC_ISSUER": self.issuer,
            "AI_PROXY_OIDC_AUDIENCE": self.url,
            "AI_PROXY_OIDC_REDIRECT_URIS": f"{self.url}/api/auth/callback",
            "AI_PROXY_OIDC_BROWSER_ORIGINS": self.url,
            "AI_PROXY_OIDC_INTROSPECTION_SECRET": stored["introspection_secret"],
            "AI_PROXY_VAULT_KEYS": stored["vault_keys"],
            "AI_PROXY_AUTH_HOST": HOST,
            "AI_PROXY_AUTH_PORT": str(self.auth_port),
            "PYTHONUNBUFFERED": "1",
        }


def _write_private(path: Path, text: str) -> None:
    """Write a file only its owner can read (on Windows the profile's ACLs apply), atomically."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def running(process: Process | None) -> bool:
    """Whether this is still the process we started: same id, same start time, our mark on its command line."""
    if process is None:
        return False
    import psutil

    try:
        found = psutil.Process(process.pid)
        if abs(found.create_time() - process.started) > 1.0:
            return False
        return any(MARK in part for part in found.cmdline())
    except (psutil.Error, OSError):
        return False


def port_free(port: int, host: str = HOST) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # As uvicorn binds: on Linux and macOS a just-stopped server's TIME_WAIT connections would
        # otherwise fail the probe for a minute. Not on Windows, where SO_REUSEADDR could bind over
        # a program that's listening (and TIME_WAIT never blocks a bind).
        if sys.platform != "win32":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def spawn(program: str, instance: Instance, environment: dict[str, str]) -> Process:
    """Start one program in the background, its output to logs/<program>.out.log."""
    import psutil

    log = instance.log(program)
    log.parent.mkdir(parents=True, exist_ok=True)
    # The mark goes after the code, where the program never reads it (sys.argv is reset).
    command = [sys.executable, "-c", f"{ENTRY[program]}  # {MARK}"]
    options: dict[str, Any] = {}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    with open(log, "ab") as output:
        child = subprocess.Popen(command, env=environment, cwd=str(instance.home), stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=subprocess.STDOUT, **options)
    return Process(child.pid, psutil.Process(child.pid).create_time())


def _get(url: str) -> dict[str, Any] | None:
    try:
        with build_opener(ProxyHandler({})).open(url, timeout=3) as response:
            return json.loads(response.read().decode())
    except (HTTPError, URLError, OSError, ValueError):
        return None


def wait_for(check: Callable[[], bool], process: Process, seconds: float = READY_SECONDS) -> bool:
    """Wait until check passes; give up early when the process has gone."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return True
        if not running(process):
            return False
        time.sleep(0.5)
    return False


def server_ready(instance: Instance) -> bool:
    status = _get(f"http://{HOST}:{instance.port}/api/auth/status")
    return bool(status and status.get("issuer") == instance.issuer and status.get("audience") == instance.url)


def auth_ready(instance: Instance) -> bool:
    discovery = _get(f"http://{HOST}:{instance.auth_port}/.well-known/openid-configuration")
    return bool(discovery and discovery.get("issuer") == instance.issuer)


def stop_process(process: Process) -> None:
    """Stop one of our processes and its children: politely, then by force after STOP_SECONDS."""
    import psutil

    if not running(process):
        return
    try:
        parent = psutil.Process(process.pid)
        family = [parent, *parent.children(recursive=True)]
    except psutil.Error:
        return
    for item in family:
        try:
            item.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(family, timeout=STOP_SECONDS)
    for item in alive:
        try:
            item.kill()
        except psutil.Error:
            pass


def start(instance: Instance, out: Any = sys.stdout, overrides: dict[str, str] | None = None) -> int:
    """Start the two processes. overrides go into their environment last: the all-in-one image
    (devhelpers/allinone/entrypoint.py) listens on every address of its container, and may name the first password."""
    if all(running(instance.processes.get(name)) for name in PROGRAMS):
        print(f"The local instance is already running at {instance.url}", file=out)
        _next_steps(instance, out)
        return 0
    # Half running: stop what's left, so the two start in order with the same settings.
    stop(instance, quiet=True)
    for port in (instance.port, instance.auth_port):
        if not port_free(port):
            print(f"Port {port} on {HOST} is in use by another program. Stop it, or pick another port with "
                  f"--port/--auth-port (the choice is kept).", file=sys.stderr)
            return 1
    instance.home.mkdir(parents=True, exist_ok=True)
    environment = {**instance.environment(), **(overrides or {})}
    instance.save()
    # The API first: it creates the database and the first account, which the issuer reads.
    for program, ready, label in (("server", server_ready, "agent-server"), ("auth", auth_ready, "agent-auth")):
        print(f"Starting {label}...", file=out, flush=True)
        instance.processes[program] = spawn(program, instance, environment)
        instance.save()
        if not wait_for(lambda: ready(instance), instance.processes[program]):
            print(f"{label} didn't start. See {instance.log(program)}", file=sys.stderr)
            stop(instance, quiet=True)
            return 1
    print(f"The local instance is running at {instance.url}", file=out)
    _next_steps(instance, out)
    return 0


def _next_steps(instance: Instance, out: Any) -> None:
    lines = [f"  Console: {instance.url}"]
    try:
        password = instance.password_path.read_text(encoding="utf-8").strip()
        lines.append(f"  Sign in as admin with the password {password} (kept in {instance.password_path} until you delete it).")
    except OSError:
        lines.append("  Sign in with your account.")
    lines += [
        "  Then, on this computer:",
        f"    agent login {instance.url}",
        f"    agent-machine login {instance.url} --enable launches,files",
        "    agent-machine roots add <a folder with your projects>",
        "    agent-machine start",
        "  Stop it with: agent-server local stop",
    ]
    print("\n".join(lines), file=out)


def stop(instance: Instance, quiet: bool = False, out: Any = sys.stdout) -> int:
    # The issuer first, then the API, the reverse of the start.
    stopped = []
    for program in reversed(PROGRAMS):
        process = instance.processes.pop(program, None)
        if running(process):
            stop_process(process)
            stopped.append(program)
    if instance.state_path.parent.is_dir():
        instance.save()
    if not quiet:
        print("Stopped the local instance." if stopped else "The local instance isn't running.", file=out)
    return 0


def status_json(instance: Instance) -> dict[str, Any]:
    """`agent-server local status --json`, what agent-tray shows: the addresses, whether each process
    runs, and the console's public address when the server has one (proxy.public_url)."""
    processes = {program: running(instance.processes.get(program)) for program in PROGRAMS}
    public = None
    if processes["server"]:
        answer = _get(f"http://{HOST}:{instance.port}/api/auth/status") or {}
        public = answer.get("public_url") or None
    return {
        "home": str(instance.home), "url": instance.url, "issuer": instance.issuer, "port": instance.port,
        "auth_port": instance.auth_port, "installed": instance.state_path.is_file(),
        "running": all(processes.values()), "processes": processes, "public_url": public,
    }


def status(instance: Instance, out: Any = sys.stdout) -> int:
    print(f"Folder : {instance.home}", file=out)
    print(f"Console: {instance.url}", file=out)
    for program, label in (("server", "agent-server"), ("auth", "agent-auth")):
        process = instance.processes.get(program)
        state = f"running (pid {process.pid})" if process and running(process) else "stopped"
        print(f"{label + ':':13} {state}", file=out)
    return 0 if all(running(instance.processes.get(name)) for name in PROGRAMS) else 3


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-server local",
        description="Run a whole server on this computer for one person: the API and agent-auth on loopback, "
                    "with everything in one folder (AI_PROXY_LOCAL_HOME, else your user's data folder).",
    )
    parser.add_argument("action", nargs="?", default="start", choices=["start", "stop", "status"])
    parser.add_argument("--port", type=int, help=f"The console and API's port (default {DEFAULT_PORT}; kept for next time)")
    parser.add_argument("--auth-port", type=int, help=f"agent-auth's port (default {DEFAULT_AUTH_PORT}; kept for next time)")
    parser.add_argument("--json", action="store_true", help="With status: the same as JSON (for agent-tray)")
    args = parser.parse_args(argv)
    instance = Instance.load(instance_home())
    if args.action == "stop":
        return stop(instance)
    if args.action == "status":
        if args.json:
            print(json.dumps(status_json(instance)))
            return 0
        return status(instance)
    if args.port or args.auth_port:
        if any(running(item) for item in instance.processes.values()):
            print("Stop the local instance before changing its ports.", file=sys.stderr)
            return 1
        instance.port = args.port or instance.port
        instance.auth_port = args.auth_port or instance.auth_port
    if instance.port == instance.auth_port:
        print("--port and --auth-port must differ.", file=sys.stderr)
        return 2
    return start(instance)
