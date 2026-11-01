"""agent-machine downloading a llama.cpp model's GGUF files itself, into its GGUF folder, as the Go one
does (machine/internal/providers/llamacpp_files_test.go)."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

import pytest

from agent_py.machine import llamacpp, providers
from test_providers import Upstream, reply
from test_servers import on

FILES = {
    "Qwen3-8B-Q4_K_M.gguf": b"q4" * 3000,
    "Q8_0/Qwen3-8B-Q8_0-00001-of-00002.gguf": b"a" * 2500,
    "Q8_0/Qwen3-8B-Q8_0-00002-of-00002.gguf": b"b" * 1500,
    "mmproj-BF16.gguf": b"bf16 projector",
    "mmproj-F16.gguf": b"f16 projector",
}


class HuggingFace:
    """Plays Hugging Face: a repo listing, and its files with ranges."""

    def __init__(self) -> None:
        self.ranges: List[str] = []
        self.bad = ""
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                path = self.path.split("?")[0]
                if path == "/api/models/someone/Qwen3-8B-GGUF/tree/main":
                    listing = [{"type": "file", "path": name, "size": len(data),
                                "lfs": {"oid": "0" * 64 if name == fake.bad else hashlib.sha256(data).hexdigest(), "size": len(data)}}
                               for name, data in FILES.items()]
                    listing += [{"type": "file", "path": "README.md", "size": 10}, {"type": "directory", "path": "Q8_0"}]
                    return self.answer(200, json.dumps(listing).encode())
                prefix = "/someone/Qwen3-8B-GGUF/resolve/main/"
                name = path[len(prefix):] if path.startswith(prefix) else ""
                if name not in FILES:
                    return self.answer(404, b"not found")
                data = FILES[name]
                wanted = self.headers.get("range", "")
                if wanted.startswith("bytes="):
                    start = int(wanted[len("bytes="):].rstrip("-"))
                    fake.ranges.append(f"{name}:{start}")
                    return self.answer(206, data[start:])
                self.answer(200, data)

            def answer(self, status: int, data: bytes) -> None:
                self.send_response(status)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch) -> Path:
    folder = tmp_path / "machine-home"
    monkeypatch.setenv("AI_PROXY_MACHINE_HOME", str(folder))
    for name in (providers.LLAMA_PORT_ENV, "HF_TOKEN", "TERMUX_VERSION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PREFIX", "")
    return folder


@pytest.fixture
def hf():
    fake = HuggingFace()
    yield fake
    fake.close()


@pytest.fixture
def made():
    upstreams: List[Upstream] = []
    yield upstreams
    for upstream in upstreams:
        upstream.close()


def manager(hf: HuggingFace) -> providers.Manager:
    made = providers.Manager(on)
    made.catalogs.hugging_face_url = hf.url + "/api/models"
    made.hugging_face_files_url = hf.url
    return made


def folder() -> Path:
    return Path(llamacpp.models_dir())


def folder_router(made: List[Upstream], monkeypatch) -> Upstream:
    """An old router serving the GGUF folder, as agent-machine's own: it lists the folder and has no downloads."""
    def models(handler: BaseHTTPRequestHandler) -> None:
        listed = [{"id": entry.name, "status": {"value": "unloaded"}} for entry in folder().iterdir()] if folder().is_dir() else []
        reply({"data": listed})(handler)

    router = Upstream({"GET /models": models, "POST /models/unload": reply({"success": True})})
    made.append(router)
    monkeypatch.setenv(providers.LLAMA_PORT_ENV, str(router.port))
    return router


def test_llama_files_plans_the_quants_shards_and_projector(hf):
    answer = manager(hf).handle({"op": "files", "kind": "llamacpp", "port": providers.llama_server_port(), "model": "someone/Qwen3-8B-GGUF:q8_0"})
    plan = answer["result"]
    assert answer["ok"] and plan["name"] == "Qwen3-8B-Q8_0" and plan["dir"] == str(folder() / "Qwen3-8B-Q8_0") and plan["serves_folder"] is True, answer
    first, projector = plan["files"][0], plan["files"][2]
    assert first["url"] == hf.url + "/someone/Qwen3-8B-GGUF/resolve/main/Q8_0/Qwen3-8B-Q8_0-00001-of-00002.gguf"
    assert first["path"] == os.path.join("Qwen3-8B-Q8_0", "Qwen3-8B-Q8_0-00001-of-00002.gguf") and len(plan["files"]) == 3
    assert projector["repo_path"] == "mmproj-F16.gguf"
    # "user/repo" alone is Q4_K_M; a quant the repo hasn't says which it has.
    plan = manager(hf).handle({"op": "files", "kind": "llamacpp", "port": 1, "model": "someone/Qwen3-8B-GGUF"})["result"]
    assert plan["model"] == "someone/Qwen3-8B-GGUF:Q4_K_M" and plan["serves_folder"] is False
    answer = manager(hf).handle({"op": "files", "kind": "llamacpp", "port": 1, "model": "someone/Qwen3-8B-GGUF:Q2_K"})
    assert not answer["ok"] and "Q4_K_M, Q8_0" in answer["error"], answer


