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


def test_the_uninstallers_are_public_and_need_no_build(tmp_path):
    from test_auth import org_settings

    with TestClient(create_app(org_settings(tmp_path)), base_url="http://192.168.1.5:9999", client=("192.168.1.9", 5000)) as client:
        sh = client.get("/uninstall.sh")
        assert sh.status_code == 200 and "\r" not in sh.text and sh.text.startswith("#!/bin/sh\n")
        ps1 = client.get("/uninstall.ps1")
        assert ps1.status_code == 200 and "\r\n" in ps1.text and "param([string]$Only" in ps1.text
    # The same file whatever server serves it: nothing of the request goes in.
    assert installer.uninstaller("sh") == (installer.TEMPLATES / "uninstall.sh").read_text(encoding="utf-8").replace("\r\n", "\n")


# A stand-in for agent, agent-proxy and agent-machine: logout signs out of as many servers as
# its state file says, one per run, as the real ones do; every run is recorded.
FAKE_PROGRAM_SH = """#!/bin/sh
name=${0##*/}
echo "$*" >> "$FAKE_STATE/$name.ran"
case "$1" in
    stop) echo "agent-machine isn't running." ;;
    proxy) echo "agent-proxy stopped; \\`agent\\` starts it again when needed." ;;
    logout)
        n=$(cat "$FAKE_STATE/$name" 2>/dev/null || echo 0)
        if [ "$n" -gt 0 ]; then echo "Signed out of http://server$n."; echo $((n - 1)) > "$FAKE_STATE/$name"
        else echo "Not signed in to that server."; fi ;;
esac
"""


def fake_settings(home: Path, config: Path) -> list[Path]:
    """The folders --purge deletes, with a file in each."""
    folders = [config / "agent-proxy", config / "agent-machine", home / ".cache" / "agent-proxy"]
    for folder in folders:
        folder.mkdir(parents=True)
        (folder / "credentials.json").write_text("{}", encoding="utf-8")
    return folders


@pytest.mark.skipif(shutil.which("sh") is None or sys.platform == "darwin", reason="needs sh; on a Mac it would remove the real launchd agent")
def test_uninstall_sh_removes_the_programs_and_with_purge_signs_out(tmp_path):
    prefix, state, home = tmp_path / "bin", tmp_path / "state", tmp_path / "home"
    prefix.mkdir(), state.mkdir()
    for program in ("agent", "agent-proxy", "agent-machine"):
        (prefix / program).write_text(FAKE_PROGRAM_SH, encoding="utf-8", newline="\n")
        (prefix / program).chmod(0o755)
    (state / "agent").write_text("2", encoding="utf-8")
    folders = fake_settings(home, home / ".config")
    script = tmp_path / "uninstall.sh"
    script.write_text(installer.uninstaller("sh"), encoding="utf-8", newline="\n")
    posix = lambda path: str(path).replace("\\", "/")
    env = {**os.environ, "HOME": posix(home), "XDG_CONFIG_HOME": posix(home / ".config"), "FAKE_STATE": posix(state)}
    for name in ("AI_PROXY_CREDENTIALS", "AI_PROXY_MACHINE_HOME"):
        env.pop(name, None)

    def uninstall(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["sh", posix(script), "--prefix", posix(prefix), *args], env=env, capture_output=True,
                              text=True, timeout=60, stdin=subprocess.DEVNULL)

    # Just agent-machine: stopped first, the rest left alone, and settings stay without --purge.
    done = uninstall("--only", "agent-machine")
    assert done.returncode == 0, done.stdout + done.stderr
    assert (state / "agent-machine.ran").read_text().split() == ["stop"]
    assert not (prefix / "agent-machine").exists() and (prefix / "agent").exists() and (prefix / "agent-proxy").exists()
    assert all(folder.exists() for folder in folders) and "--purge" in done.stdout
    # The rest with --purge: signed out of both servers before it goes, then the folders deleted.
    done = uninstall("--purge")
    assert done.returncode == 0, done.stdout + done.stderr
    # agent-proxy asked to stop first (the way agent update does), then signed out one server at a time.
    assert (state / "agent.ran").read_text().splitlines() == ["proxy stop"] + ["logout"] * 3
    assert "Stopped agent-proxy." in done.stdout
    assert "Signed out of http://server2." in done.stdout and "Signed out of http://server1." in done.stdout
    assert not list(prefix.iterdir())
    assert not any(folder.exists() for folder in folders)
    assert "Nothing to remove" in uninstall().stdout


