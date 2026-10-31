"""Run a self-contained demo proxy for README screenshots.

Starts fake Ollama machines and a fake Anthropic API, seeds account-agent
sessions (Claude Code, Copilot CLI, Codex) shaped like real captured traffic
but with made-up content, then starts agent-proxy against them, shares three
made-up agent terminals with it and keeps real traffic streaming through it.
Nothing touches your real logs, config, accounts or access key.
The isolated issuer listens on :18200. Sign in as demo / demo-screenshot-password.

    python scripts/screenshots/demo.py                # proxy on http://127.0.0.1:8199
    node scripts/screenshots/capture.mjs http://127.0.0.1:8199 docs/screenshots

Stop it with Ctrl+C.
"""

from __future__ import annotations

import asyncio
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
from agent_server import config_io, defaults
from agent_server.config import Settings
from agent_server.db import Database
from agent_server.issuer import Issuer
from agent_server.version import PROTOCOL, build_version
from agent_server.telemetry import RequestTelemetry

ROOT = Path(__file__).resolve().parents[2]
PROXY_PORT = 8199
INTERCEPT_PORT = 18199
AUTH_PORT = 18200
LAPTOP, DESKTOP, ANTHROPIC, DOCKER = 18434, 18435, 18436, 18437
PROXY = f"http://127.0.0.1:{PROXY_PORT}"
AUTH = f"http://127.0.0.1:{AUTH_PORT}"
DEMO_USER, DEMO_PASSWORD = "demo", "demo-screenshot-password"
DEMO_ADMIN_PASSWORD = "demo-admin-screenshot-password"
GB = 1024**3


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
            if self.closed:
                return
            try:
                self.socket.send(data)
            except (ConnectionClosed, OSError):
                self._connect()
                self.socket.send(data)

    def close(self):
        with self.lock:
            self.closed = True
            self.socket.close()

    def __iter__(self):
        from websockets.exceptions import ConnectionClosed

        while not self.closed:
            try:
                if time.monotonic() >= self.renew:
                    with self.lock:
                        self._connect()
                yield self.socket.recv(timeout=1)
            except TimeoutError:
                continue
            except (ConnectionClosed, OSError):
                if not self.closed:
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

        async def stream():
            words = (REASONING if think else "").split(" ")
            await asyncio.sleep(delay * 12)  # prompt evaluation
            count = 0
            for word in words:
                if word:
                    count += 1
                    yield json.dumps({"model": model, "message": {"role": "assistant", "content": "", "thinking": word + " "}, "done": False}) + "\n"
                    await asyncio.sleep(delay)
            for word in ANSWER.split(" "):
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
                                     "tags": {"project_path": f"/home/dev/{project}", "owner": DEMO_USER,
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
                   b"Reading src/charts/Usage.tsx\r\nReading src/pages/Dashboard.tsx\r\n\r\n")

    def spin() -> None:
        frames, seconds = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", 0
        while True:
            working.append(f"\r\x1b[K\x1b[33m{frames[seconds % len(frames)]}\x1b[0m Thinking… ({seconds}s)".encode())
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
]
RUNS_CONTAINERS = {"enabled": True, "cli": "docker", "found": True, "online": True, "version": "28.4.0", "error": "", "engines": ENGINES}

# The demo server's own version (VERSION and the commit), so the model machines are up to date.
SERVER_VERSION = build_version()

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
     "containers": RUNS_CONTAINERS, "launches": False, "roots": []},
    {"id": "build-box", "name": "build-box", "os": "linux", "arch": "amd64", "version": "0.3.2", "address": "192.168.1.40",
     "agents": ["claude-account", "codex-account", "qwen", "opencode"], "providers": [{"type": "ollama", "port": 11434, "models": 6, "version": "0.12.3"}],
     "containers": {"enabled": False, "cli": "docker", "found": True, "online": False, "version": "", "error": "", "engines": []},
     "servers": {"enabled": True, "ollama": {"found": True, "running": True, "managed": True, "version": "0.12.3", "bind": "0.0.0.0"},
                 "strata": {"found": False, "profiles": []}},
     "launches": True, "roots": ["/home/joey/code", "/srv/repos"], "commands": True, "updates": True,
     # MCP servers it shares (agent-machine mcp add); nothing listens on the bridge's port in the demo.
     "mcp": {"port": 18198, "key": "demo", "servers": [
         {"name": "github", "transport": "stdio", "description": "GitHub issues and pull requests", "found": True},
         {"name": "postgres", "transport": "stdio", "description": "The staging database, read-only", "found": True},
         {"name": "docs", "transport": "http", "description": "Internal docs search", "found": True},
     ]}},
]

