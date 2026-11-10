"""`agent-server downloads fetch`: a release's files become a build the server hands out."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_server import fetch_builds, version
from agent_server.app import create_app
from agent_server.config import Settings
from agent_server.downloads import Downloads
from test_installer import tar_gz, zip_bytes

RELEASE = "0.4.0"
BASE = "https://example.test/releases/download/v0.4.0"
WHEEL = f"ai_proxy_agent_py-{RELEASE}-py3-none-any.whl"


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    # A commit after the release: the server offers the release's builds as the newest of its X.Y.Z.
    monkeypatch.setattr(version, "current", lambda: f"{RELEASE}+abc1234")


def release_files() -> dict[str, bytes]:
    """A GitHub Release as devhelpers/scripts/release/release.py leaves it: flat, under SHA256SUMS."""
    files = {
        f"agent-{RELEASE}-linux-amd64.tar.gz": tar_gz("linux-amd64", {"agent": b"agent", "agent-proxy": b"proxy", "install.sh": b"#!/bin/sh\n"}),
        f"agent-machine-{RELEASE}-linux-amd64.tar.gz": tar_gz("linux-amd64", {"agent-machine": b"machine", "install.sh": b"#!/bin/sh\n"}),
        f"agent-{RELEASE}-windows-amd64.zip": zip_bytes("windows-amd64", {"agent.exe": b"agent.exe", "install.ps1": b"# install"}),
        WHEEL: b"wheel",
        f"ai-control-mesh-{RELEASE}.zip": b"the server bundle, which a server doesn't hand out",
        "install-local.sh": b"#!/bin/sh\n",
    }
    files["SHA256SUMS"] = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in files.items()).encode()
    return files


def getter(files: dict[str, bytes], asked: list[str] | None = None):
    def get(url: str) -> bytes:
        assert url.startswith(BASE + "/")
        if asked is not None:
            asked.append(url.rsplit("/", 1)[1])
        return files[url.rsplit("/", 1)[1]]

    return get


def test_a_fetched_release_is_laid_out_as_a_build_and_handed_out(tmp_path):
    asked: list[str] = []
    dist = tmp_path / "dist"
    dist.mkdir()
    target = fetch_builds.fetch(dist, RELEASE, BASE, getter(release_files(), asked), io.StringIO())

    assert target == dist / RELEASE and not list(dist.glob(".*"))
    # Only what a server hands out is downloaded: not the server bundle or the web installers.
    assert f"ai-control-mesh-{RELEASE}.zip" not in asked and "install-local.sh" not in asked
    build = Downloads(dist).served()
    assert build is not None and build.version == RELEASE and not build.exact
    assert set(build.sums) == {
        f"linux-amd64/agent-{RELEASE}-linux-amd64.tar.gz", f"linux-amd64/agent-machine-{RELEASE}-linux-amd64.tar.gz",
        f"windows-amd64/agent-{RELEASE}-windows-amd64.zip", f"python/{WHEEL}",
        # The programs themselves, for `agent update` and `agent-machine update`.
        "linux-amd64/artifact/agent", "linux-amd64/artifact/agent-proxy", "linux-amd64/artifact/agent-machine",
        "linux-amd64/artifact/install.sh", "windows-amd64/artifact/agent.exe", "windows-amd64/artifact/install.ps1",
    }
    for path, digest in build.sums.items():
        assert hashlib.sha256((target / path).read_bytes()).hexdigest() == digest, path
    assert (target / "linux-amd64/artifact/agent-machine").read_bytes() == b"machine"

    # The one-line installer now has builds to name, and the downloads are served.
    with TestClient(create_app(Settings(log_dir=tmp_path / "logs", dist_dir=dist)), base_url="http://127.0.0.1:8181", client=("127.0.0.1", 5000)) as client:
        script = client.get("/install.sh").text
        assert "FILES=''" not in script and f"/downloads/{RELEASE}/linux-amd64/agent-{RELEASE}-linux-amd64.tar.gz" in script
        assert client.get(f"/downloads/{RELEASE}/linux-amd64/artifact/agent").content == b"agent"


def test_a_file_that_doesnt_match_the_release_changes_nothing(tmp_path):
    dist = tmp_path / "dist"
    earlier = dist / RELEASE
    earlier.mkdir(parents=True)
    (earlier / "kept").write_text("an earlier fetch")
    files = release_files()
    files[WHEEL] = b"something else"
    with pytest.raises(SystemExit, match="doesn't match"):
        fetch_builds.fetch(dist, RELEASE, BASE, getter(files), io.StringIO())
    assert [path.name for path in dist.iterdir()] == [RELEASE] and (earlier / "kept").is_file()


def test_archive_entries_outside_their_platform_folder_are_left_out(tmp_path):
    files = release_files()
    name = f"agent-{RELEASE}-linux-amd64.tar.gz"
    # tar_gz puts every file under <platform>/: a name with a folder in it lands deeper, and is skipped.
    files[name] = tar_gz("linux-amd64", {"agent": b"agent", "sub/escape": b"no"})
    del files["SHA256SUMS"]
    files["SHA256SUMS"] = "".join(f"{hashlib.sha256(data).hexdigest()}  {item}\n" for item, data in files.items()).encode()
    dist = tmp_path / "dist"
    dist.mkdir()
    target = fetch_builds.fetch(dist, RELEASE, BASE, getter(files), io.StringIO())
    assert sorted(path.name for path in (target / "linux-amd64" / "artifact").iterdir()) == ["agent", "agent-machine", "install.sh"]


def test_the_command_refuses_a_version_that_isnt_a_release(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AI_PROXY_DIST_DIR", str(tmp_path / "dist"))
    assert fetch_builds.main(["fetch", "--version", "../x"]) == 2
    assert "isn't a release" in capsys.readouterr().err
    assert not (tmp_path / "dist").exists()
