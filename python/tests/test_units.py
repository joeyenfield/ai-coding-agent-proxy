"""The Go programs' unit tests (client/internal/... and machine/internal/..._test.go), for the Python
agent, agent-proxy and agent-machine: the pieces that don't need a real agent-server."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Tuple

import httpx
import pytest

from agent_py import common, login, oauth, update
import agent_py.client as client_main
from agent_py.client import launcher, localproxy
from agent_py.client import settings as client_settings
import agent_py.machine as machine_main
from agent_py.machine import mcp, presence
from agent_py.machine import settings as machine_settings

Seen = List[Tuple[str, str, Dict[str, str], bytes]]


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal inheritance")
def test_plain_agents_keep_ctrl_c_and_restore_the_launchers_handler(tmp_path):
    previous = signal.getsignal(signal.SIGINT)
    # With inherited SIG_IGN the child survives this signal and exits with 99 instead.
    child = "import os, signal; os.kill(os.getpid(), signal.SIGINT); os._exit(99)"
    assert launcher.run_plain([sys.executable, "-c", child], str(tmp_path), dict(os.environ)) == 1
    assert signal.getsignal(signal.SIGINT) == previous


@contextmanager
def fake_server(answer: Callable[[str, str, Dict[str, str], bytes], Tuple[int, Any]]) -> Iterator[Tuple[str, Seen]]:
    """A stand-in agent-server on a free port: answer(method, path, headers, body) gives (status, JSON or bytes).
    Every request is kept in seen, its body read whole (chunked included), its headers lowercased."""
    seen: Seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def handle_any(self) -> None:
            headers = {name.lower(): value for name, value in self.headers.items()}
            if "chunked" in headers.get("transfer-encoding", "").lower():
                body = b""
                while True:
                    size = int(self.rfile.readline().strip() or b"0", 16)
                    if not size:
                        self.rfile.readline()
                        break
                    body += self.rfile.read(size)
                    self.rfile.readline()
            else:
                body = self.rfile.read(int(headers.get("content-length") or 0))
            seen.append((self.command, self.path, headers, body))
            status, value = answer(self.command, self.path, headers, body)
            data = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PATCH = do_DELETE = do_PUT = handle_any

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()


# client/internal/process/lookpath_test.go

@pytest.mark.skipif(sys.platform != "linux", reason="WSL is Linux")
def test_windows_drives_in_wsl_are_skipped_when_looking_for_a_program(tmp_path, monkeypatch):
    windows, linux = tmp_path / "c" / "npm", tmp_path / "bin"
    for folder in (windows, linux):
        folder.mkdir(parents=True)
    (windows / "opencode").write_text("#!/bin/sh\n")
    (windows / "opencode").chmod(0o755)
    mounts = tmp_path / "mounts"
    mounts.write_text(f"proc /proc proc rw 0 0\nC:\\134 {tmp_path / 'c'} 9p rw,noatime,aname=drvfs;path=C:\\;uid=1000 0 0\n")
    monkeypatch.setattr(common, "MOUNTS_FILE", str(mounts))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", f"{linux}{os.pathsep}{windows}")
    assert common.look_path("opencode") is None
    assert common.environment() == "wsl"
    # A link in a Linux folder to it doesn't count either.
    (linux / "opencode").symlink_to(windows / "opencode")
    assert common.look_path("opencode") is None
    # A Linux build is found.
    (linux / "opencode").unlink()
    (linux / "opencode").write_text("#!/bin/sh\n")
    (linux / "opencode").chmod(0o755)
    assert common.look_path("opencode") == str(linux / "opencode")
    # Outside WSL (no drvfs mounts) it's plain shutil.which.
    mounts.write_text("proc /proc proc rw 0 0\n")
    (linux / "opencode").unlink()
    assert common.look_path("opencode") == str(windows / "opencode")
    assert common.environment() == "linux"


def test_an_agent_in_its_own_install_folder_is_found(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.chdir(empty)
    folder = home / ".opencode" / "bin"
    folder.mkdir(parents=True)
    name = "fake-agent" + (".exe" if sys.platform == "win32" else "")
    (folder / name).write_text("#!/bin/sh\n")
    (folder / name).chmod(0o755)
    found = common.look_path("fake-agent")
    assert found and Path(found).parent == folder
    assert common.look_path("not-an-agent") is None


# client/internal/launcher/launcher_test.go

def test_arguments_go_to_the_launcher_or_the_agent():
    options = launcher.parse_args(["qwen", "proj", "--model", "qwen3.6:35b", "--yolo", "--trace", "--share=team", "--team", "core", "--", "--model", "x"])
    assert (options.agent, options.project, options.model, options.trace) == ("qwen", "proj", "qwen3.6:35b", True)
    assert options.extra == ["--yolo", "--model", "x"]
    assert options.sharing() == {"visibility": "team", "team": "core", "access": "view"}
    options = launcher.parse_args(["claude", "-m=sonnet", "--control", "--let-others-type", "--no-remote"])
    assert (options.project, options.model, options.control, options.sharing()["access"], options.extra) == (".", "sonnet", True, "control", [])
    options = launcher.parse_args(["qwen", "--mcp", "desk/github, desk/docs", "--mcp=laptop"])
    assert options.mcp == ["desk/github", "desk/docs", "laptop"] and options.extra == []
    with pytest.raises(launcher.UsageError):
        launcher.parse_args(["qwen", "--share", "everyone"])
    with pytest.raises(launcher.UsageError):
        launcher.parse_args(["qwen", "--model"])


def test_a_shells_one_argument_is_its_folder():
    # As agent-machine runs it for a terminal from the console: the flag before or after the folder.
    for args in (["--shell", "/work/api", "--headless", "--launch-id", "l9"], ["/work/api", "--shell", "--headless"]):
        options = launcher.parse_args(args)
        assert options.shell and options.agent == "" and options.project == "/work/api" and options.headless, args
    options = launcher.parse_args(["--shell"])
    assert options.project == "." and options.agent == ""
    with pytest.raises(launcher.UsageError):
        launcher.parse_args(["--shell", "/a", "/b"])
    assert launcher.shell_command()


def test_environment_overrides_replace_inherited_values(monkeypatch):
    monkeypatch.setenv("Https_Proxy", "old")
    monkeypatch.setenv("KEEP", "1")
    merged = launcher.merge_env({"HTTPS_PROXY": "new", "AI_PROXY_SESSION": "s1"})
    assert merged["HTTPS_PROXY"] == "new" and merged["KEEP"] == "1" and merged["AI_PROXY_SESSION"] == "s1"
    # Windows names are case-insensitive, so the inherited spelling goes.
    assert any(name == "Https_Proxy" for name in merged) == (sys.platform != "win32")


def test_ensure_container_starts_the_backend_and_waits_until_it_serves(monkeypatch):
    monkeypatch.setattr(launcher, "CONTAINER_POLL", 0.01)
    state = {"polls": 0, "started": False}

    def answer(method, path, headers, body):
        if method == "POST" and path == "/api/containers/box/start":
            state["started"] = True
            return 200, {}
        if method == "POST" and path == "/api/containers/other/start":
            return 409, {"detail": "strata-iq3 uses that GPU."}
        if path == "/api/containers/box":
            state["polls"] += 1
            return 200, {"ready": state["started"] and state["polls"] > 3, "phase": "loading" if state["started"] else "stopped", "engine_label": "Ollama"}
        if path == "/api/containers/other":
            return 200, {"ready": False, "phase": "stopped", "engine_label": "Strata"}
        return 404, {"detail": "Not found"}

    with fake_server(answer) as (url, _):
        launcher.ensure_container(url, "box", False, 60)
        assert state["started"]
        with pytest.raises(launcher.LaunchError, match="--switch"):
            launcher.ensure_container(url, "other", False, 60)
    assert launcher.duration(4 * 3600) == "4h0m0s" and launcher.duration(90) == "1m30s" and launcher.duration(5) == "5s"


def test_version_notices_say_what_to_update():
    same = {"version": "0.4.0"}
    assert launcher.version_notices("0.4.0", same, same) == []
    lines = launcher.version_notices("0.4.0", {}, {"version": "0.5.0"})
    assert len(lines) == 2 and "agent proxy stop" in lines[0] and "agent update" in lines[1]
    # A build without a version, or a server that doesn't say, gives no notice about the server.
    assert launcher.version_notices("dev", {}, {"version": "0.5.0"}) == []
    assert launcher.version_notices("0.4.0", same, None) == []


def test_an_unexpected_answer_from_the_server_says_so():
    with fake_server(lambda *request: (200, b"<html>a captive portal</html>")) as (url, _):
        with pytest.raises(launcher.LaunchError, match="GET /api/agents: unexpected answer"):
            launcher.API(url).call("GET", "/api/agents")
    with fake_server(lambda *request: (200, b"")) as (url, _):
        assert launcher.API(url).call("PATCH", "/api/sessions/s1", {"ended": True}) is None


# machine/internal/presence/version_test.go

def test_the_machines_version_notice_says_what_to_update():
    assert presence.version_notice("0.4.0", "0.4.0") == ""
    assert "agent-machine update" in presence.version_notice("0.4.0", "0.5.0")
    assert presence.version_notice("dev", "0.5.0") == "" and presence.version_notice("0.4.0", "") == ""


# client/internal/localproxy/proxy_test.go and version_test.go

def proxy_for(url: str, tmp_path: Path) -> localproxy.AgentProxy:
    upstream = common.Upstream(lambda: tmp_path / "credentials.json", fixed=url)
    return localproxy.AgentProxy(upstream, 18183)


def through(proxy: localproxy.AgentProxy, method: str, path: str, **kwargs: Any) -> httpx.Response:
    async def call() -> httpx.Response:
        transport = httpx.ASGITransport(app=proxy, client=("127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8184") as http:
            # Only the headers each test gives, as an agent sends them.
            http.headers.clear()
            response = await http.request(method, path, **kwargs)
        await proxy.aclose()
        return response

    return asyncio.run(call())


def test_agent_traffic_is_signed_and_preserves_vendor_authorization(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PROXY_TOKEN", "machine-token")
    with fake_server(lambda *request: (200, {"ok": True})) as (url, seen):
        async def body():
            yield b'{"stream":'
            yield b"true}"

        answer = through(proxy_for(url, tmp_path), "POST", "/session/s1/v1/chat/completions?x=1", content=body(),
                         headers={"Authorization": "Bearer agent-key", "Cookie": "secret=1", "User-Agent": "claude-cli/2", "X-Forwarded-For": "10.0.0.9"})
        assert answer.status_code == 200 and answer.json() == {"ok": True}
        method, path, headers, sent = seen[-1]
        assert (method, path, sent) == ("POST", "/session/s1/v1/chat/completions?x=1", b'{"stream":true}')
        # Vendor Authorization remains independent of the OAuth credential identifying this machine.
        assert headers["authorization"] == "Bearer agent-key" and headers["x-ai-proxy-token"] == "machine-token" and "cookie" not in headers
        # The agent's own User-Agent, this hop's version, nothing of httpx's or of an earlier hop.
        assert headers["user-agent"] == "claude-cli/2" and headers["x-ai-proxy-version"] == common.VERSION
        assert "x-forwarded-for" not in headers and headers.get("accept-encoding") == "identity"
        # A body of known length keeps it; a GET has none.
        answer = through(proxy_for(url, tmp_path), "POST", "/v1/messages", content=b"{}", headers={"Accept-Encoding": "gzip"})
        assert seen[-1][2]["content-length"] == "2" and "transfer-encoding" not in seen[-1][2] and seen[-1][2]["accept-encoding"] == "gzip"
        through(proxy_for(url, tmp_path), "GET", "/v1/models")
        assert seen[-1][3] == b"" and "transfer-encoding" not in seen[-1][2] and "user-agent" not in seen[-1][2]


def test_launcher_calls_are_signed_and_the_health_is_the_go_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PROXY_TOKEN", "machine-token")
    with fake_server(lambda *request: (200, {"running": True, "url": "http://server:8183"})) as (url, seen):
        info = through(proxy_for(url, tmp_path), "GET", "/api/intercept").json()
        assert info["url"] == "http://127.0.0.1:18183"
        assert seen[-1][2]["x-ai-proxy-token"] == "machine-token" and "accept-encoding" not in seen[-1][2]
        assert through(proxy_for(url, tmp_path), "GET", "/api/users").status_code == 404
        health = through(proxy_for(url, tmp_path), "GET", "/health").json()
    assert set(health) == {"status", "role", "server", "user", "intercept_port", "version", "protocol"}


# client/internal/login/login_test.go

def test_login_saves_a_token_and_logout_revokes_it(tmp_path, monkeypatch, capsys):
    credentials = tmp_path / "credentials.json"
    monkeypatch.delenv("AI_PROXY_TOKEN", raising=False)
    # A coding agent working on this repository runs inside a launch whose setup code
    # must not steer this login onto the setup-code path (as in the Go login test).
    monkeypatch.delenv("AI_PROXY_SETUP_CODE", raising=False)
    monkeypatch.setattr(oauth.time, "sleep", lambda wait: None)
    revoked: List[str] = []

    def answer(method, path, headers, body):
        if path == "/api/auth/status":
            return 200, {"required": True, "issuer": url, "client_id": "mesh-cli"}
        if path == "/.well-known/openid-configuration":
            return 200, {"issuer": url, **{key + "_endpoint": url + "/" + endpoint for key, endpoint in (
                ("device_authorization", "device_authorization"), ("token", "token"), ("userinfo", "userinfo"), ("revocation", "revoke"),
            )}}
        if path == "/device_authorization":
            return 200, {"device_code": "device", "user_code": "CODE", "verification_uri": url + "/verify", "expires_in": 120, "interval": 1}
        if path == "/token":
            return 200, {"access_token": "oauth-access", "refresh_token": "oauth-refresh", "expires_in": 3600, "token_type": "Bearer"}
        if path == "/userinfo":
            assert headers["authorization"] == "Bearer oauth-access"
            return 200, {"sub": "u1", "preferred_username": "alice"}
        if method == "POST" and path == "/revoke":
            from urllib.parse import parse_qs
            revoked.append(parse_qs(body.decode())["token"][0])
            return 200, {}
        return 404, {}

    def agent_login(*args: str) -> int:
        return login.login(list(args), program="agent", credentials_path=lambda: credentials, default_server="http://127.0.0.1:1",
                           label="agent-proxy", done="this machine's agent-proxy uses it",
                           signed_in="This machine's agent-proxy sends everything there from now on (agent logout to stop).")

    with fake_server(answer) as (url, _):
        assert agent_login(url) == 0
        assert f"Signed in to {url} as alice." in capsys.readouterr().out
        upstream = common.Upstream(lambda: credentials, use_env=False)
        assert upstream.server() == url and upstream.token(url) == "oauth-access"
        assert login.logout([], program="agent", credentials_path=lambda: credentials) == 0 and revoked == ["oauth-refresh"]
        with pytest.raises(oauth.OAuthError, match="sign-in required"):
            upstream.token(url)
        assert agent_login("--user", "alice", url) == 2
    with fake_server(lambda *request: (200, {"required": False})) as (url, _):
        assert agent_login(url) == 1


def test_agent_machine_keeps_its_own_oauth_sign_in(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AI_PROXY_MACHINE_HOME", str(tmp_path / "machine-home"))
    monkeypatch.setenv("AI_PROXY_TOKEN", "client-only")
    monkeypatch.setenv("AI_PROXY_MACHINE_TOKEN", "machine-only")
    assert machine_main.main(["login", "--user", "bob"]) == 2
    upstream = common.Upstream(lambda: tmp_path / "machine-home" / "credentials.json", use_env=False)
    assert upstream.token("https://server.example") == "machine-only"


# client/internal/update/update_test.go

def test_an_old_server_without_downloads_says_so():
    with fake_server(lambda *request: (404, {"detail": "Not Found"})) as (url, _):
        with pytest.raises(update.UpdateError, match="doesn't offer downloads"):
            update.fetch(url, "agent")


def test_agent_update_takes_the_build_of_the_server_its_agent_proxy_uses(monkeypatch):
    monkeypatch.setenv("AI_PROXY_URL", "http://from-env:9999")
    monkeypatch.setattr(common, "health", lambda base, timeout=0.75: {"role": "agent-proxy", "server": "http://proxied:9999"})
    assert client_main.update_server() == "http://proxied:9999"
    assert client_main.update_server("http://chosen:1/") == "http://chosen:1"
    monkeypatch.setattr(common, "health", lambda base, timeout=0.75: None)
    assert client_main.update_server() == "http://from-env:9999"


# machine/internal/presence/fleet_test.go, folders_test.go: the machine's own checks

def test_only_folders_that_exist_inside_the_roots_count(tmp_path):
    root = tmp_path / "work"
    (root / "api").mkdir(parents=True)
    machine = machine_settings.Machine(id="m1", roots=[str(root), str(tmp_path / "gone")])
    assert machine.within(str(root / "api"))[1]
    assert not machine.within(str(root / "not-yet"))[1]
    assert not machine.within(str(tmp_path / "gone"))[1]
    assert not machine.within(str(tmp_path))[1]
    # A launch still makes a new project folder below a root.
    machine.launches = True
    made = presence.check({"project": str(root / "new" / "app")}, machine)
    assert Path(made).is_dir()
    with pytest.raises(presence.Refused):
        presence.check({"project": str(tmp_path / "elsewhere")}, machine)


def test_folders_are_listed_in_order_without_files(tmp_path):
    root = tmp_path / "work"
    for name in ("beta", "Alpha", ".hidden", "gamma"):
        (root / name).mkdir(parents=True)
    (root / "notes.txt").write_text("x")
    machine = machine_settings.Machine(id="m1", launches=True, roots=[str(root)])
    answer = presence.folders({"request": "r1", "path": str(root)}, machine)
    assert answer["ok"] and [item["name"] for item in answer["folders"]] == ["Alpha", "beta", "gamma"]
    assert answer["parent"] is None and not answer["truncated"]


# machine/internal/presence/files_test.go: the file viewer's own checks

def test_files_are_listed_and_read_only_inside_the_roots(tmp_path):
    from agent_py.machine import files

    root, outside = tmp_path / "work", tmp_path / "outside"
    for path, data in {"README.md": b"# Hello\n", "b.txt": b"b", "src/main.go": b"package main\n", ".env": b"SECRET=1",
                       ".git/config": b"[core]", "image.bin": b"\0\1\2"}.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(data)
    outside.mkdir()
    (outside / "secret.txt").write_text("outside")
    try:
        os.symlink(outside / "secret.txt", root / "out.txt")
        linked = True
    except OSError:
        linked = False
    machine = machine_settings.Machine(id="m1", files=True, roots=[str(root)])

    answer = files.answer({"request": "r1", "op": "list", "path": str(root)}, machine)
    assert answer["ok"] and answer["type"] == "files_result" and answer["request"] == "r1"
    # Folders first, then files, by name; nothing hidden and no link out of the root.
    assert [f"{item['kind']}:{item['name']}" for item in answer["entries"]] == ["folder:src", "file:b.txt", "file:image.bin", "file:README.md"]
    assert answer["parent"] is None
    inside = files.answer({"op": "list", "path": str(root / "src")}, machine)
    assert [item["name"] for item in inside["entries"]] == ["main.go"] and inside["parent"] == str(root)

    read = files.answer({"op": "read", "path": str(root / "README.md")}, machine)
    assert read["ok"] and read["content"] == "# Hello\n" and read["binary"] is False and read["truncated"] is False and read["size"] == 8
    binary = files.answer({"op": "read", "path": str(root / "image.bin")}, machine)
    assert binary["ok"] and binary["binary"] is True and "content" not in binary

    refused = [("read", root / ".env"), ("read", root / ".git" / "config"), ("list", root / ".git"), ("read", outside / "secret.txt"),
               ("read", root / ".." / "outside" / "secret.txt"), ("list", outside), ("read", root / "src"), ("read", "relative.txt"),
               ("list", ""), ("delete", root / "b.txt")]
    if linked:
        refused.append(("read", root / "out.txt"))
    for op, path in refused:
        answer = files.answer({"op": op, "path": str(path)}, machine)
        assert answer["ok"] is False and answer["error"], (op, path, answer)

    # Launches and commands don't share files: only the files toggle does.
    machine = machine_settings.Machine(id="m1", launches=True, commands=True, roots=[str(root)])
    answer = files.answer({"op": "list", "path": str(root)}, machine)
    assert answer["ok"] is False and "files on" in answer["error"]


def test_files_reads_at_most_the_limit(tmp_path):
    from agent_py.machine import files

    # A two-byte character straddles the limit: it's dropped, and the rest is still text.
    (tmp_path / "long.txt").write_bytes(b"a" * (files.FILE_READ_LIMIT - 1) + "é".encode() + b"tail")
    machine = machine_settings.Machine(id="m1", files=True, roots=[str(tmp_path)])
    answer = files.answer({"op": "read", "path": str(tmp_path / "long.txt")}, machine)
    assert answer["ok"] and answer["truncated"] and answer["binary"] is False and len(answer["content"]) == files.FILE_READ_LIMIT - 1


# machine/internal/mcp/bridge_test.go and command/mcp_test.go

def test_a_broken_mcp_json_shares_nothing_and_says_why(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AI_PROXY_MACHINE_HOME", str(tmp_path))
    (tmp_path / "mcp.json").write_text("{not json")
    with pytest.raises(ValueError, match="mcp.json"):
        mcp.load()
    assert mcp.Bridge().describe() is None
    assert machine_main.main(["mcp"]) == 1
    assert "mcp.json" in capsys.readouterr().err


def test_terminal_size_is_this_terminals_not_the_environments(monkeypatch):
    from agent_py.client import terminal

    monkeypatch.setenv("COLUMNS", "3")
    monkeypatch.setenv("LINES", "3")
    cols, rows = terminal.size()
    assert (cols, rows) != (3, 3)


def test_the_proxy_names_itself_by_the_program_that_runs_it(monkeypatch):
    monkeypatch.setattr(client_settings, "PROGRAM", "agent")
    monkeypatch.setattr(localproxy, "main", lambda name, args: 0)
    monkeypatch.setattr(sys, "argv", ["agent-proxy"])
    assert client_main.proxy_main() == 0 and client_settings.PROGRAM == "agent-proxy"


def test_a_cmd_agent_runs_in_comspec_in_the_conpty(monkeypatch, tmp_path):
    """client/internal/terminal TestWindowsCommand: npm's .cmd shims run in %COMSPEC% /d /s /c."""
    from agent_py.client import terminal

    monkeypatch.setenv("COMSPEC", "C:/Windows/system32/cmd.exe")
    program, arguments = terminal.windows_command(["C:/Program Files/npm/opencode.cmd", "--model", "a b"])
    assert program == "C:/Windows/system32/cmd.exe"
    assert arguments == '/d /s /c ""C:/Program Files/npm/opencode.cmd" --model "a b""'
    assert terminal.windows_command(["C:/bin/claude.exe"]) == ("C:/bin/claude.exe", "")
    if sys.platform != "win32":
        return
    # A real .cmd in a real ConPTY, with a space in its folder as npm's often have.
    folder = tmp_path / "npm shims"
    folder.mkdir()
    shim = folder / "agent.cmd"
    shim.write_text("@echo off\r\necho shim says %1\r\nexit /b 3\r\n")
    monkeypatch.delenv("COMSPEC")
    child = terminal.WindowsChild([str(shim), "hello"], str(tmp_path), dict(os.environ), 80, 24)
    output = b""
    while b"shim says hello" not in output:
        chunk = child.read()
        if not chunk:
            break
        output += chunk
    assert b"shim says hello" in output, output
    assert child.wait() == 3


