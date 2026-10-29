"""The containers the Python agent-machine runs for the server: the Go package's tests
(machine/internal/containers/containers_test.go), and the `containers` settings commands."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_py import machine as command
from agent_py.machine import containers, settings
from agent_py.machine.containers import CLI, Manager, Spec, SpecError

FAKE_DOCKER = Path(__file__).resolve().parents[2] / "server" / "tests" / "fake_docker.py"


def refused(raw, message: str) -> None:
    with pytest.raises(SpecError) as error:
        containers.validate(raw)
    assert message in str(error.value), (raw, str(error.value))


def test_specs_accept_only_known_engines_settings_and_plain_images():
    spec = containers.validate({"engine": "strata", "env": {"MODEL": "IQ3_XXS", "CONTEXT": 32768, "VISION": ""}})
    assert spec == Spec(engine="strata", image="ghcr.io/niko1221/strata:latest-cuda13", port=8080, gpus="all", env={"MODEL": "IQ3_XXS", "CONTEXT": "32768"})
    # A JSON number written as a float reads the same, as Go reads every number.
    assert containers.validate({"engine": "strata", "env": {"CONTEXT": 32768.0}}).env == {"CONTEXT": "32768"}
    for raw, message in [
        ({"engine": "vllm"}, "Unknown engine"),
        ({"engine": "strata", "image": "--privileged"}, "Image"),
        ({"engine": "strata", "image": "ollama/ollama:latest x"}, "Image"),
        ({"engine": "strata", "env": {"LD_PRELOAD": "x"}}, "isn't a Strata setting"),
        ({"engine": "strata", "env": {"MODEL": "a\nb"}}, "invalid value"),
        ({"engine": "strata", "env": "MODEL=x"}, "env must be an object"),
        ({"engine": "strata", "gpus": "0;rm"}, "GPUs"),
        ({"engine": "strata", "gpus": "0\n"}, "GPUs"),
        ({"engine": "strata", "port": 80}, "Port"),
        ({"engine": "strata", "port": "1_0000"}, "Port must be a number"),
        ({"engine": "strata", "port": "x"}, "Port must be a number"),
        ({"engine": "ollama", "volume": "../etc"}, "Volume"),
        ({"engine": "ollama", "volume": "/data,readonly"}, "commas"),
        (None, "container must be an object"),
    ]:
        refused(raw, message)
    spec = containers.validate({"engine": "ollama", "volume": "D:\\models", "gpus": "0, 1", "port": "11500"})
    assert (spec.volume, spec.gpus, spec.port) == ("D:\\models", "0,1", 11500)
    assert containers.validate({"engine": "ollama", "gpus": ""}).gpus == "none"
    for name in ["", "-x", "a b", "a" * 41, "../x", "a\n"]:
        with pytest.raises(SpecError):
            containers.check_name(name)
    with pytest.raises(SpecError):
        containers.check_key("abc$(x)")
    with pytest.raises(SpecError):
        containers.check_bind("192.168.1.2")
    assert containers.check_bind("") == "127.0.0.1"


def test_run_args_follow_the_engine_templates_and_pythons_digest():
    """The digests were computed by the server's run_args before containers moved to machines (and
    the Go agent-machine's test has the same ones); they have to match, or every existing container
    would show as outdated."""
    spec = containers.validate({"engine": "strata", "port": 8081, "gpus": "0,1", "env": {"MODEL": "IQ3_XXS", "KV": "q4_0"}})
    args, digest = containers.run_args("strata-iq3", spec, "k123", "127.0.0.1")
    assert digest == "67833d78f3a7389c"
    assert args == ["run", "-d", "--name", "ai-proxy-strata-iq3", "--label", "ai-proxy.backend=strata-iq3", "--label", "ai-proxy.engine=strata",
                    "-p", "127.0.0.1:8081:8080", "-v", "strata-data:/data", "--stop-timeout", "120", "--ulimit", "memlock=-1:-1", "--shm-size", "2g",
                    "--gpus", '"device=0,1"', "-e", "API_KEY=k123", "-e", "KV=q4_0", "-e", "MODEL=IQ3_XXS", "-e", "PRESET=strata-iq3",
                    "--label", "ai-proxy.spec=67833d78f3a7389c", "ghcr.io/niko1221/strata:latest-cuda13"]
    shown = containers.command_line("docker", args, "k123")
    assert "--gpus '\"device=0,1\"' -e 'API_KEY=<api key>' -e KV=q4_0" in shown and "k123" not in shown
    other = Spec(engine="ollama", image="ollama/ollama:latest", port=11435, gpus="none", env={"OLLAMA_KV_CACHE_TYPE": "q8_0", "X": "caf\u00e9 \"q\" <&>"})
    args, digest = containers.run_args("ollama-box", other, "", "0.0.0.0")
    joined = " ".join(args)
    assert digest == "43c74135e0d20aed"
    assert "--gpus" not in joined and "PRESET" not in joined and "-v ollama-models:/root/.ollama" in joined
    changed = containers.validate({"engine": "strata", "port": 8081, "gpus": "0,1", "env": {"MODEL": "IQ3_S"}})
    assert containers.run_args("strata-iq3", changed, "k123", "127.0.0.1")[1] != "67833d78f3a7389c"


def test_everything_is_refused_until_the_machine_allows_containers():
    manager = Manager(machine=lambda: settings.Machine(id="m"))
    result = manager.handle({"type": "container", "request": "r1", "op": "status"})
    assert result["ok"] is False and result["type"] == "container_result" and result["request"] == "r1"
    assert "agent-machine containers on" in result["error"]
    described = manager.describe()
    assert described["enabled"] is False and described["engines"] == []
    # Nor does it learn ports for the tunnel.
    manager.handle({"op": "start", "name": "s1", "spec": {"engine": "ollama", "port": 11500}})
    assert manager.ports() == set()


def wait(manager: Manager, name: str, spec: dict) -> dict:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = manager.handle({"op": "status", "containers": {name: {"spec": spec, "key": "k1"}}})
        item = result["containers"][name]
        if item["job"] and item["job"]["state"] != "running":
            return item
        time.sleep(0.05)
    raise AssertionError(f"{name} didn't finish")


def test_jobs_start_switch_and_stop_containers_with_the_cli(tmp_path, monkeypatch):
    """Drives the server's own fake docker (server/tests/fake_docker.py) through a start, a switch and
    a stop, as the server's console would."""
    state = tmp_path / "docker.json"
    monkeypatch.setenv(containers.COMMAND_ENV, json.dumps([sys.executable, str(FAKE_DOCKER)]))
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    containers.forget_detection()
    manager = Manager(machine=lambda: settings.Machine(id="m", containers=True))
    described = manager.describe()
    assert described["online"] is True and described["version"] == "27.3.1" and len(described["engines"]) == 2, described
    assert described["engines"][0]["id"] == "strata" and "flags" not in described["engines"][0]
    spec = {"engine": "strata", "port": 18080, "env": {"MODEL": "IQ3_XXS"}}

    started = manager.handle({"op": "start", "name": "s1", "spec": spec, "key": "k1"})
    assert started["ok"] is True, started
    item = wait(manager, "s1", spec)
    job = item["job"]
    assert job["state"] == "done" and item["outdated"] is False and "<api key>" in item["command"], item
    lines = "|".join(job["lines"])
    assert "Pulling ghcr.io/niko1221/strata:latest-cuda13" in lines and "Creating the container" in lines
    assert item["state"]["running"] and item["state"]["health"] == "starting", item["state"]
    assert manager.ports() == {18080}
    logs = manager.handle({"op": "logs", "name": "s1", "key": "k1"})
    assert "<api key>" in logs["text"] and "k1" not in logs["text"], logs

    other = {"engine": "strata", "port": 18081}
    switched = manager.handle({"op": "start", "name": "s2", "spec": other, "stop_first": [{"name": "s1", "engine": "strata"}]})
    assert switched["ok"] is True and switched["job"]["action"] == "switch", switched
    assert wait(manager, "s2", other)["job"]["state"] == "done"
    docker = json.loads(state.read_text())
    assert docker["containers"]["ai-proxy-s1"]["running"] is False and docker["containers"]["ai-proxy-s2"]["running"] is True
    assert sum(1 for call in docker["calls"] if call[0] == "pull") == 1
    assert ["stop", "-t", "120", "ai-proxy-s1"] in docker["calls"]
    # A changed spec shows as outdated.
    assert manager.handle({"op": "status", "containers": {"s2": {"spec": {**other, "gpus": "none"}}}})["containers"]["s2"]["outdated"] is True
    assert manager.handle({"op": "remove", "name": "s1", "engine": "strata"})["ok"] is True
    assert "ai-proxy-s1" not in json.loads(state.read_text())["containers"]
    assert manager.handle({"op": "start", "name": "s3", "spec": {"engine": "strata", "image": "--privileged"}})["ok"] is False
    assert manager.handle({"op": "start", "name": "../x", "spec": spec})["ok"] is False
    assert manager.handle({"op": "frobnicate", "name": "s1"})["error"] == 'unknown container request "frobnicate"'


