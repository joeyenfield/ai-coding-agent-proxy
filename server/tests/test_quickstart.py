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


ROOT = Path(__file__).resolve().parents[2]
QUICKSTART = ROOT / "deploy" / "compose" / "quickstart"


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


def test_quickstart_vault_key_survives_restarts_and_decrypts_stored_keys(tmp_path, monkeypatch):
    from agent_server.provider_accounts import Vault

    monkeypatch.delenv("AI_PROXY_VAULT_KEYS", raising=False)
    monkeypatch.delenv("AI_PROXY_VAULT_KEYS_FILE", raising=False)
    start = module("start")
    directory = tmp_path / "private"
    start.configure_vault(directory)
    path = Path(os.environ["AI_PROXY_VAULT_KEYS_FILE"])
    original = path.read_bytes()
    vault = Vault.from_environment()
    assert vault.configured
    version, nonce, ciphertext = vault.encrypt("deepseek-test-key", b"provider")
    assert b"deepseek-test-key" not in original
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    monkeypatch.delenv("AI_PROXY_VAULT_KEYS_FILE")
    start.configure_vault(directory)
    assert path.read_bytes() == original
    assert Vault.from_environment().decrypt(version, nonce, ciphertext, b"provider") == "deepseek-test-key"
    monkeypatch.delenv("AI_PROXY_VAULT_KEYS_FILE")
    start.configure_vault(tmp_path / "another-instance")
    assert Path(os.environ["AI_PROXY_VAULT_KEYS_FILE"]).read_bytes() != original


def test_quickstart_vault_publishes_without_hardlinks(tmp_path, monkeypatch):
    from agent_server.provider_accounts import Vault

    monkeypatch.delenv("AI_PROXY_VAULT_KEYS", raising=False)
    monkeypatch.delenv("AI_PROXY_VAULT_KEYS_FILE", raising=False)
    start = module("start")

    def unsupported(source, target):
        raise OSError("hardlinks are not supported here")

    monkeypatch.setattr(start.os, "link", unsupported)
    directory = tmp_path / "private"
    start.configure_vault(directory)
    path = Path(os.environ["AI_PROXY_VAULT_KEYS_FILE"])
    assert path.read_text(encoding="ascii").startswith("1:")
    vault = Vault.from_environment()
    version, nonce, ciphertext = vault.encrypt("deepseek-test-key", b"provider")
    assert vault.decrypt(version, nonce, ciphertext, b"provider") == "deepseek-test-key"


