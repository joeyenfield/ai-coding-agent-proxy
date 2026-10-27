"""The Go components (agent and agent-proxy, agent-machine) against a real agent-server, the way they run.

Everything an agent machine does goes through its agent-proxy: the launcher's API calls,
agents' model requests, account agents' HTTPS (tunnelled to the server's intercept) and
terminals shared with --control. A machine in the fleet is agent-machine: launches (which
run the agent command) and containers. This builds both from ../client and ../machine,
runs them on real ports, and checks the contract between the programs. The server's
upstream is mocked. Skipped when Go isn't installed.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from websockets.sync.client import connect

from agent_server import version
from agent_server.app import create_app
from agent_server.auth import UserStore
from agent_server.config import Backend, InterceptSettings, Settings


CLIENT = Path(__file__).resolve().parents[2] / "client"
MACHINE = Path(__file__).resolve().parents[2] / "machine"
PASSWORD = "correct horse"
# The client's own variables, and proxies an outer agent may have set: none may leak in.
CLEARED = (
    "AI_PROXY_URL", "AI_PROXY_TOKEN", "AI_PROXY_USERNAME", "AI_PROXY_PASSWORD", "AI_PROXY_CONTROL",
    "HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy",
)


# Built as tools/dist builds what ships. A plain debug build of the client gets quarantined by
# Windows Defender's machine-learning check (Trojan:Win32/Bearfoos.A!ml, a false positive).
RELEASE_FLAGS = ["-trimpath", "-ldflags", "-s -w"]


@pytest.fixture(scope="session")
def agent_binary(tmp_path_factory) -> Path:
    go = shutil.which("go")
    if not go or not (CLIENT / "go.mod").exists():
        pytest.skip("Go isn't installed")
    output = tmp_path_factory.mktemp("client") / ("agent.exe" if os.name == "nt" else "agent")
    env = {key: value for key, value in os.environ.items() if key not in CLEARED}
    env.setdefault("GOTOOLCHAIN", "local")
    built = subprocess.run([go, "build", *RELEASE_FLAGS, "-o", str(output), "./cmd/agent"], cwd=CLIENT, env=env, capture_output=True, text=True, timeout=600)
    assert built.returncode == 0, built.stderr
    return output


@pytest.fixture(scope="session")
def machine_binary(tmp_path_factory, agent_binary) -> Path:
    output = tmp_path_factory.mktemp("machine") / ("agent-machine.exe" if os.name == "nt" else "agent-machine")
    env = {key: value for key, value in os.environ.items() if key not in CLEARED}
    env.setdefault("GOTOOLCHAIN", "local")
    built = subprocess.run([shutil.which("go"), "build", *RELEASE_FLAGS, "-o", str(output), "./cmd/agent-machine"], cwd=MACHINE, env=env, capture_output=True, text=True, timeout=600)
    assert built.returncode == 0, built.stderr
    return output


@contextmanager
def fleet_machine(m, machine_binary: Path, tmp_path: Path, *settings: list[str], env: dict | None = None):
    """agent-machine on the same computer as m's agent-proxy: signed in to m's server, with settings, running."""
    machine_env = {**m.env, "AI_PROXY_MACHINE_HOME": str(tmp_path / "machine-home"), "AI_PROXY_AGENT": str(m.binary), **(env or {})}

    def command(*args: str) -> None:
        done = subprocess.run([str(machine_binary), *args], env=machine_env, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        assert done.returncode == 0, done.stdout + done.stderr

    command("login", m.server)
    for args in settings:
        command(*args)
    process = subprocess.Popen([str(machine_binary), "run"], env=machine_env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 20
        listed: list = []
        while time.monotonic() < deadline and not listed:
            listed = [item for item in httpx.get(f"{m.server}/api/machines").json()["machines"] if item["connected"]]
            time.sleep(0.2)
        assert listed, "agent-machine didn't join the fleet"
        status = subprocess.run([str(machine_binary)], env=machine_env, capture_output=True, text=True, timeout=30)
        yield SimpleNamespace(box=listed[0], status=status.stdout, command=command)
    finally:
        process.terminate()
        process.wait(timeout=10)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for(url: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url + "/health", timeout=0.5).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.05)
    raise AssertionError(f"{url} didn't start")


@contextmanager
def serve(app):
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    wait_for(base)
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def upstream(seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "ollama.test":
            lines = [{"message": {"role": "assistant", "content": "hi"}, "done": False}, {"message": {"content": ""}, "done": True, "eval_count": 1}]
            return httpx.Response(200, headers={"content-type": "application/x-ndjson"}, content="".join(json.dumps(line) + "\n" for line in lines).encode())
        if request.url.host == "api.anthropic.test":
            return httpx.Response(200, json={"type": "message", "model": "claude-x", "usage": {"input_tokens": 3, "output_tokens": 2}})
        return httpx.Response(404)
    return handler


# An agent for the launcher to run: it saves its arguments, the proxy's variables and the
# settings file the launcher wrote for it, and exits 3. Its executable is this Python, so
# it's found the same way on every platform.
FAKE_AGENT = (
    "import json, os, sys; settings = os.environ.get('FAKE_SETTINGS'); "
    "json.dump({'argv': sys.argv[1:], 'cwd': os.getcwd(), 'env': {k: v for k, v in os.environ.items() "
    "if k.startswith(('OPENAI_', 'AI_PROXY_', 'FAKE_'))}, 'settings': json.load(open(settings)) if settings else None}, "
    "open('agent-ran.json', 'w')); sys.exit(3)"
)


def agents_file(tmp_path: Path) -> Path:
    import yaml

    path = tmp_path / "agents.yaml"
    path.write_text(yaml.safe_dump({"agents": {
        "fake": {
            "name": "Fake agent", "executable": sys.executable, "protocol": "openai-chat",
            "env": {"OPENAI_BASE_URL": "{openai_url}", "FAKE_MODEL": "{model}", "FAKE_PROJECT": "{project}", "FAKE_SETTINGS": "{files_dir}/settings.json"},
            "files": {"settings.json": {"model": "{model}", "context": "{context_window}"}},
            "args": ["-c", FAKE_AGENT, "{model}"],
        },
        "claude-account": {"name": "Claude (account)", "executable": "claude", "protocol": "anthropic", "intercept": True},
    }}))
    return path


@contextmanager
def machine(tmp_path, agent_binary, accounts: bool = False, prepare=None):
    """An agent-server, and this machine's agent-proxy (the Go binary) pointed at it.

    prepare(env) runs `agent` commands on the machine before its agent-proxy starts.
    """
    settings = Settings(
        log_dir=tmp_path / "server", backends_file=tmp_path / "backends.yaml",
        default_backend="test", backends={"test": Backend("test", "http://ollama.test")},
        intercept=InterceptSettings(ca_dir=tmp_path / "certs"), agents_file=agents_file(tmp_path), dist_dir=tmp_path / "dist",
    )
    if accounts:
        UserStore(settings.users_path).add_user("dave", PASSWORD)
    server_app = create_app(settings)
    seen: list[httpx.Request] = []
    with serve(server_app) as server:
        runtime = server_app.state.runtime
        runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream(seen)))
        port, intercept_port = free_port(), free_port()
        env = {key: value for key, value in os.environ.items() if key not in CLEARED}
        env.update(AI_PROXY_CREDENTIALS=str(tmp_path / "machine" / "credentials.json"), AI_PROXY_URL=server,
                   AI_PROXY_LOCAL_PORT=str(port), AI_PROXY_LOCAL_INTERCEPT_PORT=str(intercept_port))
        if prepare:
            prepare(env)
        process = subprocess.Popen([str(agent_binary), "proxy"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        proxy = f"http://127.0.0.1:{port}"
        try:
            wait_for(proxy)
            yield SimpleNamespace(server=server, proxy=proxy, runtime=runtime, seen=seen, intercept_port=intercept_port, env=env, binary=agent_binary)
        finally:
            process.terminate()
            process.wait(timeout=10)


def run_agent(m, *args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([str(m.binary), *args], env={**m.env, **kwargs.pop("env", {})}, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, **kwargs)


def new_session(proxy: str, client: str = "qwen", backend: str = "test") -> str:
    response = httpx.post(f"{proxy}/api/sessions", json={"client": client, "backend": backend})
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def ca_context(proxy: str, tmp_path) -> ssl.SSLContext:
    path = tmp_path / "server-ca.pem"
    path.write_text(httpx.get(f"{proxy}/api/intercept/ca.pem").text)
    return ssl.create_default_context(cafile=str(path))


def test_the_launcher_reaches_the_server_only_through_the_proxys_short_list(tmp_path, agent_binary):
    with machine(tmp_path, agent_binary) as m:
        session_id = new_session(m.proxy)
        assert m.runtime.sessions.get(session_id).client == "qwen"
        assert httpx.get(f"{m.proxy}/health").json()["server"] == m.server
        # Agents here use the proxy's own HTTPS port.
        assert httpx.get(f"{m.proxy}/api/intercept").json()["url"] == f"http://127.0.0.1:{m.intercept_port}"
        assert "BEGIN CERTIFICATE" in httpx.get(f"{m.proxy}/api/intercept/bundle.pem").text
        # The rest of the server's API isn't reachable through it.
        assert httpx.get(f"{m.proxy}/api/requests").status_code == 404
        assert httpx.post(f"{m.proxy}/api/server/restart", json={}).status_code == 404
        # Nor is it from web pages: DNS rebinding names another host, other sites send their Origin.
        assert httpx.get(f"{m.proxy}/api/config", headers={"host": "evil.example"}).status_code == 403
        assert httpx.post(f"{m.proxy}/api/sessions", json={"client": "x"}, headers={"origin": "http://evil.example"}).status_code == 403


def test_model_requests_stream_through_to_the_server(tmp_path, agent_binary):
    with machine(tmp_path, agent_binary) as m:
        session_id = new_session(m.proxy)
        with httpx.stream("POST", f"{m.proxy}/session/{session_id}/api/chat", json={"model": "m", "messages": []}) as response:
            lines = [json.loads(line) for line in response.iter_lines() if line]
        assert response.status_code == 200
        assert lines[0]["message"]["content"] == "hi" and lines[-1]["done"] is True
        assert m.seen[0].url == "http://ollama.test/api/chat"
        [record] = m.runtime.records(m.runtime.sessions.get(session_id))
        assert record["backend"] == "test"


def test_account_agents_https_is_tunnelled_to_the_servers_intercept(tmp_path, agent_binary):
    with machine(tmp_path, agent_binary) as m:
        session_id = new_session(m.proxy, "claude-account", "direct")
        proxy_url = f"http://{session_id}:agent-proxy@127.0.0.1:{m.intercept_port}"
        with httpx.Client(proxy=proxy_url, verify=ca_context(m.proxy, tmp_path)) as agent:
            response = agent.post("https://api.anthropic.test/v1/messages", json={"model": "claude-x", "messages": []}, headers={"authorization": "Bearer sk-agent"})
        assert response.status_code == 200 and response.json()["model"] == "claude-x"
        # The server sent it on with the agent's own credentials, and recorded it in the session.
        assert str(m.seen[0].url) == "https://api.anthropic.test/v1/messages"
        assert m.seen[0].headers["authorization"] == "Bearer sk-agent"
        [record] = m.runtime.records(m.runtime.sessions.get(session_id))
        assert (record["kind"], record["host"], record["output_tokens"]) == ("intercept", "api.anthropic.test", 2)


def test_a_controlled_terminal_is_relayed_to_the_server(tmp_path, agent_binary):
    with machine(tmp_path, agent_binary) as m:
        session_id = new_session(m.proxy, "claude")
        host_url = m.proxy.replace("http", "ws") + f"/api/terminals/{session_id}/host"
        # Playing the launcher's side of the protocol, through the agent-proxy.
        with connect(host_url, proxy=None) as host:
            host.send(json.dumps({"type": "hello", "cols": 100, "rows": 30, "command": "claude"}))
            assert json.loads(host.recv(timeout=5)) == {"type": "resume", "from": 0}
            host.send(b"hello from the agent machine\r\n")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not httpx.get(f"{m.server}/api/terminals").json()["terminals"]:
                time.sleep(0.05)
            with connect(m.server.replace("http", "ws") + f"/api/terminals/{session_id}/view", proxy=None) as viewer:
                assert json.loads(viewer.recv(timeout=5))["terminal"]["cols"] == 100
                replay = b""
                while True:
                    message = viewer.recv(timeout=5)
                    if isinstance(message, bytes):
                        replay += message
                    elif json.loads(message)["type"] == "replayed":
                        break
                assert replay == b"hello from the agent machine\r\n"
                viewer.send(json.dumps({"type": "input", "text": "go", "enter": True}))
                assert json.loads(host.recv(timeout=5)) == {"type": "input", "parts": ["go", "\r"]}
                host.send(json.dumps({"type": "exit", "status": 0}))
                assert json.loads(viewer.recv(timeout=5))["terminal"]["exit_status"] == 0
        # The server's refusal reaches the launcher as its close code.
        with connect(m.proxy.replace("http", "ws") + "/api/terminals/no-such-session/host", proxy=None) as host:
            host.send(json.dumps({"type": "hello", "cols": 80, "rows": 24}))
            with pytest.raises(Exception) as closed:
                host.recv(timeout=5)
            assert getattr(closed.value, "rcvd", None) and closed.value.rcvd.code == 4404


def test_the_launcher_runs_the_servers_agent_with_the_rendered_environment(tmp_path, agent_binary):
    project = tmp_path / "my-project"
    project.mkdir()
    with machine(tmp_path, agent_binary) as m:
        listed = run_agent(m, "--list")
        assert listed.returncode == 0, listed.stderr
        assert "fake" in listed.stdout and "installed" in listed.stdout and f"carrying traffic to {m.server}" in listed.stdout
        ran = run_agent(m, "fake", str(project), "--model", "qwen3.6:35b", "--trace", "--", "--yolo")
        assert ran.returncode == 3, ran.stdout + ran.stderr
        saved = json.loads((project / "agent-ran.json").read_text())
        [session] = [item for item in m.runtime.sessions.list() if item.client == "fake"]
        assert f"Session : {session.session_id}" in ran.stdout
        assert saved["argv"] == ["qwen3.6:35b", "--yolo"]
        assert Path(saved["cwd"]).resolve() == project.resolve()
        # The agent talks to this machine's agent-proxy, never to the server itself.
        assert saved["env"]["OPENAI_BASE_URL"] == f"{m.proxy}/session/{session.session_id}/v1"
        assert saved["env"]["AI_PROXY_SESSION"] == session.session_id
        assert Path(saved["env"]["FAKE_PROJECT"]).resolve() == project.resolve()
        # The launcher wrote the agent's settings file where the server pointed it, and removed it after.
        assert saved["settings"]["model"] == "qwen3.6:35b"
        assert not Path(saved["env"]["FAKE_SETTINGS"]).parent.exists()
        assert session.trace is True and session.backend == "test" and session.project == "my-project"
        assert session.tags["prepared_by"] == "agent" and Path(session.tags["project_path"]).resolve() == project.resolve()
        assert session.ended_at is not None and session.exit_status == 3
        missing = run_agent(m, "nope", str(project))
        assert missing.returncode == 1 and "unknown agent 'nope'" in missing.stderr


def test_with_accounts_the_machine_signs_in_once_for_everything(tmp_path, agent_binary):
    with machine(tmp_path, agent_binary, accounts=True) as m:
        status = httpx.get(f"{m.proxy}/api/auth/status").json()
        assert status["required"] and status["identity"] is None
        assert httpx.post(f"{m.proxy}/api/sessions", json={"client": "qwen", "backend": "test"}).status_code == 401
        # Terminals too: the launcher's probe reads this 401 as "run agent login".
        assert httpx.get(f"{m.proxy}/api/remote").status_code == 401
        # Account agents' connections are refused with a 407 from the HTTPS proxy.
        with httpx.Client(proxy=f"http://127.0.0.1:{m.intercept_port}", verify=False) as agent, pytest.raises(httpx.ProxyError, match="407"):
            agent.get("https://api.anthropic.test/")
        refused = run_agent(m, "fake", str(tmp_path), "--model", "m")
        assert refused.returncode == 1 and "agent login" in refused.stderr

        signed = run_agent(m, "login", m.server, "--user", "dave", env={"AI_PROXY_PASSWORD_INPUT": PASSWORD})
        assert signed.returncode == 0, signed.stderr
        assert httpx.get(f"{m.proxy}/health").json()["user"] == "dave"
        session_id = new_session(m.proxy, "claude-account", "direct")
        assert m.runtime.sessions.get(session_id).tags["owner"] == "dave"
        with httpx.Client(proxy=f"http://{session_id}:agent-proxy@127.0.0.1:{m.intercept_port}", verify=ca_context(m.proxy, tmp_path)) as agent:
            assert agent.post("https://api.anthropic.test/v1/messages", json={"model": "claude-x"}).status_code == 200
        assert m.runtime.auth.store.tokens("dave")[0]["label"].startswith("agent-proxy on ")
        assert run_agent(m, "logout").returncode == 0
        assert m.runtime.auth.store.tokens("dave") == []


def test_the_agent_proxy_alone_doesnt_join_the_fleet(tmp_path, agent_binary):
    with machine(tmp_path, agent_binary) as m:
        new_session(m.proxy)
        assert httpx.get(f"{m.server}/api/machines").json()["machines"] == []
        assert "fleet" not in httpx.get(f"{m.proxy}/health").json()
        moved = run_agent(m, "machine", "launches", "on")
        assert moved.returncode == 2 and "agent-machine" in moved.stderr


def test_a_fleet_machine_runs_an_agent_the_console_starts(tmp_path, agent_binary, machine_binary):
    project = tmp_path / "work" / "api"
    project.mkdir(parents=True)
    settings = (["launches", "on"], ["roots", "add", str(tmp_path / "work")], ["name", "build-box"])
    with machine(tmp_path, agent_binary) as m, fleet_machine(m, machine_binary, tmp_path, *settings) as fleet:
        box = fleet.box
        assert box["name"] == "build-box" and box["launches"] is True and "fake" in box["agents"]
        assert f"connected to {m.server}" in fleet.status and "Launches  : on" in fleet.status
        # A folder outside the machine's roots is refused before it's sent.
        assert httpx.post(f"{m.server}/api/machines/{box['id']}/launch", json={"agent": "fake", "project": str(tmp_path), "model": "m"}).status_code == 400
        response = httpx.post(f"{m.server}/api/machines/{box['id']}/launch", json={"agent": "fake", "project": str(project), "model": "qwen3.6:35b"}, timeout=90)
        assert response.status_code == 201, response.text
        session_id = response.json()["session_id"]
        session = m.runtime.sessions.get(session_id)
        assert session.client == "fake" and session.tags["machine"] == box["id"] and session.tags["machine_name"] == "build-box"
        assert session.tags.get("launch") == response.json()["launch"]
        # It runs headless, its terminal shared, and ends with the agent's own exit status.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and m.runtime.sessions.get(session_id).ended_at is None:
            time.sleep(0.2)
        assert m.runtime.sessions.get(session_id).exit_status == 3
        saved = json.loads((project / "agent-ran.json").read_text())
        assert saved["argv"] == ["qwen3.6:35b"] and saved["env"]["AI_PROXY_SESSION"] == session_id
        [launch] = httpx.get(f"{m.server}/api/machines/{box['id']}").json()["history"]
        assert launch["session_id"] == session_id and launch["error"] is None


def test_a_fleet_machine_runs_the_containers_the_server_asks_for(tmp_path, agent_binary, machine_binary):
    docker_state = tmp_path / "docker.json"
    docker = {"AI_PROXY_CONTAINER_COMMAND": json.dumps([sys.executable, str(Path(__file__).with_name("fake_docker.py"))]), "FAKE_DOCKER_STATE": str(docker_state)}
    with machine(tmp_path, agent_binary) as m, fleet_machine(m, machine_binary, tmp_path, ["containers", "on"], env=docker):
        [box] = httpx.get(f"{m.server}/api/containers").json()["machines"]
        assert box["connected"]
        assert box["enabled"] and box["online"] and {engine["id"] for engine in box["engines"]} == {"strata", "ollama"}
        created = httpx.post(f"{m.server}/api/containers", json={"name": "s1", "machine": box["id"], "engine": "strata", "port": 18555, "env": {"MODEL": "IQ3_XXS"}})
        assert created.status_code == 201, created.text
        assert httpx.post(f"{m.server}/api/containers/s1/start", json={}, timeout=30).status_code == 202
        deadline = time.monotonic() + 30
        item: dict = {}
        while time.monotonic() < deadline:
            item = httpx.get(f"{m.server}/api/containers/s1", timeout=30).json()
            if item["job"] and item["job"]["state"] != "running":
                break
            time.sleep(0.2)
        assert item["job"]["state"] == "done", item
        assert item["phase"] == "loading" and item["command"].startswith("docker run") and "<api key>" in item["command"]
        # The machine built the container from the spec, with the key the server keeps for it.
        container = json.loads(docker_state.read_text())["containers"]["ai-proxy-s1"]
        assert container["env"] == {"API_KEY": m.runtime.containers.keys.get("s1"), "MODEL": "IQ3_XXS", "PRESET": "s1"}
        assert container["ports"] == ["127.0.0.1:18555:8080"] and container["volumes"] == ["strata-data:/data"]
        assert m.runtime.containers.keys.get("s1") not in httpx.get(f"{m.server}/api/containers/s1/log", timeout=30).text
        assert httpx.delete(f"{m.server}/api/containers/s1", timeout=60).status_code == 200
        assert "ai-proxy-s1" not in json.loads(docker_state.read_text())["containers"]


def test_a_fleet_machine_starts_and_stops_strata_when_the_owner_asks(tmp_path, agent_binary, machine_binary):
    strata_state = tmp_path / "strata.json"
    strata_state.write_text(json.dumps({"profiles": {"app-1": "stopped"}}))
    fake = {"AI_PROXY_STRATA_COMMAND": json.dumps([sys.executable, str(Path(__file__).with_name("fake_strata.py"))]), "FAKE_STRATA_STATE": str(strata_state)}
    with machine(tmp_path, agent_binary) as m, fleet_machine(m, machine_binary, tmp_path, ["servers", "on"], env=fake) as fleet:
        servers = httpx.get(f"{m.server}/api/machines/{fleet.box['id']}").json()["servers"]
        assert servers["enabled"] and servers["strata"]["profiles"] == [{"id": "app-1", "state": "stopped", "url": None, "error": None}]
        assert "Servers   : on" in fleet.status
        started = httpx.post(f"{m.server}/api/machines/{fleet.box['id']}/servers/strata/start", json={"profile": "app-1"}, timeout=60)
        assert started.status_code == 200, started.text
        assert started.json()["servers"]["strata"]["profiles"][0]["state"] == "ready"
        assert json.loads(strata_state.read_text())["profiles"]["app-1"] == "ready"
        refused = httpx.post(f"{m.server}/api/machines/{fleet.box['id']}/servers/strata/start", json={"profile": "other"}, timeout=60)
        assert refused.status_code == 409 and "no Strata profile" in refused.json()["detail"]
        stopped = httpx.post(f"{m.server}/api/machines/{fleet.box['id']}/servers/strata/stop", json={"profile": "app-1"}, timeout=60)
        assert stopped.status_code == 200 and stopped.json()["servers"]["strata"]["profiles"][0]["state"] == "stopped"


@contextmanager
def fake_ollama_server():
    """An Ollama on a free loopback port: two models, one loaded, and a pull that reports progress."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    seen: list[tuple[str, str, dict]] = []

    class Handler(BaseHTTPRequestHandler):
        def reply(self, value: object, *, lines: bool = False) -> None:
            body = "".join(json.dumps(item) + chr(10) for item in value) if lines else json.dumps(value)
            data = body.encode()
            self.send_response(200)
            self.send_header("content-type", "application/x-ndjson" if lines else "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            seen.append(("GET", self.path, {}))
            if self.path == "/api/tags":
                self.reply({"models": [{"name": "qwen3:8b", "size": 5, "details": {"family": "qwen3"}}, {"name": "gemma3:4b", "size": 3, "details": {}}]})
            elif self.path == "/api/ps":
                self.reply({"models": [{"name": "qwen3:8b", "size": 10, "size_vram": 5, "context_length": 32768}]})
            else:
                self.reply({"version": "0.12.3"})

        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            seen.append(("POST", self.path, body))
            if self.path == "/api/pull":
                self.reply([{"status": "pulling manifest"}, {"status": "downloading", "total": 100, "completed": 100}, {"status": "success"}], lines=True)
            else:
                self.reply({"done": True})

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1], seen
    finally:
        server.shutdown()


