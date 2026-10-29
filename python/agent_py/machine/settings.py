"""agent-machine's own state, in its own folder (home()): the server it signed in to
(credentials.json), the machine's settings (machine.json), its log and how its connection is
doing (status.json). The same files as the Go agent-machine's, so either can run on a machine.

It's separate from the agent command's sign-in on purpose: a machine can supply models to
one server, or as one account, while its agents use another.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from .. import common

PROGRAM = "agent-machine"


def home() -> Path:
    """AI_PROXY_MACHINE_HOME, or agent-machine in the user's config folder."""
    if os.environ.get("AI_PROXY_MACHINE_HOME"):
        return common.expand_home(os.environ["AI_PROXY_MACHINE_HOME"])
    return common.config_dir() / "agent-machine"


def credentials_path() -> Path:
    return home() / "credentials.json"


def log_path() -> Path:
    return home() / "agent-machine.log"


def status_path() -> Path:
    return home() / "status.json"


def machine_path() -> Path:
    return home() / "machine.json"


def upstream(fixed: str = "") -> common.Upstream:
    # Only its own sign-in: AI_PROXY_URL and AI_PROXY_TOKEN are the agent command's.
    return common.Upstream(credentials_path, fixed=fixed, use_env=False)


@dataclass
class Machine:
    """This machine as the server's fleet knows it: a stable id, and what the server may do here.
    Everything is off until the owner turns it on."""

    id: str = ""
    name: str = ""
    launches: bool = False
    roots: List[str] = field(default_factory=list)
    # How the server reaches this machine's model servers, when the address it sees isn't it.
    address: str = ""
    # Lets the server run model servers here in containers. `docker run` is root on this machine,
    # so it's off until the owner turns it on.
    containers: bool = False
    # "docker" or "podman"; empty (the default) uses whichever is installed and answering.
    container_cli: str = ""
    # Lets the server change the models of the model servers here (pull, delete, load, unload).
    servers: bool = False
    # Lets the owner open a shell here from the console, in one of the roots: a remote terminal, off until turned on.
    commands: bool = False
    # Lets the server update this package here to its own build, from the console: off until turned on.
    updates: bool = False

    @classmethod
    def load(cls) -> "Machine":
        path = machine_path()
        data = None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            if not os.environ.get("AI_PROXY_MACHINE_HOME"):
                # Carry the old file over once: the server knows the machine (and its backends) by its id.
                former = common.config_dir() / "agent-proxy" / "machine.json"
                try:
                    data = json.loads(former.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    data = None
        machine = cls()
        if isinstance(data, dict):
            for key in ("id", "name", "address", "container_cli"):
                setattr(machine, key, str(data.get(key) or ""))
            for key in ("launches", "containers", "servers", "commands", "updates"):
                setattr(machine, key, bool(data.get(key)))
            machine.roots = [str(root) for root in data.get("roots") or [] if isinstance(root, str)]
        if not machine.id:
            machine.id = secrets.token_hex(8)
            machine.save()
        elif data is not None and not path.exists():
            machine.save()
        return machine

    def save(self) -> None:
        value = asdict(self)
        for key in ("name", "address", "container_cli"):
            if not value[key]:
                value.pop(key)
        if not value["roots"]:
            value.pop("roots")
        common.write_json(machine_path(), value)

    def display_name(self) -> str:
        return self.name or socket.gethostname() or self.id

    def within(self, folder: str) -> Tuple[str, bool]:
        """Whether folder is one of the roots or inside one. Both are made absolute and links
        resolved, so "..", links and case on Windows can't step outside. Only folders that exist
        count, a root included: a link can't be resolved otherwise (the Go one's EvalSymlinks)."""
        if not os.path.exists(folder):
            return "", False
        resolved = os.path.realpath(os.path.abspath(folder))
        for root in self.roots:
            if not os.path.exists(root):
                continue
            base = os.path.realpath(os.path.abspath(root))
            try:
                if os.path.commonpath([fold(base), fold(resolved)]) == fold(base):
                    return resolved, True
            except ValueError:
                continue
        return "", False


def fold(path: str) -> str:
    return os.path.normcase(path) if sys.platform == "win32" else path


@dataclass
class Status:
    """How a running `agent-machine run` is doing, for `agent-machine` and `agent-machine stop` in another terminal."""

    pid: int = 0
    server: str = ""
    state: str = ""
    at: float = 0.0
    server_version: str = ""
    started: str = ""

    @classmethod
    def load(cls) -> "Status":
        try:
            data = json.loads(status_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        return cls(int(data.get("pid") or 0), str(data.get("server") or ""), str(data.get("state") or ""), float(data.get("at") or 0),
                   str(data.get("server_version") or ""), str(data.get("started") or ""))

    def save(self) -> None:
        self.at = time.time()
        value = asdict(self)
        for key in ("server_version", "started"):
            if not value[key]:
                value.pop(key)
        common.write_json(status_path(), value)


def alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_process(pid: int) -> None:
    import signal

    # On Windows this is TerminateProcess: there's no SIGTERM to catch.
    os.kill(pid, signal.SIGTERM)


def running() -> Optional[Status]:
    """The agent-machine already in the fleet, if one is."""
    status = Status.load()
    if status.pid == 0 or status.pid == os.getpid() or not alive(status.pid):
        return None
    return status
