"""The one-line installers, run for real against a server: curl … | sh and irm … | iex.

The build is fake: each archive holds a stand-in install.sh or install.ps1 that records
how it was run, so the test checks what the one-liner downloads, checks and hands over.
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_server import installer, version
from agent_server.downloads import Downloads
from agent_server.app import create_app
from agent_server.config import Settings
from test_client import serve


VERSION = "0.4.0+abc1234"
LOCAL = {"base_url": "http://127.0.0.1:8181", "client": ("127.0.0.1", 50000)}
RECORD_SH = '#!/bin/sh\nprintf "%s\\n" "$@" > "$RECORD"\nls "$(dirname "$0")" >> "$RECORD"\n'
RECORD_PS1 = 'param([string]$Prefix = "", [switch]$Service)\n"prefix=$Prefix service=$Service" | Set-Content $env:RECORD\n'


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    monkeypatch.setenv("AI_PROXY_VERSION", VERSION)
    version.current.cache_clear()
    yield
    version.current.cache_clear()


def tar_gz(platform: str, files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{platform}/{name}")
            info.size, info.mode = len(data), 0o755
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def zip_bytes(platform: str, files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(f"{platform}/{name}", data)
    return buffer.getvalue()


def fake_build(dist: Path, broken: str | None = None) -> None:
    files = {
        f"linux-amd64/agent-{VERSION}-linux-amd64.tar.gz": tar_gz("linux-amd64", {"agent": b"#!/bin/sh\n", "agent-proxy": b"#!/bin/sh\n", "install.sh": RECORD_SH.encode()}),
        f"linux-amd64/agent-machine-{VERSION}-linux-amd64.tar.gz": tar_gz("linux-amd64", {"agent-machine": b"#!/bin/sh\n", "install.sh": RECORD_SH.encode()}),
        f"windows-amd64/agent-{VERSION}-windows-amd64.zip": zip_bytes("windows-amd64", {"agent.exe": b"x", "install.ps1": RECORD_PS1.encode()}),
        f"windows-amd64/agent-machine-{VERSION}-windows-amd64.zip": zip_bytes("windows-amd64", {"agent-machine.exe": b"x", "install.ps1": RECORD_PS1.encode()}),
    }
    folder = dist / VERSION
    lines = []
    for path, data in files.items():
        (folder / path).parent.mkdir(parents=True, exist_ok=True)
        (folder / path).write_bytes(data)
        digest = "0" * 64 if broken and broken in path else hashlib.sha256(data).hexdigest()
        lines.append(f"{digest}  {path}")
    (folder / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def app_for(tmp_path: Path):
    return create_app(Settings(log_dir=tmp_path / "logs", dist_dir=tmp_path / "dist"))


def test_the_scripts_carry_the_servers_address_and_its_builds(tmp_path):
    fake_build(tmp_path / "dist")
    with TestClient(app_for(tmp_path), **LOCAL) as client:
        sh = client.get("/install.sh")
        assert sh.status_code == 200 and "\r" not in sh.text
        assert "SERVER='http://127.0.0.1:8181'" in sh.text and f"VERSION='{VERSION}'" in sh.text
        assert f"linux-amd64 agent-machine {hashlib.sha256((tmp_path / 'dist' / VERSION / f'linux-amd64/agent-machine-{VERSION}-linux-amd64.tar.gz').read_bytes()).hexdigest()}" in sh.text
        assert "windows-amd64" not in sh.text.split("FILES='")[1].split("'")[0]
        ps1 = client.get("/install.ps1").text
        assert "$Server = 'http://127.0.0.1:8181'" in ps1 and "windows-amd64 agent " in ps1 and "linux-amd64 agent " not in ps1
    # A Host header that isn't a plain address never reaches the script.
    downloads = Downloads(tmp_path / "dist")
    for host in ("http://x';rm -rf ~;'", "http://a$(id)", "http://a`id`:80", "javascript:x"):
        with pytest.raises(ValueError):
            installer.script("sh", host, downloads)
    assert "SERVER='http://[::1]:9999'" in installer.script("sh", "http://[::1]:9999/", downloads)


def test_the_scripts_are_public_with_accounts(tmp_path):
    from test_auth import org_settings

    fake_build(tmp_path / "dist")
    settings = org_settings(tmp_path)
    settings.dist_dir = tmp_path / "dist"
    with TestClient(create_app(settings), base_url="http://192.168.1.5:9999", client=("192.168.1.9", 5000)) as client:
        assert client.get("/api/status").status_code == 401
        assert "SERVER='http://192.168.1.5:9999'" in client.get("/install.sh").text


def test_without_a_build_the_script_says_so(tmp_path):
    with TestClient(app_for(tmp_path), **LOCAL) as client:
        assert "FILES=''" in client.get("/install.sh").text


def sh_tools() -> str | None:
    shell = shutil.which("sh")
    if not shell or not all(shutil.which(tool) for tool in ("curl", "tar")):
        return None
    return shell


@pytest.mark.skipif(sh_tools() is None, reason="needs sh, curl and tar")
def test_curl_pipe_sh_downloads_checks_and_runs_the_installer(tmp_path):
    fake_build(tmp_path / "dist")
    record = tmp_path / "ran.txt"
    with serve(app_for(tmp_path)) as server:
        env = {**os.environ, "RECORD": str(record).replace("\\", "/"), "AGENT_PLATFORM": "linux-amd64"}
        done = subprocess.run(
            [sh_tools(), "-c", f"curl -fsSL {server}/install.sh | sh -s -- --no-login --service --prefix /opt/agents"],
            env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
        )
        assert done.returncode == 0, done.stdout + done.stderr
        ran = record.read_text().split()
        # Both archives unpacked into one folder, and their installer run once with the options.
        assert ran[:3] == ["--service", "--prefix", "/opt/agents"]
        assert {"agent", "agent-proxy", "agent-machine", "install.sh"} <= set(ran[3:])
        assert "Next: agent login" in done.stdout and "Next: agent-machine login" in done.stdout
        record.unlink()
        only = subprocess.run([sh_tools(), "-c", f"curl -fsSL {server}/install.sh | sh -s -- --only agent-machine --no-login"],
                              env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        assert only.returncode == 0 and "agent-proxy" not in record.read_text() and "agent-machine" in record.read_text()
    # A download that doesn't match its SHA-256 is never installed.
    fake_build(tmp_path / "dist", broken="agent-machine")
    record.unlink()
    with serve(app_for(tmp_path)) as server:
        refused = subprocess.run([sh_tools(), "-c", f"curl -fsSL {server}/install.sh | sh -s -- --no-login"],
                                 env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        assert refused.returncode != 0 and "SHA-256" in refused.stderr and not record.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell")
def test_irm_iex_downloads_checks_and_runs_the_installer(tmp_path):
    fake_build(tmp_path / "dist")
    record = tmp_path / "ran.txt"
    with serve(app_for(tmp_path)) as server:
        command = (
            f"$env:AGENT_PLATFORM = 'windows-amd64'; $env:RECORD = '{record}'; "
            f"& ([scriptblock]::Create((irm {server}/install.ps1))) -NoLogin -Prefix '{tmp_path / 'bin'}'"
        )
        done = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                              capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        assert done.returncode == 0, done.stdout + done.stderr
        assert record.read_text().strip() == f"prefix={tmp_path / 'bin'} service=False"
        assert "Next: agent login" in done.stdout and "Next: agent-machine login" in done.stdout
