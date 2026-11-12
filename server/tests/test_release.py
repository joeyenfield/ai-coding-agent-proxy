"""devhelpers/release_scripts/release/release.py: the files of a GitHub Release, and the web installers it fills in."""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "devhelpers" / "release_scripts" / "release" / "release.py"
spec = importlib.util.spec_from_file_location("release", SCRIPT)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

VERSION = "3.0.0"
BASE = "https://github.com/owner/repo/releases/download/v3.0.0"


def checkout(tmp_path: Path) -> Path:
    """The parts of a checkout a release takes, small."""
    root = tmp_path / "root"
    for name, text in {
        "VERSION": VERSION, "LICENSE": "MIT", "server/pyproject.toml": "[project]", "server/README.md": "server",
        "server/agent_server/__init__.py": "", "server/agent_server/__pycache__/x.cpython-313.pyc": "junk",
        "ui/dist/index.html": "<div id=\"root\"></div>", "ui/package.json": "{}", "docs/index.md": "# Docs",
        "benchmarks/tests/one.json": "{}",
    }.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    return root


def built(tmp_path: Path, tamper: bool = False) -> Path:
    """dist/<version>/ as tools/dist leaves it: archives, artifacts and a wheel, under SHA256SUMS."""
    dist = tmp_path / "dist" / VERSION
    files = {
        f"linux-amd64/agent-{VERSION}-linux-amd64.tar.gz": b"agent linux",
        f"linux-amd64/agent-machine-{VERSION}-linux-amd64.tar.gz": b"machine linux",
        f"windows-amd64/agent-{VERSION}-windows-amd64.zip": b"agent windows",
        f"windows-amd64/agent-machine-{VERSION}-windows-amd64.zip": b"machine windows",
        "linux-amd64/artifact/agent": b"a binary",
        f"python/ai_proxy_agent_py-{VERSION}-py3-none-any.whl": b"wheel",
    }
    lines = []
    for path, data in files.items():
        (dist / path).parent.mkdir(parents=True, exist_ok=True)
        (dist / path).write_bytes(data)
        lines.append(f"{hashlib.sha256(data).hexdigest()}  {path}")
    if tamper:
        (dist / f"linux-amd64/agent-{VERSION}-linux-amd64.tar.gz").write_bytes(b"something else")
    (dist / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dist


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_release_has_the_bundle_every_archive_and_installers_that_check_them(tmp_path):
    out = tmp_path / "out"
    release.make(VERSION, BASE + "/", built(tmp_path), out, checkout(tmp_path))

    names = sorted(path.name for path in out.iterdir())
    assert names == sorted([
        f"ai-control-mesh-{VERSION}.zip", f"agent-{VERSION}-linux-amd64.tar.gz", f"agent-machine-{VERSION}-linux-amd64.tar.gz",
        f"agent-{VERSION}-windows-amd64.zip", f"agent-machine-{VERSION}-windows-amd64.zip",
        f"ai_proxy_agent_py-{VERSION}-py3-none-any.whl", "install-local.sh", "install-local.ps1",
        "uninstall-local.sh", "uninstall-local.ps1", "SHA256SUMS",
    ])
    # The bundle unpacks into a folder the server takes as its root (VERSION and server/), named
    # exactly by BUILD, with the built console and no UI sources (so it never tries to rebuild it).
    with zipfile.ZipFile(out / f"ai-control-mesh-{VERSION}.zip") as bundle:
        inside = set(bundle.namelist())
        assert bundle.read("BUILD").decode().strip() == VERSION
    assert {"VERSION", "LICENSE", "server/pyproject.toml", "server/agent_server/__init__.py", "ui/dist/index.html",
            "docs/index.md", "benchmarks/tests/one.json"} <= inside
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in inside)
    assert "ui/package.json" not in inside

    sh = (out / "install-local.sh").read_bytes().decode()
    ps1 = (out / "install-local.ps1").read_bytes().decode()
    assert "\r" not in sh and "\r\n" in ps1
    # The uninstallers go out as they are, each with its system's line endings.
    assert "\r" not in (out / "uninstall-local.sh").read_bytes().decode()
    assert "\r\n" in (out / "uninstall-local.ps1").read_bytes().decode()
    for text in (sh, ps1):
        assert "{{" not in text
        assert f"BASE='{BASE}'" in text.replace("$Base = ", "BASE=")
        assert f"ai-control-mesh-{VERSION}.zip {digest(out / f'ai-control-mesh-{VERSION}.zip')}" in text
    # Each installer carries only its own systems' archives, each with its SHA-256.
    # Both carry the Python wheel, for --python and for systems without a Go build.
    wheel = f"ai_proxy_agent_py-{VERSION}-py3-none-any.whl"
    assert f"WHEEL='{wheel} {digest(out / wheel)}'" in sh and f"$Wheel = '{wheel} {digest(out / wheel)}'" in ps1
    linux = f"linux-amd64 agent {digest(out / f'agent-{VERSION}-linux-amd64.tar.gz')} agent-{VERSION}-linux-amd64.tar.gz"
    windows = f"windows-amd64 agent-machine {digest(out / f'agent-machine-{VERSION}-windows-amd64.zip')} agent-machine-{VERSION}-windows-amd64.zip"
    assert linux in sh and windows not in sh
    assert windows in ps1 and linux not in ps1

    sums = dict(line.split("  ")[::-1] for line in (out / "SHA256SUMS").read_text(encoding="utf-8").splitlines())
    assert set(sums) == set(names) - {"SHA256SUMS"}
    assert all(sums[name] == digest(out / name) for name in sums)


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")
def test_the_filled_in_sh_installer_parses(tmp_path):
    out = tmp_path / "out"
    release.make(VERSION, BASE, built(tmp_path), out, checkout(tmp_path))
    for name in ("install-local.sh", "uninstall-local.sh"):
        assert subprocess.run(["sh", "-n", str(out / name)], capture_output=True).returncode == 0
    # Piped into sh there's no file to read the options from: --help prints them all the same.
    for name, option in (("install-local.sh", "--no-client"), ("uninstall-local.sh", "--purge")):
        piped = subprocess.run(["sh", "-s", "--", "--help"], input=(out / name).read_bytes(), capture_output=True)
        assert piped.returncode == 0 and option in piped.stdout.decode()