def test_an_agents_websocket_is_signed_and_passed_through(tmp_path, monkeypatch):
    """client/internal/localproxy: agents' routes pass WebSocket upgrades through, as Go's
    ReverseProxy does: the agent's own headers go on, cookies and origin don't, the machine signs."""
    from starlette.testclient import TestClient
    from websockets.sync.server import serve

    seen: Dict[str, Any] = {}

    def handler(connection) -> None:
        seen["path"] = connection.request.path
        seen["headers"] = {name.lower(): value for name, value in connection.request.headers.raw_items()}
        seen["user_agents"] = [value for name, value in connection.request.headers.raw_items() if name.lower() == "user-agent"]
        for message in connection:
            connection.send(f"echo:{message}")

    with serve(handler, "127.0.0.1", 0, subprotocols=["realtime"]) as upstream:
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        port = upstream.socket.getsockname()[1]
        proxy = proxy_for(f"http://127.0.0.1:{port}", tmp_path)
        monkeypatch.setattr(proxy.upstream, "token", lambda server: "machine-token")
        client = TestClient(proxy, base_url="http://127.0.0.1:8184")
        local_headers = {"host": "127.0.0.1:8184", "authorization": "Bearer agent-key", "cookie": "secret=1", "user-agent": "some-agent/1.0"}
        with client.websocket_connect("/v1/realtime?model=m", headers=local_headers, subprotocols=["realtime"]) as socket:
            # The server's chosen subprotocol comes back through the proxy.
            assert socket.accepted_subprotocol == "realtime"
            socket.send_text("hi")
            assert socket.receive_text() == "echo:hi"
        assert seen["path"] == "/v1/realtime?model=m"
        assert seen["headers"]["authorization"] == "Bearer agent-key"
        assert seen["headers"][common.TOKEN_HEADER.lower()] == "machine-token"
        assert "cookie" not in seen["headers"] and "origin" not in seen["headers"]
        # The agent keeps its own User-Agent, once, as with Go's rewriteAgent.
        assert seen["user_agents"] == ["some-agent/1.0"]
        # Another site's page is refused before anything is dialled.
        seen.clear()
        with client.websocket_connect("/v1/realtime", headers={"host": "127.0.0.1:8184", "origin": "https://evil.test"}) as socket:
            with pytest.raises(Exception) as closed:
                socket.receive_text()
        assert getattr(closed.value, "code", None) == 4403 and not seen
        upstream.shutdown()


