from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import httpx
import yaml
from fastapi.testclient import TestClient

from agent_server.app import Supervisor, create_app
from agent_server.config import Backend, Settings
from oidc_helpers import authenticated_client, access_token


LOCAL = {"base_url": "http://127.0.0.1:8181", "client": ("127.0.0.1", 50000)}


def settings(tmp_path: Path) -> Settings:
    return Settings(log_dir=tmp_path / "logs", backends_file=tmp_path / "backends.yaml", default_backend="test", backends={"test": Backend("test", "http://ollama.test")})


def test_restart_needs_the_supervisor_and_this_computer(tmp_path):
    with authenticated_client(create_app(settings(tmp_path)), **LOCAL) as client:
        refused = client.post("/api/server/restart", json={})
        assert refused.status_code == 409 and "--reload" in refused.json()["detail"]
        assert client.get("/api/status").json()["restart"] == {"available": False, "pending": False, "address_pinned": False}
    restarted = threading.Event()
    app = create_app(settings(tmp_path), restart=restarted.set)
    with TestClient(app) as stranger:
        assert stranger.post("/api/server/restart", json={}).status_code == 401
    with authenticated_client(app, **LOCAL) as client:
        assert client.post("/api/server/restart", json={}, headers={"origin": "http://evil.example"}).status_code == 403
        accepted = client.post("/api/server/restart", json={})
        assert accepted.status_code == 202 and accepted.json()["waiting_for"] == {"requests": 0}
        assert restarted.wait(5)
        assert client.get("/api/status").json()["restart"]["pending"] is True


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_supervisor_serves_again_with_the_configuration_on_disk(tmp_path, monkeypatch):
    headers = {}

    def app_with_issuer(*args, **kwargs):
        app = create_app(*args, **kwargs)
        headers["x-ai-proxy-token"] = access_token(app)
        return app

    monkeypatch.setattr("agent_server.app.create_app", app_with_issuer)
    first, second = free_port(), free_port()
    config = tmp_path / "backends.yaml"

    def write(port: int, backends: dict) -> None:
        config.write_text(yaml.safe_dump({
            "proxy": {"listen_host": "127.0.0.1", "listen_port": port, "url": f"http://127.0.0.1:{port}"},
            "default_backend": "laptop", "backends": backends, "intercept": {"enabled": False},
        }))

    write(first, {"laptop": {"type": "ollama", "url": "http://127.0.0.1:9"}})
    for name, value in {"AI_PROXY_BACKENDS": config, "AI_PROXY_LOG_DIR": tmp_path / "logs", "AI_PROXY_REMOTE_KEY_FILE": tmp_path / "key",
                        "AI_PROXY_MODELS": tmp_path / "models.yaml"}.items():
        monkeypatch.setenv(name, str(value))
    supervisor = Supervisor()
    thread = threading.Thread(target=supervisor.serve, args=(Settings.load,), kwargs={"build": False}, daemon=True)
    thread.start()

    def health(port: int) -> dict | None:
        try:
            return httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5).json()
        except httpx.HTTPError:
            return None

    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not health(first):
            time.sleep(0.05)
        before = health(first)["proxy_instance_id"]
        # Change the config on disk, as an editor (or the Settings page) would, then restart from the console.
        write(second, {"laptop": {"type": "ollama", "url": "http://127.0.0.1:9"}, "fresh": {"type": "ollama", "url": "http://127.0.0.1:10"}})
        accepted = httpx.post(f"http://127.0.0.1:{first}/api/server/restart", json={}, headers=headers)
        # It says where it comes back, so the console can follow it to a new port.
        assert accepted.status_code == 202 and accepted.json()["listen"] == {"host": "127.0.0.1", "port": second}
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not health(second):
            time.sleep(0.1)
        after = health(second)
        assert after and after["proxy_instance_id"] != before
        status = httpx.get(f"http://127.0.0.1:{second}/api/status", headers=headers).json()
        assert "fresh" in status["backends"] and status["listen_address"] == f"127.0.0.1:{second}"
        assert health(first) is None
    finally:
        supervisor.stop()
        thread.join(timeout=10)
    assert not thread.is_alive()