def test_a_direct_pull_downloads_checks_and_serves_the_files(hf, made, monkeypatch):
    router = folder_router(made, monkeypatch)
    updates: List[Dict[str, Any]] = []
    answer = manager(hf).handle({"op": "pull", "kind": "llamacpp", "port": router.port, "model": "someone/Qwen3-8B-GGUF:Q8_0", "method": "direct"}, updates.append)
    assert answer["ok"], answer
    for name in ("Qwen3-8B-Q8_0-00001-of-00002.gguf", "Qwen3-8B-Q8_0-00002-of-00002.gguf", "mmproj-F16.gguf"):
        assert (folder() / "Qwen3-8B-Q8_0" / name).is_file(), name
    assert updates[-1]["status"] == "success"
    # The router deletes only its cache; agent-machine deletes its own folder's models.
    assert manager(hf).handle({"op": "delete", "kind": "llamacpp", "port": router.port, "model": "Qwen3-8B-Q8_0"})["ok"]
    assert not (folder() / "Qwen3-8B-Q8_0").exists()


def test_an_old_router_on_its_own_port_falls_back_to_a_direct_pull(hf, made, monkeypatch):
    router = folder_router(made, monkeypatch)
    # Half a file from an earlier try: the download carries on from there.
    data = FILES["Qwen3-8B-Q4_K_M.gguf"]
    (folder() / "Qwen3-8B-Q4_K_M").mkdir(parents=True)
    (folder() / "Qwen3-8B-Q4_K_M" / "Qwen3-8B-Q4_K_M.gguf.part").write_bytes(data[:1000])
    updates: List[str] = []
    answer = manager(hf).handle({"op": "pull", "kind": "llamacpp", "port": router.port, "model": "someone/Qwen3-8B-GGUF"}, lambda update: updates.append(str(update.get("status"))))
    assert answer["ok"] and "agent-machine downloads the files itself" in updates[0], (answer, updates)
    assert (folder() / "Qwen3-8B-Q4_K_M" / "Qwen3-8B-Q4_K_M.gguf").read_bytes() == data and hf.ranges == ["Qwen3-8B-Q4_K_M.gguf:1000"]
    # Asked for the router only, it says it's too old.
    answer = manager(hf).handle({"op": "pull", "kind": "llamacpp", "port": router.port, "model": "someone/Qwen3-8B-GGUF:Q8_0", "method": "router"})
    assert "too old" in answer["error"], answer


def test_a_download_that_isnt_what_hugging_face_lists_is_deleted(hf, made, monkeypatch):
    hf.bad = "Qwen3-8B-Q4_K_M.gguf"
    router = folder_router(made, monkeypatch)
    answer = manager(hf).handle({"op": "pull", "kind": "llamacpp", "port": router.port, "model": "someone/Qwen3-8B-GGUF:Q4_K_M", "method": "direct"})
    assert not answer["ok"] and "SHA-256" in answer["error"], answer
    assert list((folder() / "Qwen3-8B-Q4_K_M").iterdir()) == []


def test_a_router_not_serving_the_folder_is_restarted(hf, made, monkeypatch):
    serving = {"now": False}

    def models(handler: BaseHTTPRequestHandler) -> None:
        reply({"data": [{"id": "Qwen3-8B-Q4_K_M", "status": {"value": "unloaded"}}] if serving["now"] else []})(handler)

    router = Upstream({"GET /models": models})
    made.append(router)
    monkeypatch.setenv(providers.LLAMA_PORT_ENV, str(router.port))
    downloader = manager(hf)
    downloader.restart_llama = lambda: serving.update(now=True)
    answer = downloader.handle({"op": "pull", "kind": "llamacpp", "port": router.port, "model": "someone/Qwen3-8B-GGUF", "method": "direct"})
    assert answer["ok"] and serving["now"], answer
    # Another llama-server (a container, another port) doesn't serve agent-machine's folder.
    assert not downloader.handle({"op": "pull", "kind": "llamacpp", "port": router.port + 1, "model": "someone/Qwen3-8B-GGUF", "method": "direct"})["ok"]


def test_llama_server_always_has_a_gguf_folder():
    config = llamacpp.TABLE.load()
    assert llamacpp.managed_env(config)["LLAMA_ARG_MODELS_DIR"] == str(Path(os.environ["AI_PROXY_MACHINE_HOME"]) / "llamacpp-models")
    assert llamacpp.environment({}, config)["LLAMA_ARG_MODELS_DIR"] == llamacpp.models_dir()
    chosen = llamacpp.TABLE.save({**config, "models_dir": "/srv/gguf"})
    assert llamacpp.models_dir() == "/srv/gguf" and llamacpp.environment({}, chosen)["LLAMA_ARG_MODELS_DIR"] == "/srv/gguf"