def test_mcp_routing_is_released_when_a_full_stream_closes():
    """machine/internal/mcp bridge.go: route selects on out.closed and s.done, so a reply blocked
    on a stream whose consumer went away never wedges the session's routing."""

    async def scenario() -> None:
        session = mcp.Session("s1", "server", None)  # type: ignore[arg-type]
        out = mcp.Stream(True)
        session.posts.append(out)
        for number in range(64):
            out.queue.put_nowait(b"filler")
        routing = asyncio.ensure_future(session.route(b'{"jsonrpc":"2.0","method":"notify"}'))
        await asyncio.sleep(0.05)
        assert not routing.done()  # held: the queue is full, as Go's channel would be
        session.forget(out, [])
        await asyncio.wait_for(routing, 2)  # released at once by the close, no polling
        assert out.queue.qsize() == 64 and session.backlog == []  # dropped, as Go drops it
        # A session that ends releases a blocked route too.
        other = mcp.Stream(True)
        session.posts.append(other)
        for number in range(64):
            other.queue.put_nowait(b"filler")
        blocked = asyncio.ensure_future(session.route(b'{"jsonrpc":"2.0","method":"notify"}'))
        await asyncio.sleep(0.05)
        session.done.set()
        await asyncio.wait_for(blocked, 2)

    asyncio.run(scenario())


