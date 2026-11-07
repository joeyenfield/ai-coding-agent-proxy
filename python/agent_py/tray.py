"""agent-tray: an icon in the system tray for AI Control Mesh on this computer.

It shows what runs here and starts and stops it with the commands you'd type yourself:

- the local server (`agent-server local`, docs/local.md), when this computer has one: start, stop,
  and open the console, on localhost or at the server's public address when it has one;
- this machine (`agent-machine`): start, stop, whether it's signed in and in the fleet, and its
  features, each switched on or off from the menu;
- agent (`agent proxy status`): whether it's signed in, and whether agent-proxy runs, which the
  menu starts or stops.

It reads their `status --json` every few seconds, never their files, so it works the same with the
Go programs and the Python ones. One runs per user (a lock in the config folder). pystray and
Pillow are the `tray` extra, imported only to show the icon; `agent-tray --check` says whether an
icon can be shown here, and the installers start it only then. `agent-tray --autostart on|off`
starts it at sign-in (the Run key on Windows, a LaunchAgent on macOS, XDG autostart on Linux).
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import common

NAME = "AI Control Mesh"
APP_ID = "ai-control-mesh-tray"
POLL_SECONDS = 5.0
# The machine's toggles (agent-machine status --json's features), as the menu names them.
FEATURES = (
    ("launches", "Start agents here"),
    ("files", "Browse files"),
    ("commands", "Terminals"),
    ("servers", "Manage model servers"),
    ("containers", "Run containers"),
    ("updates", "Updates from the console"),
)
COLORS = {"running": (46, 160, 67, 255), "partial": (210, 153, 34, 255), "stopped": (128, 128, 128, 255)}
# Where autostart lives. Tests point them elsewhere.
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
LAUNCH_AGENT = "com.ai-control-mesh.tray"


def _quiet() -> Dict[str, Any]:
    """No console window for the commands it runs (the tray itself has none on Windows)."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def program(name: str) -> Optional[str]:
    """A program by name: on PATH, where the installers put them, else beside this Python.

    PATH comes first: the tray's own environment also has the Python agent and agent-machine
    (the wheel it came in), which mustn't stand in for the Go ones you installed."""
    suffixes = (".exe", ".cmd") if os.name == "nt" else ("",)
    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt":
        folders = [Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Programs" / "agent-proxy"]
    else:
        folders = [Path.home() / ".local" / "bin", Path("/usr/local/bin")]
    folders.append(Path(sys.executable).parent)
    for folder in folders:
        for suffix in suffixes:
            if (folder / (name + suffix)).is_file():
                return str(folder / (name + suffix))
    return None


def ask(command: List[str], timeout: float = 20) -> Optional[Dict[str, Any]]:
    """Run a status command and read the JSON it prints last; None when it fails."""
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, **_quiet())
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    if done.returncode != 0 or not lines:
        return None
    try:
        answer = json.loads(lines[-1])
    except ValueError:
        return None
    return answer if isinstance(answer, dict) else None


@dataclass
class Snapshot:
    """What runs here: each part is None when its program isn't on this computer."""

    server: Optional[Dict[str, Any]] = None
    machine: Optional[Dict[str, Any]] = None
    agent: Optional[Dict[str, Any]] = None
    programs: Dict[str, str] = field(default_factory=dict)


STATUS = {
    "server": ("agent-server", ["local", "status", "--json"]),
    "machine": ("agent-machine", ["status", "--json"]),
    "agent": ("agent", ["proxy", "status", "--json"]),
}


def snapshot() -> Snapshot:
    """Ask the three programs at once: the server's answer takes a moment (it's Python starting)."""
    found = {part: program(name) for part, (name, _) in STATUS.items()}
    shot = Snapshot(programs={STATUS[part][0]: path for part, path in found.items() if path})
    with ThreadPoolExecutor(max_workers=3) as pool:
        answers = {part: pool.submit(ask, [path, *STATUS[part][1]]) for part, path in found.items() if path}
        for part, answer in answers.items():
            setattr(shot, part, answer.result())
    return shot


def state(shot: Snapshot) -> str:
    """running when the server and the machine (those that are here) run, partial when some do."""
    parts = []
    if shot.server is not None:
        parts.append(bool(shot.server.get("running")))
    if shot.machine is not None:
        parts.append(bool(shot.machine.get("running") and shot.machine.get("signed_in")))
    if not parts or not any(parts):
        return "stopped"
    return "running" if all(parts) else "partial"


def _host(url: str) -> str:
    return url.split("://", 1)[-1].rstrip("/")


