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

from agent_py import common, login, update
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


def test_agent_traffic_streams_through_without_the_machines_token(tmp_path, monkeypatch):
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
        # The agent's own Authorization goes through (hosted APIs need it); the machine's token and cookies don't.
        assert headers["authorization"] == "Bearer agent-key" and "x-ai-proxy-token" not in headers and "cookie" not in headers
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
    monkeypatch.setenv("AI_PROXY_PASSWORD_INPUT", "correct horse")
    revoked: List[str] = []

    def answer(method, path, headers, body):
        if path == "/api/auth/status":
            return 200, {"required": True}
        if path == "/api/auth/token":
            sent = json.loads(body)
            if (sent["username"], sent["password"]) != ("alice", "correct horse"):
                return 401, {"detail": "That username and password don't match."}
            return 200, {"id": "t1", "token": "aip_secret", "user": "alice"}
        if method == "DELETE" and path == "/api/auth/tokens/t1":
            revoked.append(headers.get("x-ai-proxy-token", ""))
            return 200, {}
        return 404, {}

    def agent_login(*args: str) -> int:
        return login.login(list(args), program="agent", credentials_path=lambda: credentials, default_server="http://127.0.0.1:1",
                           label="agent-proxy", done="this machine's agent-proxy uses it",
                           signed_in="This machine's agent-proxy sends everything there from now on (agent logout to stop).")

    with fake_server(answer) as (url, _):
        assert agent_login(url, "--user", "alice") == 0
        assert f"Signed in to {url} as alice. This machine's agent-proxy sends everything there from now on (agent logout to stop)." in capsys.readouterr().out
        upstream = common.Upstream(lambda: credentials, use_env=False)
        assert upstream.server() == url and upstream.token(url) == "aip_secret"
        assert login.logout([], program="agent", credentials_path=lambda: credentials) == 0 and revoked == ["aip_secret"]
        assert upstream.token(url) == ""
        monkeypatch.setenv("AI_PROXY_PASSWORD_INPUT", "wrong")
        assert agent_login("--user", "alice", url) == 1
    with fake_server(lambda *request: (200, {"required": False})) as (url, _):
        assert agent_login(url) == 0
        assert f"{url} doesn't use accounts, so there's no sign-in; this machine's agent-proxy uses it from now on." in capsys.readouterr().out


def test_agent_machines_login_says_how_to_start_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AI_PROXY_MACHINE_HOME", str(tmp_path / "machine-home"))
    monkeypatch.setenv("AI_PROXY_PASSWORD_INPUT", "pw")
    token = {"id": "t2", "token": "aip_m", "user": "bob"}
    with fake_server(lambda method, path, *rest: (200, {"required": True} if path == "/api/auth/status" else token)) as (url, _):
        assert machine_main.main(["login", url, "--user", "bob"]) == 0
    assert (f"Signed in to {url} as bob. agent-machine supplies this machine's providers there from now on "
            "(agent-machine logout to stop). Start it with: agent-machine start") in capsys.readouterr().out


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