def local_install(tmp_path: Path, kind: str) -> tuple[Path, Path, Path]:
    """What install-local leaves, small: the instance's folder with its data, app/ and venv/, and a
    prefix. The bundle's own uninstaller is one that only records how it was called."""
    home, prefix, record = tmp_path / "home", tmp_path / "bin", tmp_path / "ran.txt"
    for path, text in {
        home / "local.json": "{}", home / "logs" / "agent-server.sqlite3": "data", home / "venv" / "pyvenv.cfg": "",
        prefix / "agent-server.cmd": "@echo off", prefix / "something-else": "not this install's",
    }.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    uninstaller = home / "app" / VERSION / "server" / "agent_server" / "install" / f"uninstall.{kind}"
    uninstaller.parent.mkdir(parents=True)
    if kind == "sh":
        uninstaller.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$RECORD"\n', encoding="utf-8", newline="\n")
    else:
        uninstaller.write_text('param([string]$Prefix = "", [switch]$Purge)\n"prefix=$Prefix purge=$Purge" | Set-Content $env:RECORD\n', encoding="utf-8")
    return home, prefix, record


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")
def test_the_sh_uninstaller_removes_the_programs_and_keeps_the_data_unless_purged(tmp_path):
    import os

    out = tmp_path / "out"
    release.make(VERSION, BASE, built(tmp_path), out, checkout(tmp_path))
    home, prefix, record = local_install(tmp_path, "sh")
    posix = lambda path: str(path).replace("\\", "/")  # noqa: E731
    env = {**os.environ, "AI_PROXY_LOCAL_HOME": posix(home), "RECORD": posix(record)}
    script = out / "uninstall-local.sh"
    done = subprocess.run(["sh", "-s", "--", "--prefix", posix(prefix)], input=script.read_bytes(), env=env, capture_output=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    # agent and agent-machine went through the bundle's own uninstaller, in the same prefix.
    assert record.read_text().splitlines() == ["--prefix", posix(prefix)]
    assert not (home / "app").exists() and not (home / "venv").exists()
    assert (home / "logs" / "agent-server.sqlite3").is_file() and (prefix / "something-else").is_file()
    assert "Its data is still in" in done.stdout.decode()
    # Again, with nothing but the data left, and --purge: that goes too. --keep-client leaves the programs.
    record.unlink()
    purged = subprocess.run(["sh", "-s", "--", "--prefix", posix(prefix), "--purge", "--keep-client"], input=script.read_bytes(), env=env, capture_output=True, timeout=60)
    assert purged.returncode == 0 and not home.exists() and not record.exists()
    gone = subprocess.run(["sh", "-s"], input=script.read_bytes(), env=env, capture_output=True, timeout=60)
    assert gone.returncode == 0 and "no local install" in gone.stdout.decode()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell")
def test_the_powershell_uninstaller_removes_the_programs_and_keeps_the_data_unless_purged(tmp_path):
    import os

    out = tmp_path / "out"
    release.make(VERSION, BASE, built(tmp_path), out, checkout(tmp_path))
    home, prefix, record = local_install(tmp_path, "ps1")
    env = {**os.environ, "AI_PROXY_LOCAL_HOME": str(home), "RECORD": str(record)}

    def run(options: str) -> subprocess.CompletedProcess:
        command = f"& ([scriptblock]::Create((Get-Content -Raw '{out / 'uninstall-local.ps1'}'))) -Prefix '{prefix}' {options}"
        return subprocess.run(["powershell", "-NoProfile", "-Command", command], env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)

    done = run("")
    assert done.returncode == 0, done.stdout + done.stderr
    assert record.read_text().strip() == f"prefix={prefix} purge=False"
    assert not (home / "app").exists() and not (home / "venv").exists() and not (prefix / "agent-server.cmd").exists()
    # Another program's file keeps the folder, and so its PATH entry.
    assert (home / "logs" / "agent-server.sqlite3").is_file() and (prefix / "something-else").is_file()
    purged = run("-Purge -KeepClient")
    assert purged.returncode == 0 and not home.exists()
    assert "no local install" in run("").stdout


def test_a_release_refuses_what_an_installer_cant_carry_or_trust(tmp_path):
    root, dist = checkout(tmp_path), built(tmp_path)
    with pytest.raises(SystemExit, match="base URL"):
        release.make(VERSION, "https://example.com/x'; rm -rf ~; echo '", dist, tmp_path / "out", root)
    with pytest.raises(SystemExit, match="isn't X.Y.Z"):
        release.make("3.0.0; true", BASE, dist, tmp_path / "out", root)
    with pytest.raises(SystemExit, match="doesn't match"):
        release.make(VERSION, BASE, built(tmp_path / "tampered", tamper=True), tmp_path / "out", root)
    # The Python wheel is required: it's how a system without a Go build gets agent and agent-machine.
    without = built(tmp_path / "without")
    next((without / "python").glob("*.whl")).unlink()
    with pytest.raises(SystemExit, match="Python wheel"):
        release.make(VERSION, BASE, without, tmp_path / "out", root)
    (root / "ui" / "dist" / "index.html").unlink()
    with pytest.raises(SystemExit, match="ui/dist"):
        release.make(VERSION, BASE, dist, tmp_path / "out", root)