def tooltip(shot: Snapshot) -> str:
    """A short title (Windows keeps 127 characters)."""
    lines = [NAME]
    if shot.server is not None:
        lines.append("Server: running" if shot.server.get("running") else "Server: stopped")
    if shot.machine is not None:
        if not shot.machine.get("signed_in"):
            lines.append("Machine: not signed in")
        else:
            running = str(shot.machine.get("state") or "")
            lines.append("Machine: " + (("in the fleet" if running.startswith("connected") else running) if shot.machine.get("running") else "stopped"))
    return "\n".join(lines)[:127]


@dataclass
class Item:
    """One menu line: text alone when it has no action, a submenu when it has children."""

    label: str = ""
    action: Optional[Callable[[], None]] = None
    checked: Optional[bool] = None
    children: List["Item"] = field(default_factory=list)
    separator: bool = False
    default: bool = False


SEPARATOR = Item(separator=True)


class Actions:
    """What the menu does: the same commands you'd type. tray refreshes once they're done."""

    def __init__(self, shot_programs: Callable[[], Dict[str, str]], done: Callable[[Optional[str]], None]):
        self.programs = shot_programs
        self.done = done

    def _run(self, name: str, *arguments: str) -> None:
        path = self.programs().get(name) or program(name)
        if not path:
            self.done(f"{name} isn't installed here.")
            return

        def work() -> None:
            try:
                result = subprocess.run([path, *arguments], capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL, **_quiet())
            except (OSError, subprocess.SubprocessError) as error:
                self.done(f"{name} {' '.join(arguments)}: {error}")
                return
            failure = None
            if result.returncode != 0:
                lines = [line for line in (result.stderr + result.stdout).splitlines() if line.strip()]
                failure = lines[-1] if lines else f"{name} {' '.join(arguments)} failed"
            self.done(failure)

        threading.Thread(target=work, daemon=True).start()

    def server_start(self) -> None:
        self._run("agent-server", "local", "start")

    def server_stop(self) -> None:
        self._run("agent-server", "local", "stop")

    def machine_start(self) -> None:
        self._run("agent-machine", "start")

    def machine_stop(self) -> None:
        self._run("agent-machine", "stop")

    def feature(self, name: str, on: bool) -> None:
        self._run("agent-machine", name, "on" if on else "off")

    def proxy_stop(self) -> None:
        self._run("agent", "proxy", "stop")

    def proxy_start(self) -> None:
        # agent-proxy runs in the foreground: started in the background here, as agent does by itself.
        path = self.programs().get("agent") or program("agent")
        if not path:
            self.done("agent isn't installed here.")
            return
        options: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        else:
            options["start_new_session"] = True
        try:
            subprocess.Popen([path, "proxy"], **options)
        except OSError as error:
            self.done(f"agent proxy: {error}")
            return
        self.done(None)

    def open(self, url: str) -> None:
        webbrowser.open(url)

    def autostart(self, on: bool) -> None:
        try:
            set_autostart(on)
        except OSError as error:
            self.done(f"Start at sign-in: {error}")
            return
        self.done(None)