def test_one_job_per_container_at_a_time():
    manager = Manager()
    release = threading.Event()
    manager.run("s1", "start", lambda job: release.wait(10))
    with pytest.raises(SpecError, match="s1 is busy with start"):
        manager.run("s1", "stop", lambda job: None)
    manager.run("s2", "stop", lambda job: None)
    release.set()


def test_podman_gets_the_gpus_as_cdi_devices():
    spec = Spec(engine="ollama", image="ollama/ollama:latest", port=11500, gpus="0,1")
    joined = " ".join(containers.run_args_for("podman", "box", spec, "", "0.0.0.0")[0])
    assert "--device nvidia.com/gpu=0 --device nvidia.com/gpu=1" in joined and "--gpus" not in joined
    spec.gpus = "all"
    assert "--device nvidia.com/gpu=all" in " ".join(containers.run_args_for("podman", "box", spec, "", "0.0.0.0")[0])
    spec.gpus = "none"
    assert "gpu" not in " ".join(containers.run_args_for("podman", "box", spec, "", "0.0.0.0")[0])
    # Docker's arguments, and so existing containers' digests, are unchanged.
    assert containers.run_args_for("docker", "box", spec, "", "0.0.0.0") == containers.run_args("box", spec, "", "0.0.0.0")


def test_the_cli_that_answers_is_chosen():
    def script(code: str) -> list:
        return [sys.executable, "-c", code]

    # Docker installed but its engine down; Podman on Linux, answering without a Server part.
    docker_down = CLI("docker", script("import sys; print('{\"Client\": {\"Version\": \"27\"}, \"Server\": null}'); sys.exit(1)"))
    podman_up = CLI("podman", script("print('{\"Client\": {\"Version\": \"5.2.0\"}}')"))
    assert containers.pick(docker_down, podman_up).name == "podman"
    runtime = podman_up.probe()
    assert runtime["online"] and runtime["version"] == "5.2.0", runtime
    assert not docker_down.probe()["online"]
    docker_up = CLI("docker", script("print('{\"Client\": {\"Version\": \"27\"}, \"Server\": {\"Version\": \"27.3\"}}')"))
    assert containers.pick(docker_up, podman_up).name == "docker"
    assert docker_up.probe()["version"] == "27.3"
    # Neither answers: the installed one, so the console says why it isn't answering.
    assert containers.pick(docker_down, CLI("podman", script("import sys; sys.exit(125)"))).name == "docker"


