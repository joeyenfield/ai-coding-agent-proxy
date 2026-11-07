"""agent-tray: its menu for what runs here, its icon's state, starting at sign-in, and the status it reads.

The icon itself (pystray) needs a desktop; everything it shows is built as plain items here, from the
same `status --json` answers the real programs give.
"""

from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path

import pytest

from agent_py import tray
from conftest import script

SERVER = {"url": "http://localhost:19999", "installed": True, "running": True, "processes": {"server": True, "auth": True},
          "public_url": "https://mesh.example.com"}
MACHINE = {"server": "http://localhost:19999", "signed_in": True, "user": "admin", "running": True, "state": "connected to http://localhost:19999",
           "features": {"launches": True, "files": True, "commands": False}, "roots": ["/home/me/projects"]}
AGENT = {"server": "http://localhost:19999", "signed_in": True, "user": "admin", "running": True, "port": 8184}
PROGRAMS = {"agent-server": "agent-server", "agent-machine": "agent-machine", "agent": "agent"}


class Recorder(tray.Actions):
    def __init__(self):
        super().__init__(lambda: PROGRAMS, lambda failure: None)
        self.calls = []

    def _run(self, name, *arguments):
        self.calls.append([name, *arguments])

    def open(self, url):
        self.calls.append(["open", url])

    def proxy_start(self):
        self.calls.append(["agent", "proxy"])

    def autostart(self, on):
        self.calls.append(["autostart", on])


def by_label(items):
    found = {}
    for item in items:
        found[item.label] = item
        found.update(by_label(item.children))
    return found


def test_everything_running_shows_the_consoles_both_addresses_and_the_features_to_switch():
    act = Recorder()
    shot = tray.Snapshot(SERVER, MACHINE, AGENT, PROGRAMS)
    items = tray.menu(shot, act, starts_at_sign_in=True, quit=lambda: act.calls.append(["quit"]))
    labels = by_label(items)
    assert "Server: running at localhost:19999" in labels and "This machine: in the fleet at localhost:19999 as admin" in labels
    assert "agent: signed in to localhost:19999 as admin" in labels and "agent-proxy: running on port 8184" in labels
    # Double-clicking the icon opens the console; the server's public address is there too.
    assert [item.label for item in items if item.default] == ["Open the console"]
    labels["Open the console"].action()
    labels["Open mesh.example.com"].action()
    labels["Stop the server"].action()
    labels["Stop agent-machine"].action()
    labels["Stop agent-proxy"].action()
    # Each feature shows whether it's on, and a click switches it the other way.
    assert labels["Browse files"].checked is True and labels["Terminals"].checked is False
    labels["Browse files"].action()
    labels["Terminals"].action()
    assert [child.label for child in labels["Folders (1)"].children] == ["/home/me/projects"]
    assert labels["Start at sign-in"].checked is True
    labels["Start at sign-in"].action()
    labels["Quit"].action()
    assert act.calls == [
        ["open", "http://localhost:19999"], ["open", "https://mesh.example.com"], ["agent-server", "local", "stop"],
        ["agent-machine", "stop"], ["agent", "proxy", "stop"], ["agent-machine", "files", "off"],
        ["agent-machine", "commands", "on"], ["autostart", False], ["quit"],
    ]
    assert tray.state(shot) == "running"
    assert tray.tooltip(shot) == "AI Control Mesh\nServer: running\nMachine: in the fleet"


def test_what_is_stopped_can_be_started_and_a_missing_sign_in_says_how():
    act = Recorder()
    server = {**SERVER, "running": False, "processes": {"server": False, "auth": False}, "public_url": None}
    machine = {**MACHINE, "signed_in": False, "user": "", "running": False, "state": "stopped", "roots": []}
    agent = {**AGENT, "signed_in": False, "running": False}
    shot = tray.Snapshot(server, machine, agent, PROGRAMS)
    labels = by_label(tray.menu(shot, act, starts_at_sign_in=False, quit=lambda: None))
    assert "Server: stopped" in labels and "Open the console" not in labels
    assert "This machine: not signed in to localhost:19999" in labels
    assert "Sign in with: agent-machine login http://localhost:19999" in labels
    assert labels["Sign in with: agent-machine login http://localhost:19999"].action is None
    assert "Start agent-machine" not in labels  # it can't join the fleet before signing in
    assert "None: agent-machine roots add <folder>" in labels
    assert "agent: not signed in (agent login http://localhost:19999)" in labels
    labels["Start the server"].action()
    labels["Start agent-proxy"].action()
    assert act.calls == [["agent-server", "local", "start"], ["agent", "proxy"]]
    assert tray.state(shot) == "stopped"
    # Signed in but stopped: start it. With the server running, that's partly running.
    shot = tray.Snapshot(SERVER, {**MACHINE, "running": False}, None, {"agent-server": "x", "agent-machine": "y"})
    labels = by_label(tray.menu(shot, act, starts_at_sign_in=False, quit=lambda: None))
    assert "This machine: stopped (signed in as admin)" in labels and "Start agent-machine" in labels
    assert tray.state(shot) == "partial"