def test_presence_renews_its_token_over_the_open_connection(tmp_path, monkeypatch):
    """machine/internal/presence: TestPresenceRenewsItsTokenOverTheOpenConnection."""
    from websockets.asyncio.server import serve

    monkeypatch.setattr(presence, "RENEW_EVERY", 0.05)
    issued = iter(f"access-{number}" for number in range(1, 1000))

    async def scenario() -> None:
        dialed: List[str] = []
        renewed: asyncio.Queue = asyncio.Queue()

        async def handler(connection) -> None:
            dialed.append(connection.request.headers.get(common.TOKEN_HEADER, ""))
            async for raw in connection:
                message = json.loads(raw)
                if message["type"] == "hello":
                    await connection.send(json.dumps({"type": "welcome"}))
                elif message["type"] == "token":
                    await renewed.put(message["token"])

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            machine = presence.Presence(common.Upstream(credentials_path=lambda: tmp_path / "credentials.json", fixed=f"http://127.0.0.1:{port}", use_env=False))
            current = {"token": next(issued)}
            # Every check after the first finds a newer token, as one in its last 30 seconds would be refreshed.
            monkeypatch.setattr(machine.upstream, "token", lambda server: current["token"])
            monkeypatch.setattr(machine, "describe", lambda server, kind: {"type": kind, "id": "m1"})
            monkeypatch.setattr(machine, "check_version", lambda server: None)
            stop = asyncio.Event()
            running = asyncio.ensure_future(machine.run(stop))
            try:
                for _ in range(100):
                    if dialed:
                        break
                    await asyncio.sleep(0.05)
                assert dialed == ["access-1"]
                await asyncio.sleep(0.1)
                current["token"] = next(issued)
                assert await asyncio.wait_for(renewed.get(), 10) == "access-2"
                await asyncio.sleep(0.2)
                # Renewing doesn't reconnect.
                assert dialed == ["access-1"]
            finally:
                stop.set()
                await asyncio.wait_for(running, 10)

    asyncio.run(scenario())