FAKE_PROGRAM_GO = """package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

func main() {
	name := strings.TrimSuffix(filepath.Base(os.Args[0]), ".exe")
	state := filepath.Join(os.Getenv("FAKE_STATE"), name)
	ran, _ := os.OpenFile(state+".ran", os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	fmt.Fprintln(ran, strings.Join(os.Args[1:], " "))
	ran.Close()
	switch os.Args[1] {
	case "proxy":
		if len(os.Args) > 2 && os.Args[2] == "stop" {
			// The proxy below isn't on agent proxy stop's port: the uninstaller has to end it by its path.
			fmt.Println("agent-proxy isn't running.")
			return
		}
		time.Sleep(2 * time.Minute)
	case "stop":
		fmt.Println("agent-machine isn't running.")
	case "logout":
		data, _ := os.ReadFile(state)
		n, _ := strconv.Atoi(strings.TrimSpace(string(data)))
		if n == 0 {
			fmt.Println("Not signed in to that server.")
			return
		}
		fmt.Printf("Signed out of http://server%d.\\n", n)
		// The real logout says so on stderr when revoking fails: Windows PowerShell mustn't stop at it.
		fmt.Fprintln(os.Stderr, "logout: local credentials forgotten, but issuer revocation failed")
		os.WriteFile(state, []byte(strconv.Itoa(n-1)), 0o644)
		os.Exit(1)
	}
}
"""


@pytest.mark.skipif(sys.platform != "win32" or shutil.which("go") is None, reason="Windows PowerShell, and Go to build a stand-in program")
def test_uninstall_ps1_stops_agent_proxy_removes_the_programs_and_with_purge_signs_out(tmp_path):
    prefix, state, home = tmp_path / "bin", tmp_path / "state", tmp_path / "home"
    prefix.mkdir(), state.mkdir(), home.mkdir()
    source = tmp_path / "fake" / "main.go"
    source.parent.mkdir()
    source.write_text(FAKE_PROGRAM_GO, encoding="utf-8")
    go_env = {key: value for key, value in os.environ.items() if key.upper() not in {"HTTPS_PROXY", "HTTP_PROXY"}}
    built = subprocess.run(["go", "build", "-o", str(prefix / "agent.exe"), "main.go"], cwd=source.parent,
                           env={**go_env, "GOTOOLCHAIN": "local"}, capture_output=True, text=True, timeout=300)
    assert built.returncode == 0, built.stderr
    for program in ("agent-proxy.exe", "agent-machine.exe"):
        shutil.copy(prefix / "agent.exe", prefix / program)
    (state / "agent-machine").write_text("1", encoding="utf-8")
    folders = fake_settings(home, home / "AppData" / "Roaming")
    env = {**os.environ, "USERPROFILE": str(home), "APPDATA": str(home / "AppData" / "Roaming"),
           "LOCALAPPDATA": str(home / "AppData" / "Local"), "FAKE_STATE": str(state),
           "AI_PROXY_SCHEDULED_TASK": "agent-machine-test-that-does-not-exist"}
    for name in ("AI_PROXY_CREDENTIALS", "AI_PROXY_MACHINE_HOME"):
        env.pop(name, None)
    script = tmp_path / "uninstall.ps1"
    script.write_text(installer.uninstaller("ps1"), encoding="utf-8", newline="")
    # agent-proxy as the launcher starts it: Windows can't delete its program while it runs.
    proxy = subprocess.Popen([str(prefix / "agent.exe"), "proxy", "--port", "1"], env=env)
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                               "-Prefix", str(prefix), "-Purge"], env=env, capture_output=True, text=True, timeout=120,
                              stdin=subprocess.DEVNULL)
        assert done.returncode == 0, done.stdout + done.stderr
        assert proxy.wait(timeout=10) is not None and "Stopped agent-proxy" in done.stdout
    finally:
        proxy.kill()
    assert (state / "agent-machine.ran").read_text().split() == ["stop", "logout", "logout"]
    assert "Signed out of http://server1." in done.stdout and "revocation failed" in done.stdout
    assert not prefix.exists()
    assert not any(folder.exists() for folder in folders)