def test_only_what_is_installed_is_shown():
    shot = tray.Snapshot(None, MACHINE, None, {"agent-machine": "agent-machine"})
    labels = by_label(tray.menu(shot, Recorder(), starts_at_sign_in=False, quit=lambda: None))
    assert not any(label.startswith(("Server", "agent:", "agent-proxy")) for label in labels)
    assert tray.state(shot) == "running"


def test_starting_at_sign_in_on_linux_and_macos(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.setattr(tray, "autostart_command", lambda: ["/opt/my env/bin/python", "-m", "agent_py.tray"])
    tray.set_autostart(True, platform="linux")
    entry = (tmp_path / "config" / "autostart" / "ai-control-mesh-tray.desktop").read_text(encoding="utf-8")
    assert 'Exec="/opt/my env/bin/python" -m agent_py.tray\n' in entry and tray.autostart_enabled("linux")
    tray.set_autostart(False, platform="linux")
    assert not tray.autostart_enabled("linux")
    tray.set_autostart(True, platform="darwin")
    plist = plistlib.loads((tmp_path / "home" / "Library" / "LaunchAgents" / "com.ai-control-mesh.tray.plist").read_bytes())
    assert plist["ProgramArguments"] == ["/opt/my env/bin/python", "-m", "agent_py.tray"] and plist["RunAtLoad"] is True
    tray.set_autostart(False, platform="darwin")
    assert not tray.autostart_enabled("darwin")


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows registry")
def test_starting_at_sign_in_on_windows(monkeypatch):
    import winreg

    monkeypatch.setattr(tray, "RUN_KEY", r"Software\AIControlMeshTests\Run")
    try:
        tray.set_autostart(True, platform="win32")
        assert tray.autostart_enabled("win32")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, tray.RUN_KEY) as key:
            command = winreg.QueryValueEx(key, tray.NAME)[0]
        assert "pythonw.exe" in command.lower() and command.endswith("-m agent_py.tray")
        tray.set_autostart(False, platform="win32")
        assert not tray.autostart_enabled("win32")
    finally:
        for key in (tray.RUN_KEY, r"Software\AIControlMeshTests"):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
            except OSError:
                pass


def test_an_icon_needs_a_desktop():
    assert tray.desktop_available({}, "win32") and tray.desktop_available({}, "darwin")
    assert not tray.desktop_available({}, "linux")
    assert tray.desktop_available({"WAYLAND_DISPLAY": "wayland-0"}, "linux")


def test_it_reads_the_real_programs_status(tmp_path, monkeypatch, agent_binary, machine_binary):
    # The Python agent and agent-machine, and agent-server, answering their status --json in a home of the test's own.
    server_binary = script(tmp_path / "server", "agent-server", "agent_server.app:main")
    for name in [name for name in os.environ if name.startswith("AI_PROXY_")]:
        monkeypatch.delenv(name)
    # A new APPDATA moves Python's user site-packages on Windows, where agent-server's packages may be,
    # and an editable install's .pth isn't read from PYTHONPATH: name both folders.
    import site

    server_sources = str(Path(__file__).resolve().parents[2] / "server")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [os.environ.get("PYTHONPATH"), server_sources, site.getusersitepackages()])))
    for variable in ("APPDATA", "XDG_CONFIG_HOME", "LOCALAPPDATA"):
        monkeypatch.setenv(variable, str(tmp_path / variable.lower()))
    monkeypatch.setenv("AI_PROXY_LOCAL_HOME", str(tmp_path / "mesh"))
    monkeypatch.setenv("AI_PROXY_URL", "http://localhost:19999")
    monkeypatch.setenv("AI_PROXY_LOCAL_PORT", "28197")
    paths = {"agent-server": str(server_binary), "agent-machine": str(machine_binary), "agent": str(agent_binary)}
    monkeypatch.setattr(tray, "program", lambda name: paths.get(name))
    shot = tray.snapshot()
    assert shot.server["url"] == "http://localhost:19999" and shot.server["installed"] is False and shot.server["running"] is False
    # agent-machine takes its server from its own sign-in, never AI_PROXY_URL: here, none yet.
    assert shot.machine["server"].startswith("http") and shot.machine["signed_in"] is False and shot.machine["running"] is False
    assert set(shot.machine["features"]) == {name for name, _ in tray.FEATURES}
    assert shot.agent == {"server": "http://localhost:19999", "signed_in": False, "user": "", "running": False, "port": 28197, "version": ""}
    assert tray.state(shot) == "stopped"
