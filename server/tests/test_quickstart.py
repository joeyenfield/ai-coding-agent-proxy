"""The local Compose helpers use real OAuth credentials and transparent TCP relays."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
from urllib.parse import urlencode

from fastapi.testclient import TestClient
import pytest
import yaml

from agent_server.config import Settings
from agent_server.issuer import create_auth_app


QUICKSTART = Path(__file__).resolve().parents[2] / "quickstart"


def module(name):
    spec = importlib.util.spec_from_file_location("quickstart_" + name, QUICKSTART / f"{name}.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_quickstart_credentials_work_with_real_issuer_and_survive_restart(tmp_path, monkeypatch):
    machine = module("machine")
    monkeypatch.setenv("AI_PROXY_USERNAME", "admin")
    monkeypatch.setenv("AI_PROXY_PASSWORD", "secret")
    settings = Settings(log_dir=tmp_path / "logs", oidc_issuer=machine.ISSUER,
                        oidc_introspection_secret="quickstart-test-secret")
    app = create_auth_app(settings)
    with TestClient(app, base_url=machine.ISSUER) as issuer:
        requests = []

        class Browser:
            def request(self, path, data=None, *, origin=False):
                requests.append((path, data))
                headers = {"Origin": machine.ISSUER} if origin else {}
                if data is not None:
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                response = issuer.request("POST" if data is not None else "GET", path,
                                          content=urlencode(data) if data is not None else None,
                                          headers=headers)
                response.raise_for_status()
                return response.text

        monkeypatch.setattr(machine, "Browser", Browser)
        paths = [tmp_path / name / "credentials.json" for name in ("agent-machine", "agent-proxy")]
        tokens = []
        for path in paths:
            machine.sign_in(path, "secret")
            saved = json.loads(path.read_text())
            entry = saved["servers"][saved["default"]]
            assert entry["url"] == machine.API
            assert entry["user"] == "admin"
            assert entry["token_endpoint"] == machine.ISSUER + "/token"
            assert "secret" not in path.read_text()
            if os.name != "nt":
                assert path.stat().st_mode & 0o777 == 0o600
            active = issuer.post("/introspect", data={"token": entry["access_token"]},
                                 auth=(settings.oidc_introspection_client, settings.oidc_introspection_secret))
            assert active.json()["active"] is True
            tokens.append(entry["refresh_token"])
            before = path.read_bytes()
            count = len(requests)
            machine.sign_in(path, "unused-on-restart")
            assert path.read_bytes() == before and len(requests) == count
            rotated = issuer.post("/token", data={"client_id": entry["client_id"],
                                  "grant_type": "refresh_token", "refresh_token": entry["refresh_token"]})
            assert rotated.status_code == 200
        assert tokens[0] != tokens[1]
        assert all("password" not in (data or {}) or path == "/device" for path, data in requests)


@pytest.mark.asyncio
async def test_quickstart_relay_preserves_bytes_and_half_close():
    start = module("start")
    payload = b"GET /socket HTTP/1.1\r\nUpgrade: websocket\r\n\r\n\x00\xff\x81\x02hi"

    async def upstream(reader, writer):
        received = await reader.read()
        writer.write(b"reply:" + received)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(upstream, "127.0.0.1", 0) as target:
        port = target.sockets[0].getsockname()[1]
        async with await asyncio.start_server(
            lambda r, w: start.relay(r, w, "127.0.0.1", port), "127.0.0.1", 0,
        ) as proxy:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.sockets[0].getsockname()[1])
            writer.write(payload)
            await writer.drain()
            writer.write_eof()
            assert await asyncio.wait_for(reader.read(), timeout=5) == b"reply:" + payload
            writer.close()
            await writer.wait_closed()


def test_compose_resolves_services_and_isolates_machine_storage():
    compose = yaml.safe_load((QUICKSTART / "compose.yaml").read_text())
    services = compose["services"]
    assert set(services) == {"postgres", "server", "auth", "machine-1", "machine-2", "ollama"}
    # The server and agent-auth keep everything in PostgreSQL, as their own roles; it publishes no port.
    assert "ports" not in services["postgres"]
    assert services["server"]["environment"]["AI_PROXY_DATABASE_URL"].startswith("postgresql+psycopg://mesh_api:")
    assert services["auth"]["environment"]["AI_PROXY_AUTH_DATABASE_URL"].startswith("postgresql+psycopg://mesh_auth:")
    for service in ("server", "auth"):
        assert services[service]["depends_on"]["postgres"]["condition"] == "service_healthy"
    init = (QUICKSTART / "../server/docker/postgres-init.sh").resolve()
    assert any(str(volume).startswith("../server/docker/postgres-init.sh:") for volume in services["postgres"]["volumes"]) and init.is_file()
    # No shared namespaces: Podman can't recreate a container another one shares with.
    # machine-1's start.py relays its loopback 11434 to Ollama instead.
    assert not any("network_mode" in service for service in services.values())
    assert services["machine-1"]["environment"]["QUICKSTART_OLLAMA"] == "1"
    assert services["machine-1"]["depends_on"]["ollama"]["condition"] == "service_healthy"
    assert "ollama" not in services["machine-2"]["depends_on"]
    assert services["server"]["environment"]["AI_PROXY_OIDC_ISSUER"] == "http://localhost:20000"
    for service in ("server", "auth"):
        assert all(port.startswith("127.0.0.1:") for port in services[service]["ports"])
    for number in (1, 2):
        machine = services[f"machine-{number}"]
        assert f"machine-{number}-home:/home/agent" in machine["volumes"]
        assert f"machine-{number}-workspace:/workspace" in machine["volumes"]
        for dependency in machine["depends_on"]:
            assert dependency in services
    for service in services.values():
        assert "docker.sock" not in str(service.get("volumes", []))
        build = service.get("build")
        if build:
            root = (QUICKSTART / build["context"]).resolve()
            assert (root / build["dockerfile"]).is_file()
