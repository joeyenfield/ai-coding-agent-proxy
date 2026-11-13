"""Run a self-contained demo proxy for README screenshots.

Starts fake Ollama machines and a fake Anthropic API, seeds account-agent
sessions (Claude Code, Copilot CLI, Codex) shaped like real captured traffic
but with made-up content, then starts agent-proxy against them, shares three
made-up agent terminals with it and keeps real traffic streaming through it.
Nothing touches your real logs, config, accounts or access key.
The isolated issuer listens on :18200. Sign in as demo / demo-screenshot-password.

    python devhelpers/scripts/screenshots/demo.py --capture [name,...]   # start it, take the screenshots, stop it
    python devhelpers/scripts/screenshots/demo.py                # proxy on http://127.0.0.1:8199, until stopped
    node devhelpers/scripts/screenshots/capture.mjs http://127.0.0.1:8199 docs/screenshots
    python devhelpers/scripts/screenshots/demo.py --stop         # stop one left running (from another shell or agent)

Stop it with Ctrl+C, --stop or SIGTERM; each stops the server and issuer it started and removes its folder.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from agent_server.sessions import SessionStore
from agent_server.auth import UserStore
from agent_server import config_io, defaults, pricing
from agent_server.config import Settings
from agent_server.db import Database, schema
from agent_server.issuer import Issuer
from agent_server.version import PROTOCOL, build_version
from agent_server.telemetry import RequestTelemetry

ROOT = Path(__file__).resolve().parents[3]
PROXY_PORT = 8199
INTERCEPT_PORT = 18199
AUTH_PORT = 18200
LAPTOP, DESKTOP, ANTHROPIC, DOCKER = 18434, 18435, 18436, 18437
PROXY = f"http://127.0.0.1:{PROXY_PORT}"
AUTH = f"http://127.0.0.1:{AUTH_PORT}"
DEMO_USER, DEMO_PASSWORD = "demo", "demo-screenshot-password"
DEMO_ORG = "demo-team"
DEMO_ADMIN_PASSWORD = "demo-admin-screenshot-password"
GB = 1024**3
# When the demo started: its machines' agent-machines count their uptime and traffic from here.
STARTED = time.time()
PORTS = (PROXY_PORT, AUTH_PORT, INTERCEPT_PORT, LAPTOP, DESKTOP, ANTHROPIC, DOCKER)
# Where a running demo says what it started, so --stop (from another shell, or an agent that lost the
# process) can stop it: the demo's own process id, its server's and issuer's, and its folder.
STATE = ROOT / ".screenshot-demo.json"
# Set once the seeded traffic is in, so --capture doesn't photograph an empty console.
seeded = threading.Event()
# Set as the demo stops, so its traffic and fake machines stop before the server does rather than erroring against it.
stopping = threading.Event()


def ports_in_use() -> list[int]:
    import socket

    busy = []
    for port in PORTS:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                busy.append(port)
    return busy


def kill_tree(pid: int) -> None:
    """Stops a process and whatever it started. On Windows a hard kill skips the demo's own cleanup,
    so its children go too (/T); elsewhere SIGTERM lets the demo clean up itself."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return
    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def remove_leftovers(keep: Path | None = None) -> None:
    """Removes the folders of demos that were killed before they could remove their own."""
    for folder in ROOT.glob(".screenshot-demo-*"):
        if folder.is_dir() and folder != keep:
            shutil.rmtree(folder, ignore_errors=True)


def stop() -> int:
    """--stop: stops the demo this checkout is running, if any, and tidies what killed ones left."""
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        state = None
    busy = ports_in_use()
    # Only kill when the demo's ports are still taken: an old state file's process ids may belong to
    # something else by now.
    if state and busy:
        for pid in [state.get("pid"), *state.get("children", [])]:
            if isinstance(pid, int):
                kill_tree(pid)
        for _ in range(50):
            if not ports_in_use():
                break
            time.sleep(0.2)
    STATE.unlink(missing_ok=True)
    left = ports_in_use()
    if not left:
        remove_leftovers()
    if left:
        print(f"Ports {', '.join(map(str, left))} are still in use, by something this demo didn't start (or "
              f"started without {STATE.name}). Find it with netstat and stop it by hand.", file=sys.stderr)
        return 1
    print("demo stopped" if state and busy else "no demo was running", flush=True)
    return 0


def capture(names: str) -> int:
    """--capture: the screenshots, from the demo this process is running."""
    seeded.wait(timeout=120)
    command = ["node", str(Path(__file__).resolve().with_name("capture.mjs")), PROXY, str(ROOT / "docs" / "screenshots")]
    if names:
        command.append(names)
    return subprocess.run(command, cwd=ROOT).returncode


class DemoCredentials(httpx.Auth):
    """Only genuine issuer credentials, renewed before their five-minute expiry."""

    def __init__(self, secret: str):
        self.secret = secret
        self.token, self.expires = "", 0.0
        self.lock = threading.Lock()

    def headers(self) -> dict[str, str]:
        with self.lock:
            if time.monotonic() >= self.expires:
                with httpx.Client(trust_env=False, timeout=10) as client:
                    response = client.post(AUTH + "/token", data={"grant_type": "client_credentials", "scope": "mesh-api"},
                                           auth=("screenshot-demo", self.secret))
                    response.raise_for_status()
                    result = response.json()
                self.token = result["access_token"]
                self.expires = time.monotonic() + result["expires_in"] - 60
            return {"X-AI-Proxy-Token": self.token, "X-AI-Proxy-Protocol": str(PROTOCOL)}

    def auth_flow(self, request):
        request.headers.update(self.headers())
        yield request


credentials: DemoCredentials


def api_client(timeout: float = 10) -> httpx.Client:
    return httpx.Client(auth=credentials, timeout=timeout, trust_env=False)


class DemoSocket:
    """Reconnect fake hosts and fleet members with a fresh credential every four minutes."""

    def __init__(self, url: str, hello: dict):
        self.url, self.hello = url, hello
        self.lock = threading.RLock()
        self.closed = False
        self.socket = None
        self._connect()

    def _connect(self):
        from websockets.sync.client import connect

        if self.socket:
            self.socket.close()
        self.socket = connect(self.url, proxy=None, max_size=None, additional_headers=credentials.headers())
        self.socket.send(json.dumps({"type": "hello", **self.hello}))
        self.socket.recv(timeout=10)
        self.renew = credentials.expires + 5

    def send(self, data):
        from websockets.exceptions import ConnectionClosed

        with self.lock:
            if self.closed or stopping.is_set():
                return
            try:
                self.socket.send(data)
            except (ConnectionClosed, OSError):
                if stopping.is_set():
                    return
                self._connect()
                self.socket.send(data)

    def close(self):
        with self.lock:
            self.closed = True
            self.socket.close()

    def __iter__(self):
        from websockets.exceptions import ConnectionClosed

        while not self.closed and not stopping.is_set():
            try:
                if time.monotonic() >= self.renew:
                    with self.lock:
                        self._connect()
                yield self.socket.recv(timeout=1)
            except TimeoutError:
                continue
            except (ConnectionClosed, OSError):
                if not self.closed and not stopping.is_set():
                    time.sleep(1)
                    try:
                        with self.lock:
                            self._connect()
                    except (ConnectionClosed, OSError):
                        continue


# --------------------------------------------------------------------------- fake upstreams

REASONING = (
    "The webhook handler calls the payment provider once and gives up on any error. "
    "Transient 502s and timeouts should be retried, but declined cards must not be. "
    "I'll wrap the call in a retry helper with exponential backoff and jitter, cap it at five attempts, "
    "and only retry on network errors and 5xx responses. "
)
ANSWER = (
    "I'll add a small `retry_with_backoff` helper and use it in `handle_webhook`.\n\n"
    "1. Retry only on `httpx.TransportError` and 5xx responses.\n"
    "2. Back off 0.5 s, 1 s, 2 s, 4 s with ±20% jitter.\n"
    "3. Give up after five attempts and log the final error with the event id.\n\n"
    "Let me read `payments/webhooks.py` first."
)
# What a thinking model says before a review's answer: the console's Review tab shows it while it waits.
REVIEW_REASONING = (
    "The engineer wants the few changes that would save the most. The score is low and the findings overlap, so group them by cause. "
    "Twenty-four of forty tool calls were searches: the first prompt was one line and named no file, so the agent had to find the test. "
    "The largest results are four runs of the whole test suite, verbose, about 37,000 tokens each: that is most of the context's growth. "
    "The system prompt and the unused MCP tools cost the same on every call, which adds up over the session. "
    "Six failed edits on one file look like one cause, not six. I'll order the suggestions by what each saves and quote the commands. "
)
REVIEW_ANSWER = (
    "## Summary\n"
    "A one-line prompt sent the agent hunting: most of its tool calls were searches for a test it was never pointed at, "
    "and four full test logs then filled the context. The fix itself was one small edit.\n\n"
    "## What to change\n"
    "1. Name the test in the prompt (`tests/shop/test_checkout.py::test_total_rounds`). 24 of 40 tool calls were searches and listings that only found it.\n"
    "2. Run the one failing test, quietly: `pytest tests/shop/test_checkout.py -q -x`. `pytest tests/ -v` ran four times and each log put about 37,000 tokens into the context.\n"
    "3. Remove the Jira, Confluence, Figma and Sentry MCP servers from this project. None of their 46 tools was called, and they cost about 10,000 tokens on every call.\n"
    "4. Cut `CLAUDE.md` down. About 21,000 tokens of rules are sent with every call; keep the few that apply to every task and link to the rest.\n"
    "5. When an edit fails twice, have the agent read the lines again before retrying. The same `Edit` failed six times on indentation.\n\n"
    "## What went well\n"
    "- The agent found the real cause, float rounding, rather than adding a retry to hide the flakiness.\n"
    "- Two thirds of the tokens read came from the cache."
)


CODEX_REASONING = (
    "The export job writes rows straight to the final file, so a crash leaves a partial export and the next run starts over. "
    "Writing each batch to a temporary file and recording the last exported id in a checkpoint lets a restart pick up where it stopped. "
)
CODEX_ANSWER = (
    "I'll make `nightly_export` resumable:\n\n"
    "1. Keep a checkpoint table with the last exported `order_id` per run.\n"
    "2. Write each 10k-row batch to `export.tmp` and rename it into place once it's flushed.\n"
    "3. On start, resume from the checkpoint instead of the beginning.\n\n"
    "Starting with `jobs/export.py`."
)


def ollama_app(models: list[tuple[str, float, str, str]], loaded: dict[str, float], delay: float) -> FastAPI:
    app = FastAPI()

    @app.get("/api/version")
    async def version():
        return {"version": "0.34.4"}

    @app.get("/api/tags")
    async def tags():
        return {"models": [
            {"name": name, "size": int(size * GB), "modified_at": "2026-09-28T10:00:00Z",
             "details": {"family": family, "parameter_size": params, "quantization_level": "Q4_K_M"}}
            for name, size, family, params in models
        ]}

    @app.get("/api/ps")
    async def ps():
        expires = (datetime.now(timezone.utc) + timedelta(minutes=27)).isoformat()
        result = []
        for name, size, family, params in models:
            if name in loaded:
                total = int(size * GB * 1.15)
                result.append({"name": name, "model": name, "size": total, "size_vram": int(total * loaded[name]),
                               "context_length": 65536, "expires_at": expires,
                               "details": {"family": family, "parameter_size": params, "quantization_level": "Q4_K_M"}})
        return {"models": result}

    @app.post("/api/chat")
    async def chat(request: Request):
        body = await request.json()
        model = body.get("model", "qwen3.6:35b")
        think = body.get("think") is not False
        prompt_tokens = 9000 + random.randint(0, 4000)

        # A session's AI review (the console's Review tab) gets an answer that reads like one.
        first = (body.get("messages") or [{}])[0]
        reviewing = first.get("role") == "system" and str(first.get("content") or "").startswith("You review one recorded session")

        async def stream():
            words = ((REVIEW_REASONING if reviewing else REASONING) if think else "").split(" ")
            await asyncio.sleep(delay * 12)  # prompt evaluation
            count = 0
            for word in words:
                if word:
                    count += 1
                    yield json.dumps({"model": model, "message": {"role": "assistant", "content": "", "thinking": word + " "}, "done": False}) + "\n"
                    await asyncio.sleep(delay)
            for word in (REVIEW_ANSWER if reviewing else ANSWER).split(" "):
                count += 1
                yield json.dumps({"model": model, "message": {"role": "assistant", "content": word + " "}, "done": False}) + "\n"
                await asyncio.sleep(delay)
            yield json.dumps({
                "model": model, "message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
                "prompt_eval_count": prompt_tokens, "eval_count": count,
                "prompt_eval_duration": int(delay * 12 * 1e9), "eval_duration": int(count * delay * 1e9),
            }) + "\n"

        if body.get("stream", True):
            return StreamingResponse(stream(), media_type="application/x-ndjson")
        return JSONResponse({"model": model, "message": {"role": "assistant", "content": ANSWER}, "done": True,
                             "prompt_eval_count": prompt_tokens, "eval_count": 120})

    return app


def anthropic_app() -> FastAPI:
    app = FastAPI()

    def sse(event: dict) -> str:
        return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"

    @app.post("/v1/messages/count_tokens")
    async def count_tokens():
        return {"input_tokens": 14211}

    @app.post("/v1/messages")
    async def messages(request: Request):
        body = await request.json()

        async def stream():
            yield sse({"type": "message_start", "message": {"id": "msg_demo", "type": "message", "role": "assistant", "model": body.get("model"),
                       "content": [], "usage": {"input_tokens": 6, "cache_read_input_tokens": 14100, "cache_creation_input_tokens": 0, "output_tokens": 1}}})
            yield sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
            for word in ANSWER.split(" "):
                await asyncio.sleep(0.03)
                yield sse({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": word + " "}})
            yield sse({"type": "content_block_stop", "index": 0})
            yield sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 96}})
            yield sse({"type": "message_stop"})

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app