def test_a_fleet_machine_manages_its_model_server_for_the_console(tmp_path, agent_binary, machine_binary):
    with fake_ollama_server() as (port, seen), machine(tmp_path, agent_binary) as m, fleet_machine(m, machine_binary, tmp_path, ["servers", "on"]) as fleet:
        base = f"{m.server}/api/machines/{fleet.box['id']}"
        state = httpx.get(f"{base}/providers/ollama-{port}", timeout=30).json()
        assert state["online"] and state["version"] == "0.12.3", state
        assert [(model["name"], model["loaded"]) for model in state["models"]] == [("gemma3:4b", False), ("qwen3:8b", True)]
        assert state["models"][1]["gpu_percent"] == 50
        lines = [json.loads(line) for line in httpx.post(f"{base}/providers/ollama-{port}/models/pull", json={"name": "devstral:24b"}, timeout=60).text.splitlines()]
        assert lines[-1]["status"] == "success" and any(line.get("completed") == 100 for line in lines), lines
        assert httpx.post(f"{base}/providers/ollama-{port}/models/unload", json={"name": "qwen3:8b"}, timeout=30).status_code == 200
        assert ("POST", "/api/pull", {"model": "devstral:24b", "stream": True}) in seen
        assert any(method == "POST" and path == "/api/generate" and body.get("keep_alive") == 0 for method, path, body in seen)
        # The machine's load, sampled there.
        metrics = httpx.get(f"{base}/metrics", timeout=30).json()
        assert metrics["memory"]["total"] > 0 and "cpu" in metrics
        # Ollama's settings live on the machine, in agent-machine's folder.
        settings = httpx.get(f"{base}/servers/ollama", timeout=30).json()
        assert "host" not in {setting["key"] for setting in settings["schema"]["settings"]}
        saved = httpx.put(f"{base}/servers/ollama", json={**settings["config"], "num_parallel": 2}, timeout=30)
        assert saved.status_code == 200 and saved.json()["env"]["OLLAMA_NUM_PARALLEL"] == "2"
        assert json.loads((tmp_path / "machine-home" / "ollama.json").read_text())["num_parallel"] == 2
        refused = httpx.put(f"{base}/servers/ollama", json={**settings["config"], "extra_env": "LD_PRELOAD=/tmp/x.so"}, timeout=30)
        assert refused.status_code == 409 and "OLLAMA_" in refused.json()["detail"]
        # Off on the machine: it still reports, but changes nothing.
        fleet.command("servers", "off")
        refused = httpx.post(f"{base}/providers/ollama-{port}/models/delete", json={"name": "gemma3:4b"}, timeout=30)
        assert refused.status_code == 409 and "agent-machine servers on" in refused.json()["detail"]
        assert httpx.get(f"{base}/providers/ollama-{port}", timeout=30).json()["online"]


