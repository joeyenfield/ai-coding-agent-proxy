"""The project's version, the protocol gate in front of the API, and the builds the server hands out."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_server import version
from agent_server.app import create_app
from agent_server.config import Settings
from agent_server.downloads import describe
from test_auth import org_settings, signed_in, ws_headers


LOCAL = {"base_url": "http://127.0.0.1:8181", "client": ("127.0.0.1", 50000)}
VERSION = "0.4.0+abc1234"


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    monkeypatch.setenv("AI_PROXY_VERSION", VERSION)
    version.current.cache_clear()
    yield
    version.current.cache_clear()


def build(dist: Path, name: str, files: dict[str, bytes], listed: set[str] | None = None) -> None:
    folder = dist / name
    lines = []
    for path, data in files.items():
        target = folder / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if listed is None or path in listed:
            lines.append(f"{hashlib.sha256(data).hexdigest()}  {path}")
    (folder / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def app_with(tmp_path: Path, **settings) -> object:
    return create_app(Settings(log_dir=tmp_path / "logs", dist_dir=tmp_path / "dist", **settings))


def test_the_build_version_comes_from_the_version_file(tmp_path):
    (tmp_path / "VERSION").write_text("1.4.2\n", encoding="utf-8")
    assert version.build_version(tmp_path, environ={}) == "1.4.2+unknown"  # no git here
    assert version.build_version(tmp_path, environ={"AI_PROXY_VERSION": "2.0.0"}) == "2.0.0"
    (tmp_path / "VERSION").write_text("not a version", encoding="utf-8")
    assert version.base_version(tmp_path) == "0.0.0"


def test_health_says_the_version_and_protocol(tmp_path):
    with TestClient(app_with(tmp_path), **LOCAL) as client:
        health = client.get("/health").json()
        assert health["version"] == VERSION and health["protocol"] == version.PROTOCOL and health["min_protocol"] == version.MIN_PROTOCOL


def test_programs_on_a_protocol_the_server_doesnt_speak_are_told_to_update(tmp_path):
    with TestClient(app_with(tmp_path), **LOCAL) as client:
        ok = {"x-ai-proxy-protocol": str(version.PROTOCOL), "x-ai-proxy-version": VERSION, "user-agent": "agent/" + VERSION}
        assert client.get("/api/status", headers=ok).status_code == 200
        # Browsers and other tools don't say: they pass.
        assert client.get("/api/status").status_code == 200
        for protocol in (str(version.PROTOCOL + 1), str(version.MIN_PROTOCOL - 1), "nonsense"):
            refused = client.get("/api/agents", headers={**ok, "x-ai-proxy-protocol": protocol, "user-agent": "agent-machine/0.1.0", "x-ai-proxy-version": "0.1.0"})
            assert refused.status_code == 426
            body = refused.json()
            assert body["version"] == VERSION and "agent-machine 0.1.0" in body["detail"] and "agent-machine update" in body["detail"]
        # Agent traffic is gated the same way, before it reaches a backend.
        assert client.post("/v1/chat/completions", json={}, headers={"x-ai-proxy-protocol": "99"}).status_code == 426


def test_a_machine_on_another_protocol_is_closed_with_the_reason(tmp_path):
    with TestClient(app_with(tmp_path), **LOCAL) as client:
        headers = {"host": "127.0.0.1:8181", "x-ai-proxy-protocol": "99", "user-agent": "agent-machine/9.0.0", "x-ai-proxy-version": "9.0.0"}
        with client.websocket_connect("/api/machines/connect", headers=headers) as socket:
            with pytest.raises(Exception) as closed:
                socket.receive_text()
        assert getattr(closed.value, "code", None) == version.CLOSE_UPGRADE


def test_machines_on_another_build_say_an_update_is_there(tmp_path):
    app = create_app(org_settings(tmp_path))
    alice = signed_in(app, "alice")
    with alice:
        with alice.websocket_connect("/api/machines/connect", headers=ws_headers(alice)) as socket:
            socket.send_text(json.dumps({"type": "hello", "id": "m1", "name": "laptop", "version": "0.3.0", "protocol": version.PROTOCOL}))
            assert json.loads(socket.receive_text())["type"] == "welcome"
            [machine] = alice.get("/api/machines").json()["machines"]
            assert machine["protocol"] == version.PROTOCOL
            assert machine["update"] == {"available": True, "server_version": VERSION, "command": "agent-machine update"}
            socket.send_text(json.dumps({"type": "update", "version": VERSION}))
            socket.send_text(json.dumps({"type": "ping"}))
            # The update is applied before the next read answers.
            for _ in range(50):
                if not alice.get("/api/machines").json()["machines"][0]["update"]["available"]:
                    break
            assert not alice.get("/api/machines").json()["machines"][0]["update"]["available"]


def test_the_server_hands_out_only_its_builds_listed_files(tmp_path):
    files = {
        "linux-amd64/agent-0.4.0+abc1234-linux-amd64.tar.gz": b"client",
        "linux-amd64/agent-machine-0.4.0+abc1234-linux-amd64.deb": b"deb",
        "linux-amd64/artifact/agent-machine": b"binary",
        "linux-amd64/artifact/install.sh": b"#!/bin/sh",
        "windows-amd64/agent-0.4.0+abc1234-windows-amd64.msi": b"msi",
        "android/agent-proxy-0.4.0+abc1234.apk": b"apk",
        "linux-amd64/secret.txt": b"not listed",
    }
    build(tmp_path / "dist", VERSION, files, listed=set(files) - {"linux-amd64/secret.txt"})
    build(tmp_path / "dist", "0.3.0", {"linux-amd64/old.tar.gz": b"old"})
    with TestClient(app_with(tmp_path), **LOCAL) as client:
        index = client.get("/downloads/index.json").json()
        assert index["version"] == VERSION and index["exact"] and index["server_version"] == VERSION and index["hint"] is None
        by_path = {item["path"]: item for item in index["files"]}
        assert set(by_path) == set(files) - {"linux-amd64/secret.txt"}
        machine = by_path["linux-amd64/artifact/agent-machine"]
        assert (machine["kind"], machine["component"], machine["platform"], machine["size"]) == ("binary", "agent-machine", "linux-amd64", 6)
        assert machine["sha256"] == hashlib.sha256(b"binary").hexdigest()
        assert by_path["android/agent-proxy-0.4.0+abc1234.apk"]["kind"] == "android"
        # /android is the app of the served build, to type on a phone.
        short = client.get("/android", follow_redirects=False)
        assert short.status_code == 302 and short.headers["location"] == by_path["android/agent-proxy-0.4.0+abc1234.apk"]["url"]
        assert client.get("/downloads/android.apk").content == b"apk"
        got = client.get(machine["url"])
        assert got.status_code == 200 and got.content == b"binary"
        for path in (f"/downloads/{VERSION}/linux-amd64/secret.txt", "/downloads/0.3.0/linux-amd64/old.tar.gz",
                     f"/downloads/{VERSION}/../0.3.0/SHA256SUMS", f"/downloads/{VERSION}/SHA256SUMS"):
            assert client.get(path).status_code == 404, path


def test_downloads_are_public_even_with_accounts(tmp_path):
    build(tmp_path / "dist", VERSION, {"linux-amd64/artifact/agent": b"x"})
    settings = org_settings(tmp_path)
    settings.dist_dir = tmp_path / "dist"
    with TestClient(create_app(settings), base_url="http://192.168.1.5:9999", client=("192.168.1.9", 5000)) as client:
        assert client.get("/api/status").status_code == 401
        assert client.get("/downloads/index.json").json()["files"][0]["path"] == "linux-amd64/artifact/agent"
        assert client.get(f"/downloads/{VERSION}/linux-amd64/artifact/agent").content == b"x"


def test_without_its_own_build_the_server_offers_one_of_the_same_release_or_says_how_to_build(tmp_path):
    with TestClient(app_with(tmp_path), **LOCAL) as client:
        assert client.get("/android", follow_redirects=False).status_code == 404
        empty = client.get("/downloads/index.json").json()
        assert empty["version"] is None and empty["files"] == [] and "build-dist" in empty["hint"]
        build(tmp_path / "dist", "0.4.0+fff0000.dirty", {"linux-amd64/artifact/agent": b"x"})
        build(tmp_path / "dist", "0.3.9", {"linux-amd64/artifact/agent": b"y"})
        near = client.get("/downloads/index.json").json()
        assert near["version"] == "0.4.0+fff0000.dirty" and near["exact"] is False


@pytest.mark.parametrize("path, expected", [
    ("darwin-arm64/agent-machine-1.0.0-darwin-arm64.pkg", ("darwin", "arm64", "agent-machine", "pkg")),
    ("linux-armv7/agent-1.0.0-linux-armv7.pkg.tar.gz", ("linux", "armv7", "agent", "arch")),
    ("linux-amd64/agent-1.0.0-linux-amd64.apk", ("linux", "amd64", "agent", "apk")),
    ("windows-386/artifact/agent-proxy.exe", ("windows", "386", "agent", "binary")),
    ("windows-386/artifact/install.ps1", ("windows", "386", None, "installer")),
])
def test_files_are_described_by_where_tools_dist_puts_them(path, expected):
    item = describe(path)
    assert (item["os"], item["arch"], item["component"], item["kind"]) == expected
