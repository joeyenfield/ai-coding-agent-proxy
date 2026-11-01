"""A stand-in for llama.cpp's llama-server in router mode, for tests.

`python fake_llamacpp.py --host H --port P` serves:

  GET  /health                 {"status": "ok"}
  GET  /props                  the LLAMA_* and GGML_* variables it started with ("env"), and "build_info"
  GET  /models                 the models in its cache (LLAMA_CACHE, else ./fake-llama-cache), each with a status
  POST /models                 a "download": a file appears in the cache after a few progress events on /models/sse
  DELETE /models?model=        removes a cached model
  POST /models/load|unload     changes a model's status
  GET  /models/sse             server-sent events, as llama-server sends them
  POST /v1/chat/completions    a short answer naming the model
  POST /v1/messages            the same, Anthropic's way

`--version` prints the build as llama-server does. FAKE_LLAMACPP_FAIL=1 makes it exit at
once; FAKE_LLAMACPP_KEY makes every route but /health want that key (LLAMA_API_KEY too).
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

BUILD = 9999
CACHE = Path(os.environ.get("LLAMA_CACHE") or "fake-llama-cache")
STATUS: dict[str, str] = {}
LISTENERS: list[queue.Queue[dict]] = []
LOCK = threading.Lock()


def file_of(model: str) -> Path:
    return CACHE / (model.replace("/", "_").replace(":", "_") + ".gguf")


def cached() -> dict[str, Path]:
    models = {}
    if CACHE.is_dir():
        for path in CACHE.glob("*.gguf"):
            name = path.with_suffix(".name")
            models[name.read_text().strip() if name.exists() else path.stem] = path
    return models


def broadcast(event: dict) -> None:
    with LOCK:
        for listener in LISTENERS:
            listener.put(event)


def download(model: str) -> None:
    STATUS[model] = "downloading"
    total = 3000
    for done in (1000, 2000, 3000):
        time.sleep(0.05)
        broadcast({"model": model, "event": "download_progress", "data": {"https://huggingface.co/fake.gguf": {"done": done, "total": total}}})
    if model.endswith(":FAIL"):
        STATUS.pop(model, None)
        broadcast({"model": model, "event": "download_failed", "data": {"error": "no such quant"}})
        return
    CACHE.mkdir(parents=True, exist_ok=True)
    file_of(model).write_bytes(b"GGUF" + b"\0" * 2996)
    file_of(model).with_suffix(".name").write_text(model)
    STATUS[model] = "unloaded"
    broadcast({"model": model, "event": "download_finished", "data": {}})


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def reply(self, status: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def allowed(self) -> bool:
        key = os.environ.get("FAKE_LLAMACPP_KEY") or os.environ.get("LLAMA_API_KEY")
        if not key or self.path == "/health" or self.headers.get("authorization") == f"Bearer {key}":
            return True
        self.reply(401, {"error": {"code": 401, "message": "Invalid API Key", "type": "authentication_error"}})
        return False

    def body(self) -> dict:
        length = int(self.headers.get("content-length") or 0)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        url = urlsplit(self.path)
        if url.path == "/health":
            return self.reply(200, {"status": "ok"})
        if not self.allowed():
            return None
        if url.path == "/props":
            env = {name: value for name, value in os.environ.items() if name.startswith(("LLAMA_", "GGML_"))}
            return self.reply(200, {"build_info": f"b{BUILD}-fake", "env": env, "argv": sys.argv[1:]})
        if url.path in ("/models", "/v1/models"):
            data = []
            for model, path in sorted(cached().items()):
                data.append({"id": model, "object": "model", "owned_by": "llamacpp", "path": str(path),
                             "status": {"value": STATUS.get(model, "unloaded")}})
            for model, state in STATUS.items():
                if state == "downloading":
                    data.append({"id": model, "status": {"value": "downloading", "progress": {"f": {"done": 1, "total": 3}}}})
            return self.reply(200, {"object": "list", "data": data})
        if url.path == "/models/sse":
            listener: queue.Queue[dict] = queue.Queue()
            with LOCK:
                LISTENERS.append(listener)
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                while True:
                    try:
                        event = listener.get(timeout=1)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                with LOCK:
                    LISTENERS.remove(listener)
            return None
        return self.reply(404, {"error": {"code": 404, "message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802
        if not self.allowed():
            return None
        url = urlsplit(self.path)
        data = self.body()
        model = str(data.get("model") or "")
        if url.path == "/models":
            threading.Thread(target=download, args=(model,), daemon=True).start()
            return self.reply(200, {"success": True})
        if url.path in ("/models/load", "/models/unload"):
            if model not in cached():
                return self.reply(400, {"error": {"code": 400, "message": f"model {model} not found"}})
            STATUS[model] = "loaded" if url.path.endswith("load") and not url.path.endswith("unload") else "unloaded"
            broadcast({"model": model, "event": "model_status", "data": {"status": STATUS[model]}})
            return self.reply(200, {"success": True})
        if url.path == "/v1/chat/completions":
            STATUS[model] = "loaded"
            return self.reply(200, {"id": "fake", "object": "chat.completion", "model": model, "choices": [
                {"index": 0, "message": {"role": "assistant", "content": f"hello from {model}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 3, "total_tokens": 6}})
        if url.path == "/v1/messages":
            STATUS[model] = "loaded"
            return self.reply(200, {"id": "msg_fake", "type": "message", "role": "assistant", "model": model,
                                    "content": [{"type": "text", "text": f"hello from {model}"}], "stop_reason": "end_turn",
                                    "usage": {"input_tokens": 3, "output_tokens": 3}})
        return self.reply(404, {"error": {"code": 404, "message": "not found"}})

    def do_DELETE(self) -> None:  # noqa: N802
        if not self.allowed():
            return None
        url = urlsplit(self.path)
        model = (parse_qs(url.query).get("model") or [""])[0]
        path = cached().get(model)
        if url.path != "/models" or path is None:
            return self.reply(400, {"error": {"code": 400, "message": f"model {model} is not in the cache"}})
        path.unlink()
        path.with_suffix(".name").unlink(missing_ok=True)
        STATUS.pop(model, None)
        broadcast({"model": model, "event": "model_remove"})
        return self.reply(200, {"success": True})

    def log_message(self, *args: object) -> None:
        pass


def main() -> int:
    args = sys.argv[1:]
    if "--version" in args:
        print(f"version: {BUILD} (abcdef1)\nbuilt with fake for tests", file=sys.stderr)
        return 0
    if os.environ.get("FAKE_LLAMACPP_FAIL"):
        print("error: couldn't bind HTTP server socket", file=sys.stderr)
        return 1
    host = os.environ.get("LLAMA_ARG_HOST", "127.0.0.1")
    port = int(os.environ.get("LLAMA_ARG_PORT", "9931"))
    for flag, value in zip(args, args[1:]):
        if flag == "--host":
            host = value
        elif flag == "--port":
            port = int(value)
    if "-m" in args or "--model" in args:
        print("fake llama-server: only router mode", file=sys.stderr)
        return 2
    print(f"main: server is listening on http://{host}:{port} (router mode)", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