def test_quickstart_vault_without_hardlinks_keeps_a_racing_starts_key(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_PROXY_VAULT_KEYS", raising=False)
    monkeypatch.delenv("AI_PROXY_VAULT_KEYS_FILE", raising=False)
    start = module("start")
    directory = tmp_path / "private"
    winner = "1:" + "A" * 43 + "=\n"

    def raced(source, target):
        # Another start publishes its key after this one checked, before it links.
        Path(target).write_text(winner, encoding="ascii")
        raise OSError("hardlinks are not supported here")

    monkeypatch.setattr(start.os, "link", raced)
    start.configure_vault(directory)
    assert Path(os.environ["AI_PROXY_VAULT_KEYS_FILE"]).read_text(encoding="ascii") == winner
    assert sorted(item.name for item in directory.iterdir()) == ["vault-keys"]


@pytest.mark.parametrize("variable", ["AI_PROXY_VAULT_KEYS", "AI_PROXY_VAULT_KEYS_FILE"])
def test_quickstart_vault_honors_explicit_configuration(tmp_path, monkeypatch, variable):
    monkeypatch.delenv("AI_PROXY_VAULT_KEYS", raising=False)
    monkeypatch.delenv("AI_PROXY_VAULT_KEYS_FILE", raising=False)
    monkeypatch.setenv(variable, "operator-supplied")
    directory = tmp_path / "private"
    module("start").configure_vault(directory)
    assert not directory.exists()
    assert os.environ[variable] == "operator-supplied"


def test_quickstart_does_not_replace_an_empty_existing_vault_key(tmp_path, monkeypatch):
    monkeypatch.delenv("AI_PROXY_VAULT_KEYS", raising=False)
    monkeypatch.delenv("AI_PROXY_VAULT_KEYS_FILE", raising=False)
    directory = tmp_path / "private"
    directory.mkdir()
    path = directory / "vault-keys"
    path.write_bytes(b"")
    with pytest.raises(RuntimeError, match="restore it from backup"):
        module("start").configure_vault(directory)
    assert path.read_bytes() == b""


def test_gpu_override_gives_only_ollama_the_nvidia_gpus_by_cdi_name():
    # Podman ignores Compose's deploy.resources device reservations; a CDI device name is what it honours.
    override = yaml.safe_load((QUICKSTART / "compose.gpu.yaml").read_text())
    assert override == {"services": {"ollama": {"devices": ["nvidia.com/gpu=all"]}}}
    # The main file stays runnable on computers without a GPU.
    compose = yaml.safe_load((QUICKSTART / "compose.yaml").read_text())
    assert not any("devices" in service or "deploy" in service for service in compose["services"].values())


def test_compose_resolves_services_and_isolates_machine_storage():
    compose = yaml.safe_load((QUICKSTART / "compose.yaml").read_text())
    services = compose["services"]
    assert set(services) == {"postgres-init", "postgres", "server", "auth", "machine-1", "machine-2", "ollama"}
    # The server and agent-auth keep everything in PostgreSQL, as their own roles; it publishes no port.
    assert "ports" not in services["postgres"]
    assert services["server"]["environment"]["AI_PROXY_DATABASE_URL"].startswith("postgresql+psycopg://mesh_api:")
    assert services["auth"]["environment"]["AI_PROXY_AUTH_DATABASE_URL"].startswith("postgresql+psycopg://mesh_auth:")
    for service in ("server", "auth"):
        assert services[service]["depends_on"]["postgres"]["condition"] == "service_healthy"
    # The file needs nothing beside it, so it runs without a checkout: no service mounts a host
    # path, and the helpers come from the images, where the Dockerfiles put them in /quickstart.
    for name, service in services.items():
        assert not any(str(volume).startswith((".", "/", "~")) for volume in service.get("volumes", [])), name
    server_image = (ROOT / "server" / "Dockerfile").read_text(encoding="utf-8")
    sandbox_image = (ROOT / "sandbox" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY deploy/compose/quickstart/start.py server/docker/postgres-init.sh /quickstart/" in server_image
    assert "COPY deploy/compose/quickstart/start.py deploy/compose/quickstart/machine.py /quickstart/" in sandbox_image
    assert (ROOT / "server" / "docker" / "postgres-init.sh").is_file()
    # PostgreSQL's first-start script reaches it through a volume a one-shot container fills from the server's image.
    init = services["postgres-init"]
    assert init["image"] == services["server"]["image"] and "/quickstart/postgres-init.sh" in init["entrypoint"][-1]
    assert "postgres-initdb:/initdb" in init["volumes"]
    assert "postgres-initdb:/docker-entrypoint-initdb.d:ro" in services["postgres"]["volumes"]
    assert services["postgres"]["depends_on"]["postgres-init"]["condition"] == "service_completed_successfully"
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
        # The quickstart runs the published images; devhelpers/basic builds this checkout's instead.
        assert "build" not in service
    for name in ("server", "auth"):
        assert services[name]["image"] == "${AI_PROXY_IMAGES:-ghcr.io/joeyenfield}/agent-server:${AI_PROXY_IMAGE_TAG:-latest}"
    for name in ("machine-1", "machine-2"):
        assert services[name]["image"] == "${AI_PROXY_IMAGES:-ghcr.io/joeyenfield}/agent-sandbox:${AI_PROXY_IMAGE_TAG:-latest}"


QUICKSTARTS = QUICKSTART.parent
DEVHELPERS = ROOT / "devhelpers"
# Each devhelpers stack is a deploy/compose stack built from this checkout.
PAIRS = {"basic": "quickstart", "allinone": "allinone", "peer": "peer"}
PUBLISHED = "${AI_PROXY_IMAGES:-ghcr.io/joeyenfield}/"
# Settings only a development twin has: the basic stack's static test organizations and people.
DEV_ONLY = {("basic", "server"): {"QUICKSTART_ORGS", "QUICKSTART_ACCOUNTS", "QUICKSTART_PASSWORD"}}


def services_of(folder: Path) -> dict:
    return yaml.safe_load((folder / "compose.yaml").read_text())["services"]


def mounts_resolved(folder: Path, service: dict) -> list:
    """A service's volumes, with host paths made absolute so two folders' compose files compare."""
    found = []
    for volume in service.get("volumes", []):
        source, rest = str(volume).split(":", 1)
        found.append(f"{(folder / source).resolve()}:{rest}" if source.startswith(".") else str(volume))
    return found


def test_deploy_stacks_run_only_the_published_images():
    for name in ("quickstart", "allinone", "peer", "server"):
        folder = QUICKSTARTS / name
        for key, service in services_of(folder).items():
            image = service.get("image", "")
            if "build" in service:
                # Only the server's console is built where it runs: it carries its API's address.
                assert (name, key, service.get("profiles")) == ("server", "console", ["console"]), (name, key)
                continue
            if "joeyenfield" in image:
                tag = "AI_PROXY_SITE_IMAGE_TAG" if (name, key) == ("server", "marketing-site") else "AI_PROXY_IMAGE_TAG"
                assert image.startswith(PUBLISHED) and image.endswith(f":${{{tag}:-latest}}"), (name, key)
            if name != "server":
                assert all(str(port).startswith("127.0.0.1:") for port in service.get("ports", [])), (name, key)
            for volume in service.get("volumes", []):
                source = str(volume).split(":", 1)[0]
                # Every file a stack mounts from the checkout is there (dist/ only once something is built).
                if source.startswith(".") and not source.endswith("/dist"):
                    assert (folder / source).resolve().exists(), (name, key, source)


def test_devhelpers_stacks_are_the_deploy_stacks_built_from_the_checkout():
    for dev, deploy in PAIRS.items():
        built, published = services_of(DEVHELPERS / dev), services_of(QUICKSTARTS / deploy)
        assert set(built) == set(published), dev
        for key, service in built.items():
            original = published[key]
            if original.get("image", "").startswith(PUBLISHED):
                # Local names, so a build never overwrites a published image; the context is the root.
                assert service["image"].startswith("localhost/ai-control-mesh-dev"), (dev, key)
                assert (DEVHELPERS / dev / service["build"]["context"]).resolve() == ROOT
                assert (ROOT / service["build"]["dockerfile"]).is_file(), (dev, key)
            else:
                assert service["image"] == original["image"] and "build" not in service, (dev, key)
            assert mounts_resolved(DEVHELPERS / dev, service) == mounts_resolved(QUICKSTARTS / deploy, original), (dev, key)
            rest = {field: value for field, value in service.items() if field not in ("image", "build", "volumes")}
            extra = DEV_ONLY.get((dev, key), set())
            if extra:
                assert extra <= set(rest["environment"]) and not extra & set(original.get("environment", {})), (dev, key)
                rest["environment"] = {name: value for name, value in rest["environment"].items() if name not in extra}
            assert rest == {field: value for field, value in original.items() if field not in ("image", "build", "volumes")}, (dev, key)
        # Their own project names, so a development build never opens the deploy stack's volumes.
        names = [yaml.safe_load((folder / "compose.yaml").read_text())["name"] for folder in (DEVHELPERS / dev, QUICKSTARTS / deploy)]
        assert names[0] == names[1].replace("-quickstart", "") + "-dev", names


def test_the_server_build_override_builds_its_published_images_from_the_checkout():
    server = QUICKSTARTS / "server"
    compose = services_of(server)
    override = yaml.safe_load((DEVHELPERS / "server" / "compose.build.yaml").read_text())["services"]
    assert set(override) == {key for key, service in compose.items() if service.get("image", "").startswith(PUBLISHED)}
    for service in override.values():
        # Compose reads every file's paths from the first file's folder: deploy/compose/server.
        assert (server / service["build"]["context"]).resolve() == ROOT
        assert "/" not in service["image"] and (ROOT / service["build"]["dockerfile"]).is_file()


def test_the_peer_quickstart_is_two_servers_that_can_link():
    services = yaml.safe_load((QUICKSTARTS / "peer" / "compose.yaml").read_text())["services"]
    assert set(services) == {"postgres-a", "postgres-b", "server-a", "auth-a", "server-b", "auth-b", "laptop-a", "gpu-box", "ollama"}
    a, b = services["server-a"]["environment"], services["server-b"]["environment"]
    # Each its own mesh id, published address (by service name, inside the stack), database and issuer.
    assert (a["AI_PROXY_MESH_ID"], b["AI_PROXY_MESH_ID"]) == ("server-a", "server-b")
    assert (a["AI_PROXY_MESH_URL"], b["AI_PROXY_MESH_URL"]) == ("http://server-a:9999", "http://server-b:9999")
    assert "postgres-a" in a["AI_PROXY_DATABASE_URL"] and "postgres-b" in b["AI_PROXY_DATABASE_URL"]
    assert a["AI_PROXY_OIDC_ISSUER"] != b["AI_PROXY_OIDC_ISSUER"]
    # Different host names, so one browser keeps both consoles' cookies.
    assert a["AI_PROXY_OIDC_BROWSER_ORIGINS"].startswith("http://localhost:") and b["AI_PROXY_OIDC_BROWSER_ORIGINS"].startswith("http://127.0.0.1:")
    # No port is the basic quickstart's or the all-in-one's, so they can run beside it.
    ports = [port for service in services.values() for port in service.get("ports", [])]
    assert len(ports) == len(set(ports)) and not any(":19999:" in port or ":20000:" in port for port in ports)
    # The machines sign in to their own server as the organization's person.
    assert services["laptop-a"]["environment"]["QUICKSTART_USER"] == "alice" and services["gpu-box"]["environment"]["QUICKSTART_USER"] == "bob"
    assert "alice" in a["QUICKSTART_ACCOUNTS"] and "bob" in b["QUICKSTART_ACCOUNTS"]


def test_the_relay_and_the_sign_in_take_the_peer_quickstarts_addresses(monkeypatch):
    monkeypatch.setenv("QUICKSTART_AUTH_HOST", "auth-b")
    monkeypatch.setenv("QUICKSTART_AUTH_PORT", "40000")
    monkeypatch.setenv("QUICKSTART_API", "http://127.0.0.1:39999")
    monkeypatch.setenv("QUICKSTART_USER", "bob")
    start, machine = module("start"), module("machine")
    assert (start.AUTH_HOST, start.AUTH_PORT, start.SERVER_HOST, start.SERVER_PORT) == ("auth-b", 40000, "server", 19999)
    assert (machine.API, machine.USER, machine.ISSUER) == ("http://127.0.0.1:39999", "bob", "http://localhost:20000")


def test_accounts_name_their_role_and_teams():
    start = module("start")
    assert start.account_arguments("acme:alice") == ["agent-server", "user", "add", "alice", "--org", "acme"]
    assert start.account_arguments("acme:alice:admin:platform+web") == [
        "agent-server", "user", "add", "alice", "--org", "acme", "--admin", "--team", "platform", "--team", "web",
    ]
    assert start.account_arguments("acme:bob:member:") == ["agent-server", "user", "add", "bob", "--org", "acme"]
    # A role and no teams: no team named after the role.
    assert start.account_arguments("acme:frank:member") == ["agent-server", "user", "add", "frank", "--org", "acme"]
    assert start.account_arguments("acme:grace:admin") == ["agent-server", "user", "add", "grace", "--org", "acme", "--admin"]


def test_the_basic_stack_has_two_organizations_with_two_people_each():
    server = services_of(DEVHELPERS / "basic")["server"]["environment"]
    orgs = dict(item.split("=", 1) for item in server["QUICKSTART_ORGS"].split(","))
    accounts = [entry.strip().split(":") for entry in server["QUICKSTART_ACCOUNTS"].split(",") if entry.strip()]
    assert len(orgs) == 2
    for org in orgs:
        people = [entry for entry in accounts if entry[0] == org]
        assert len(people) == 2 and sum(entry[2] == "admin" for entry in people) == 1, org
    assert {entry[0] for entry in accounts} == set(orgs)
    # The deploy quickstart stays a clean demo.
    assert "QUICKSTART_ACCOUNTS" not in services_of(QUICKSTART)["server"]["environment"]


TESTDATA = DEVHELPERS / "testdata"


def test_the_test_data_stack_gives_each_organization_a_machine_with_ollama():
    compose = yaml.safe_load((TESTDATA / "compose.yaml").read_text())
    services = compose["services"]
    server = services["server"]["environment"]
    accounts = [entry.strip().split(":") for entry in server["QUICKSTART_ACCOUNTS"].split(",") if entry.strip()]
    admins = {entry[1]: entry[0] for entry in accounts if entry[2:3] == ["admin"]}
    orgs = dict(item.split("=", 1) for item in server["QUICKSTART_ORGS"].split(","))
    assert {entry[0] for entry in accounts} == set(orgs) and len(orgs) >= 3
    for org in orgs:
        machine = services[f"{org}-box"]
        env = machine["environment"]
        # Signed in as its organization's admin, who may adopt its Ollama and grant the rest of it access.
        assert admins[env["QUICKSTART_USER"]] == org
        assert env["QUICKSTART_OLLAMA"] == "1" and env["QUICKSTART_OLLAMA_HOST"] == f"ollama-{org}"
        assert f"ollama-{org}" in services and f"ollama-{org}" in machine["depends_on"]
        assert env["QUICKSTART_MCP"].startswith("toolbox=") and env["QUICKSTART_ADOPT"] == "ollama"
        assert services[f"pull-{org}"]["environment"]["OLLAMA_HOST"] == f"ollama-{org}:11434"
        for volume in machine["volumes"]:
            source = str(volume).split(":", 1)[0]
            if source.startswith("."):
                assert (TESTDATA / source).resolve().exists(), source
    # Development only: built from the checkout under the basic stack's local names, on ports of its own.
    assert compose["name"].endswith("-dev") and "testdata" not in PAIRS
    assert all(service.get("image", "").startswith(("localhost/ai-control-mesh-dev", "docker.io/")) for service in services.values())
    ports = [port for service in services.values() for port in service.get("ports", [])]
    assert all(port.startswith("127.0.0.1:") for port in ports) and not any(":19999:" in port or ":20000:" in port for port in ports)


def test_the_toolbox_mcp_server_lists_and_calls_its_tools():
    spec = importlib.util.spec_from_file_location("testdata_toolbox", TESTDATA / "mcp_toolbox.py")
    toolbox = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(toolbox)
    assert toolbox.answer({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    started = toolbox.answer({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    assert started["result"]["serverInfo"]["name"] == "toolbox"
    names = {tool["name"] for tool in toolbox.answer({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
    assert {"now", "echo", "roll_dice", "word_count", "add_note", "list_notes"} <= names
    echoed = toolbox.answer({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "hi"}}})
    assert echoed["result"]["content"][0]["text"] == "hi"
    counted = toolbox.answer({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "word_count", "arguments": {"text": "a b\nc"}}})
    assert counted["result"]["content"][0]["text"].startswith("2 lines, 3 words")
    assert toolbox.answer({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope"}})["error"]["code"] == -32602
