"""llama.cpp on the machine: its settings, starting and stopping it, installing it, its models and its
containers. Mirrors machine/internal/servers/llamacpp_test.go, providers/llamacpp_test.go and the
llama.cpp test in containers/containers_test.go."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

from agent_py.machine import containers, llamacpp, providers, servers, settings
from test_providers import Upstream, reply, send
from test_servers import fake, free_port, on


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch) -> Path:
    folder = tmp_path / "machine-home"
    monkeypatch.setenv("AI_PROXY_MACHINE_HOME", str(folder))
    for name in (servers.STRATA_ENV, servers.SYSTEMCTL_ENV):
        monkeypatch.setenv(name, "[]")
    for name in ("FAKE_LLAMACPP_FAIL", "LLAMA_CACHE", llamacpp.RELEASES_ENV, llamacpp.APT_ENV, providers.LLAMA_PORT_ENV, "TERMUX_VERSION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PREFIX", "")
    monkeypatch.setattr(llamacpp, "_job", None)
    return folder


def test_llamacpp_settings_are_checked_and_saved():
    table = llamacpp.TABLE
    config = table.load()
    assert config["ctx_size"] == 16384 and config["fit"] is True
    for refused in (
        {"ctx_size": "big"}, {"flash_attn": "sometimes"}, {"cache_type_k": "q2"}, {"device": "CUDA0\nLLAMA_ARG_AGENT=1"},
        {"extra_env": "PATH=/tmp"}, {"extra_env": "LLAMA_ARG_HOST=0.0.0.0"}, {"extra_env": "LLAMA_API_KEY=secret"},
        {"extra_env": "LLAMA_ARG_TOOLS_RUNTIME=docker:alpine"}, {"extra_env": "LLAMA_ARG_MCP_SERVERS_JSON={}"},
        {"extra_env": "LLAMA_ARG_MODELS_PRESET=/etc/presets.ini"},
    ):
        with pytest.raises(servers.Refused):
            table.save(refused)
    config = table.save({"ctx_size": 8192.0, "reasoning_budget": 0.0, "fit": False, "gpu_layers": "all", "extra_env": "GGML_VK_VISIBLE_DEVICES=0\nLLAMA_ARG_BATCH=1024"})
    env = table.managed(config)
    assert {name: env[name] for name in ("LLAMA_ARG_CTX_SIZE", "LLAMA_ARG_THINK_BUDGET", "LLAMA_ARG_FIT", "LLAMA_ARG_N_GPU_LAYERS", "LLAMA_ARG_UI", "GGML_VK_VISIBLE_DEVICES", "LLAMA_ARG_BATCH", "LLAMA_ARG_JINJA")} == {
        "LLAMA_ARG_CTX_SIZE": "8192", "LLAMA_ARG_THINK_BUDGET": "0", "LLAMA_ARG_FIT": "off", "LLAMA_ARG_N_GPU_LAYERS": "all", "LLAMA_ARG_UI": "off",
        "GGML_VK_VISIBLE_DEVICES": "0", "LLAMA_ARG_BATCH": "1024", "LLAMA_ARG_JINJA": "1"}
    # Empty settings are unset.
    assert env["LLAMA_ARG_DEVICE"] is None and env["LLAMA_CACHE"] is None
    assert table.load()["ctx_size"] == 8192
    assert len(llamacpp.warnings(config)) == 1
    # What the console mustn't set doesn't come in from agent-machine's own environment either.
    started = llamacpp.environment({"LLAMA_ARG_HOST": "0.0.0.0", "LLAMA_API_KEY": "x", "LLAMA_ARG_AGENT": "1", "HOME": "/home/me"}, config)
    assert "LLAMA_ARG_HOST" not in started and "LLAMA_API_KEY" not in started and "LLAMA_ARG_AGENT" not in started and started["HOME"] == "/home/me"
    # The same multi-line refusal applies to Ollama's table.
    with pytest.raises(servers.Refused, match="one line"):
        servers.save_config({"models_dir": "/x\nOLLAMA_HOST=0.0.0.0"})


def test_llamacpp_starts_with_its_settings_serves_and_stops(home, monkeypatch):
    monkeypatch.setenv(llamacpp.LLAMA_ENV, fake("fake_llamacpp.py"))
    manager = servers.Manager(on, llama_port=free_port())
    state = manager.describe()["llamacpp"]
    assert state["found"] and not state["running"] and state["version"] == "b9999" and state["source"] == "found", state
    answer = manager.handle({"request": "c", "op": "configure", "kind": "llamacpp", "config": {"ctx_size": 32768, "cache_dir": str(home / "models")}})
    assert answer["ok"] and answer["llamacpp_settings"], answer
    answer = manager.handle({"request": "s", "op": "start", "kind": "llamacpp"})
    assert answer["ok"], answer
    try:
        state = answer["servers"]["llamacpp"]
        assert state["running"] and state["managed"], state
        props = httpx.get(f"http://127.0.0.1:{manager.llama_port}/props", timeout=5).json()
        assert props["env"]["LLAMA_ARG_CTX_SIZE"] == "32768" and props["env"]["LLAMA_CACHE"] == str(home / "models")
        assert "--host 127.0.0.1" in " ".join(props["argv"])
        assert manager.handle({"request": "g", "op": "settings", "kind": "llamacpp"})["llamacpp_settings"]["applied_matches_saved"] is True
        # Ollama's start and stop leave what agent-machine knows about llama-server alone.
        servers.save_state(servers.with_ollama(0, "", None, False))
        assert servers.load_state()["llamacpp_pid"]
        assert manager.handle({"request": "r", "op": "restart", "kind": "llamacpp"})["ok"]
    finally:
        answer = manager.handle({"request": "t", "op": "stop", "kind": "llamacpp"})
    assert answer["ok"] and not answer["servers"]["llamacpp"]["running"], answer
    assert "LLAMA_ARG_CTX_SIZE=32768" in llamacpp.log_path().read_text(encoding="utf-8")


def test_a_llama_server_that_fails_to_start_says_why(monkeypatch):
    monkeypatch.setenv(llamacpp.LLAMA_ENV, fake("fake_llamacpp.py"))
    monkeypatch.setenv("FAKE_LLAMACPP_FAIL", "1")
    answer = servers.Manager(on, llama_port=free_port()).handle({"request": "s", "op": "start", "kind": "llamacpp"})
    assert not answer["ok"] and "couldn't bind" in answer["error"], answer


@pytest.mark.parametrize("output, want", [
    (b"version: 0.8.0-dev (build 12032, commit dde879bc4)\nbuilt with Clang", "b12032"),
    (b"version: 7031 (abc1234)\nbuilt with cc", "b7031"),
    (b"something else", ""),
])
def test_the_build_is_read_from_either_version_line(output, want):
    assert llamacpp.parse_version(output) == want


def test_sycl_builds_are_named_for_intel():
    assert llamacpp.variant_label("sycl", "windows")[0] == "SYCL (Intel)"
    assert llamacpp.variant_label("sycl-fp16", "linux")[0] == "SYCL FP16 (Intel)"


@pytest.mark.parametrize("platform_name, goos, goarch, want", [
    ("ubuntu-x64", "linux", "amd64", "cpu"),
    ("ubuntu-vulkan-x64", "linux", "amd64", "vulkan"),
    ("ubuntu-cuda-12.8-x64", "linux", "amd64", "cuda-12.8"),
    ("ubuntu-cuda-13.4-arm64", "linux", "arm64", "cuda-13.4"),
    ("linux-arm64-snapdragon", "linux", "arm64", "snapdragon"),
    ("ubuntu-26.04-s390x", "linux", "amd64", ""),
    ("ubuntu-x64", "linux", "arm64", ""),
    ("win-cpu-x64", "windows", "amd64", "cpu"),
    ("win-cuda-12.4-x64", "windows", "amd64", "cuda-12.4"),
    ("win-opencl-adreno-arm64", "windows", "arm64", "opencl-adreno"),
    ("macos-arm64", "darwin", "arm64", "cpu"),
    ("android-arm64", "linux", "arm64", ""),
    ("win-cpu-x64", "linux", "amd64", ""),
])
def test_release_assets_are_matched_to_this_computer(platform_name, goos, goarch, want):
    assert llamacpp.variant_of(platform_name, goos, goarch) == want


def test_a_releases_builds_are_its_variants():
    found = {"tag_name": "b100", "assets": [
        {"name": "llama-b100-bin-win-cuda-12.4-x64.zip", "size": 10},
        {"name": "cudart-llama-bin-win-cuda-12.4-x64.zip", "size": 5},
        {"name": "llama-b100-bin-win-cpu-x64.zip", "size": 2},
        {"name": "llama-b100-bin-win-vulkan-x64.zip", "size": 3},
        {"name": "llama-b99-bin-win-hip-x64.zip", "size": 3},
        {"name": "llama-b100-xcframework.zip", "size": 3},
    ]}
    variants = llamacpp.release_variants(found, "windows", "amd64")
    assert [variant["id"] for variant in variants] == ["cpu", "cuda-12.4", "vulkan"]
    cuda = variants[1]
    assert cuda["_runtime"]["name"] == "cudart-llama-bin-win-cuda-12.4-x64.zip" and cuda["size"] == 15


def archive(files: Dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    if os.name == "nt":
        with zipfile.ZipFile(buffer, "w") as writer:
            for name, body in files.items():
                writer.writestr(name, body)
        return buffer.getvalue()
    with tarfile.open(fileobj=buffer, mode="w:gz") as writer:
        for name, body in files.items():
            data = body.encode()
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o755
            writer.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def fake_github(monkeypatch, made: List[Upstream], data: bytes, digest: str) -> None:
    systems, arch = llamacpp.platform_tokens(llamacpp.go_os(), llamacpp.go_arch())
    if not systems or not arch:
        pytest.skip("llama.cpp publishes no build for this computer")
    name = f"llama-b100-bin-win-cpu-{arch}.zip" if os.name == "nt" else f"llama-b100-bin-{systems[0]}-{arch}.tar.gz"
    upstream = Upstream({})
    made.append(upstream)
    base = f"http://127.0.0.1:{upstream.port}"
    upstream.routes["GET /releases"] = reply([
        {"tag_name": "b101", "assets": []},
        {"tag_name": "b100", "prerelease": True, "assets": [{"name": name, "size": len(data), "digest": digest, "browser_download_url": base + "/download"}]},
    ])
    upstream.routes["GET /download"] = lambda handler: send(handler, 200, data, "application/octet-stream")
    monkeypatch.setenv(llamacpp.RELEASES_ENV, base + "/releases")


@pytest.fixture
def made():
    servers_made: List[Upstream] = []
    yield servers_made
    for upstream in servers_made:
        upstream.close()


def wait_install() -> Dict[str, Any]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        progress = llamacpp.progress()
        if progress and progress["state"] != "installing":
            return progress
        time.sleep(0.05)
    raise AssertionError("the install didn't finish")


def test_installing_from_github_checks_the_download(monkeypatch, made):
    monkeypatch.delenv(llamacpp.LLAMA_ENV, raising=False)
    data = archive({"llama-b100/" + llamacpp.server_name(): "#!/bin/sh\n", "llama-b100/libggml-cpu.so": "lib"})
    fake_github(monkeypatch, made, data, "sha256:" + "0" * 64)
    manager = servers.Manager(on, llama_port=free_port())
    options = manager.handle({"request": "o", "op": "install_options", "kind": "llamacpp"})["llamacpp_install"]
    assert options["release"] == "b100" and len(options["variants"]) == 1 and options["variants"][0]["recommended"], options
    assert not manager.handle({"request": "i", "op": "install", "kind": "llamacpp", "variant": "cpu; rm -rf /"})["ok"]
    assert manager.handle({"request": "i", "op": "install", "kind": "llamacpp", "variant": "cpu"})["ok"]
    done = wait_install()
    assert done["state"] == "failed" and "isn't what GitHub lists" in done["error"], done
    assert not llamacpp.load_install().get("binary")

    fake_github(monkeypatch, made, data, sha(data))
    assert manager.handle({"request": "i2", "op": "install", "kind": "llamacpp", "variant": "cpu"})["ok"]
    done = wait_install()
    assert done["state"] == "installed" and done["release"] == "b100", done
    record = llamacpp.load_install()
    assert record["source"] == "github" and record["variant"] == "cpu" and record["binary"].startswith(str(llamacpp.install_root()))
    assert llamacpp.llama_command() == [record["binary"]]
    state = manager.describe()["llamacpp"]
    assert (state["source"], state["release"], state["variant"]) == ("github", "b100", "cpu")
    assert manager.handle({"request": "u", "op": "uninstall", "kind": "llamacpp"})["ok"]
    assert not llamacpp.install_root().exists()


def test_archives_cant_write_outside_their_folder(monkeypatch, made):
    data = archive({"../../escaped": "x", "llama/" + llamacpp.server_name(): "x"})
    fake_github(monkeypatch, made, data, sha(data))
    llamacpp.start_install("cpu")
    done = wait_install()
    assert done["state"] == "failed" and "outside the archive" in done["error"], done
    assert not (settings.home().parent / "escaped").exists()


@pytest.mark.skipif(os.name == "nt", reason="Termux is Android's")
def test_installing_in_termux_uses_its_packages(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    monkeypatch.setenv("PREFIX", str(prefix))
    monkeypatch.setenv("TERMUX_VERSION", "0.118")
    calls = tmp_path / "calls"
    script = tmp_path / "apt-get"
    script.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\nif [ "$1" = install ]; then mkdir -p "{prefix}/bin" && touch "{prefix}/bin/llama-server"; fi\n')
    monkeypatch.setenv(llamacpp.APT_ENV, json.dumps(["/bin/sh", str(script)]))
    manager = servers.Manager(on, llama_port=free_port())
    options = manager.handle({"request": "o", "op": "install_options", "kind": "llamacpp"})["llamacpp_install"]
    assert options["source"] == "termux" and options["variants"][0]["id"] == "opencl"
    assert manager.handle({"request": "i", "op": "install", "kind": "llamacpp", "variant": "metal"})["ok"]
    assert wait_install()["state"] == "failed"
    assert manager.handle({"request": "i", "op": "install", "kind": "llamacpp", "variant": "opencl"})["ok"]
    assert wait_install()["state"] == "installed"
    asked = calls.read_text()
    assert "update" in asked and "install -y -o Dpkg::Options::=--force-confnew llama-cpp llama-cpp-backend-opencl ocl-icd opencl-vendor-driver" in asked
    record = llamacpp.load_install()
    assert record["source"] == "termux" and record["binary"] == str(prefix / "bin" / "llama-server")
    assert not manager.handle({"request": "u", "op": "uninstall", "kind": "llamacpp"})["ok"]


# The provider: llama-server's router, played by an Upstream.

def router(made: List[Upstream], key: str = "", outcome: str = "") -> Upstream:
    started = threading.Event()
    model = settings.home().parent / "qwen.gguf"
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b"\0" * 1234)

    def guard(route):
        def guarded(handler: BaseHTTPRequestHandler) -> None:
            if key and handler.headers.get("authorization") != f"Bearer {key}":
                send(handler, 401, b'{"error":{"code":401,"message":"Invalid API Key"}}', "application/json")
                return
            route(handler)
        return guarded

    def post_models(handler: BaseHTTPRequestHandler) -> None:
        started.set()
        reply({"success": True})(handler)

    def events(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("content-type", "text/event-stream")
        handler.send_header("connection", "close")
        handler.end_headers()
        handler.wfile.flush()
        handler.close_connection = True
        if not started.wait(5):
            return
        for line in ('{"model":"someone/else-GGUF:Q4_K_M","event":"download_finished","data":{}}',
                     '{"model":"ggml-org/new-GGUF:Q4_K_M","event":"download_progress","data":{"u1":{"done":40,"total":100},"u2":{"done":10,"total":100}}}',
                     outcome):
            handler.wfile.write(f"data: {line}\n\n".encode())
            handler.wfile.flush()

    upstream = Upstream({
        "GET /models": guard(reply({"data": [
            {"id": "unsloth/zeta-GGUF:Q4_K_M", "path": "", "status": {"value": "unloaded", "failed": True}},
            {"id": "ggml-org/qwen-GGUF:Q8_0", "path": str(model), "status": {"value": "loaded"}, "meta": {"n_ctx_train": 32768}},
            {"id": "big/one-GGUF:Q4_K_M", "status": {"value": "downloading", "progress": {"a": {"done": 5, "total": 10}, "b": {"done": 5, "total": 10}}}},
        ]})),
        "POST /models": guard(post_models),
        "GET /models/sse": guard(events),
        "DELETE /models": guard(reply({"success": True})),
        "POST /models/load": guard(reply({"success": True})),
        "POST /models/unload": guard(reply({"success": False, "error": {"message": "model is not loaded"}})),
    })
    made.append(upstream)
    return upstream


def off() -> settings.Machine:
    return settings.Machine()


def test_llama_status_lists_the_routers_models(made):
    fake_router = router(made)
    state = providers.Manager(off).handle({"op": "status", "kind": "llamacpp", "port": fake_router.port})["state"]
    models = state["models"]
    assert state["online"] and state["router"] and len(models) == 3
    assert models[0]["name"] == "ggml-org/qwen-GGUF:Q8_0" and models[0]["loaded"] and models[0]["size"] == 1234 and models[0]["context_train"] == 32768
    assert models[1]["state"] == "downloading" and models[1]["progress"]["completed"] == 10
    assert models[2]["failed"] is True
    assert providers.Manager(off).handle({"op": "status", "kind": "llamacpp", "port": 1})["state"]["online"] is False


def test_llama_pull_relays_the_downloads_progress(made):
    fake_router = router(made, "k3y", '{"model":"ggml-org/new-GGUF:Q4_K_M","event":"download_finished","data":{}}')
    updates: List[Dict[str, Any]] = []
    answer = providers.Manager(on).handle({"op": "pull", "kind": "llamacpp", "port": fake_router.port, "model": "ggml-org/new-GGUF:Q4_K_M", "key": "k3y"}, updates.append)
    assert answer["ok"], answer
    assert fake_router.bodies["POST /models"]["model"] == "ggml-org/new-GGUF:Q4_K_M"
    # Another model's events aren't this pull's.
    assert len(updates) == 3 and updates[1]["completed"] == 50 and updates[1]["total"] == 200 and updates[2]["status"] == "success", updates
    # Without the container's key, the router refuses.
    assert providers.Manager(on).handle({"op": "status", "kind": "llamacpp", "port": fake_router.port})["state"]["online"] is False


def test_llama_pull_says_why_a_download_failed(made):
    fake_router = router(made, outcome='{"model":"ggml-org/new-GGUF:Q4_K_M","event":"download_failed","data":{"error":"repo not found"}}')
    answer = providers.Manager(on).handle({"op": "pull", "kind": "llamacpp", "port": fake_router.port, "model": "ggml-org/new-GGUF:Q4_K_M"})
    assert not answer["ok"] and answer["error"] == "repo not found", answer
    for bad in ("not-a-repo", "a/b/c", "a/b:Q4?x=1", "../x"):
        assert not providers.Manager(on).handle({"op": "pull", "kind": "llamacpp", "port": fake_router.port, "model": bad})["ok"], bad


def test_llama_delete_load_unload(made):
    fake_router = router(made)
    manager = providers.Manager(on)
    assert manager.handle({"op": "delete", "kind": "llamacpp", "port": fake_router.port, "model": "ggml-org/qwen-GGUF:Q8_0"})["ok"]
    answer = manager.handle({"op": "load", "kind": "llamacpp", "port": fake_router.port, "model": "ggml-org/qwen-GGUF:Q8_0"})
    assert answer["ok"] and fake_router.bodies["POST /models/load"]["model"] == "ggml-org/qwen-GGUF:Q8_0"
    answer = manager.handle({"op": "unload", "kind": "llamacpp", "port": fake_router.port, "model": "x/y"})
    assert not answer["ok"] and "not loaded" in answer["error"], answer
    assert not providers.Manager(off).handle({"op": "delete", "kind": "llamacpp", "port": fake_router.port, "model": "x/y"})["ok"]


def test_llama_log_is_agent_machines(home):
    manager = providers.Manager(off)
    assert not manager.handle({"op": "log", "kind": "llamacpp", "port": 8080})["ok"]
    home.mkdir(parents=True, exist_ok=True)
    (home / "llamacpp.log").write_text("main: server is listening")
    assert manager.handle({"op": "log", "kind": "llamacpp", "port": providers.LLAMA_PORT})["text"] == "main: server is listening"


def test_a_llamacpp_container_serves_its_router_with_the_servers_key():
    spec = containers.validate({"engine": "llamacpp", "image": "ghcr.io/ggml-org/llama.cpp:server-cuda", "port": 19931.0, "env": {"LLAMA_ARG_CTX_SIZE": "32768"}})
    with pytest.raises(containers.SpecError):
        containers.validate({"engine": "llamacpp", "port": 19931.0, "env": {"LLAMA_ARG_AGENT": "1"}})
    args, _ = containers.run_args_for("docker", "llama", spec, "s3cret", "0.0.0.0")
    joined = " ".join(args)
    for want in ("-p 0.0.0.0:19931:8080", "-v llamacpp-models:/root/.cache/llama.cpp", "-e LLAMA_API_KEY=s3cret", "-e LLAMA_ARG_HOST=0.0.0.0",
                 "-e LLAMA_ARG_PORT=8080", "-e LLAMA_ARG_CTX_SIZE=32768"):
        assert want in joined, (want, joined)
    assert "s3cret" not in containers.command_line("docker", args, "s3cret")