# build-box's folders inside its launch folders, for the folder picker (agent-machine lists them there).
FOLDERS = {
    "/home/joey/code": ["api", "ai-coding-agent-proxy", "dotfiles", "web"],
    "/home/joey/code/api": ["cmd", "internal", "migrations"],
    "/home/joey/code/api/internal": ["auth", "billing", "store"],
    "/home/joey/code/ai-coding-agent-proxy": ["client", "machine", "python", "server", "ui"],
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
LOGS = {
    "strata": "[strata] serving qwen3.6-35b-a3b IQ3_XXS on :8080\n[strata] done: 812 tokens in 9.4 s\n",
    "ollama": "time=2026-10-26T09:00:00Z level=INFO msg=\"Listening on 0.0.0.0:11434\"\n",
}


# The fake model servers' ports: the only ones FakeProviders asks over HTTP.
DEMO_PORTS = {LAPTOP, DESKTOP, DOCKER}


class FakeProviders:
    """A machine's model servers, answered the way agent-machine does: it asks the demo's fake servers on loopback."""

    def __init__(self, machine_id: str):
        self.machine_id = machine_id
        self.ollama_config: dict[str, Any] = {setting["key"]: setting["default"] for setting in OLLAMA_SETTINGS} | {"extra_env": ""}

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
            return {"ok": True}
        if op in {"delete", "load", "unload"}:
            if kind == "strata":
                return {"ok": True, "result": {"status": "loaded" if op == "load" else "unloaded"}}
            return {"ok": True, "result": {{"delete": "deleted", "load": "loaded", "unload": "unloaded"}[op]: request.get("model")}}
        if op == "log":
            return {"ok": True, "text": LOGS.get(kind, "")}
        return {"ok": False, "error": f"unknown request: {op} {kind}"}

    def status(self, kind: str, base: str, request: dict) -> dict:
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
        machine, wave = HARDWARE[self.machine_id], (time.time() % 20) / 20
        gpu_total = machine["gpu_gb"] * GB
        return {
            "hostname": self.machine_id, "platform": "Linux 6.14.0", "timestamp": time.time(),
            "cpu": {"percent": round(18 + 14 * wave + random.random() * 6, 1), "count": machine["cores"]},
            "memory": {"used": int(machine["ram_gb"] * 0.42 * GB), "total": int(machine["ram_gb"] * GB), "percent": 42.0},
            "gpus": [{"index": 0, "name": machine["gpu"], "utilization": round(72 + 20 * wave + random.random() * 6, 1),
                      "memory_used": int(gpu_total * 0.93), "memory_total": int(gpu_total),
                      "temperature": 67 + int(6 * wave), "power_watts": round(280 + 60 * wave, 1)}],
            "gpu_source": "nvidia-smi", "gpu_error": None,
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
    while True:
        # Two requests overlap so the Live page always has something mid-stream.
        threads = [threading.Thread(target=openai_turn, args=(client, qwen, "qwen3.6:35b")),
                   threading.Thread(target=openai_turn, args=(client, opencode, "qwen3.8:latest"))]
        for thread in threads:
            thread.start()
            time.sleep(4)
        for thread in threads:
            thread.join()


def main() -> None:
    import socket

    global credentials
    for port in (PROXY_PORT, AUTH_PORT, INTERCEPT_PORT, LAPTOP, DESKTOP, ANTHROPIC, DOCKER):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                sys.exit(f"Port {port} is already in use; is another demo still running?")
    random.seed(7)
    work = ROOT / f".screenshot-demo-{secrets.token_hex(6)}"
    work.mkdir(mode=0o700)
    logs = work / "logs"
    settings = Settings(log_dir=logs, remote_key_file=work / "remote-access-key",
                        oidc_issuer=AUTH, oidc_audience=PROXY, oidc_state_dir=work / "oidc",
                        oidc_redirect_uris=[PROXY + "/api/auth/callback"], oidc_browser_origins=[PROXY],
                        oidc_introspection_secret=secrets.token_urlsafe(48))
    # The demo's own database (a SQLite file in its logs/), which the server below opens too.
    db = settings.open_database()
    users = UserStore(db)
    # The admin capture.mjs signs in as for admin-only pages (Server settings, Server stats).
    users.add_user("demo-admin", DEMO_ADMIN_PASSWORD, role="admin")
    users.add_user(DEMO_USER, DEMO_PASSWORD, role="member")
    issuer = Issuer(settings)
    try:
        with issuer.state.transaction():
            credentials = DemoCredentials(issuer.provision("screenshot-demo", DEMO_USER))
    finally:
        issuer.auth.client.close()
        issuer.state.db.close()
    seed_accounts(db)
    backends = {
        # A made-up public name, so Running agents shows how a phone pairs from anywhere.
        "proxy": {"listen_host": "127.0.0.1", "listen_port": PROXY_PORT, "url": PROXY, "public_url": "https://agents.example.net"},
        "default_backend": "laptop",
        "backends": {
            # Every local provider is on one of the FLEET machines below; only the hosted API isn't.
            "laptop": {"type": "ollama", "url": f"http://127.0.0.1:{LAPTOP}", "machine": "ai-laptop"},
            "desktop": {"type": "ollama", "url": f"http://127.0.0.1:{DESKTOP}", "machine": "ai-desktop"},
            "docker": {"type": "docker", "url": f"http://127.0.0.1:{DOCKER}", "machine": "ai-desktop"},
            "anthropic": {"type": "anthropic", "url": f"http://127.0.0.1:{ANTHROPIC}"},
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
        credentials.headers()
        print(f"demo proxy on {PROXY} (data in {work})", flush=True)
        print(f"isolated demo sign-in: {DEMO_USER} / {DEMO_PASSWORD}, admin demo-admin / {DEMO_ADMIN_PASSWORD}; issuer {AUTH}", flush=True)
        share_terminals()
        join_fleet()
        run_containers()
        drive()
    except KeyboardInterrupt:
        pass
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