def test_a_tunnel_renews_its_token_while_it_carries_a_reply(tmp_path, monkeypatch):
    """machine/internal/presence: TestATunnelRenewsItsTokenWhileItCarriesAReply."""
    from websockets.asyncio.server import serve

    monkeypatch.setattr(presence, "RENEW_EVERY", 0.05)

    async def scenario() -> None:
        renewed: asyncio.Queue = asyncio.Queue()

        async def model_server(reader, writer) -> None:
            await reader.read(1)

        async def handler(connection) -> None:
            async for message in connection:
                if isinstance(message, str) and json.loads(message).get("type") == "token":
                    await renewed.put(json.loads(message)["token"])
                    return

        model = await asyncio.start_server(model_server, "127.0.0.1", 0)
        port = model.sockets[0].getsockname()[1]
        async with serve(handler, "127.0.0.1", 0) as server:
            url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            machine = presence.Presence(common.Upstream(credentials_path=lambda: tmp_path / "credentials.json", fixed=url, use_env=False))
            current = {"token": "access-1"}
            monkeypatch.setattr(machine.upstream, "token", lambda server: current["token"])
            machine.tunnel_ports = {port}
            carrying = asyncio.ensure_future(machine.tunnel(url, {"type": "tunnel", "tunnel": "t1", "port": port}))
            try:
                await asyncio.sleep(0.1)
                current["token"] = "access-2"
                assert await asyncio.wait_for(renewed.get(), 10) == "access-2"
            finally:
                carrying.cancel()
                model.close()

    asyncio.run(scenario())