def test_a_missing_cli_or_command_override_says_so(monkeypatch):
    monkeypatch.setenv(containers.COMMAND_ENV, "not json")
    cli = containers.new_cli("podman")
    assert cli.command() is None
    assert cli.run(5, "version") == (127, "", "podman isn't installed or isn't on PATH")
    assert cli.probe() == {"cli": "podman", "found": False, "online": False, "version": "", "error": "podman isn't installed or isn't on PATH"}
    assert containers.new_cli("anything").name == "docker"


def test_settings_commands_change_machine_json(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_PROXY_MACHINE_HOME", str(tmp_path / "home"))
    # The status display probes the CLI; a missing one keeps it quick and offline.
    monkeypatch.setenv(containers.COMMAND_ENV, "[]")
    root = tmp_path / "root"
    root.mkdir()
    for args in (["containers", "on"], ["containers", "cli", "podman"], ["launches", "on"], ["roots", "add", str(root)], ["name", "gpu-box"]):
        assert command.main(args) == 0, args
    machine = settings.Machine.load()
    assert machine.containers and machine.container_cli == "podman" and machine.launches and len(machine.roots) == 1 and machine.name == "gpu-box" and machine.id
    saved = json.loads((tmp_path / "home" / "machine.json").read_text())
    assert saved["containers"] is True and saved["container_cli"] == "podman"
    for args in (["containers", "maybe"], ["containers", "cli", "lxc"], ["roots", "add", str(root / "missing")], ["launches"], ["unknown"]):
        assert command.main(args) == 2, args
    assert command.main(["containers", "cli", "auto"]) == 0
    # Go leaves an empty container_cli out of machine.json.
    assert "container_cli" not in json.loads((tmp_path / "home" / "machine.json").read_text())
    assert command.main(["roots", "remove", str(root)]) == 0 and settings.Machine.load().roots == []
