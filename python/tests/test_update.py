"""`agent-machine update` and `agent update` in Python: the server's wheel, checked, installed with pip.

The update runs in a throwaway virtualenv, so it never touches the Python running the tests; the
programs themselves still come from this checkout (PYTHONPATH), and the wheel is a stand-in with
the package's name and the server's version.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx
import pytest

from agent_server import version
from test_client import machine

NAME = "ai_proxy_agent_py-9.9.9-py3-none-any.whl"


def stand_in_wheel() -> bytes:
    info = "ai_proxy_agent_py-9.9.9.dist-info"
    files = {
        "agent_py_update_marker/__init__.py": "VERSION = '9.9.9'\n",
        f"{info}/METADATA": "Metadata-Version: 2.1\nName: ai-proxy-agent-py\nVersion: 9.9.9\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        record = []
        for name, text in files.items():
            archive.writestr(name, text)
            digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=").decode()
            record.append(f"{name},sha256={digest},{len(text)}")
        record.append(f"{info}/RECORD,,")
        archive.writestr(f"{info}/RECORD", "\n".join(record) + "\n")
    return buffer.getvalue()


@pytest.fixture
def served_build(tmp_path, monkeypatch):
    """The server runs 9.9.9 and offers a Python wheel of it."""
    monkeypatch.setenv("AI_PROXY_VERSION", "9.9.9")
    version.current.cache_clear()
    data = stand_in_wheel()
    folder = tmp_path / "dist" / "9.9.9" / "python"
    folder.mkdir(parents=True)
    (folder / NAME).write_bytes(data)
    sums = tmp_path / "dist" / "9.9.9" / "SHA256SUMS"
    sums.write_text(f"{hashlib.sha256(data).hexdigest()}  python/{NAME}\n")
    yield sums
    version.current.cache_clear()


@pytest.fixture(scope="module")
def venv(tmp_path_factory) -> Path:
    folder = tmp_path_factory.mktemp("venv")
    subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", str(folder)], check=True, capture_output=True, timeout=300)
    return folder / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def installed(python: Path) -> str:
    shown = subprocess.run([str(python), "-m", "pip", "show", "ai-proxy-agent-py"], capture_output=True, text=True)
    return next((line.split(":", 1)[1].strip() for line in shown.stdout.splitlines() if line.startswith("Version:")), "")


def test_the_programs_update_from_the_servers_python_build(tmp_path, agent_binary, served_build, venv):
    with machine(tmp_path, agent_binary) as m:
        # The programs report their own version, not the server's (which the test set in this process).
        env = {key: value for key, value in m.env.items() if key != "AI_PROXY_VERSION"}
        env["AI_PROXY_MACHINE_HOME"] = str(tmp_path / "machine-home")
        done = subprocess.run([str(venv), "-m", "agent_py.machine", "update", "--server", m.server], env=env, capture_output=True, text=True, timeout=300)
        assert done.returncode == 0, done.stdout + done.stderr
        assert "-> 9.9.9" in done.stdout and installed(venv) == "9.9.9"
        # agent too, which stops the agent-proxy afterwards so the next agent starts the new one.
        done = subprocess.run([str(venv), "-m", "agent_py.client", "update", "--force"], env=env, capture_output=True, text=True, timeout=300)
        assert done.returncode == 0, done.stdout + done.stderr
        assert "Stopped the running agent-proxy" in done.stdout
        # A wheel that doesn't match the server's SHA-256 installs nothing.
        served_build.write_text(f"{'0' * 64}  python/{NAME}\n")
        subprocess.run([str(venv), "-m", "pip", "uninstall", "-y", "ai-proxy-agent-py"], capture_output=True, timeout=120)
        refused = subprocess.run([str(venv), "-m", "agent_py.machine", "update", "--server", m.server, "--force"], env=env, capture_output=True, text=True, timeout=300)
        assert refused.returncode == 1 and "SHA-256" in refused.stderr, refused.stdout + refused.stderr
        assert installed(venv) == ""


def test_the_console_updates_a_python_machine_that_takes_updates(tmp_path, agent_binary, served_build, venv):
    from conftest import script
    from test_client import fleet_machine

    # agent-machine on the virtualenv's Python, so the update's pip install lands there.
    launcher = tmp_path / "launcher"
    launcher.mkdir()
    binary = script(launcher, "agent-machine", "agent_py.machine:main", python=str(venv))
    home = tmp_path / "machine-home"
    with machine(tmp_path, agent_binary) as m:
        m.env.pop("AI_PROXY_VERSION", None)
        with fleet_machine(m, binary, tmp_path, ["updates", "on"], env={"AI_PROXY_MACHINE_STARTED": "background"}) as fleet:
            base = f"{m.server}/api/machines/{fleet.box['id']}"
            assert fleet.box["updates"] is True
            first = json.loads((home / "status.json").read_text())["pid"]
            started = httpx.post(f"{base}/update", json={"agent": True}, timeout=60)
            assert started.status_code == 200, started.text
            assert started.json()["updating"] is True and started.json()["to"] == "9.9.9"
            deadline = time.monotonic() + 120
            status: dict = {}
            while time.monotonic() < deadline:
                status = json.loads((home / "status.json").read_text())
                if installed(venv) == "9.9.9" and status.get("pid") not in (0, first) and status.get("state", "").startswith("connected"):
                    break
                time.sleep(0.5)
            assert installed(venv) == "9.9.9", (home / "agent-machine.log").read_text()[-2000:]
            assert status.get("pid") not in (0, first) and status["state"].startswith("connected"), status
        subprocess.run([str(venv), "-m", "agent_py.machine", "stop"], env={**m.env, "AI_PROXY_MACHINE_HOME": str(home)}, capture_output=True, timeout=30)


def test_a_checkout_says_the_same_version_as_the_server_built_from_it():
    from agent_py import common

    assert common.checkout_version(Path(__file__).resolve().parents[2]) == version.build_version(environ={})
    assert json.dumps(common.VERSION)  # a plain string either way