def test_the_programs_speak_the_servers_protocol(agent_binary, machine_binary):
    for binary in (agent_binary, machine_binary):
        printed = subprocess.run([str(binary), "version"], capture_output=True, text=True, timeout=30).stdout
        assert f"(protocol {version.PROTOCOL})" in printed, printed


def test_agent_machine_updates_itself_from_the_servers_downloads(tmp_path, agent_binary, machine_binary, monkeypatch):
    import hashlib

    monkeypatch.setenv("AI_PROXY_VERSION", "9.9.9")
    version.current.cache_clear()
    try:
        goarch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}[__import__("platform").machine().lower()]
        platform = f"{ {'win32': 'windows'}.get(sys.platform, sys.platform)}-{goarch}"
        name = "agent-machine.exe" if os.name == "nt" else "agent-machine"
        # The server's build: the same program, which is all update needs to check.
        new = machine_binary.read_bytes() + b"\0new build"
        folder = tmp_path / "dist" / "9.9.9" / platform / "artifact"
        folder.mkdir(parents=True)
        (folder / name).write_bytes(new)
        (tmp_path / "dist" / "9.9.9" / "SHA256SUMS").write_text(f"{hashlib.sha256(new).hexdigest()}  {platform}/artifact/{name}\n")
        installed = tmp_path / "bin" / name
        installed.parent.mkdir()
        installed.write_bytes(machine_binary.read_bytes())
        with machine(tmp_path, agent_binary) as m:
            env = {**m.env, "AI_PROXY_MACHINE_HOME": str(tmp_path / "machine-home")}
            done = subprocess.run([str(installed), "update", "--server", m.server], env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
            assert done.returncode == 0, done.stdout + done.stderr
            assert installed.read_bytes() == new
            # A wrong checksum replaces nothing.
            (tmp_path / "dist" / "9.9.9" / "SHA256SUMS").write_text(f"{'0' * 64}  {platform}/artifact/{name}\n")
            refused = subprocess.run([str(installed), "update", "--server", m.server, "--force"], env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
            assert refused.returncode != 0 and installed.read_bytes() == new, refused.stdout + refused.stderr
    finally:
        version.current.cache_clear()


def test_a_machine_the_server_cant_reach_serves_agents_through_its_tunnel(tmp_path, agent_binary, machine_binary):
    """The machine says an address nothing answers on (TEST-NET), so agents' requests only arrive through its tunnel."""
    docker = {"AI_PROXY_CONTAINER_COMMAND": json.dumps([sys.executable, str(Path(__file__).with_name("fake_docker.py"))]), "FAKE_DOCKER_STATE": str(tmp_path / "docker.json")}
    with fake_ollama_server() as (port, seen), machine(tmp_path, agent_binary) as m, \
            fleet_machine(m, machine_binary, tmp_path, ["containers", "on"], ["address", "203.0.113.9"], env=docker) as fleet:
        created = httpx.post(f"{m.server}/api/containers", json={"name": "o1", "machine": fleet.box["id"], "engine": "ollama", "port": port})
        assert created.status_code == 201, created.text
        assert created.json()["url"] == f"http://203.0.113.9:{port}"
        # The machine learns its containers' ports from the server's requests about them.
        assert httpx.get(f"{m.server}/api/containers", timeout=30).status_code == 200
        assert m.runtime.fleet.machines[fleet.box["id"]].tunnel
        # A real HTTP client this time (machine() mocks the server's upstream): the bytes go through the tunnel.
        m.runtime.client = httpx.AsyncClient(trust_env=False, timeout=30)
        session = new_session(m.server, backend="o1")
        listed = httpx.get(f"{m.server}/session/{session}/v1/models", timeout=30)
        assert listed.status_code == 200, listed.text
        assert {"qwen3:8b", "gemma3:4b"} <= {item["id"] for item in listed.json()["data"]}
        assert any(method == "GET" and path == "/api/tags" for method, path, _ in seen)
        # Keep-alive: the next request reuses the tunnel, and it still works.
        assert httpx.get(f"{m.server}/session/{session}/v1/models", timeout=30).status_code == 200