def test_presence_resends_an_unchanged_token_and_survives_a_failing_sign_in(tmp_path, monkeypatch):
    """The current token goes again every RESEND_EVERY, in case the server missed it; a token check
    that fails (a lock, a refresh) leaves the hello's agents empty instead of ending the connection."""
    from websockets.asyncio.server import serve

    monkeypatch.setattr(presence, "RENEW_EVERY", 0.05)
    monkeypatch.setattr(presence, "RESEND_EVERY", 0.1)
    # The real describe() runs: it reads (and makes) the machine's folder, which must be this test's.
    monkeypatch.setenv("AI_PROXY_MACHINE_HOME", str(tmp_path / "machine"))

    async def scenario() -> None:
        hellos: List[Dict[str, Any]] = []
        renewed: asyncio.Queue = asyncio.Queue()

        async def handler(connection) -> None:
            async for raw in connection:
                message = json.loads(raw)
                if message["type"] == "hello":
                    hellos.append(message)
                    await connection.send(json.dumps({"type": "welcome"}))
                elif message["type"] == "token":
                    await renewed.put(message["token"])

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            machine = presence.Presence(common.Upstream(credentials_path=lambda: tmp_path / "credentials.json", fixed=f"http://127.0.0.1:{port}", use_env=False))
            calls = {"count": 0}

            def token(server: str) -> str:
                calls["count"] += 1
                # The hello's agent listing is the second call (after dial's): it fails.
                if calls["count"] == 2:
                    raise oauth.OAuthError("credentials are locked")
                return "access-1"

            monkeypatch.setattr(machine.upstream, "token", token)
            monkeypatch.setattr(machine, "detect", lambda: [])
            monkeypatch.setattr(machine, "check_version", lambda server: None)
            stop = asyncio.Event()
            running = asyncio.ensure_future(machine.run(stop))
            try:
                assert await asyncio.wait_for(renewed.get(), 10) == "access-1"
                assert len(hellos) == 1 and hellos[0]["agents"] == []
            finally:
                stop.set()
                await asyncio.wait_for(running, 10)

    asyncio.run(scenario())


def test_reconnect_delays_are_jittered():
    delays = {presence.jitter(10) for _ in range(50)}
    assert all(5 <= delay <= 15 for delay in delays) and len(delays) > 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_a_process_we_may_not_signal_is_not_ours(monkeypatch):
    """As process.Alive in Go: on Android a stale status file's id is often another app's process."""
    assert machine_settings.alive(os.getpid())

    def refused(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(machine_settings.os, "kill", refused)
    assert not machine_settings.alive(12345)


def test_with_color_names_a_terminal_only_when_the_environment_doesnt():
    """client/internal/terminal TestWithColorNamesATerminalOnlyWhenTheEnvironmentDoesnt."""
    from agent_py.client.terminal import with_color

    assert with_color({"HOME": "/h"}) == {"HOME": "/h", "TERM": "xterm-256color", "COLORTERM": "truecolor"}
    assert with_color({"TERM": "dumb", "COLORTERM": "24bit"}) == {"TERM": "xterm-256color", "COLORTERM": "24bit"}
    assert with_color({"TERM": "screen"}) == {"TERM": "screen"}