def model_runner_app() -> FastAPI:
    """Docker Model Runner: its model store, loaded models and OpenAI streaming under /engines/v1."""
    app = FastAPI()
    pulled = [
        {"id": "sha256:4f1c", "tags": ["docker.io/ai/qwen3-coder:latest"], "created": 1_790_000_000,
         "config": {"format": "gguf", "quantization": "Q4_K_M", "parameters": "30.53 B", "architecture": "qwen3moe", "size": "17.28 GiB", "context_size": 65536}},
        {"id": "sha256:9a2e", "tags": ["docker.io/ai/qwen3:latest", "docker.io/ai/qwen3:8B-Q4_K_M"], "created": 1_786_000_000,
         "config": {"format": "gguf", "quantization": "Q4_K_M", "parameters": "8.19 B", "architecture": "qwen3", "size": "4.68 GiB", "context_size": 40960}},
        {"id": "sha256:77b0", "tags": ["hf.co/unsloth/gemma-3-12b-it-gguf:Q4_K_M"], "created": 1_780_000_000,
         "config": {"format": "gguf", "quantization": "Q4_K_M", "parameters": "11.77 B", "architecture": "gemma3", "size": "6.79 GiB", "context_size": 131072}},
    ]
    loaded = {"ai/qwen3-coder"}

    @app.get("/engines/status")
    async def engines():
        return {"llama.cpp": "running llama.cpp latest-cuda (sha256:5d3e) version: b6410", "vllm": "not installed"}

    @app.get("/models")
    async def models():
        return pulled

    @app.get("/engines/ps")
    async def ps():
        return [{"backend_name": "llama.cpp", "model_name": name, "mode": "completion", "in_use": False} for name in sorted(loaded)]

    @app.get("/engines/df")
    async def df():
        return {"models_disk_usage": int(28.75 * GB), "default_backend_disk_usage": 96_000_000}

    @app.post("/engines/unload")
    async def unload(request: Request):
        names = (await request.json()).get("models") or []
        loaded.difference_update(names)
        return {"unloaded_runners": len(names)}

    @app.post("/engines/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        model = body.get("model", "ai/qwen3-coder")
        loaded.add(model.removesuffix(":latest"))
        if request.headers.get("x-preload-only"):
            return JSONResponse({})

        async def stream():
            await asyncio.sleep(0.4)
            words = ANSWER.split(" ")
            for word in words:
                yield f"data: {json.dumps({'model': model, 'choices': [{'index': 0, 'delta': {'content': word + ' '}}]})}\n\n"
                await asyncio.sleep(0.04)
            usage = {"prompt_tokens": 8200 + random.randint(0, 3000), "completion_tokens": len(words)}
            yield f"data: {json.dumps({'model': model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}], 'usage': usage})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/models/create")
    async def pull(request: Request):
        name = (await request.json()).get("from", "")
        total = int(2.1 * GB)

        async def progress():
            for step in range(1, 21):
                await asyncio.sleep(0.15)
                yield json.dumps({"type": "progress", "message": "", "total": total, "layer": {"id": "sha256:demo", "size": total, "current": total * step // 20}, "mode": "pull"}) + "\n"
            pulled.insert(0, {"id": f"sha256:{abs(hash(name)) % 10**6:06d}", "tags": [name], "created": int(time.time()), "config": {"format": "gguf"}})
            yield json.dumps({"type": "success", "message": "Model pulled successfully", "mode": "pull"}) + "\n"

        return StreamingResponse(progress(), media_type="application/json")

    return app


def serve(app: FastAPI, port: int) -> None:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()


# --------------------------------------------------------------------------- seeded account traffic

def iso(when: datetime) -> str:
    return when.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Seeder:
    def __init__(self, db: Database):
        self.store = SessionStore(db)
        self.clock = datetime.now(timezone.utc) - timedelta(minutes=50)

    def session(self, client: str, project: str, minutes_ago: float, ended: bool = True):
        session = self.store.create({"client": client, "backend": "direct", "project": project, "trace": True,
                                     "tags": {"project_path": f"/home/dev/{project}", "owner": DEMO_USER, "tenant": DEMO_ORG,
                                              "visibility": "private", "access": "control"}})
        session.started_at = iso(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))
        self.clock = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        self.current = session
        self.ended = ended
        return session

    def add(self, method: str, host: str, path: str, *, status: int = 200, ms: float = 120, model: str = "",
            kind: str = "intercept", sent: int = 400, received: int = 1800, request=None, response=None,
            usage: tuple[int, int, int, int] | None = None, ttft: float | None = None, headers: list | None = None,
            response_headers: list | None = None) -> None:
        session = self.current
        session.request_sequence += 1
        session.request_count += 1
        request_id = f"{session.request_sequence:06d}"
        telemetry = RequestTelemetry(session.session_id, request_id, session.client, model, host, path, sent,
                                     kind=kind, method=method, host=host)
        telemetry.status = status
        telemetry.response_bytes = received
        telemetry.completed = True
        if usage:
            telemetry.input_tokens, telemetry.output_tokens, telemetry.cache_read_tokens, telemetry.cache_creation_tokens = usage
        record = telemetry.finish()
        generation = ms - (ttft or 0)
        record.update(
            timestamp=iso(self.clock), total_ms=ms, ttft_ms=ttft, prompt_eval_ms=ttft,
            generation_ms=generation if ttft else None,
            generation_tps=round(record["output_tokens"] * 1000 / generation, 3) if ttft and generation > 0 else None,
            prompt_tps=round(record["input_tokens"] * 1000 / ttft, 3) if ttft else None,
            error_type="upstream_error" if status >= 400 else None,
        )
        # An account agent's model calls are priced as the server prices them: at API prices, on a subscription.
        cost = pricing.estimate(pricing.DEFAULT_PRICES, record, "subscription") if model and usage else None
        if cost is not None:
            record["cost"] = cost
        self.store.append_request(session, record)
        scheme_host = f"https://{host}{path}"
        http = {
            "method": method, "url": scheme_host,
            "request_headers": headers or [["accept", "application/json"], ["authorization", "Bearer [redacted]"], ["content-type", "application/json"]],
            "status": status, "reason": "OK" if status < 300 else "Switching Protocols" if status == 101 else "Not Found",
            "response_headers": response_headers or [["content-type", "application/json"], ["date", "Sun, 04 Oct 2026 20:15:02 GMT"], ["set-cookie", "[redacted]"]],
        }
        self.store.write_trace(session, request_id, {"metadata": record, "request": request, "response": response, "http": http})
        session.last_activity_at = record["timestamp"]
        self.clock += timedelta(milliseconds=ms + random.randint(80, 900))

    def finish(self) -> None:
        if self.ended:
            self.current.ended_at = iso(self.clock + timedelta(seconds=20))
            self.current.exit_status = 0
        self.store.touch(self.current)
        self.current.last_activity_at = iso(self.clock)
        self.store._write_session(self.current)


CLAUDE_TOOLS = ["Bash", "Read", "Edit", "Write", "Grep", "Glob", "TodoWrite", "WebFetch", "WebSearch", "Task", "NotebookEdit"]
PROMPT = "Add retry with exponential backoff to the payment webhook handler. Don't retry declined cards."


def anthropic_events(model: str) -> list[dict]:
    events = [
        {"type": "message_start", "message": {"model": model, "usage": {"input_tokens": 4, "cache_read_input_tokens": 18234, "cache_creation_input_tokens": 512, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
    ]
    events += [{"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": part + " "}} for part in REASONING.split(". ")]
    events += [{"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}]
    events += [{"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": line + "\n"}} for line in ANSWER.split("\n")]
    events += [
        {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "toolu_demo", "name": "Read", "input": {}}},
        {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "{\"file_path\": \"payments/webhooks.py\"}"}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 214}},
        {"type": "message_stop"},
    ]
    return events


def claude_reply(*blocks: dict) -> list[dict]:
    """A streamed Anthropic reply made of these blocks (text, thinking, tool_use)."""
    events: list[dict] = [{"type": "message_start", "message": {"model": "claude-opus-5-5", "usage": {"input_tokens": 3, "cache_read_input_tokens": 18234, "output_tokens": 1}}}]
    for index, block in enumerate(blocks):
        if block["type"] == "tool_use":
            events.append({"type": "content_block_start", "index": index, "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}})
            events.append({"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}})
        elif block["type"] == "thinking":
            events.append({"type": "content_block_start", "index": index, "content_block": {"type": "thinking", "thinking": ""}})
            events.append({"type": "content_block_delta", "index": index, "delta": {"type": "thinking_delta", "thinking": block["text"]}})
        else:
            events.append({"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}})
            events.append({"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": block["text"]}})
    return events + [{"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 240}}, {"type": "message_stop"}]


def claude_conversation(seed: Seeder, claude_headers: list) -> None:
    """Claude Code's model calls as it makes them on an account: the main chat sent a step at a time and chained by
    request id, a sub-agent, and the calls it makes on the side (a title, a suggested next prompt, a recap)."""
    tools = [{"name": name, "input_schema": {"type": "object"}} for name in CLAUDE_TOOLS]

    def call(request_class: str, upstream: str, messages: list, reply: list, *, previous: str | None = None, agent: str | None = None,
             model: str = "claude-opus-5-5", system: str = "You are Claude Code, Anthropic's official CLI for Claude.", with_tools: bool = True,
             ms: int = 4200, usage: tuple[int, int, int, int] = (3, 240, 18234, 512)) -> None:
        line = "x-anthropic-billing-header: cc_version=2.1.289; cc_entrypoint=cli;" + (f" cc_prev_req={previous};" if previous else "") + " cc_turn_origin=human;"
        headers = claude_headers + [["x-claude-code-request-class", request_class]] + ([["x-claude-code-agent-id", agent]] if agent else [])
        request = {"model": model, "max_tokens": 32000, "stream": True, "system": [{"type": "text", "text": line}, {"type": "text", "text": system}], "messages": messages}
        if with_tools:
            request["tools"] = tools
        seed.add("POST", "api.anthropic.com", "/v1/messages", ms=ms, ttft=min(1300, ms // 3), model=model, sent=46000, received=7800, usage=usage, headers=headers,
                 response_headers=[["content-type", "text/event-stream"], ["request-id", upstream], ["anthropic-ratelimit-unified-status", "allowed"]],
                 request=request, response=claude_reply(*reply))

    def text(value: str) -> dict:
        return {"type": "text", "text": value}

    def use(call_id: str, name: str, value: dict) -> dict:
        return {"type": "tool_use", "id": call_id, "name": name, "input": value}

    def result(call_id: str, value: str) -> dict:
        return {"type": "tool_result", "tool_use_id": call_id, "content": value}

    reminder = text("<system-reminder>\nAs you answer the user's questions, you can use the following context:\n# currentDate\nToday's date is 2026-10-04.\n</system-reminder>")
    first = [{"role": "user", "content": [reminder, text(PROMPT)]}]
    read = use("toolu_read", "Read", {"file_path": "payments/webhooks.py"})
    grep = use("toolu_grep", "Grep", {"pattern": "declined", "path": "payments/"})
    task = use("toolu_task", "Task", {"description": "Find the webhook's callers", "prompt": "Find every caller of handle_webhook and say which of them expect it to raise."})
    edit = use("toolu_edit", "Edit", {"file_path": "payments/webhooks.py", "old_string": "    return deliver(event)", "new_string": "    return retry_with_backoff(lambda: deliver(event), attempts=5)"})
    test = use("toolu_test", "Bash", {"command": "pytest tests/test_webhooks.py -q", "description": "Run the webhook tests"})
    done = ("Done. `handle_webhook` now retries transient failures:\n\n"
            "- Transport errors and 5xx answers are retried up to five times, backing off 0.5 s, 1 s, 2 s and 4 s with jitter.\n"
            "- Declined cards (`402` with `card_declined`) fail at once, as before.\n"
            "- Each retry is logged with the event id and the attempt number.\n\n"
            "`tests/test_webhooks.py` passes (14 tests).")

    # Claude Code names the session from the first message, on the side.
    call("auxiliary", "req_demo_title", [{"role": "user", "content": f"<session>\n{PROMPT}\n</session>\nWrite the title in the predominant language of the session."}],
         [text("Webhook retry backoff")], model="claude-haiku-5-5", with_tools=False, ms=900,
         system="You are naming a coding session so the user can pick it out of a long list. The title is a short noun phrase.")
    call("main", "req_demo_1", first, [{"type": "thinking", "text": REASONING}, text(ANSWER), read], ms=6400)
    call("main", "req_demo_2", [{"role": "user", "content": [result("toolu_read", "async def handle_webhook(event):\n    ...\n    return deliver(event)\n")]}],
         [text("Before changing it I'll check how declines are reported, and have a sub-agent find the callers."), grep, task], previous="req_demo_1")
    # The sub-agent works on its own, from the task the main agent gave it.
    call("subagent", "req_demo_sub1", [{"role": "user", "content": "Find every caller of handle_webhook and say which of them expect it to raise."}],
         [use("toolu_sub_grep", "Grep", {"pattern": "handle_webhook", "path": "."})], agent="a1b2c3demo", ms=2100)
    call("subagent", "req_demo_sub2", [{"role": "user", "content": [result("toolu_sub_grep", "payments/routes.py:41\npayments/worker.py:88\ntests/test_webhooks.py:12")]}],
         [text("Two callers: `routes.py` returns 500 when it raises, and `worker.py` requeues the event. Neither expects retries inside it.")],
         previous="req_demo_sub1", agent="a1b2c3demo", ms=2600)
    # The person typed something while the agent was working; Claude Code passes it on with the tool results.
    call("main", "req_demo_3", [{"role": "user", "content": [
        result("toolu_grep", "payments/errors.py:7:    if body.get('code') == 'card_declined':"),
        result("toolu_task", "Two callers: routes.py returns 500 when it raises, and worker.py requeues the event. Neither expects retries inside it."),
        text("The user sent a new message while you were working:\nAlso log each retry with the attempt number.\n\n"
             "This is how Claude Code surfaces messages the user sends mid-turn, within the running turn, alongside the next tool result."),
    ]}], [text("Declines come back as `card_declined`, so those stay out of the retry. I'll log each attempt too."), edit], previous="req_demo_2")
    call("main", "req_demo_4", [{"role": "user", "content": [result("toolu_edit", "The file payments/webhooks.py has been updated.")]}], [test], previous="req_demo_3", ms=2400)
    call("main", "req_demo_5", [{"role": "user", "content": [result("toolu_test", "..............\n14 passed in 1.92s")]}], [text(done)], previous="req_demo_4", ms=5200)
    # Calls on the side carry the whole conversation, with an instruction of Claude Code's at the end.
    history = [*first, {"role": "assistant", "content": [read]}, {"role": "user", "content": [result("toolu_read", "...")]},
               {"role": "assistant", "content": [text(done)]}]
    call("auxiliary", "req_demo_suggest", [*history, {"role": "user", "content": "[SUGGESTION MODE: Suggest what the user might naturally type next into Claude Code.]"}],
         [text("commit this")], previous="req_demo_5", ms=1500)
    call("auxiliary", "req_demo_recap", [*history, {"role": "user", "content": "The user stepped away and is coming back. Recap in under 40 words."}],
         [text("Adding retries with backoff to the payment webhook; done and tested. Next: commit the change.")], previous="req_demo_5", ms=1700)


def wasteful_conversation(seed: Seeder, claude_headers: list) -> None:
    """A session for the Review tab to find things in: a long system prompt, four MCP servers nobody called, a
    hunt through the project for one test, the same file read five times, a test log pasted whole, edits that
    didn't apply and pauses that let the cache run out."""
    servers = {"jira": 14, "confluence": 11, "figma": 9, "sentry": 12}
    about = "Use this tool when the task needs it. Arguments are validated; errors come back as text. " * 9
    tools = [{"name": name, "description": about, "input_schema": {"type": "object"}} for name in CLAUDE_TOOLS]
    tools += [{"name": f"mcp__{server}__{verb}_{number}", "description": about, "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}}}
              for server, count in servers.items() for number, verb in enumerate(["search", "get", "list", "update"] * 4) if number < count]
    rules = "\n".join(f"- Rule {number}: follow the team's convention for {topic} in every module, and say so in the pull request."
                      for number in range(1, 420) for topic in ("naming", "logging"))
    system = "You are Claude Code, Anthropic's official CLI for Claude.\n\n# Project instructions (CLAUDE.md)\n" + rules
    messages: list = [{"role": "user", "content": "the checkout test is flaky, fix it"}]
    number = 0

    def step(name: str, given: dict, output: str, error: bool = False) -> None:
        nonlocal number
        number += 1
        messages.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"toolu_w{number}", "name": name, "input": given}]})
        messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"toolu_w{number}", "content": output, "is_error": error}]})

    for pattern in ("checkout", "flaky", "test_checkout", "def test_", "retry", "sleep(", "freeze_time", "Cart(", "fixture", "order_total", "tax", "rounding"):
        step("Grep", {"pattern": pattern, "path": "."}, "\n".join(f"services/shop/module_{n}.py:{n * 7}: {pattern}" for n in range(40)))
        step("Glob", {"pattern": f"**/*{pattern.strip('(')}*.py"}, "tests/shop/test_checkout.py\nservices/shop/checkout.py")
    for _ in range(5):
        step("Read", {"file_path": "services/shop/checkout.py"}, "class Checkout:\n    def total(self, cart):\n        ...\n" * 400)
    log = "\n".join(f"tests/shop/test_checkout.py::test_case_{n} PASSED [{n % 100:3d}%]" for n in range(2600))
    for _ in range(4):
        step("Bash", {"command": "pytest tests/ -v", "description": "Run the tests"}, log + "\nFAILED tests/shop/test_checkout.py::test_total_rounds - assert 10.01 == 10.0")
    for _ in range(6):
        step("Edit", {"file_path": "services/shop/checkout.py", "old_string": "return round(total, 2)", "new_string": "return quantize(total)"},
             "String to replace not found in file.", error=True)
    step("Edit", {"file_path": "services/shop/checkout.py", "old_string": "        return round(total, 2)", "new_string": "        return quantize(total)"}, "The file services/shop/checkout.py has been updated.")
    reply = claude_reply({"type": "text", "text": "The total was rounded as a float. It now uses Decimal quantize, and `test_total_rounds` passes."})
    line = "x-anthropic-billing-header: cc_version=2.1.289; cc_entrypoint=cli; cc_turn_origin=human;"
    request = {"model": "claude-opus-5-5", "max_tokens": 32000, "stream": True, "tools": tools, "messages": messages,
               "system": [{"type": "text", "text": line}, {"type": "text", "text": system}]}
    headers = claude_headers + [["x-claude-code-request-class", "main"]]
    # (new input, output, read from the cache, written to it): the context grows with every tool result.
    usage = [(40, 310, 0, 61_000), (38, 280, 61_000, 9_000), (35, 420, 70_000, 12_000), (41, 380, 82_000, 14_000), (36, 510, 96_000, 11_000),
             (44, 290, 0, 118_000), (39, 330, 118_000, 9_000), (42, 610, 127_000, 13_000), (37, 270, 0, 151_000), (45, 480, 151_000, 12_000), (40, 350, 163_000, 9_000)]
    for position, counts in enumerate(usage):
        if counts[2] == 0 and position:
            # Left waiting past the cache's five minutes: the whole prompt is written to it again.
            seed.clock += timedelta(minutes=9)
        seed.add("POST", "api.anthropic.com", "/v1/messages", ms=9000, ttft=2600, model="claude-opus-5-5", sent=640_000, received=5200, usage=counts, headers=headers,
                 response_headers=[["content-type", "text/event-stream"], ["request-id", f"req_waste_{position}"]],
                 request=request, response=reply)
        if position == 6:
            seed.add("POST", "api.anthropic.com", "/v1/messages", status=429, ms=300, model="claude-opus-5-5", sent=640_000, received=180, headers=headers, request=request,
                     response={"type": "error", "error": {"type": "rate_limit_error", "message": "This request would exceed your account's rate limit."}})


def seed_accounts(db: Database) -> None:
    seed = Seeder(db)
    org = "3f6c2a1e-9b7d-4c55-8e21-0a4d5f6b7c8d"
    claude_headers = [["accept", "application/json"], ["authorization", "Bearer [redacted]"], ["content-type", "application/json"],
                      ["user-agent", "claude-cli/2.1.289 (external, sdk-cli)"], ["anthropic-beta", "claude-code-20250219,oauth-2025-04-20,interleaved-thinking-2025-05-14"],
                      ["anthropic-version", "2023-06-01"], ["x-app", "cli"], ["x-claude-code-session-id", "[redacted]"]]

    # Claude Code on a claude.ai account.
    seed.session("claude-account", "payments-api", 46)
    seed.add("POST", "api.anthropic.com", "/api/oauth/validate", ms=140, response={"valid": True, "scopes": ["user:inference", "user:profile", "user:sessions:claude_code"]})
    seed.add("GET", "api.anthropic.com", "/api/claude_cli/bootstrap", ms=220, response={"client_data": {}, "features": {"auto_compact": True}})
    seed.add("GET", "api.anthropic.com", "/api/claude_code/policy_limits", status=304, ms=60, received=0)
    seed.add("GET", "api.anthropic.com", "/v1/mcp_servers", ms=180, response={"data": [{"name": "linear", "type": "url"}]})
    for _ in range(6):
        seed.add("GET", "api.anthropic.com", f"/api/oauth/organizations/{org}/claude_code_settings", ms=random.randint(300, 460), received=24000)
    seed.add("POST", "mcp-proxy.anthropic.com", "/v1/mcp/mcpsrv_01DemoServer", ms=360, response={"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "search_issues"}]}})
    seed.add("POST", "api.anthropic.com", "/v1/messages/count_tokens", ms=190, model="claude-opus-5-5", response={"input_tokens": 18750},
             request={"model": "claude-opus-5-5", "messages": [{"role": "user", "content": PROMPT}]})
    claude_conversation(seed, claude_headers)
    for _ in range(3):
        seed.add("POST", "api.anthropic.com", "/api/event_logging/v2/batch", ms=90, sent=7400, received=60, response={"accepted": 12})
    seed.add("POST", "http-intake.logs.us5.datadoghq.com", "/api/v2/logs", status=202, ms=110, received=2)
    seed.finish()

    # A second Claude Code session, wasteful on purpose, for the Review tab. Fewer requests than the one above:
    # capture.mjs takes an agent's busiest session for its other screenshots.
    seed.session("claude-account", "monolith", 190)
    wasteful_conversation(seed, claude_headers)
    seed.finish()

    # GitHub Copilot CLI on a GitHub account.
    seed.session("copilot-account", "web-dashboard", 30)
    seed.add("GET", "api.github.com", "/copilot_internal/user", ms=210, response={"copilot_plan": "individual", "chat_enabled": True})
    seed.add("GET", "api.github.com", "/repos/github/copilot-cli/releases", ms=1800, received=48000)
    seed.add("GET", "api.individual.githubcopilot.com", "/models", ms=330, received=59500,
             response={"data": [{"id": "claude-sonnet-5"}, {"id": "gpt-5.5"}, {"id": "gemini-3-pro"}]})
    seed.add("POST", "api.individual.githubcopilot.com", "/mcp/readonly", ms=800, received=3400)
    seed.add("POST", "api.individual.githubcopilot.com", "/v1/messages", ms=5200, ttft=1150, model="claude-sonnet-5", sent=64600, received=8100,
             usage=(2, 305, 22100, 0),
             request={"model": "claude-sonnet-5", "stream": True, "max_tokens": 16000,
                      "messages": [{"role": "user", "content": "Why does the chart flicker when the date range changes?"}]},
             response=anthropic_events("claude-sonnet-5"))
    for _ in range(4):
        seed.add("POST", "telemetry.individual.githubcopilot.com", "/telemetry", ms=290, sent=2700, received=62)
    seed.finish()

    # Codex CLI on a ChatGPT plan; model turns travel over a WebSocket.
    seed.session("codex-account", "data-pipeline", 14, ended=False)
    seed.add("GET", "chatgpt.com", "/backend-api/codex/models", ms=310, received=609000)
    seed.add("GET", "chatgpt.com", "/backend-api/wham/accounts/check", ms=150)
    for _ in range(5):
        seed.add("GET", "chatgpt.com", "/backend-api/ps/plugins/list", ms=random.randint(500, 900), received=700000)
    seed.add("GET", "chatgpt.com", "/backend-api/codex/responses", kind="upgrade", status=101, ms=42000, sent=68000, received=241000,
             response={"messages": {"sent": 2, "received": 18, "turns": 2}},
             headers=[["upgrade", "websocket"], ["connection", "Upgrade"], ["authorization", "Bearer [redacted]"], ["originator", "codex_cli_rs"]])
    codex_events = [{"type": "response.created", "response": {"model": "gpt-6.1-sol"}}]
    codex_events += [{"type": "response.reasoning_summary_text.delta", "delta": part + ". "} for part in CODEX_REASONING.split(". ") if part.strip()]
    codex_events += [{"type": "response.output_text.delta", "delta": line + "\n"} for line in CODEX_ANSWER.split("\n")]
    codex_events += [{"type": "response.completed", "response": {"usage": {"input_tokens": 13297, "output_tokens": 241, "input_tokens_details": {"cached_tokens": 11264}}}}]
    seed.add("WS", "chatgpt.com", "/backend-api/codex/responses", status=101, ms=1500, ttft=1300, model="gpt-6.1-sol",
             usage=(12296, 0, 0, 0), sent=56000, received=118000,
             request={"type": "response.create", "model": "gpt-6.1-sol", "generate": False, "stream": True, "input": []},
             response=[{"type": "response.created"}, {"type": "response.completed", "response": {"usage": {"input_tokens": 12296, "output_tokens": 0}}}])
    seed.add("WS", "chatgpt.com", "/backend-api/codex/responses", status=101, ms=7800, ttft=1300, model="gpt-6.1-sol",
             usage=(13297, 241, 11264, 0), sent=11600, received=122000,
             request={"type": "response.create", "model": "gpt-6.1-sol", "stream": True,
                      "instructions": "You are Codex, a coding agent.",
                      "input": [{"role": "user", "content": [{"type": "input_text", "text": "Make the nightly export job resumable after a crash."}]}]},
             response=codex_events)
    seed.add("POST", "ab.chatgpt.com", "/otlp/v1/metrics", status=202, ms=95, sent=65000, received=16)
    seed.finish()


# --------------------------------------------------------------------------- seeded history

# The demo team's other people: their sessions are theirs, so the demo's own lists don't show them,
# but the team's analytics (which an admin sees) have several people to split by.
TEAMMATES = ("priya", "sam", "lena", "marco")
HISTORY_DAYS = 34

# What a past session ran: agent, where its calls went, the record's kind, the model, how its cost is
# counted (None: a machine's own model server, which costs nothing), and the tokens of a call
# (input, output, cache read, cache written), each varied a little.
HISTORY_KINDS = {
    "claude-opus": ("claude-account", "api.anthropic.com", "intercept", "claude-opus-5-5", "subscription", (900, 1400, 52000, 2600)),
    "claude-haiku": ("claude-account", "api.anthropic.com", "intercept", "claude-haiku-5-5", "subscription", (700, 500, 21000, 900)),
    "claude-sonnet": ("claude-subscription", "anthropic", "generate", "claude-sonnet-5", "subscription", (1200, 1100, 38000, 1800)),
    "codex": ("codex-account", "chatgpt.com", "intercept", "gpt-6.1-sol", "subscription", (14000, 600, 11000, 0)),
    "copilot": ("copilot-account", "api.individual.githubcopilot.com", "intercept", "claude-sonnet-5", "subscription", (600, 450, 22000, 0)),
    "deepseek": ("opencode", "deepseek", "generate", "deepseek-flash", "api", (26000, 900, 18000, 0)),
    "openai": ("aider", "openai", "generate", "gpt-6-luna", "api", (19000, 1300, 9000, 0)),
    "astra": ("codex", "openai", "generate", "gpt-6-astra", "api", (8000, 1500, 4000, 0)),
    "qwen": ("qwen", "laptop", "generate", "qwen3.6:35b", None, (9000, 700, 0, 0)),
    "local": ("opencode", "desktop", "generate", "qwen3.8:latest", None, (11000, 600, 0, 0)),
}
# Who uses what, and how much: sessions on a working day, and what they tend to run. The demo's own
# past sessions stay off the agents the screenshots look up by name (qwen, claude-account).
HISTORY_PEOPLE = {
    DEMO_USER: (1.4, ("codex", "copilot", "claude-sonnet", "deepseek", "local")),
    "priya": (2.6, ("claude-opus", "claude-opus", "claude-haiku", "astra")),
    "sam": (1.8, ("codex", "codex", "openai", "qwen")),
    "lena": (1.2, ("deepseek", "deepseek", "claude-sonnet", "local")),
    "marco": (0.8, ("qwen", "local", "copilot")),
}
HISTORY_PROJECTS = ("payments-api", "web-dashboard", "data-pipeline", "cli-tools", "docs-site", "infra")


def seed_history(db: Database) -> None:
    """Five weeks of the team's past sessions, so Analytics, Costs and the session list have days, people,
    models and providers to show. Records only (no payloads): each priced as the server would price it."""
    from agent_server import pricing

    store = SessionStore(db)
    # Its own dice, so adding history doesn't change what the rest of the demo rolls.
    dice = random.Random(11)
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    for back in range(HISTORY_DAYS, 0, -1):
        day = today - timedelta(days=back)
        # Quiet weekends, and a team that uses it more as the weeks go by.
        busy = (0.15 if day.weekday() >= 5 else 1.0) * (0.55 + 0.45 * (HISTORY_DAYS - back) / HISTORY_DAYS)
        for person, (rate, kinds) in HISTORY_PEOPLE.items():
            for _ in range(round(rate * busy * dice.uniform(0.3, 1.7))):
                client, backend, kind, model, basis, tokens = HISTORY_KINDS[dice.choice(kinds)]
                project = dice.choice(HISTORY_PROJECTS)
                session = store.create({"client": client, "backend": backend if basis != "subscription" or kind == "generate" else "direct",
                                        "model": model, "project": project,
                                        "tags": {"project_path": f"/home/dev/{project}", "owner": person, "tenant": DEMO_ORG,
                                                 "visibility": "private", "access": "control"}})
                clock = day + timedelta(hours=dice.uniform(7, 17), minutes=dice.uniform(0, 59))
                session.started_at = iso(clock)
                for _ in range(dice.randint(3, 24)):
                    session.request_sequence += 1
                    session.request_count += 1
                    telemetry = RequestTelemetry(session.session_id, f"{session.request_sequence:06d}", client, model, backend, "/v1/messages", 4000,
                                                 kind=kind, method="POST", host=backend if kind == "intercept" else None)
                    telemetry.status = 200
                    telemetry.completed = True
                    telemetry.input_tokens, telemetry.output_tokens, telemetry.cache_read_tokens, telemetry.cache_creation_tokens = (
                        round(value * dice.uniform(0.4, 1.8)) for value in tokens
                    )
                    record = telemetry.finish()
                    took = dice.uniform(1500, 14000)
                    record.update(timestamp=iso(clock), total_ms=took)
                    cost = pricing.estimate(pricing.DEFAULT_PRICES, record, basis) if basis else None
                    if cost is not None:
                        record["cost"] = cost
                    store.append_request(session, record)
                    clock += timedelta(milliseconds=took, seconds=dice.uniform(5, 240))
                session.last_activity_at = iso(clock)
                session.ended_at = iso(clock + timedelta(seconds=30))
                session.exit_status = 0
                store._write_session(session)


# --------------------------------------------------------------------------- driving real traffic

TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "Read a file", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]


def openai_turn(client: httpx.Client, session_id: str, model: str) -> None:
    body = {"model": model, "stream": True, "stream_options": {"include_usage": True}, "max_tokens": 2048, "tools": TOOLS,
            "messages": [{"role": "system", "content": "You are a coding agent working in a Python repository."},
                         {"role": "user", "content": PROMPT}]}
    with client.stream("POST", f"{PROXY}/session/{session_id}/v1/chat/completions", json=body) as response:
        for _ in response.iter_lines():
            pass


def anthropic_turn(client: httpx.Client, session_id: str, model: str) -> None:
    body = {"model": model, "max_tokens": 4096, "stream": True, "system": "You are Claude Code.",
            "messages": [{"role": "user", "content": PROMPT}],
            "tools": [{"name": "Read", "description": "Read a file", "input_schema": {"type": "object"}}]}
    with client.stream("POST", f"{PROXY}/session/{session_id}/anthropic/v1/messages", json=body,
                       headers={"authorization": "Bearer demo-token", "anthropic-version": "2023-06-01"}) as response:
        for _ in response.iter_lines():
            pass


def _box(lines: list[str], width: int = 78) -> str:
    """A bordered prompt like the agent's permission dialogs; lines may carry colour codes."""
    blue, reset = "\x1b[34m", "\x1b[0m"
    rows = [f"{blue}╭{'─' * width}╮{reset}"]
    for line in lines:
        padding = width - 1 - len(re.sub(r"\x1b\[[\d;]*m", "", line))
        rows.append(f"{blue}│{reset} {line}{' ' * padding}{blue}│{reset}")
    rows.append(f"{blue}╰{'─' * width}╯{reset}")
    return "\r\n".join(rows) + "\r\n"


AGENT_SCREEN = (
    "\x1b[38;2;215;119;87m✻\x1b[0m \x1b[1mWelcome to Claude Code\x1b[0m\r\n"
    "  \x1b[2mcwd: C:\\work\\payments-api\x1b[0m\r\n\r\n"
    "\x1b[2m>\x1b[0m Add retries with backoff to PaymentClient.charge and cover them with tests\r\n\r\n"
    "\x1b[38;2;215;119;87m⏺\x1b[0m I'll wrap the charge call in an exponential backoff and add tests for the retry limits.\r\n\r\n"
    "\x1b[32m⏺\x1b[0m \x1b[1mUpdate\x1b[0m(src/payments/client.py)\r\n"
    "  ⎿  Updated src/payments/client.py with \x1b[32m18 additions\x1b[0m and \x1b[31m3 removals\x1b[0m\r\n\r\n"
    "\x1b[32m⏺\x1b[0m \x1b[1mBash\x1b[0m(pytest tests/test_client.py -q)\r\n"
    "  ⎿  \x1b[32m12 passed\x1b[0m in 0.84s\r\n\r\n"
    "\x1b[38;2;215;119;87m⏺\x1b[0m Retries are in. One more edit adds a test for the jitter bounds.\r\n\r\n"
    + _box([
        "\x1b[1mEdit file\x1b[0m tests/test_client.py",
        "",
        "Do you want to make this edit to test_client.py?",
        "\x1b[36m❯ 1. Yes\x1b[0m",
        "  2. Yes, and don't ask again this session",
        "  3. No, and tell Claude what to do differently",
    ])
)


class FakeLauncher:
    """The launcher's side of a shared terminal, for made-up agents: what they print goes to
    the server as the Go client sends it, and what the console types comes back to typed."""

    def __init__(self, session_id: str, command: str, size: tuple[int, int], typed: Any):
        url = PROXY.replace("http", "ws", 1) + f"/api/terminals/{session_id}/host"
        self.socket = DemoSocket(url, {"cols": size[0], "rows": size[1], "command": command})
        self.lock = threading.Lock()
        self.typed = typed
        threading.Thread(target=self._receive, daemon=True).start()

    def append(self, data: bytes) -> None:
        with self.lock:
            self.socket.send(data)

    def finish(self, status: int) -> None:
        with self.lock:
            self.socket.send(json.dumps({"type": "exit", "status": status}))
            self.socket.close()

    def _receive(self) -> None:
        try:
            for message in self.socket:
                data = json.loads(message) if isinstance(message, str) else {}
                for part in data.get("parts") or [] if data.get("type") == "input" else []:
                    self.typed(part, self)
        except Exception:  # noqa: BLE001 - the demo is stopping
            pass


def share_terminals() -> None:
    """Agents' terminals, shared the way the launcher shares them: one waiting, one working, one finished."""
    client = api_client()

    def typed(text: str, terminal: FakeLauncher) -> None:
        if text not in {"\r", "\x1b"}:
            terminal.append(f"\r\n\x1b[2m>\x1b[0m {text}\r\n\r\n\x1b[38;2;215;119;87m⏺\x1b[0m On it.\r\n".encode())

    def start(agent: str, model: str, backend: str, project: str, size: tuple[int, int], tags: dict[str, str]) -> tuple[str, FakeLauncher, FakeLauncher]:
        body = {"client": agent, "model": model, "backend": backend, "project": project, "tags": tags}
        session_id = client.post(f"{PROXY}/api/sessions", json=body).json()["session_id"]
        terminal = FakeLauncher(session_id, agent, size, typed)
        return session_id, terminal, terminal

    _, waiting, _ = start("claude-account", "", "direct", "payments-api", (82, 30), {"hostname": "ai-laptop", "machine_name": "ai-laptop"})
    waiting.append(AGENT_SCREEN.encode())

    _, working, _ = start("opencode", "qwen3.8:latest", "desktop", "web-dashboard", (100, 28), {"machine": "ai-desktop", "machine_name": "ai-desktop"})
    working.append(b"\x1b[1mopencode\x1b[0m  qwen3.8:latest via agent-proxy\r\n\r\n> Make the usage chart load lazily\r\n\r\n"
                   b"Reading src/charts/Usage.tsx\r\nReading src/pages/Dashboard.tsx\r\n\r\n"
                   b"\x1b[36m\xe2\x97\x86\x1b[0m The chart pulls in the whole charting library on first paint. I'll split it\r\n"
                   b"  into its own chunk and load it when the panel scrolls into view.\r\n\r\n"
                   b"\x1b[32m\xe2\x9c\x93\x1b[0m \x1b[1mEdit\x1b[0m src/pages/Dashboard.tsx  \x1b[32m+14\x1b[0m \x1b[31m-3\x1b[0m\r\n"
                   b"    \x1b[32m+ const Usage = lazy(() => import(\"../charts/Usage\"));\x1b[0m\r\n"
                   b"    \x1b[32m+ <Suspense fallback={<ChartSkeleton />}>\x1b[0m\r\n"
                   b"\x1b[32m\xe2\x9c\x93\x1b[0m \x1b[1mBash\x1b[0m npm run build\r\n"
                   b"    dist/assets/Usage-3f9c.js   \x1b[2m84.2 kB\x1b[0m  (was in index.js)\r\n\r\n")

    # Different agents on every machine: Codex and Copilot at work on build-box, and Qwen Code on
    # ai-laptop done and waiting at its prompt (quiet, with no question on its screen).
    _, codex, _ = start("codex-account", "", "direct", "data-pipeline", (96, 26), {"machine": "build-box", "machine_name": "build-box"})
    codex.append(b"\x1b[1m>_ OpenAI Codex\x1b[0m  \x1b[2mgpt-6.1-sol \xc2\xb7 ~/src/data-pipeline\x1b[0m\r\n\r\n"
                 b"> Make the nightly export job resumable after a crash\r\n\r\n"
                 b"\x1b[2m\xe2\x80\xa2\x1b[0m Explored jobs/export.py, jobs/checkpoint.py\r\n"
                 b"\x1b[32m\xe2\x80\xa2\x1b[0m Edited jobs/export.py \x1b[32m+42\x1b[0m \x1b[31m-9\x1b[0m\r\n\r\n")
    _, copilot, _ = start("copilot-account", "", "direct", "infra", (96, 26), {"machine": "build-box", "machine_name": "build-box"})
    copilot.append(b"\x1b[1mGitHub Copilot\x1b[0m  \x1b[2m~/src/infra\x1b[0m\r\n\r\n"
                   b"> Bump the Postgres chart to 17 and fix the failing upgrade test\r\n\r\n"
                   b"\x1b[32m\xe2\x9c\x93\x1b[0m Read charts/postgres/values.yaml\r\n"
                   b"\x1b[32m\xe2\x9c\x93\x1b[0m Ran helm template charts/postgres\r\n"
                   b"\x1b[32m\xe2\x9c\x93\x1b[0m Edited charts/postgres/Chart.yaml  \x1b[2mappVersion 16.4 \xe2\x86\x92 17.2\x1b[0m\r\n"
                   b"\x1b[32m\xe2\x9c\x93\x1b[0m Edited tests/upgrade_test.go  \x1b[32m+6\x1b[0m \x1b[31m-2\x1b[0m\r\n\r\n"
                   b"  The upgrade test pinned the old image tag. It now reads the tag from the\r\n"
                   b"  chart, so the next bump won't break it.\r\n\r\n"
                   b"\x1b[33m\xe2\x97\x8f\x1b[0m go test ./tests/... -run Upgrade\r\n")
    _, qwen, _ = start("qwen", "qwen3.6:35b", "laptop", "docs-site", (96, 26), {"hostname": "ai-laptop", "machine_name": "ai-laptop"})
    qwen.append(b"\x1b[1;35mQwen Code\x1b[0m  \x1b[2mqwen3.6:35b \xc2\xb7 ~/src/docs-site\x1b[0m\r\n\r\n"
                b"> Fix the broken links in the deployment guide\r\n\r\n"
                b"\x1b[32m\xe2\x9c\x94\x1b[0m ReadFile docs/deployment.md\r\n"
                b"\x1b[32m\xe2\x9c\x94\x1b[0m Grep \"](/\" docs/  \x1b[2m31 matches in 9 files\x1b[0m\r\n"
                b"\x1b[32m\xe2\x9c\x94\x1b[0m Edit docs/deployment.md  \x1b[32m+4\x1b[0m \x1b[31m-4\x1b[0m\r\n"
                b"\x1b[32m\xe2\x9c\x94\x1b[0m Edit docs/helm.md  \x1b[32m+1\x1b[0m \x1b[31m-1\x1b[0m\r\n"
                b"\x1b[32m\xe2\x9c\x94\x1b[0m Shell npm run check-links  \x1b[32m0 broken\x1b[0m\r\n\r\n"
                b"\x1b[35m\xe2\x9c\xa6\x1b[0m Fixed 4 links in docs/deployment.md and docs/helm.md: three pointed at\r\n"
                b"  pages that moved under docs/guides/, one at a heading that was renamed.\r\n\r\n"
                b"\x1b[2m>\x1b[0m ")

    def spin() -> None:
        frames, seconds = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", 0
        while not stopping.is_set():
            frame = frames[seconds % len(frames)]
            working.append(f"\r\x1b[K\x1b[33m{frame}\x1b[0m Thinking… ({seconds}s)".encode())
            codex.append(f"\r\x1b[K\x1b[36m{frame}\x1b[0m Working ({seconds + 40}s \xb7 esc to interrupt)".encode())
            copilot.append(f"\r\x1b[K\x1b[35m{frame}\x1b[0m Running tests ({seconds + 12}s)".encode())
            seconds += 1
            time.sleep(1)

    threading.Thread(target=spin, daemon=True).start()

    finished_id, finished, share = start("codex", "gpt-oss:20b", "desktop", "cli-tools", (90, 24), {"machine": "ai-desktop", "machine_name": "ai-desktop"})
    finished.append(b"> Rename the --out flag to --output\r\n\r\nUpdated 3 files. All 41 tests pass.\r\n")
    time.sleep(1)
    share.finish(0)
    client.patch(f"{PROXY}/api/sessions/{finished_id}", json={"ended": True, "exit_status": 0})


CONTAINERS = {
    "strata-iq3": {"engine": "strata", "image": "ghcr.io/niko1221/strata:latest-cuda13", "port": 18438, "gpus": "all",
                   "env": {"MODEL": "IQ3_XXS", "CONTEXT": "32768", "KV": "q4_0", "VISION": "off"}},
    "strata-coder": {"engine": "strata", "image": "ghcr.io/niko1221/strata:latest-cuda13", "port": 18439, "gpus": "all",
                     "env": {"FAMILY": "coder", "MODEL": "IQ1_M", "CONTEXT": "64k"}},
    # Points at the fake laptop Ollama, so it answers like a running container.
    "ollama-cpu": {"engine": "ollama", "image": "ollama/ollama:latest", "port": LAPTOP, "gpus": "none",
                   "env": {"OLLAMA_KV_CACHE_TYPE": "q8_0", "OLLAMA_CONTEXT_LENGTH": "32768"}},
}
CONTAINER_MACHINES = {"strata-iq3": "ai-desktop", "strata-coder": "ai-desktop", "ollama-cpu": "ai-laptop"}


# The engines a machine's client offers (client/internal/containers), trimmed to what the console shows.
ENGINES = [
    {"id": "strata", "label": "Strata", "backend_type": "strata", "image": "ghcr.io/niko1221/strata:latest-cuda13",
     "images": [f"ghcr.io/niko1221/strata:latest-{flavor}" for flavor in ("cuda13", "cuda12", "rocm-gfx120x")],
     "port": 8080, "data_path": "/data", "volume": "strata-data", "health_path": "/health", "stop_timeout": 120,
     "options": [{"env": env, "label": label, "choices": [], "hint": ""} for env, label in
                 (("MODEL", "Model size"), ("FAMILY", "Family"), ("CONTEXT", "Context"), ("KV", "KV cache"), ("VISION", "Vision"), ("PARALLEL", "Parallel requests"))],
     "summary": "Strata's own image. The first start downloads the model into the shared volume (tens of GB), later starts take a minute or two to load it."},
    {"id": "ollama", "label": "Ollama", "backend_type": "ollama", "image": "ollama/ollama:latest", "images": ["ollama/ollama:latest", "ollama/ollama:rocm"],
     "port": 11434, "data_path": "/root/.ollama", "volume": "ollama-models", "health_path": "/api/version", "stop_timeout": 30,
     "options": [{"env": env, "label": label, "choices": [], "hint": ""} for env, label in
                 (("OLLAMA_CONTEXT_LENGTH", "Context"), ("OLLAMA_KV_CACHE_TYPE", "KV cache"), ("OLLAMA_FLASH_ATTENTION", "Flash attention"))],
     "summary": "The official Ollama image. Models pulled into the shared volume are there for every Ollama container."},
    {"id": "llamacpp", "label": "llama.cpp", "backend_type": "llamacpp", "image": "ghcr.io/ggml-org/llama.cpp:server-cuda",
     "images": ["ghcr.io/ggml-org/llama.cpp:server-cuda", "ghcr.io/ggml-org/llama.cpp:server"],
     "port": 8080, "data_path": "/root/.cache/llama.cpp", "volume": "llamacpp-models", "health_path": "/health", "stop_timeout": 30,
     "options": [{"env": env, "label": label, "choices": [], "hint": ""} for env, label in
                 (("LLAMA_ARG_CTX_SIZE", "Context"), ("LLAMA_ARG_N_PARALLEL", "Parallel requests"), ("LLAMA_ARG_THINK_BUDGET", "Thinking budget"))],
     "summary": "llama.cpp's official server image (server-cuda for NVIDIA, server for CPU). Download models from Hugging Face on its model server's tab."},
]
RUNS_CONTAINERS = {"enabled": True, "cli": "docker", "found": True, "online": True, "version": "28.4.0", "error": "", "engines": ENGINES}

# The demo server's own version (VERSION and the commit), so the model machines are up to date.
SERVER_VERSION = build_version()

# A phone in Termux running llama.cpp (Termux's OpenCL build), installed and started from the console.
LLAMA_STATE = {"known": True, "found": True, "running": True, "managed": True, "port": 9931, "version": "b12032",
               "source": "termux", "variant": "opencl", "release": None, "platform": "termux", "install": None}

# Model machines say they're this computer (127.0.0.1), where the demo's fake model servers listen, so the
# backends above are theirs. build-box runs an Ollama nobody has added yet.
FLEET = [
    {"id": "ai-desktop", "name": "ai-desktop", "os": "linux", "arch": "amd64", "version": SERVER_VERSION, "address": "127.0.0.1",
     "agents": ["qwen", "opencode"], "providers": [{"type": "ollama", "port": DESKTOP, "models": 4, "version": "0.12.3"},
                                                   {"type": "docker", "port": DOCKER, "models": 2, "version": ""}],
     "containers": RUNS_CONTAINERS, "launches": False, "roots": [],
     # Its Ollama runs as a service (not agent-machine's to stop); two Strata profiles wait to start.
     "servers": {"enabled": True, "ollama": {"found": True, "running": True, "managed": False, "version": "0.12.3"},
                 "strata": {"found": True, "profiles": [{"id": "qwen-iq3", "state": "stopped"}, {"id": "coder", "state": "stopped"}]}}},
    {"id": "ai-laptop", "name": "ai-laptop", "os": "windows", "arch": "amd64", "version": SERVER_VERSION, "address": "127.0.0.1",
     "agents": ["claude-account", "copilot-account"], "providers": [{"type": "ollama", "port": LAPTOP, "models": 2, "version": "0.12.3"}],
     "containers": RUNS_CONTAINERS, "launches": False, "roots": [],
     # The provider CLIs there, as `agent accounts detect` reports them (never a token).
     "accounts": {"at": time.time(), "clis": [
         {"provider": "claude", "installed": True, "version": "2.1.4 (Claude Code)", "auth": "signed_in", "method": "claude.ai", "subscription": True, "modes": ["runner"]},
         {"provider": "codex", "installed": False, "auth": "unknown", "modes": ["runner"]},
         {"provider": "copilot", "installed": True, "version": "1.0.2", "auth": "unknown", "modes": ["runner", "token"]},
     ]}},
    {"id": "build-box", "name": "build-box", "os": "linux", "arch": "amd64", "version": "0.3.2", "address": "192.168.1.40",
     "agents": ["claude-account", "codex-account", "qwen", "opencode"], "providers": [{"type": "ollama", "port": 11434, "models": 6, "version": "0.12.3"}],
     "containers": {"enabled": False, "cli": "docker", "found": True, "online": False, "version": "", "error": "", "engines": []},
     "servers": {"enabled": True, "ollama": {"found": True, "running": True, "managed": True, "version": "0.12.3", "bind": "0.0.0.0"},
                 "strata": {"found": False, "profiles": []}},
     "launches": True, "roots": ["/home/joey/code", "/srv/repos"], "commands": True, "updates": True, "files": True,
     "accounts": {"at": time.time(), "clis": [
         {"provider": "claude", "installed": True, "version": "2.1.4 (Claude Code)", "auth": "signed_in", "method": "claude.ai", "subscription": True, "modes": ["runner"]},
         {"provider": "codex", "installed": True, "version": "codex-cli 0.52.0", "auth": "signed_in", "method": "chatgpt", "subscription": True, "modes": ["runner"]},
         {"provider": "copilot", "installed": False, "auth": "unknown", "modes": ["runner", "token"]},
     ]},
     # MCP servers it shares (agent-machine mcp add); nothing listens on the bridge's port in the demo.
     "mcp": {"port": 18198, "key": "demo", "servers": [
         {"name": "github", "transport": "stdio", "description": "GitHub issues and pull requests", "found": True},
         {"name": "postgres", "transport": "stdio", "description": "The staging database, read-only", "found": True},
         {"name": "docs", "transport": "http", "description": "Internal docs search", "found": True},
     ]}},
    {"id": "pocket", "name": "pocket", "os": "linux", "arch": "arm64", "version": SERVER_VERSION, "address": "127.0.0.1",
     "agents": ["codex"], "providers": [{"type": "llamacpp", "port": 9931, "models": 2}],
     "containers": {"enabled": False, "cli": "docker", "found": False, "online": False, "version": "", "error": "", "engines": []},
     "servers": {"enabled": True, "ollama": {"found": False, "running": False, "managed": False, "version": None},
                 "strata": {"found": False, "profiles": []}, "llamacpp": LLAMA_STATE},
     "launches": False, "roots": []},
]

# build-box's folders inside its launch folders, for the folder picker (agent-machine lists them there).
FOLDERS = {
    "/home/joey/code": ["api", "ai-control-mesh", "dotfiles", "web"],
    "/home/joey/code/api": ["cmd", "docs", "internal", "migrations"],
    "/home/joey/code/api/internal": ["auth", "billing", "store"],
    "/home/joey/code/ai-control-mesh": ["client", "machine", "python", "server", "ui"],
    "/home/joey/code/web": ["public", "src"],
    "/srv/repos": ["infra", "mirror"],
}


def fake_folders(request: dict, roots: list[str]) -> dict:
    """The folders_result agent-machine sends: the folders inside one, never above a launch folder."""
    folder = str(request.get("path") or "").rstrip("/")
    names = FOLDERS.get(folder, [])
    up = folder.rsplit("/", 1)[0]
    parent = up if folder not in roots and any(up == root or up.startswith(root + "/") for root in roots) else None
    return {"ok": True, "parent": parent, "folders": [{"name": name, "path": f"{folder}/{name}"} for name in names]}


# A few of build-box's files, for the file viewer (agent-machine reads them there).
FILES = {
    "/home/joey/code/api/README.md": (
        "# api\n\nThe billing and accounts service. To put it on a server, see [Deploying](docs/deploy.md).\n\n"
        "![Project logo](logo.png)\n\n"
        "## Running it\n\n```bash\ngo run ./cmd/api --config config.yaml\n```\n\n"
        "It listens on **:8080** and keeps its data in PostgreSQL. The [`Makefile`](Makefile) builds and tests it.\n\n"
        "| Setting | Default | What it does |\n| --- | --- | --- |\n| `listen` | `:8080` | The address it serves |\n"
        "| `database` | none | The PostgreSQL URL |\n| `workers` | `4` | Webhook deliveries at once |\n\n"
        "## Before a release\n\n- [x] Migrations run on a copy of production\n- [ ] Retries back off ([details](#running-it))\n\n"
        "> Keep secrets in the environment, never in `config.yaml`.\n"
    ),
    "/home/joey/code/api/docs/deploy.md": "# Deploying\n\n1. Build the image: `make image`.\n2. Run the migrations.\n3. Roll the pods.\n\nBack to the [README](../README.md#running-it).\n",
    "/home/joey/code/api/go.mod": "module example.com/api\n\ngo 1.24\n\nrequire (\n\tgithub.com/jackc/pgx/v5 v5.7.2\n\tgolang.org/x/sync v0.10.0\n)\n",
    "/home/joey/code/api/Makefile": "build:\n\tgo build -o bin/api ./cmd/api\n\ntest:\n\tgo test ./...\n",
}


FILE_IMAGES = {"/home/joey/code/api/logo.png": (ROOT / "site/assets/icon-192.png").read_bytes()}


def fake_files(request: dict, roots: list[str]) -> dict:
    """The files_result agent-machine sends: a folder listing, text, or a complete raster image."""
    path = str(request.get("path") or "").rstrip("/")
    if request.get("op") == "read":
        if path in FILE_IMAGES:
            data = FILE_IMAGES[path]
            return {"ok": True, "path": path, "name": path.rsplit("/", 1)[-1], "size": len(data), "modified": int(time.time()) - 3600,
                    "binary": True, "truncated": False, "image": {"media_type": "image/png", "data": base64.b64encode(data).decode("ascii")}}
        if path not in FILES:
            return {"ok": False, "error": f"couldn't read {path}"}
        text = FILES[path]
        return {"ok": True, "path": path, "name": path.rsplit("/", 1)[-1], "size": len(text.encode()), "modified": int(time.time()) - 3600, "binary": False, "truncated": False, "content": text}
    folders = fake_folders(request, roots)
    entries = [{"name": item["name"], "path": item["path"], "kind": "folder", "modified": int(time.time()) - 86400} for item in folders["folders"]]
    entries += [
        {"name": name.rsplit("/", 1)[-1], "path": name, "kind": "file", "size": len(text.encode()), "modified": int(time.time()) - 3600}
        for name, text in sorted(FILES.items(), key=lambda item: item[0].lower()) if name.rsplit("/", 1)[0] == path
    ]
    entries += [{"name": name.rsplit("/", 1)[-1], "path": name, "kind": "file", "size": len(data), "modified": int(time.time()) - 3600}
                for name, data in FILE_IMAGES.items() if name.rsplit("/", 1)[0] == path]
    entries.sort(key=lambda entry: (entry["kind"] != "folder", entry["name"].lower()))
    return {"ok": True, "path": path, "parent": folders["parent"], "entries": entries, "truncated": False}


class FakeContainers:
    """A machine's containers, kept in memory: answers the server's requests the way the client does."""

    def __init__(self):
        self.running: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}

    def handle(self, request: dict) -> dict:
        op = request.get("op")
        if op == "status":
            reports = {}
            for name, item in request.get("containers", {}).items():
                current = self.running.get(name)
                state = None
                if current:
                    # Strata reports healthy once its model is loaded.
                    state = {"status": "running" if current["running"] else "exited", "running": current["running"],
                             "health": "healthy" if current["running"] and item["spec"]["engine"] == "strata" else None,
                             "started_at": "2026-10-26T09:00:00Z", "finished_at": None, "exit_code": 0, "error": None, "spec": "demo", "image": item["spec"]["image"]}
                env = " ".join(f"-e {key}={value}" for key, value in sorted(item["spec"].get("env", {}).items()))
                command = f"docker run -d --name ai-proxy-{name} -p {item['bind']}:{item['spec']['port']}:8080 {env} {item['spec']['image']}"
                reports[name] = {"state": state, "job": self.jobs.get(name), "outdated": False, "command": command, "problem": None}
            return {"ok": True, "runtime": {"online": True}, "containers": reports}
        name = request["name"]
        if op in {"start", "restart"}:
            for other in request.get("stop_first", []):
                self.running.get(other["name"], {})["running"] = False
            self.running[name] = {"running": True}
        elif op == "stop" and name in self.running:
            self.running[name]["running"] = False
        elif op == "remove":
            self.running.pop(name, None)
            return {"ok": True}
        elif op == "logs":
            return {"ok": True, "text": "strata: serving on :8080\nstrata: model loaded\n"}
        now = time.time()
        self.jobs[name] = {"action": "switch" if request.get("stop_first") else op, "state": "done", "started_at": now, "finished_at": now, "error": None, "lines": ["Started"]}
        return {"ok": True, "job": self.jobs[name]}


HARDWARE = {
    "ai-laptop": {"cores": 16, "ram_gb": 64, "gpu": "NVIDIA RTX 5000 Ada Laptop", "gpu_gb": 32},
    "ai-desktop": {"cores": 24, "ram_gb": 128, "gpu": "NVIDIA RTX 5090", "gpu_gb": 32},
    "pocket": {"cores": 8, "ram_gb": 12, "gpu": "Adreno 750", "gpu_gb": 12},
    "build-box": {"cores": 32, "ram_gb": 256, "gpu": "NVIDIA RTX 6000 Ada", "gpu_gb": 48},
}
# A few of the settings agent-machine starts Ollama with (machine/internal/servers has them all).
OLLAMA_SETTINGS = [
    {"key": "context_length", "group": "Memory / KV cache", "label": "Context length (tokens)", "kind": "int", "default": 16384,
     "help": "16384 = recommended, 65536 = large agent sessions.", "env": ["OLLAMA_CONTEXT_LENGTH"], "choices": [], "editable": False, "minimum": 1024, "maximum": 1048576},
    {"key": "kv_cache_type", "group": "Memory / KV cache", "label": "KV cache type", "kind": "choice", "default": "q4_0",
     "help": "Quantized types require Flash Attention.", "env": ["OLLAMA_KV_CACHE_TYPE"], "choices": ["f16", "q8_0", "q4_0"], "editable": False, "minimum": 0, "maximum": 0},
    {"key": "flash_attention", "group": "Memory / KV cache", "label": "Flash Attention", "kind": "bool", "default": True,
     "help": "Required for a quantized KV cache.", "env": ["OLLAMA_FLASH_ATTENTION"], "choices": [], "editable": False, "minimum": 0, "maximum": 0},
    {"key": "num_parallel", "group": "Concurrency / scheduling", "label": "Parallel requests", "kind": "int", "default": 1,
     "help": "Each extra slot multiplies KV cache use.", "env": ["OLLAMA_NUM_PARALLEL"], "choices": [], "editable": False, "minimum": 1, "maximum": 128},
    {"key": "keep_alive", "group": "Concurrency / scheduling", "label": "Keep alive", "kind": "choice", "default": "-1",
     "help": "-1 = keep loaded forever.", "env": ["OLLAMA_KEEP_ALIVE"], "choices": ["-1", "0", "5m", "30m"], "editable": True, "minimum": 0, "maximum": 0},
]
# A few of the settings agent-machine starts llama-server with (machine/internal/servers/llamacpp_settings.go has them all).
LLAMA_SETTINGS = [
    {"key": "ctx_size", "group": "Memory / KV cache", "label": "Context length (tokens)", "kind": "int", "default": 16384,
     "help": "16384 = recommended. 0 = the model's own (with Fit on, the largest that fits).", "env": ["LLAMA_ARG_CTX_SIZE"], "choices": [], "editable": False, "minimum": 0, "maximum": 1048576},
    {"key": "cache_type_k", "group": "Memory / KV cache", "label": "KV cache type (K)", "kind": "choice", "default": "f16",
     "help": "q8_0 is about half of f16.", "env": ["LLAMA_ARG_CACHE_TYPE_K"], "choices": ["f16", "q8_0", "q4_0"], "editable": False, "minimum": 0, "maximum": 0},
    {"key": "models_max", "group": "Concurrency / scheduling", "label": "Max loaded models", "kind": "int", "default": 1,
     "help": "The least recently used is unloaded first.", "env": ["LLAMA_ARG_MODELS_MAX"], "choices": [], "editable": False, "minimum": 0, "maximum": 128},
    {"key": "gpu_layers", "group": "Hardware", "label": "GPU layers", "kind": "choice", "default": "auto",
     "help": "auto, all, or a number. 0 = CPU only.", "env": ["LLAMA_ARG_N_GPU_LAYERS"], "choices": ["auto", "all", "0"], "editable": True, "minimum": 0, "maximum": 0},
    {"key": "reasoning_budget", "group": "Reasoning", "label": "Thinking budget (tokens)", "kind": "int", "default": -1,
     "help": "-1 = no limit, 0 = don't think.", "env": ["LLAMA_ARG_THINK_BUDGET"], "choices": [], "editable": False, "minimum": -1, "maximum": 1048576},
]
LLAMA_MODELS = [
    {"name": "unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M", "state": "loaded", "loaded": True, "failed": False, "size": int(2.5 * GB),
     "path": "/data/data/com.termux/files/home/.cache/llama.cpp/unsloth_Qwen3-4B-Instruct-2507-GGUF_Qwen3-4B-Instruct-2507-Q4_K_M.gguf", "context_train": 262144, "progress": None},
    {"name": "ggml-org/gemma-3-1b-it-GGUF:Q8_0", "state": "unloaded", "loaded": False, "failed": False, "size": int(1.1 * GB),
     "path": "/data/data/com.termux/files/home/.cache/llama.cpp/ggml-org_gemma-3-1b-it-GGUF_gemma-3-1b-it-Q8_0.gguf", "context_train": 32768, "progress": None},
]
LOGS = {
    "strata": "[strata] serving qwen3.6-35b-a3b IQ3_XXS on :8080\n[strata] done: 812 tokens in 9.4 s\n",
    "ollama": "time=2026-10-26T09:00:00Z level=INFO msg=\"Listening on 0.0.0.0:11434\"\n",
    "llamacpp": "# agent-machine: starting llama-server (router mode) on 127.0.0.1:9931\nggml_opencl: device: 'QUALCOMM Adreno(TM) 750'\nmain: server is listening on http://127.0.0.1:9931 (router mode)\n",
}


# The fake model servers' ports: the only ones FakeProviders asks over HTTP.
DEMO_PORTS = {LAPTOP, DESKTOP, DOCKER}


class FakeProviders:
    """A machine's model servers, answered the way agent-machine does: it asks the demo's fake servers on loopback."""

    def __init__(self, machine_id: str):
        self.machine_id = machine_id
        self.ollama_config: dict[str, Any] = {setting["key"]: setting["default"] for setting in OLLAMA_SETTINGS} | {"extra_env": ""}
        self.llama_config: dict[str, Any] = {setting["key"]: setting["default"] for setting in LLAMA_SETTINGS} | {"extra_env": "", "reasoning_budget": 512}
        self.llama_models = json.loads(json.dumps(LLAMA_MODELS))

    def handle(self, request: dict, send: Any) -> dict:
        op, kind = request.get("op"), request.get("kind")
        base = f"http://127.0.0.1:{request.get('port')}"
        if op == "metrics":
            return {"ok": True, "metrics": self.metrics()}
        if op == "search":
            return {"ok": True, "result": search(request)}
        if op == "status":
            try:
                return {"ok": True, "state": self.status(kind, base, request)}
            except httpx.HTTPError as exc:
                return {"ok": True, "state": {"online": False, "error": f"{type(exc).__name__}: {exc}", "models": []}}
        if op == "pull":
            total = int(2.1 * GB)
            for step in range(1, 21):
                time.sleep(0.15)
                send({"type": "provider_progress", "request": request["request"], "progress": {"status": "downloading", "total": total, "completed": total * step // 20}})
            if kind == "llamacpp":
                self.llama_models.append({"name": request["model"], "state": "unloaded", "loaded": False, "failed": False, "size": total,
                                          "path": "", "context_train": None, "progress": None})
            return {"ok": True}
        if op == "files" and kind == "llamacpp":
            # What downloading it by hand means, as agent-machine answers from Hugging Face's listing.
            repo, _, quant = str(request.get("model")).partition(":")
            quant = (quant or "Q4_K_M").upper()
            name = f"{repo.split('/')[-1].removesuffix('-GGUF')}-{quant}"
            folder = "/data/data/com.termux/files/home/.config/agent-machine/llamacpp-models"
            files = [{"repo_path": f"{name}.gguf", "path": f"{name}/{name}.gguf", "size": int(4.7 * GB), "sha256": "9f2c" * 16,
                      "url": f"https://huggingface.co/{repo}/resolve/main/{name}.gguf"}]
            return {"ok": True, "result": {"model": f"{repo}:{quant}", "name": name, "folder": folder, "dir": f"{folder}/{name}",
                                           "files": files, "serves_folder": True}}
        if op == "reload" and kind == "llamacpp":
            return {"ok": True, "result": {"reloaded": "llamacpp-models"}}
        if op in {"delete", "load", "unload"} and kind == "llamacpp":
            for model in self.llama_models:
                if model["name"] == request.get("model"):
                    model.update(loaded=op == "load", state="loaded" if op == "load" else "unloaded")
            if op == "delete":
                self.llama_models = [model for model in self.llama_models if model["name"] != request.get("model")]
        if op in {"delete", "load", "unload"}:
            if kind == "strata":
                return {"ok": True, "result": {"status": "loaded" if op == "load" else "unloaded"}}
            return {"ok": True, "result": {{"delete": "deleted", "load": "loaded", "unload": "unloaded"}[op]: request.get("model")}}
        if op == "log":
            return {"ok": True, "text": LOGS.get(kind, "")}
        return {"ok": False, "error": f"unknown request: {op} {kind}"}

    def status(self, kind: str, base: str, request: dict) -> dict:
        if kind == "llamacpp":
            # The demo serves no llama-server: answer as its router would.
            return {"online": True, "router": True, "models": self.llama_models}
        if kind == "ollama" and request.get("port") not in DEMO_PORTS:
            # build-box's Ollama isn't served by the demo: never ask a real one on this computer.
            return {"online": True, "version": "0.12.3", "models": [
                {"name": name, "size": int(size * GB), "modified_at": "2026-10-20T10:00:00Z", "family": family, "parameter_size": params,
                 "quantization": "Q4_K_M", "loaded": False, "context_length": None, "size_loaded": None, "size_vram": None, "gpu_percent": None, "expires_at": None}
                for name, size, family, params in (("qwen3.6:35b", 21.1, "qwen35moe", "35.5B"), ("devstral:24b", 14.3, "mistral3", "23.6B"), ("gpt-oss:20b", 13.8, "gptoss", "20.9B"))
            ]}
        if kind == "ollama":
            tags, running = httpx.get(f"{base}/api/tags", timeout=4).json(), httpx.get(f"{base}/api/ps", timeout=4).json()
            loaded = {item["name"]: item for item in running["models"]}
            models = []
            for item in tags["models"]:
                state, details = loaded.get(item["name"]), item.get("details") or {}
                models.append({
                    "name": item["name"], "size": item["size"], "modified_at": item["modified_at"], "family": details.get("family"),
                    "parameter_size": details.get("parameter_size"), "quantization": details.get("quantization_level"), "loaded": state is not None,
                    "context_length": state and state["context_length"], "size_loaded": state and state["size"], "size_vram": state and state["size_vram"],
                    "gpu_percent": round(state["size_vram"] * 100 / state["size"]) if state else None, "expires_at": state and state["expires_at"],
                })
            version = httpx.get(f"{base}/api/version", timeout=4).json()["version"]
            return {"online": True, "version": version, "models": sorted(models, key=lambda model: model["name"])}
        if kind == "docker":
            pulled, running = httpx.get(f"{base}/models", timeout=4).json(), httpx.get(f"{base}/engines/ps", timeout=4).json()
            loaded = {item["model_name"] for item in running}
            models = []
            for item in pulled:
                tags = [tag.removeprefix("docker.io/").removesuffix(":latest") for tag in item["tags"]]
                config = item.get("config") or {}
                models.append({
                    "id": item["id"], "name": tags[0], "tags": tags, "created": item["created"], "format": config.get("format"),
                    "quantization": config.get("quantization"), "parameters": config.get("parameters"), "architecture": config.get("architecture"),
                    "size": config.get("size"), "context_size": config.get("context_size"), "loaded": tags[0] in loaded, "loading": False,
                    "in_use": False, "engine": "llama.cpp" if tags[0] in loaded else None,
                })
            return {"online": True, "engines": httpx.get(f"{base}/engines/status", timeout=4).json(), "models": models,
                    "disk": httpx.get(f"{base}/engines/df", timeout=4).json()}
        # Strata: the demo serves no Strata, so answer as a loaded one would.
        model = {"id": "qwen3.6-35b-a3b", "status": {"value": "loaded"}, "meta": {"n_ctx": 32768}}
        return {
            "online": True, "health": {"service": "strata", "model": model["id"], "loaded": True, "max_context": 32768, "api_key": True},
            "status": {"service": "strata", "model": model["id"], "loaded": True, "engine": "0.1.48",
                       "machine": {"gpu": {"name": "NVIDIA RTX 5090", "used_mib": 29800, "total_mib": 32607}}},
            "key": {"state": "ok", "source": "container" if request.get("key") else "profile qwen-iq3"},
            "models": [model, {**model, "id": "iq3", "alias_of": model["id"]}], "instance": None,
        }

    def metrics(self) -> dict:
        machine, wave, up = HARDWARE[self.machine_id], (time.time() % 20) / 20, time.time() - STARTED
        gpu_total = machine["gpu_gb"] * GB
        return {
            "hostname": self.machine_id, "platform": "Linux 6.14.0", "timestamp": time.time(),
            "cpu": {"percent": round(18 + 14 * wave + random.random() * 6, 1), "count": machine["cores"]},
            "memory": {"used": int(machine["ram_gb"] * 0.42 * GB), "total": int(machine["ram_gb"] * GB), "percent": 42.0},
            "gpus": [{"index": 0, "name": machine["gpu"], "utilization": round(72 + 20 * wave + random.random() * 6, 1),
                      "memory_used": int(gpu_total * 0.93), "memory_total": int(gpu_total),
                      "temperature": 67 + int(6 * wave), "power_watts": round(280 + 60 * wave, 1)}],
            "gpu_source": "nvidia-smi", "gpu_error": None,
            # Running totals, as a real agent-machine reports them: the console works out the rates.
            "uptime_seconds": 3 * 86400 + up, "load_average": [round(1.2 + wave, 2), 1.4, 1.1],
            "swap": {"used": int(0.3 * GB), "total": 8 * GB},
            "network": {"bytes_recv": int(41 * GB + up * 2.4e6), "bytes_sent": int(9 * GB + up * 6e5),
                        "packets_recv": int(3.1e7 + up * 2100), "packets_sent": int(1.2e7 + up * 900), "errors": 0, "interfaces": 2},
            "disks": [{"mountpoint": "/", "total": 930 * GB, "used": 512 * GB, "free": 418 * GB, "percent": 55.1,
                       "holds": ["Settings", "Data", "Launch folder"]}],
            "process": {"pid": 2417, "rss": 38 * 1024 * 1024, "threads": 24, "started_at": STARTED},
            "agent": {"connected_since": STARTED, "connects": 1, "requests": int(up / 2), "tunnels_open": 2, "tunnels_total": 14 + int(up / 30),
                      "tunnel_bytes_in": int(up * 9e3), "tunnel_bytes_out": int(up * 1.4e5)},
        }

    def llama_settings(self, request: dict) -> dict:
        if request["op"] == "configure":
            self.llama_config.update({key: value for key, value in (request.get("config") or {}).items() if key in self.llama_config})
        env = {name: str(self.llama_config[setting["key"]]) for setting in LLAMA_SETTINGS for name in setting["env"]}
        groups = list(dict.fromkeys(setting["group"] for setting in LLAMA_SETTINGS))
        return {
            "schema": {"groups": groups, "settings": LLAMA_SETTINGS}, "config": self.llama_config, "env": env, "warnings": [],
            "applied_matches_saved": True, "config_path": "/data/data/com.termux/files/home/.config/agent-machine/llamacpp.json",
            "log_path": "/data/data/com.termux/files/home/.config/agent-machine/llamacpp.log",
        }

    def ollama_settings(self, request: dict, servers: dict) -> dict:
        if request["op"] == "configure":
            self.ollama_config.update({key: value for key, value in (request.get("config") or {}).items() if key in self.ollama_config})
        env = {name: str(self.ollama_config[setting["key"]]) for setting in OLLAMA_SETTINGS for name in setting["env"]}
        groups = list(dict.fromkeys(setting["group"] for setting in OLLAMA_SETTINGS))
        return {
            "schema": {"groups": groups, "settings": OLLAMA_SETTINGS}, "config": self.ollama_config, "env": env, "warnings": [],
            "applied_matches_saved": True if servers.get("ollama", {}).get("managed") else None,
            "config_path": "/home/joey/.config/agent-machine/ollama.json", "log_path": "/home/joey/.config/agent-machine/ollama.log",
        }


def search(request: dict) -> dict:
    """Made-up catalogue results, so the demo never reaches ollama.com, Hugging Face or Docker Hub."""
    catalog, query, repo = request.get("catalog"), request.get("query") or "", request.get("repo") or ""
    if catalog == "ollama":
        models = [{"name": "qwen3-coder", "description": "Qwen3's coding models, for agents.", "capabilities": ["tools"], "sizes": ["30b", "480b"], "pulls": 1_200_000},
                  {"name": "devstral", "description": "Mistral's agentic coding model.", "capabilities": ["tools"], "sizes": ["24b"], "pulls": 640_000}]
        return {"query": query, "models": [model for model in models if query.lower() in model["name"]], "note": None}
    if catalog == "huggingface":
        return {"query": query, "repos": [{"repo": f"unsloth/{query or 'Qwen3-Coder-30B-A3B-Instruct'}-GGUF", "downloads": 182_000, "likes": 410}], "note": None}
    if catalog == "huggingface-quants":
        return {"repo": repo, "quants": [{"quant": "UD-Q4_K_XL", "size": int(17.7 * GB), "split": False}, {"quant": "Q8_0", "size": int(32.5 * GB), "split": True}], "note": None}
    if catalog == "docker-hub":
        return {"query": query, "models": [{"name": "ai/qwen3-coder", "description": "Qwen3 Coder", "pulls": 210_000, "stars": 40, "updated": "2026-10-01T00:00:00Z"}], "note": None}
    return {"repo": repo, "tags": [{"tag": "latest", "size": int(17.3 * GB), "engine": None, "updated": "2026-10-01T00:00:00Z"}]}


def llama_server(request: dict, servers: dict, providers: FakeProviders) -> dict:
    """A machine's llama.cpp, as agent-machine answers: settings, install options, an install that takes a few seconds, start and stop."""
    op, llama = request["op"], servers.setdefault("llamacpp", {**LLAMA_STATE, "found": False, "running": False, "managed": False})
    if op in {"settings", "configure"}:
        return {"llamacpp_settings": providers.llama_settings(request)}
    if op == "install_options":
        return {"llamacpp_install": {"source": "termux", "variants": [
            {"id": "opencl", "label": "OpenCL (phone GPU)", "help": "Adreno and other phone GPUs, through the phone's own OpenCL driver. The fastest on Snapdragon phones.", "recommended": True},
            {"id": "vulkan", "label": "Vulkan (phone GPU)", "help": "Through Android's Vulkan driver; often slower than OpenCL on Adreno.", "recommended": False},
            {"id": "cpu", "label": "CPU only", "recommended": False}]}}
    if op == "install":
        llama["install"] = {"state": "installing", "variant": request.get("variant"), "release": None, "step": "apt-get install", "done": 0, "total": 0, "error": None, "at": None}

        def finish() -> None:
            time.sleep(4)
            llama.update(found=True, variant=request.get("variant"), source="termux")
            llama["install"] = {**llama["install"], "state": "installed", "step": None}

        threading.Thread(target=finish, daemon=True).start()
    elif op in {"start", "restart"}:
        llama.update(running=True, managed=True)
    elif op == "stop":
        llama.update(running=False, managed=False)
    return {}


def seed_subscriptions(db: Database) -> None:
    """The demo user's own AI plans (provider_accounts.py): Claude on two machines with a declared limit and
    some use, Codex on build-box, and Copilot with a stored token waiting for a machine. Written straight into
    the database before the server starts, as the server would have written them."""
    from agent_server.db import kv
    from agent_server.provider_accounts import Assignment, ProviderAccount

    now = time.time()
    hour = int(now // 3600 * 3600)
    accounts = [
        (ProviderAccount(id="demo-claude-max", tenant=DEMO_ORG, owner=DEMO_USER, provider="claude", display_name="Claude Max", plan_name="Max 5x",
                         priority=1, created_at=now - 86400 * 20, updated_at=now, last_success_at=now - 240,
                         limit={"window": "5h", "unit": "requests", "value": 225, "source": "user_declared"}), ["ai-laptop", "build-box"]),
        (ProviderAccount(id="demo-chatgpt-plus", tenant=DEMO_ORG, owner=DEMO_USER, provider="codex", display_name="ChatGPT Plus", plan_name="Plus",
                         priority=2, created_at=now - 86400 * 9, updated_at=now, last_success_at=now - 7200,
                         observed={"last_limited_at": now - 600, "retry_after": 1800, "cooldown_until": None, "exhausted_until": now + 1200}), ["build-box"]),
        (ProviderAccount(id="demo-copilot-pro", tenant=DEMO_ORG, owner=DEMO_USER, provider="copilot", display_name="Copilot Pro", plan_name="Pro",
                         authentication_mode="token", priority=3, created_at=now - 86400 * 3, updated_at=now,
                         limit={"window": "month", "unit": "requests", "value": 300, "source": "user_declared"}), []),
    ]
    # Requests through the server in the last hours (Claude), and earlier this month (Copilot, none yet).
    usage = {"demo-claude-max": [(0, 61, 1_840_000, 212_000), (1, 48, 1_402_000, 166_000), (3, 71, 2_210_000, 251_000), (30, 90, 2_900_000, 330_000)],
             "demo-chatgpt-plus": [(1, 38, 980_000, 120_000), (26, 55, 1_310_000, 160_000)]}
    with db.write() as connection:
        for account, machines in accounts:
            kv.upsert(connection, schema.provider_accounts, {"id": account.id, "tenant": account.tenant, "owner": account.owner, "provider": account.provider, "data": account.stored()})
            for machine in machines:
                assignment = Assignment(account.id, machine, DEMO_USER, now - 86400, source="enrollment")
                kv.upsert(connection, schema.provider_account_assignments, {"account_id": account.id, "machine_id": machine, "data": assignment.stored()})
        for account_id, rows in usage.items():
            for hours_ago, requests, tokens_in, tokens_out in rows:
                kv.upsert(connection, schema.provider_account_usage, {
                    "account_id": account_id, "bucket": hour - hours_ago * 3600, "requests": requests,
                    "input_tokens": tokens_in, "output_tokens": tokens_out, "rate_limited": 1 if account_id == "demo-chatgpt-plus" and hours_ago == 1 else 0, "errors": 0,
                })


def join_fleet() -> None:
    """The made-up machines, joined the way the client's agent-proxy joins: build-box takes launches, two run containers."""
    def member(hello: dict) -> None:
        socket = DemoSocket(PROXY.replace("http", "ws", 1) + "/api/machines/connect", {**hello, "protocol": PROTOCOL})
        client = api_client()
        containers = FakeContainers()
        providers = FakeProviders(hello["id"])
        servers = json.loads(json.dumps(hello.get("servers") or {"enabled": False}))
        lock = threading.Lock()

        def send(value: dict) -> None:
            with lock:
                socket.send(json.dumps(value))

        def provide(request: dict) -> None:
            send({"type": "provider_result", "request": request["request"], **providers.handle(request, send)})

        for message in socket:
            request = json.loads(message)
            if request.get("type") == "provider":
                # Pulls take a while: each request on its own thread, as agent-machine does.
                threading.Thread(target=provide, args=(request,), daemon=True).start()
                continue
            if request.get("type") == "server" and request.get("kind") == "llamacpp":
                send({"type": "server_result", "request": request["request"], "ok": True, "servers": servers, **llama_server(request, servers, providers)})
                continue
            if request.get("type") == "server" and request.get("op") in {"settings", "configure"}:
                send({"type": "server_result", "request": request["request"], "ok": True, "servers": servers,
                      "ollama_settings": providers.ollama_settings(request, servers)})
                continue
            if request.get("type") == "server":
                # Start or stop the fake Ollama or Strata profile: just flip its state.
                running = request["op"] in {"start", "restart"}
                if request.get("kind") == "ollama":
                    servers["ollama"].update(running=running, managed=running, bind="0.0.0.0" if running else None)
                for profile in servers.get("strata", {}).get("profiles", []):
                    if profile["id"] == request.get("profile"):
                        profile["state"] = "ready" if running else "stopped"
                send({"type": "server_result", "request": request["request"], "ok": True, "servers": servers})
                continue
            if request.get("type") == "container":
                send({"type": "container_result", "request": request["request"], **containers.handle(request)})
                continue
            if request.get("type") == "update":
                # As agent-machine answers before it updates itself in the background.
                send({"type": "update_result", "request": request["request"], "ok": True, "updating": True, "from": hello["version"], "to": SERVER_VERSION})
                continue
            if request.get("type") == "folders":
                send({"type": "folders_result", "request": request["request"], **fake_folders(request, hello.get("roots") or [])})
                continue
            if request.get("type") == "files":
                send({"type": "files_result", "request": request["request"], **fake_files(request, hello.get("roots") or [])})
                continue
            if request.get("type") == "shell":
                # A terminal on the machine, as `agent --shell` opens: a prompt that echoes what's typed.
                session_id = client.post(f"{PROXY}/api/sessions", json={
                    "client": "shell", "backend": "direct", "project": request["project"].rstrip("/").rsplit("/", 1)[-1],
                    "tags": {"machine": hello["id"], "machine_name": hello["name"], "launch": request["launch"], "project_path": request["project"]},
                }).json()["session_id"]
                prompt = f"joey@{hello['name']}:{request['project'].replace('/home/joey', '~')}$ "
                terminal = FakeLauncher(session_id, "bash", (110, 30), lambda text, term, prompt=prompt: term.append(f"{text}\r\n{prompt}".encode()))
                terminal.append(prompt.encode())
                send({"type": "launched", "launch": request["launch"], "session_id": session_id})
                continue
            if request.get("type") != "launch":
                continue
            # Started "remotely": a session with a made-up terminal, as the client would open.
            session_id = client.post(f"{PROXY}/api/sessions", json={
                "client": request["agent"], "model": request.get("model") or None, "backend": "direct" if request["agent"].endswith("-account") else "laptop",
                "project": request["project"].rstrip("/").rsplit("/", 1)[-1], "tags": {"machine": hello["id"], "machine_name": hello["name"], "launch": request["launch"]},
            }).json()["session_id"]
            terminal = FakeLauncher(session_id, request["agent"], (110, 30), lambda text, term: term.append(f"> {text}\r\n".encode()))
            terminal.append(f"\x1b[1m{request['agent']}\x1b[0m started on {hello['name']} in {request['project']}\r\n\r\n> ".encode())
            send({"type": "launched", "launch": request["launch"], "session_id": session_id})

    for hello in FLEET:
        threading.Thread(target=member, args=(hello,), daemon=True).start()
    # Wait until they're in, so the containers below have their machines.
    with api_client() as client:
        for _ in range(100):
            listed = client.get(f"{PROXY}/api/machines").json().get("machines", [])
            if sum(machine["connected"] for machine in listed) == len(FLEET):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("The demo machines did not join the authenticated fleet")


def run_containers() -> None:
    """Start two containers the way the console does; strata-coder stays off, waiting to switch in."""
    client = api_client(timeout=30)
    for name in ("strata-iq3", "ollama-cpu"):
        client.post(f"{PROXY}/api/containers/{name}/start", json={})


def drive() -> None:
    client = api_client(timeout=600)

    def session(agent: str, model: str, backend: str, project: str) -> str:
        return client.post(f"{PROXY}/api/sessions", json={"client": agent, "model": model, "backend": backend,
                                                           "trace": True, "project": project}).json()["session_id"]

    qwen = session("qwen", "qwen3.6:35b", "laptop", "payments-api")
    opencode = session("opencode", "qwen3.8:latest", "desktop", "web-dashboard")
    claude = session("claude", "qwen3.6:27b-coding", "desktop", "data-pipeline")
    hosted = session("claude-subscription", "sonnet", "anthropic", "payments-api")
    docker = session("opencode", "ai/qwen3-coder", "docker", "cli-tools")
    openai_turn(client, qwen, "qwen3.6:35b")
    openai_turn(client, docker, "ai/qwen3-coder")
    anthropic_turn(client, claude, "qwen3.6:27b-coding")
    anthropic_turn(client, hosted, "claude-sonnet-5")
    client.post(f"{PROXY}/session/{hosted}/anthropic/v1/messages/count_tokens", json={"model": "claude-sonnet-5", "messages": []})
    openai_turn(client, opencode, "qwen3.8:latest")
    print("demo traffic seeded; streaming continuously", flush=True)
    seeded.set()
    while not stopping.is_set():
        # Two requests overlap so the Live page always has something mid-stream.
        threads = [threading.Thread(target=openai_turn, args=(client, qwen, "qwen3.6:35b")),
                   threading.Thread(target=openai_turn, args=(client, opencode, "qwen3.8:latest"))]
        for thread in threads:
            thread.start()
            time.sleep(4)
        for thread in threads:
            thread.join()


def main() -> None:
    import argparse
    import signal

    global credentials
    parser = argparse.ArgumentParser(description="A self-contained demo server for screenshots and visual checks.")
    parser.add_argument("--capture", nargs="?", const="", metavar="NAMES",
                        help="take the screenshots (all, or these comma-separated ones) into docs/screenshots, then stop")
    parser.add_argument("--stop", action="store_true", help="stop a demo left running, and remove what killed ones left")
    args = parser.parse_args()
    if args.stop:
        sys.exit(stop())
    busy = ports_in_use()
    if busy:
        sys.exit(f"Port {busy[0]} is already in use; is another demo still running? "
                 "Stop it with: python devhelpers/scripts/screenshots/demo.py --stop")
    remove_leftovers()

    # A stopped background task or a closed terminal sends these, not Ctrl+C: clean up for them too.
    def interrupted(*_: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, interrupted)
    random.seed(7)
    work = ROOT / f".screenshot-demo-{secrets.token_hex(6)}"
    work.mkdir(mode=0o700)
    status = 0
    logs = work / "logs"
    settings = Settings(log_dir=logs, remote_key_file=work / "remote-access-key",
                        oidc_issuer=AUTH, oidc_audience=PROXY, oidc_state_dir=work / "oidc",
                        oidc_redirect_uris=[PROXY + "/api/auth/callback"], oidc_browser_origins=[PROXY],
                        oidc_introspection_secret=secrets.token_urlsafe(48))
    # The demo's own database (a SQLite file in its logs/), which the server below opens too.
    db = settings.open_database()
    users = UserStore(db)
    # The admin capture.mjs signs in as for admin-only pages (Server settings, Server stats).
    users.add_user("demo-admin", DEMO_ADMIN_PASSWORD, role="admin")  # a site admin, in the root organization
    # The demo's user, machines and sessions are one organization's (tenant's).
    users.add_org(DEMO_ORG, "Demo team")
    users.add_user(DEMO_USER, DEMO_PASSWORD, role="member", tenant=DEMO_ORG)
    for name in TEAMMATES:
        users.add_user(name, secrets.token_urlsafe(24), role="member", tenant=DEMO_ORG)
    issuer = Issuer(settings)
    try:
        with issuer.state.transaction():
            credentials = DemoCredentials(issuer.provision("screenshot-demo", DEMO_USER))
    finally:
        issuer.auth.client.close()
        # With SQLite the issuer reads accounts through a second, read-only handle on the demo's database;
        # left open, it keeps the demo's folder from being removed on Windows.
        if issuer.accounts is not issuer.state.db:
            issuer.accounts.close()
        issuer.state.db.close()
    seed_accounts(db)
    seed_history(db)
    seed_subscriptions(db)
    backends = {
        # A made-up public name, so Running agents shows how a phone pairs from anywhere.
        "proxy": {"listen_host": "127.0.0.1", "listen_port": PROXY_PORT, "url": PROXY, "public_url": "https://agents.example.net"},
        "default_backend": "laptop",
        "backends": {
            # Every local provider is on one of the FLEET machines below; only the hosted API isn't.
            "laptop": {"type": "ollama", "url": f"http://127.0.0.1:{LAPTOP}", "machine": "ai-laptop"},
            "desktop": {"type": "ollama", "url": f"http://127.0.0.1:{DESKTOP}", "machine": "ai-desktop"},
            "docker": {"type": "docker", "url": f"http://127.0.0.1:{DOCKER}", "machine": "ai-desktop"},
            # The phone's llama.cpp (FakeProviders answers for it; nothing serves agents' traffic there in the demo).
            "pocket": {"type": "llamacpp", "url": "http://127.0.0.1:9931", "machine": "pocket"},
            "anthropic": {"type": "anthropic", "url": f"http://127.0.0.1:{ANTHROPIC}"},
            # API providers (api_providers.py): made-up keys never exist here, so they show as missing.
            "openai": {"type": "api", "url": "https://api.openai.com/v1", "options": {
                "category": "hosted", "protocols": ["openai-chat", "openai-responses"], "auth": "bearer", "key_env": "AI_PROXY_API_KEY_OPENAI",
                "models": {"fast": "gpt-4.1-mini"}, "orgs": ["*"], "priority": 0}},
            "openrouter": {"type": "api", "url": "https://openrouter.ai/api/v1", "options": {
                "category": "gateway", "protocols": ["openai-chat"], "auth": "bearer", "key_env": "AI_PROXY_API_KEY_OPENROUTER",
                "headers": {"X-Title": "AI Control Mesh"}, "models": {"qwen-coder": "qwen/qwen3-coder", "sonnet": "anthropic/claude-sonnet-4.5"},
                "fallback": ["openai"], "orgs": ["*"]}},
            # Containers the machines run, answered by FakeContainers.
            **{name: {"type": spec["engine"], "url": f"http://127.0.0.1:{spec['port']}", "machine": CONTAINER_MACHINES[name], "container": spec}
               for name, spec in CONTAINERS.items()},
        },
        "intercept": {"enabled": True, "listen_host": "127.0.0.1", "listen_port": INTERCEPT_PORT},
    }
    # The shipped Ollama tuning, as a new server has it.
    config_io.apply(settings, {**backends, "models": defaults.models()})
    db.close()
    serve(ollama_app([("qwen3.6:35b", 21.1, "qwen35moe", "35.5B"), ("qwen3.6:9b", 6.1, "qwen35", "9.0B")],
                     {"qwen3.6:35b": 1.0}, delay=0.07), LAPTOP)
    serve(ollama_app([("qwen3.8:latest", 19.4, "qwen38", "32.8B"), ("qwen3.6:27b-coding", 17.2, "qwen35", "27.4B"),
                      ("devstral:24b", 14.3, "mistral3", "23.6B"), ("gpt-oss:20b", 13.8, "gptoss", "20.9B")],
                     {"qwen3.8:latest": 1.0, "qwen3.6:27b-coding": 0.82}, delay=0.05), DESKTOP)
    serve(anthropic_app(), ANTHROPIC)
    serve(model_runner_app(), DOCKER)
    env = {**{key: value for key, value in os.environ.items() if not key.startswith("AI_PROXY_")},
           "AI_PROXY_LOG_DIR": str(logs),
           "AI_PROXY_CA_DIR": str(work / "certs"), "AI_PROXY_REMOTE_KEY_FILE": str(work / "remote-access-key"),
           "AI_PROXY_OIDC_ISSUER": AUTH, "AI_PROXY_OIDC_AUDIENCE": PROXY,
           "AI_PROXY_OIDC_REDIRECT_URIS": PROXY + "/api/auth/callback", "AI_PROXY_OIDC_BROWSER_ORIGINS": PROXY,
           "AI_PROXY_OIDC_STATE_DIR": str(work / "oidc"), "AI_PROXY_OIDC_INTROSPECTION_SECRET": settings.oidc_introspection_secret,
           "AI_PROXY_AUTH_HOST": "127.0.0.1", "AI_PROXY_AUTH_PORT": str(AUTH_PORT)}
    children = []
    try:
        children.append(subprocess.Popen([sys.executable, "-m", "agent_server.issuer"], env=env, cwd=ROOT))
        children.append(subprocess.Popen([sys.executable, "-m", "agent_server.app", "--port", str(PROXY_PORT), "--no-build"], env=env, cwd=ROOT))
        with httpx.Client(trust_env=False, timeout=0.5) as client:
            for url in (AUTH + "/.well-known/openid-configuration", PROXY + "/health"):
                for _ in range(100):
                    if any(child.poll() is not None for child in children):
                        raise RuntimeError("A demo service exited during startup")
                    try:
                        if client.get(url).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.2)
                else:
                    raise RuntimeError(f"Demo service did not become ready: {url}")
        STATE.write_text(json.dumps({"pid": os.getpid(), "children": [child.pid for child in children], "work": str(work)}))
        credentials.headers()
        print(f"demo proxy on {PROXY} (data in {work})", flush=True)
        print(f"isolated demo sign-in: {DEMO_USER} / {DEMO_PASSWORD}, admin demo-admin / {DEMO_ADMIN_PASSWORD}; issuer {AUTH}", flush=True)
        # Machines first: their model servers are backends only once their machine (and so its organization) is known.
        join_fleet()
        share_terminals()
        run_containers()
        if args.capture is None:
            drive()
        else:
            traffic = threading.Thread(target=drive, daemon=True)
            traffic.start()
            status = capture(args.capture)
            stopping.set()
            traffic.join(timeout=30)
    except KeyboardInterrupt:
        pass
    finally:
        stopping.set()
        STATE.unlink(missing_ok=True)
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        # Windows can hold the stopped server's SQLite file for a moment after it exits.
        for _ in range(25):
            shutil.rmtree(work, ignore_errors=True)
            if not work.exists():
                break
            time.sleep(0.2)
        else:
            print(f"couldn't remove {work}; the next demo removes it", file=sys.stderr)
    sys.exit(status)


if __name__ == "__main__":
    main()
