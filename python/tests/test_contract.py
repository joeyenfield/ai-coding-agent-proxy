"""The server's contract tests for agent and agent-machine (server/tests/test_client.py), run
against the Python versions, plus Python-only variants of agent-machine tests (the Ollama probe
redirected, a found model server adopted and reached through the tunnel)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

from test_client import (  # noqa: F401  (the tests are collected from here, with this folder's fixtures)
    fake_ollama_server, fleet_machine, free_port, machine, new_session,
    test_a_controlled_terminal_is_relayed_to_the_server,
    test_a_fleet_machine_lists_its_folders_and_opens_a_shell_when_commands_are_on,
    test_a_fleet_machine_manages_its_model_server_for_the_console,
    test_a_fleet_machine_runs_an_agent_the_console_starts,
    test_a_fleet_machine_runs_the_containers_the_server_asks_for,
    test_a_machine_the_server_cant_reach_serves_agents_through_its_tunnel,
    test_a_fleet_machine_starts_and_stops_strata_when_the_owner_asks,
    test_account_agents_https_is_tunnelled_to_the_servers_intercept,
    test_an_agent_uses_a_stdio_mcp_server_a_machine_shares_through_its_tunnel,
    test_model_requests_stream_through_to_the_server,
    test_the_agent_proxy_alone_doesnt_join_the_fleet,
    test_the_launcher_reaches_the_server_only_through_the_proxys_short_list,
    test_the_launcher_runs_the_servers_agent_with_the_rendered_environment,
    test_the_programs_speak_the_servers_protocol,
    test_with_accounts_the_machine_signs_in_once_for_everything,
)


def probing(tmp_path, port: int, ollama_port: int = 0) -> dict:
    """agent-machine's environment, with it looking for Ollama on port instead of 11434, and starting
    a fake one (fake_ollama.py) on ollama_port when asked to."""
    folder = tmp_path / "site"
    folder.mkdir(exist_ok=True)
    lines = f"import agent_py.machine.providers as p\np.PROBES = (('ollama', '/api/version', {port}),)\n"
    env = {}
    if ollama_port:
        lines += f"import agent_py.machine.servers as s\ns.OLLAMA_PORT = {ollama_port}\n"
        env["AI_PROXY_OLLAMA_COMMAND"] = json.dumps([sys.executable, str(Path(__file__).parents[2] / "server" / "tests" / "fake_ollama.py")])
    (folder / "sitecustomize.py").write_text(lines)
    return {**env, "PYTHONPATH": os.pathsep.join([str(folder), os.environ["PYTHONPATH"]])}



def test_a_fleet_machine_manages_its_ollama_for_the_console(tmp_path, agent_binary, machine_binary):
    ollama_port = free_port()
    with fake_ollama_server() as (port, seen), machine(tmp_path, agent_binary) as m, \
            fleet_machine(m, machine_binary, tmp_path, ["servers", "on"], env=probing(tmp_path, port, ollama_port)) as fleet:
        base = f"{m.server}/api/machines/{fleet.box['id']}"
        [found] = fleet.box["providers"]
        assert (found["type"], found["port"], found["version"], found["models"]) == ("ollama", port, "0.12.3", 2)
        assert "Servers   : on" in fleet.status
        state = httpx.get(f"{base}/providers/ollama-{port}", timeout=30).json()
        assert state["online"] and state["version"] == "0.12.3", state
        assert [(model["name"], model["loaded"]) for model in state["models"]] == [("gemma3:4b", False), ("qwen3:8b", True)]
        assert state["models"][1]["gpu_percent"] == 50
        lines = [json.loads(line) for line in httpx.post(f"{base}/providers/ollama-{port}/models/pull", json={"name": "devstral:24b"}, timeout=60).text.splitlines()]
        assert lines[-1]["status"] == "success" and any(line.get("completed") == 100 for line in lines), lines
        assert httpx.post(f"{base}/providers/ollama-{port}/models/unload", json={"name": "qwen3:8b"}, timeout=30).status_code == 200
        assert ("POST", "/api/pull", {"model": "devstral:24b", "stream": True}) in seen
        assert any(method == "POST" and path == "/api/generate" and body.get("keep_alive") == 0 for method, path, body in seen)
        metrics = httpx.get(f"{base}/metrics", timeout=30).json()
        assert metrics["memory"]["total"] > 0 and "cpu" in metrics
        # Its Ollama: installed, not running, then started and stopped from the console with the saved settings.
        assert fleet.box["servers"]["enabled"] and fleet.box["servers"]["ollama"]["found"], fleet.box["servers"]
        settings = httpx.get(f"{base}/servers/ollama", timeout=30).json()
        assert "host" not in {setting["key"] for setting in settings["schema"]["settings"]}
        saved = httpx.put(f"{base}/servers/ollama", json={**settings["config"], "num_parallel": 2}, timeout=30)
        assert saved.status_code == 200 and saved.json()["env"]["OLLAMA_NUM_PARALLEL"] == "2"
        assert json.loads((tmp_path / "machine-home" / "ollama.json").read_text())["num_parallel"] == 2
        refused = httpx.put(f"{base}/servers/ollama", json={**settings["config"], "extra_env": "LD_PRELOAD=/tmp/x.so"}, timeout=30)
        assert refused.status_code == 409 and "OLLAMA_" in refused.json()["detail"]
        started = httpx.post(f"{base}/servers/ollama/start", json={}, timeout=60)
        assert started.status_code == 200, started.text
        assert httpx.get(f"http://127.0.0.1:{ollama_port}/api/version", timeout=5).json()["version"] == "0.0.0-fake"
        state = m.runtime.fleet.machines[fleet.box["id"]].servers["ollama"]
        assert state["running"] and state["managed"] and state["bind"] == "127.0.0.1", state
        assert "OLLAMA_NUM_PARALLEL=2" in (tmp_path / "machine-home" / "ollama.log").read_text()
        stopped = httpx.post(f"{base}/servers/ollama/stop", json={}, timeout=60)
        assert stopped.status_code == 200, stopped.text
        assert not m.runtime.fleet.machines[fleet.box["id"]].servers["ollama"]["running"]
        assert httpx.post(f"{base}/servers/strata/start", json={"profile": "x"}, timeout=30).status_code >= 400
        fleet.command("servers", "off")
        refused = httpx.post(f"{base}/providers/ollama-{port}/models/delete", json={"name": "gemma3:4b"}, timeout=30)
        assert refused.status_code == 409 and "agent-machine servers on" in refused.json()["detail"]
        assert httpx.get(f"{base}/providers/ollama-{port}", timeout=30).json()["online"]


def test_a_machine_the_server_cant_reach_serves_an_adopted_ollama_through_its_tunnel(tmp_path, agent_binary, machine_binary):
    """The machine says an address nothing answers on (TEST-NET), so agents' requests only arrive through its
    tunnel; here to a model server it found and the server adopted (the shared test reaches a container's port)."""
    with fake_ollama_server() as (port, seen), machine(tmp_path, agent_binary) as m, \
            fleet_machine(m, machine_binary, tmp_path, ["address", "203.0.113.9"], env=probing(tmp_path, port)) as fleet:
        adopted = httpx.post(f"{m.server}/api/machines/{fleet.box['id']}/providers/adopt", json={"type": "ollama"})
        assert adopted.status_code == 201, adopted.text
        assert adopted.json()["url"] == f"http://203.0.113.9:{port}"
        assert m.runtime.fleet.machines[fleet.box["id"]].tunnel
        # A real HTTP client this time (machine() mocks the server's upstream): the bytes go through the tunnel.
        m.runtime.client = httpx.AsyncClient(trust_env=False, timeout=30)
        session = new_session(m.server, backend=adopted.json()["backend"])
        listed = httpx.get(f"{m.server}/session/{session}/v1/models", timeout=30)
        assert listed.status_code == 200, listed.text
        assert {"qwen3:8b", "gemma3:4b"} <= {item["id"] for item in listed.json()["data"]}
        assert any(method == "GET" and path == "/api/tags" for method, path, _ in seen)
        # Keep-alive: the next request reuses the tunnel, and it still works.
        assert httpx.get(f"{m.server}/session/{session}/v1/models", timeout=30).status_code == 200