def menu(shot: Snapshot, act: Actions, starts_at_sign_in: bool, quit: Callable[[], None]) -> List[Item]:
    """The menu for what runs here, as plain items (built again each time it changes)."""
    items: List[Item] = [Item(NAME)]
    server = shot.server
    if "agent-server" in shot.programs:
        items.append(SEPARATOR)
        if server is None:
            items.append(Item("Server: agent-server didn't answer"))
        else:
            url = str(server.get("url") or "")
            processes = server.get("processes") or {}
            if server.get("running"):
                items.append(Item(f"Server: running at {_host(url)}"))
                items.append(Item("Open the console", lambda: act.open(url), default=True))
                public = str(server.get("public_url") or "")
                if public and public.rstrip("/") != url.rstrip("/"):
                    items.append(Item(f"Open {_host(public)}", lambda: act.open(public)))
                items.append(Item("Stop the server", act.server_stop))
            elif any(processes.values()):
                items.append(Item("Server: partly running"))
                items.append(Item("Start the server again", act.server_start, default=True))
                items.append(Item("Stop the server", act.server_stop))
            else:
                items.append(Item("Server: stopped" if server.get("installed") else "Server: not set up yet"))
                items.append(Item("Start the server", act.server_start, default=True))

    machine = shot.machine
    if "agent-machine" in shot.programs:
        items.append(SEPARATOR)
        if machine is None:
            items.append(Item("This machine: no status from agent-machine (an older version? agent-machine update)"))
        else:
            server_url = str(machine.get("server") or "")
            if not machine.get("signed_in"):
                items.append(Item(f"This machine: not signed in to {_host(server_url)}"))
                items.append(Item(f"Sign in with: agent-machine login {server_url}"))
            else:
                who = f" as {machine['user']}" if machine.get("user") else ""
                if machine.get("running"):
                    # The state is "connected to <server>" once it's in, else what it's doing ("connecting ...").
                    shown = str(machine.get("state") or "")
                    if shown.startswith("connected"):
                        shown = f"in the fleet at {_host(server_url)}"
                    items.append(Item(f"This machine: {shown}{who}"))
                    items.append(Item("Stop agent-machine", act.machine_stop))
                else:
                    items.append(Item(f"This machine: stopped (signed in{who})"))
                    items.append(Item("Start agent-machine", act.machine_start, default=not any(item.default for item in items)))
            features = machine.get("features") or {}
            items.append(Item("Features", children=[
                Item(label, (lambda name=name, on=bool(features.get(name)): act.feature(name, not on)), checked=bool(features.get(name)))
                for name, label in FEATURES
            ]))
            roots = [str(root) for root in machine.get("roots") or []]
            items.append(Item(f"Folders ({len(roots)})", children=[Item(root) for root in roots] or [Item("None: agent-machine roots add <folder>")]))

    agent = shot.agent
    if "agent" in shot.programs:
        items.append(SEPARATOR)
        if agent is None:
            items.append(Item("agent: no status (an older version? agent update)"))
        else:
            if agent.get("signed_in"):
                who = f" as {agent['user']}" if agent.get("user") else ""
                items.append(Item(f"agent: signed in to {_host(str(agent.get('server') or ''))}{who}"))
            else:
                items.append(Item(f"agent: not signed in (agent login {agent.get('server') or ''})"))
            if agent.get("running"):
                items.append(Item(f"agent-proxy: running on port {agent.get('port')}"))
                items.append(Item("Stop agent-proxy", act.proxy_stop))
            else:
                items.append(Item("agent-proxy: not running (an agent starts it)"))
                items.append(Item("Start agent-proxy", act.proxy_start))

    items += [SEPARATOR, Item("Start at sign-in", lambda: act.autostart(not starts_at_sign_in), checked=starts_at_sign_in), Item("Quit", quit)]
    return items


# Starting at sign-in.

def autostart_command() -> List[str]:
    """How sign-in starts it: this Python (pythonw on Windows, so no console) running this module."""
    executable = Path(sys.executable)
    if os.name == "nt" and executable.with_name("pythonw.exe").is_file():
        executable = executable.with_name("pythonw.exe")
    return [str(executable), "-m", "agent_py.tray"]


def _launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT}.plist"


def _desktop_entry_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart" / f"{APP_ID}.desktop"


def _desktop_quote(argument: str) -> str:
    """An Exec argument as the Desktop Entry spec quotes it."""
    if argument and not any(char in argument for char in " \t\n\"'\\><~|&;$*?#()`"):
        return argument
    escaped = "".join("\\" + char if char in "\"`$\\" else char for char in argument)
    return f'"{escaped}"'


def autostart_enabled(platform: Optional[str] = None) -> bool:
    platform = platform or sys.platform
    if platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, NAME)
            return True
        except OSError:
            return False
    if platform == "darwin":
        return _launch_agent_path().is_file()
    return _desktop_entry_path().is_file()


def set_autostart(on: bool, platform: Optional[str] = None) -> None:
    platform = platform or sys.platform
    command = autostart_command()
    if platform == "win32":
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if on:
                winreg.SetValueEx(key, NAME, 0, winreg.REG_SZ, subprocess.list2cmdline(command))
            else:
                try:
                    winreg.DeleteValue(key, NAME)
                except FileNotFoundError:
                    pass
        return
    path = _launch_agent_path() if platform == "darwin" else _desktop_entry_path()
    if not on:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if platform == "darwin":
        data = plistlib.dumps({"Label": LAUNCH_AGENT, "ProgramArguments": command, "RunAtLoad": True, "ProcessType": "Interactive"})
        path.write_bytes(data)
        return
    path.write_text(
        "[Desktop Entry]\nType=Application\n"
        f"Name={NAME}\nComment=Shows whether AI Control Mesh runs on this computer, and starts or stops it\n"
        f"Exec={' '.join(_desktop_quote(argument) for argument in command)}\n"
        "Terminal=false\nNoDisplay=true\nX-GNOME-Autostart-enabled=true\n",
        encoding="utf-8",
    )


