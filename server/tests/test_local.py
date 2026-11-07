"""agent-server local: its folder, its secrets, the children's environment, and stopping only its own processes."""

from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agent_server import local


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_the_folder_is_the_platforms_per_user_data_folder_unless_named():
    assert local.instance_home({"AI_PROXY_LOCAL_HOME": "/x/mesh", "LOCALAPPDATA": "C:/L"}, "win32") == Path("/x/mesh")
    assert local.instance_home({"LOCALAPPDATA": "C:/L"}, "win32") == Path("C:/L") / "AI Control Mesh"
    assert local.instance_home({"HOME": "/home/a"}, "darwin") == Path("/home/a/Library/Application Support/AI Control Mesh")
    assert local.instance_home({"HOME": "/home/a"}, "linux") == Path("/home/a/.local/share/ai-control-mesh")
    assert local.instance_home({"HOME": "/home/a", "XDG_DATA_HOME": "/data"}, "linux") == Path("/data/ai-control-mesh")


def test_secrets_are_made_once_and_kept_owner_only(tmp_path):
    instance = local.Instance(tmp_path / "mesh")
    first = instance.secrets()
    assert first["introspection_secret"] and first["vault_keys"].startswith("1:")
    assert local.Instance(tmp_path / "mesh").secrets() == first
    if os.name != "nt":
        assert stat.S_IMODE(instance.secrets_path.stat().st_mode) == 0o600


def test_the_children_get_a_loopback_instance_and_none_of_the_shells_ai_proxy_variables(tmp_path):
    instance = local.Instance(tmp_path, port=29111, auth_port=29112)
    environment = instance.environment({"PATH": "/bin", "AI_PROXY_DATABASE_URL": "postgresql://elsewhere", "ai_proxy_url": "x"})
    assert environment["PATH"] == "/bin"
    assert "AI_PROXY_DATABASE_URL" not in environment and "ai_proxy_url" not in environment
    assert environment["AI_PROXY_HOST"] == environment["AI_PROXY_AUTH_HOST"] == "127.0.0.1"
    assert environment["AI_PROXY_OIDC_ISSUER"] == "http://localhost:29112"
    assert environment["AI_PROXY_OIDC_AUDIENCE"] == environment["AI_PROXY_URL"] == "http://localhost:29111"
    assert environment["AI_PROXY_OIDC_REDIRECT_URIS"] == "http://localhost:29111/api/auth/callback"
    assert environment["AI_PROXY_LOG_DIR"] == str(tmp_path / "logs")
    assert environment["AI_PROXY_OIDC_INTROSPECTION_SECRET"] == instance.secrets()["introspection_secret"]


def test_the_state_file_keeps_ports_and_processes(tmp_path):
    instance = local.Instance(tmp_path, port=29111, auth_port=29112, processes={"server": local.Process(12, 34.5)})
    instance.save()
    loaded = local.Instance.load(tmp_path)
    assert (loaded.port, loaded.auth_port, loaded.processes) == (29111, 29112, {"server": local.Process(12, 34.5)})


def test_secrets_never_go_on_a_command_line(tmp_path, monkeypatch):
    seen = {}

    class Child:
        pid = os.getpid()

    def popen(command, **options):
        seen["command"] = command
        return Child()

    monkeypatch.setattr(local.subprocess, "Popen", popen)
    instance = local.Instance(tmp_path)
    local.spawn("server", instance, instance.environment())
    secret = instance.secrets()["introspection_secret"]
    assert local.MARK in seen["command"][-1]
    assert not any(secret in part for part in seen["command"])


def _sleeper(marked: bool) -> subprocess.Popen:
    code = "import time; time.sleep(60)" + (f"  # {local.MARK}" if marked else "")
    return subprocess.Popen([sys.executable, "-c", code])


def test_stop_stops_only_the_processes_it_started(tmp_path):
    import psutil

    ours, other = _sleeper(True), _sleeper(False)
    try:
        instance = local.Instance(tmp_path, processes={
            "server": local.Process(ours.pid, psutil.Process(ours.pid).create_time()),
            # Not ours: no mark on its command line, so stop leaves it alone.
            "auth": local.Process(other.pid, psutil.Process(other.pid).create_time()),
        })
        # A recorded id whose start time differs is another process that reused the id.
        assert not local.running(local.Process(ours.pid, 1.0))
        assert local.running(instance.processes["server"]) and not local.running(instance.processes["auth"])
        local.stop(instance, quiet=True)
        ours.wait(timeout=30)
        assert other.poll() is None
        assert local.Instance.load(tmp_path).processes == {}
    finally:
        for child in (ours, other):
            if child.poll() is None:
                child.kill()


def test_start_refuses_a_port_another_program_holds(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(local, "spawn", lambda *args: pytest.fail("started despite the port"))
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        instance = local.Instance(tmp_path, port=holder.getsockname()[1], auth_port=free_port())
        assert local.start(instance) == 1
    assert "in use by another program" in capsys.readouterr().err


def test_a_port_a_stopped_server_just_used_is_free(tmp_path):
    # The server closes first, so its side waits in TIME_WAIT: on Linux and macOS a plain bind fails
    # then, though the server could start again (an update stops it and starts it at once). The
    # listener sets SO_REUSEADDR as uvicorn's does: Linux lets a TIME_WAIT give way only then.
    with socket.socket() as listener:
        if sys.platform != "win32":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port)) as client:
            accepted, _ = listener.accept()
            accepted.close()
            client.recv(1)
    assert local.port_free(port)


def test_a_local_instance_starts_signs_in_its_first_user_and_stops(tmp_path, monkeypatch):
    # The real programs, on free ports in a folder of the test's own. No console rebuild.
    plain = local.Instance.environment
    monkeypatch.setattr(local.Instance, "environment", lambda self, base=None: {**plain(self, base), "AI_PROXY_UI_BUILD": "0"})
    instance = local.Instance(tmp_path / "mesh", port=free_port(), auth_port=free_port())
    try:
        # Overrides reach the children last (the all-in-one image's listen address goes this way).
        assert local.start(instance, overrides={"AI_PROXY_PUBLIC_URL": "https://mesh.example.com"}) == 0
        assert local.server_ready(instance) and local.auth_ready(instance)
        assert instance.password_path.read_text(encoding="utf-8").strip()
        assert local.status(local.Instance.load(instance.home)) == 0
        # What agent-tray reads: both running, the loopback address, and the public one to open too.
        shown = local.status_json(local.Instance.load(instance.home))
        assert shown["running"] and shown["processes"] == {"server": True, "auth": True}
        assert shown["url"] == instance.url and shown["public_url"] == "https://mesh.example.com"
    finally:
        local.stop(local.Instance.load(instance.home), quiet=True)
    assert local.port_free(instance.port) and local.port_free(instance.auth_port)
    stopped = local.status_json(local.Instance.load(instance.home))
    assert not stopped["running"] and stopped["installed"] and stopped["public_url"] is None
