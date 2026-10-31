"""The model servers agent-machine starts (agent_py.machine.servers), the same cases as the Go
agent-machine's machine/internal/servers tests, against the same fakes in server/tests."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_py.machine import servers, settings
import agent_py.machine as command_line

FAKES = Path(__file__).resolve().parents[2] / "server" / "tests"


def fake(name: str) -> str:
    return json.dumps([sys.executable, str(FAKES / name)])


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def on() -> settings.Machine:
    return settings.Machine(servers=True)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch) -> Path:
    folder = tmp_path / "machine-home"
    monkeypatch.setenv("AI_PROXY_MACHINE_HOME", str(folder))
    # Nothing real: no Strata, no systemd, unless a test names a fake.
    for name in (servers.STRATA_ENV, servers.SYSTEMCTL_ENV):
        monkeypatch.setenv(name, "[]")
    for name in (servers.SERVICE_CTL_ENV, servers.SERVICE_ENV_ENV, "FAKE_OLLAMA_FAIL"):
        monkeypatch.delenv(name, raising=False)
    return folder


def test_everything_is_refused_until_the_machine_allows_it():
    manager = servers.Manager(machine=settings.Machine)
    answer = manager.handle({"type": "server", "request": "r1", "op": "start", "kind": "ollama"})
    assert (answer["ok"], answer["type"], answer["request"]) == (False, "server_result", "r1")
    assert "servers on" in answer["error"]
    assert manager.describe() == {"enabled": False}


def test_ollama_starts_serves_and_stops(monkeypatch):
    monkeypatch.setenv(servers.OLLAMA_ENV, fake("fake_ollama.py"))
    manager = servers.Manager(machine=on, port=free_port())
    state = manager.describe()["ollama"]
    assert state["found"] and not state["running"], state
    assert not manager.handle({"request": "r1", "op": "start", "kind": "ollama", "bind": "0.0.0.0:1; evil"})["ok"]
    answer = manager.handle({"request": "r2", "op": "start", "kind": "ollama", "bind": "127.0.0.1"})
    assert answer["ok"], answer
    state = answer["servers"]["ollama"]
    assert (state["running"], state["managed"], state["version"], state["bind"]) == (True, True, "0.0.0-fake", "127.0.0.1"), state
    pid = servers.load_state()["ollama_pid"]
    # Starting it again is fine: it's already serving.
    answer = manager.handle({"request": "r3", "op": "start", "kind": "ollama"})
    assert answer["ok"] and servers.load_state()["ollama_pid"] == pid
    assert manager.handle({"request": "r4", "op": "stop", "kind": "ollama"})["ok"]
    assert not settings.alive(pid) and not manager.ollama_version()
    assert servers.state_path().read_text() == "{}\n"


def test_an_ollama_it_didnt_start_is_left_alone():
    port = free_port()
    other = subprocess.Popen([sys.executable, str(FAKES / "fake_ollama.py"), "serve"], env={**os.environ, "OLLAMA_HOST": f"127.0.0.1:{port}"})
    try:
        manager = servers.Manager(machine=on, port=port)
        for _ in range(100):
            if manager.ollama_version():
                break
            time.sleep(0.05)
        answer = manager.handle({"request": "r1", "op": "stop", "kind": "ollama"})
        assert not answer["ok"] and "can't stop it" in answer["error"], answer
        state = answer["servers"]["ollama"]
        assert state["running"] and not state["managed"], state
    finally:
        other.kill()
        other.wait()


def test_an_ollama_that_fails_to_start_says_why(monkeypatch):
    monkeypatch.setenv("FAKE_OLLAMA_FAIL", "1")
    monkeypatch.setenv(servers.OLLAMA_ENV, fake("fake_ollama.py"))
    answer = servers.Manager(machine=on, port=free_port()).handle({"request": "r1", "op": "start", "kind": "ollama"})
    assert not answer["ok"] and "address already in use" in answer["error"], answer


def test_strata_profiles_start_and_stop_through_strata(tmp_path, monkeypatch):
    state_file = tmp_path / "strata.json"
    state_file.write_text(json.dumps({"profiles": {"app-1": "stopped", "coder": "stopped"}}))
    monkeypatch.setenv("FAKE_STRATA_STATE", str(state_file))
    monkeypatch.setenv(servers.STRATA_ENV, fake("fake_strata.py"))
    manager = servers.Manager(machine=on, port=free_port())
    strata = manager.describe()["strata"]
    assert strata["found"] and len(strata["profiles"]) == 2, strata
    answer = manager.handle({"request": "r1", "op": "start", "kind": "strata", "profile": "coder"})
    assert answer["ok"], answer
    [started] = [item for item in answer["servers"]["strata"]["profiles"] if item["id"] == "coder"]
    assert started == {"id": "coder", "state": "ready", "url": "http://127.0.0.1:18777"}
    for bad in ("missing", "../app-1", "app-1 --plan", "", None, 7):
        assert not manager.handle({"request": "r2", "op": "start", "kind": "strata", "profile": bad})["ok"], bad
    assert "no Strata profile" in manager.handle({"request": "r2", "op": "start", "kind": "strata", "profile": "missing"})["error"]
    assert manager.handle({"request": "r3", "op": "stop", "kind": "strata", "profile": "coder"})["ok"]
    seen = json.loads(state_file.read_text())
    calls = {" ".join(call) for call in seen["calls"]}
    assert seen["profiles"]["coder"] == "stopped"
    assert {"runtime start --profile coder", "runtime stop --profile coder"} <= calls
    assert not any("missing" in call or "../app-1" in call for call in calls if call.startswith("runtime"))


def test_strata_profiles_follow_the_installed_models(tmp_path, monkeypatch):
    # As the Go TestStrataProfilesFollowTheInstalledModels.
    def config(name):
        path = tmp_path / f"{name}.json"
        path.write_text("{}")
        return str(path)

    iq3, coder, partial = config("strata-iq3_s"), config("strata-coder"), config("strata-q2")
    state_file = tmp_path / "strata.json"
    state_file.write_text(json.dumps({
        # app-1 names a run config that's gone; "iq3_s" is taken by a profile of another model.
        "profiles": {"app-1": "stopped", "iq3_s": "stopped"},
        "configs": {"app-1": str(tmp_path / "native-instances" / "b4bfef02.json"), "iq3_s": coder},
        "models": [
            {"id": "iq3_s", "name": "qwen3.8-flash-next-iq3_s", "source_config": iq3, "component_status": "present", "missing": []},
            {"id": "coder", "name": "qwen-coder", "source_config": coder, "component_status": "present", "missing": []},
            {"id": "q2", "name": "half-downloaded", "source_config": partial, "component_status": "missing", "missing": ["model"]},
        ],
    }))
    monkeypatch.setenv("FAKE_STRATA_STATE", str(state_file))
    monkeypatch.setenv(servers.STRATA_ENV, fake("fake_strata.py"))
    manager = servers.Manager(machine=on, port=free_port())

    def by_id():
        return {item["id"]: item for item in manager.describe()["strata"]["profiles"]}

    profiles = by_id()
    assert set(profiles) == {"app-1", "iq3_s", "iq3_s-2"}, profiles
    assert profiles["iq3_s-2"]["model"] == "qwen3.8-flash-next-iq3_s" and profiles["iq3_s"]["model"] == "qwen-coder"
    assert profiles["app-1"]["state"] == "broken" and "run config is gone" in profiles["app-1"]["broken"]
    assert set(by_id()) == set(profiles)  # asking again adds nothing
    answer = manager.handle({"request": "r1", "op": "start", "kind": "strata", "profile": "app-1"})
    assert not answer["ok"] and "run config is gone" in answer["error"], answer
    assert manager.handle({"request": "r2", "op": "start", "kind": "strata", "profile": "iq3_s-2"})["ok"]
    calls = [" ".join(call) for call in json.loads(state_file.read_text())["calls"]]
    assert [call for call in calls if call.startswith("profile create")] == [f"profile create iq3_s-2 --from-config {iq3}"]
    assert "runtime start --profile app-1" not in calls


def test_strata_without_models_list_uses_its_profiles(tmp_path, monkeypatch):
    state_file = tmp_path / "strata.json"
    state_file.write_text(json.dumps({"profiles": {"coder": "stopped"}, "no_models_list": True}))
    monkeypatch.setenv("FAKE_STRATA_STATE", str(state_file))
    monkeypatch.setenv(servers.STRATA_ENV, fake("fake_strata.py"))
    manager = servers.Manager(machine=on, port=free_port())
    strata = manager.describe()["strata"]
    assert "error" not in strata and [item["id"] for item in strata["profiles"]] == ["coder"], strata
    assert manager.handle({"request": "r1", "op": "start", "kind": "strata", "profile": "coder"})["ok"]


def test_strata_is_left_alone_while_servers_are_off(tmp_path, monkeypatch):
    state_file = tmp_path / "strata.json"
    monkeypatch.setenv("FAKE_STRATA_STATE", str(state_file))
    monkeypatch.setenv(servers.STRATA_ENV, fake("fake_strata.py"))
    manager = servers.Manager(machine=lambda: settings.Machine(), port=free_port())
    assert manager.describe() == {"enabled": False}
    assert not state_file.exists()


def test_strata_that_isnt_installed_is_said_so():
    manager = servers.Manager(machine=on, port=free_port())
    assert manager.describe()["strata"] == {"found": False, "profiles": []}
    answer = manager.handle({"request": "r1", "op": "start", "kind": "strata", "profile": "app-1"})
    assert not answer["ok"] and "Strata isn't installed" in answer["error"], answer
    assert not manager.handle({"request": "r2", "op": "restart", "kind": "strata", "profile": "app-1"})["ok"]


def test_strata_errors_and_answers_are_read_as_go_reads_them():
    assert servers.last_line("progress\n\n{\"ok\": true}\n") == '{"ok": true}'
    assert servers.strata_error({"error": {"message": "busy"}}, "") == "busy"
    assert servers.strata_error({"detail": "nope"}, "stderr") == "nope"
    assert servers.strata_error({}, " from stderr ") == "from stderr"
    assert servers.strata_error({}, "") == "it refused"


def test_a_systemd_ollama_restarts_through_its_service_with_its_settings(home, monkeypatch):
    """Started, stopped and restarted through it once `sudo agent-machine servers service` allowed it,
    with the console's settings in its environment file; until then refused with that command."""
    home.mkdir(parents=True)
    monkeypatch.setenv(servers.SYSTEMCTL_ENV, fake("fake_systemctl.py"))
    monkeypatch.setenv(servers.SERVICE_CTL_ENV, fake("fake_systemctl.py"))
    env_file = home / "ollama.env"
    monkeypatch.setenv(servers.SERVICE_ENV_ENV, str(env_file))
    monkeypatch.setenv("FAKE_SERVICE_STATE", str(home / "service.json"))
    port = free_port()
    manager = servers.Manager(machine=on, port=port)
    try:
        # Not set up yet: there's no environment file it may write.
        answer = manager.handle({"request": "r1", "op": "restart", "kind": "ollama"})
        assert not answer["ok"] and "sudo agent-machine servers service" in answer["error"], answer
        ollama = manager.describe()["ollama"]
        assert (ollama["service"], ollama["service_allowed"], ollama["managed"]) == ("systemd", False, False), ollama

        # After `sudo agent-machine servers service`: the file exists and this user may write it.
        env_file.write_text("# set up\n")
        servers.save_config({"num_parallel": 2})
        answer = manager.handle({"request": "r2", "op": "restart", "kind": "ollama", "bind": "127.0.0.1"})
        assert answer["ok"], answer
        written = env_file.read_bytes().decode()
        assert 'OLLAMA_NUM_PARALLEL="2"\n' in written and f'OLLAMA_HOST="127.0.0.1:{port}"\n' in written, written
        assert "\r" not in written and written.startswith("# Written by agent-machine")
        assert manager.ollama_version()
        ollama = manager.describe()["ollama"]
        assert (ollama["managed"], ollama["service_allowed"], ollama["running"], ollama["bind"]) == (True, True, True, "127.0.0.1"), ollama
        assert manager.ollama_settings()["applied_matches_saved"] is True
        assert servers.load_state()["service"] is True and "ollama_pid" not in servers.load_state()
        # The service really started with them.
        assert json.loads((home / "service.json").read_text())["env"]["OLLAMA_NUM_PARALLEL"] == "2"

        assert manager.handle({"request": "r3", "op": "stop", "kind": "ollama"})["ok"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and manager.ollama_version():
            time.sleep(0.1)
        assert not manager.ollama_version()
    finally:
        manager.handle({"request": "cleanup", "op": "stop", "kind": "ollama"})


def test_environment_values_are_quoted_for_systemd():
    assert servers.quote_env('a "b" \\c') == '"a \\"b\\" \\\\c"'
    assert servers.quote_env("two\nlines") == '"two lines"'


def test_setting_up_the_service_needs_linux_root_and_a_user(monkeypatch):
    monkeypatch.setattr(servers.sys, "platform", "darwin")
    with pytest.raises(servers.Refused, match="systemd service on Linux"):
        servers.set_up_service("joey", False)
    monkeypatch.setattr(servers.sys, "platform", "linux")
    monkeypatch.setattr(servers.os, "geteuid", lambda: 1000, raising=False)
    with pytest.raises(servers.Refused, match="sudo agent-machine servers service"):
        servers.set_up_service("joey", False)
    monkeypatch.setattr(servers.os, "geteuid", lambda: 0, raising=False)
    with pytest.raises(servers.Refused, match="no ollama.service"):
        servers.set_up_service("joey", False)
    monkeypatch.setenv(servers.SYSTEMCTL_ENV, fake("fake_systemctl.py"))
    for user in ("", "root", "a b", "a:b", "x,y"):
        with pytest.raises(servers.Refused, match="your own account"):
            servers.set_up_service(user, False)


def test_setting_up_the_service_writes_the_rule_the_drop_in_and_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(servers.sys, "platform", "linux")
    monkeypatch.setattr(servers.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(servers, "SUDOERS_PATH", str(tmp_path / "sudoers.d" / "agent-machine-ollama"))
    monkeypatch.setattr(servers, "DROP_IN_PATH", str(tmp_path / "system" / "ollama.service.d" / "agent-machine.conf"))
    monkeypatch.setattr(servers, "SERVICE_ENV_PATH", str(tmp_path / "agent-machine" / "ollama.env"))
    (tmp_path / "sudoers.d").mkdir()
    monkeypatch.setenv(servers.SYSTEMCTL_ENV, json.dumps(["/usr/bin/systemctl"]))
    ran = []
    real_run = servers.run

    def run(timeout, program, *args):
        ran.append([*program, *args])
        if program == ["/usr/bin/systemctl"] and args[:2] == ("show", "ollama"):
            return "loaded", None
        if program[0] in ("visudo", "chown", "/usr/bin/systemctl"):
            return "", None
        return real_run(timeout, program, *args)

    monkeypatch.setattr(servers, "run", run)
    if os.name == "nt":  # sys.platform says linux here
        # 0o440 is read-only on Windows, which then can't remove it; Linux's root can.
        real_write = servers.write_file
        monkeypatch.setattr(servers, "write_file", lambda path, text, mode: real_write(path, text, 0o644))
    servers.set_up_service("joey", False)
    rule = Path(servers.SUDOERS_PATH).read_text()
    assert rule.endswith("joey ALL=(root) NOPASSWD: /usr/bin/systemctl start ollama, /usr/bin/systemctl stop ollama, /usr/bin/systemctl restart ollama\n")
    assert not Path(servers.SUDOERS_PATH + ".new").exists()
    assert Path(servers.DROP_IN_PATH).read_text().endswith(f"[Service]\nEnvironmentFile=-{servers.SERVICE_ENV_PATH}\n")
    assert Path(servers.SERVICE_ENV_PATH).read_text() == "# agent-machine writes Ollama's settings here.\n"
    assert ["visudo", "-cf", servers.SUDOERS_PATH + ".new"] in ran
    assert ["chown", "joey", servers.SERVICE_ENV_PATH] in ran and ["/usr/bin/systemctl", "daemon-reload"] in ran

    # A rule visudo refuses is never put in place.
    os.remove(servers.SUDOERS_PATH)
    monkeypatch.setattr(servers, "run", lambda timeout, program, *args: ("bad", "exit status 1") if program[0] == "visudo" else run(timeout, program, *args))
    with pytest.raises(servers.Refused, match="didn't check out"):
        servers.set_up_service("joey", False)
    assert not Path(servers.SUDOERS_PATH).exists() and not Path(servers.SUDOERS_PATH + ".new").exists()

    monkeypatch.setattr(servers, "run", run)
    servers.set_up_service("joey", True)
    assert not any(Path(path).exists() for path in (servers.SUDOERS_PATH, servers.DROP_IN_PATH, servers.SERVICE_ENV_PATH))


def test_the_servers_service_command(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(servers, "set_up_service", lambda user, off: calls.append((user, off)))
    monkeypatch.setenv("SUDO_USER", "joey")
    assert command_line.main(["servers", "service"]) == 0
    assert command_line.main(["servers", "service", "off"]) == 0
    assert calls == [("joey", False), ("joey", True)]
    assert "Undo it with: sudo agent-machine servers service off" in capsys.readouterr().out

    def refuse(user, off):
        raise servers.Refused("run it with sudo: sudo agent-machine servers service")

    monkeypatch.setattr(servers, "set_up_service", refuse)
    assert command_line.main(["servers", "service"]) == 2
    assert "run it with sudo" in capsys.readouterr().err
    assert command_line.main(["servers", "maybe"]) == 2
    assert "servers takes on, off, or service [off]" in capsys.readouterr().err


def test_ollama_settings_are_checked_and_saved():
    manager = servers.Manager(machine=on, port=free_port())
    answer = manager.handle({"request": "r1", "op": "settings", "kind": "ollama"})
    current = answer["ollama_settings"]
    assert answer["ok"] and current["config"]["kv_cache_type"] == "q4_0" and current["applied_matches_saved"] is None
    assert "host" not in {item["key"] for item in current["schema"]["settings"]}
    for bad in ({"context_length": 10}, {"kv_cache_type": "q2"}, {"num_parallel": "many"},
                {"extra_env": "LD_PRELOAD=/tmp/x.so"}, {"extra_env": "OLLAMA_HOST=0.0.0.0:1"}, {"extra_env": "not a line"}):
        assert not manager.handle({"request": "r2", "op": "configure", "kind": "ollama", "config": bad})["ok"], bad
    answer = manager.handle({"request": "r3", "op": "configure", "kind": "ollama", "config": {
        "context_length": 32768.0, "flash_attention": False, "keep_alive": "2h", "gpus": "0", "extra_env": "# note\nOLLAMA_FOO=bar\nOLLAMA_BAZ="}})
    saved = answer["ollama_settings"]
    env = saved["env"]
    assert answer["ok"], answer
    assert (env["OLLAMA_CONTEXT_LENGTH"], env["OLLAMA_FLASH_ATTENTION"], env["CUDA_VISIBLE_DEVICES"], env["OLLAMA_FOO"]) == ("32768", "0", "0", "bar")
    assert env["OLLAMA_BAZ"] is None and env["OLLAMA_MODELS"] is None
    assert len(saved["warnings"]) == 1 and "Flash Attention" in saved["warnings"][0]
    text = servers.config_path().read_text()
    assert json.loads(text)["keep_alive"] == "2h"
    # Written as Go writes it: keys sorted, two-space indents, a newline at the end.
    assert list(json.loads(text)) == sorted(json.loads(text)) and text.endswith("}\n")
    assert servers.load_config()["context_length"] == 32768


def test_environment_applies_managed_variables():
    config = servers.default_config()
    config["models_dir"] = "/models"
    config["extra_env"] = "OLLAMA_DEBUG="
    env = servers.environment({"PATH": "/bin", "OLLAMA_MODELS": "/old", "OLLAMA_HOST": "1.2.3.4:5", "OLLAMA_DEBUG": "1", "CUDA_VISIBLE_DEVICES": "1"}, config)
    assert env["PATH"] == "/bin" and env["OLLAMA_MODELS"] == "/models"
    assert env["OLLAMA_KV_CACHE_TYPE"] == "q4_0" and env["OLLAMA_GPU_OVERHEAD"] == "0"
    assert not {"OLLAMA_HOST", "OLLAMA_DEBUG", "CUDA_VISIBLE_DEVICES"} & set(env)


def test_ollama_starts_with_its_settings_and_restarts(monkeypatch):
    monkeypatch.setenv(servers.OLLAMA_ENV, fake("fake_ollama.py"))
    manager = servers.Manager(machine=on, port=free_port())
    servers.save_config({"num_parallel": 2})
    assert manager.handle({"request": "r1", "op": "start", "kind": "ollama"})["ok"]
    try:
        assert "OLLAMA_NUM_PARALLEL=2" in servers.log_path().read_text(encoding="utf-8")
        # servers.json as the Go agent-machine writes it.
        state = json.loads(servers.state_path().read_text())
        assert list(state) == ["ollama_pid", "ollama_bind", "ollama_env"] and list(state["ollama_env"]) == sorted(state["ollama_env"])

        def matches():
            return manager.handle({"request": "r", "op": "settings", "kind": "ollama"})["ollama_settings"]["applied_matches_saved"]

        assert matches() is True
        manager.handle({"request": "r3", "op": "configure", "kind": "ollama", "config": {"num_parallel": 3}})
        assert matches() is False
        pid = servers.load_state()["ollama_pid"]
        answer = manager.handle({"request": "r5", "op": "restart", "kind": "ollama"})
        assert answer["ok"] and servers.load_state()["ollama_pid"] != pid, answer
        assert matches() is True
    finally:
        manager.handle({"request": "r9", "op": "stop", "kind": "ollama"})