def desktop_available(environ: Optional[Dict[str, str]] = None, platform: Optional[str] = None) -> bool:
    """Whether there's a desktop to show an icon in: always on Windows and macOS, a display on Linux and BSD."""
    environ = dict(os.environ) if environ is None else environ
    platform = platform or sys.platform
    if platform in ("win32", "darwin"):
        return True
    return bool(environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY"))


def available() -> bool:
    """A desktop, and pystray able to draw in it (on Linux, its backend loads only with a display)."""
    if not desktop_available():
        return False
    try:
        import PIL  # noqa: F401
        import pystray  # noqa: F401
    except Exception:  # pystray raises more than ImportError when no backend fits
        return False
    return True


def lock() -> Optional[Any]:
    """One tray per user: a lock held for as long as it runs. None when another one holds it."""
    path = common.config_dir() / "agent-tray" / "tray.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def image(status: str) -> Any:
    """The icon: three joined nodes, green when everything runs, amber when some does, grey when none."""
    from PIL import Image, ImageDraw

    color = COLORS.get(status, COLORS["stopped"])
    picture = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(picture)
    nodes = [(14, 48), (50, 48), (32, 14)]
    for start, end in ((0, 1), (1, 2), (2, 0)):
        draw.line([nodes[start], nodes[end]], fill=color, width=6)
    for x, y in nodes:
        draw.ellipse([x - 11, y - 11, x + 11, y + 11], fill=color)
    return picture


class Tray:
    """The icon, its menu, and the poll that keeps them current."""

    def __init__(self) -> None:
        import pystray

        self.pystray = pystray
        self.shot = Snapshot()
        self.starts_at_sign_in = autostart_enabled()
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.actions = Actions(lambda: self.shot.programs, self.finished)
        self.icon = pystray.Icon(APP_ID, image("stopped"), NAME, menu=pystray.Menu(self.items))

    def items(self) -> Any:
        return tuple(self.convert(item) for item in menu(self.shot, self.actions, self.starts_at_sign_in, self.quit))

    def convert(self, item: Item) -> Any:
        pystray = self.pystray
        if item.separator:
            return pystray.Menu.SEPARATOR
        if item.children:
            return pystray.MenuItem(item.label, pystray.Menu(*[self.convert(child) for child in item.children]))
        action = item.action

        def clicked(icon: Any, menu_item: Any) -> None:
            if action:
                action()

        checked = None if item.checked is None else (lambda menu_item, value=item.checked: value)
        return pystray.MenuItem(item.label, clicked, checked=checked, enabled=action is not None, default=item.default)

    def finished(self, failure: Optional[str]) -> None:
        """After an action: say what went wrong, if anything, and look again now."""
        if failure:
            try:
                self.icon.notify(failure, NAME)
            except Exception:  # not every backend can show a notification
                pass
        self.wake.set()

    def refresh(self) -> None:
        self.shot = snapshot()
        self.starts_at_sign_in = autostart_enabled()
        self.icon.icon = image(state(self.shot))
        self.icon.title = tooltip(self.shot)
        self.icon.update_menu()

    def poll(self) -> None:
        while not self.stopping.is_set():
            try:
                self.refresh()
            except Exception:  # a status that can't be read now is read again next time
                pass
            self.wake.wait(POLL_SECONDS)
            self.wake.clear()

    def quit(self) -> None:
        self.stopping.set()
        self.wake.set()
        self.icon.stop()

    def run(self) -> None:
        def setup(icon: Any) -> None:
            icon.visible = True
            threading.Thread(target=self.poll, daemon=True).start()

        self.icon.run(setup=setup)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-tray", description=(
        "An icon in the system tray: whether the local server, this machine and agent-proxy run, "
        "with their start and stop, and the console to open."))
    parser.add_argument("--check", action="store_true", help="Exit 0 when an icon can be shown here, 1 when not")
    parser.add_argument("--status", action="store_true", help="Print what the icon would show, as JSON, and stop")
    parser.add_argument("--autostart", choices=["on", "off"], help="Start it when you sign in to this computer, or not")
    args = parser.parse_args(argv)
    if args.check:
        return 0 if available() else 1
    if args.status:
        shot = snapshot()
        print(json.dumps({**asdict(shot), "state": state(shot)}))
        return 0
    if args.autostart:
        set_autostart(args.autostart == "on")
        print(f"agent-tray {'starts' if args.autostart == 'on' else 'no longer starts'} when you sign in.")
        return 0
    if not desktop_available():
        print("agent-tray: there's no desktop here to show an icon in.", file=sys.stderr)
        return 1
    try:
        import PIL  # noqa: F401
        import pystray  # noqa: F401
    except Exception as error:
        print(f"agent-tray needs pystray and Pillow (pip install 'ai-proxy-agent-py[tray]'): {error}", file=sys.stderr)
        return 1
    held = lock()
    if held is None:
        print("agent-tray is already running.")
        return 0
    try:
        Tray().run()
    finally:
        held.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
