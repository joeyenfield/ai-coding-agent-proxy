"""Run a self-contained demo proxy for README screenshots.

Starts fake Ollama machines and a fake Anthropic API, seeds account-agent
sessions (Claude Code, Copilot CLI, Codex) shaped like real captured traffic
but with made-up content, then starts agent-proxy against them and keeps real
traffic streaming through it. Nothing touches your real logs, config or
accounts.

    python scripts/screenshots/demo.py                # proxy on http://127.0.0.1:8199
    node scripts/screenshots/capture.mjs http://127.0.0.1:8199 docs/screenshots

Stop it with Ctrl+C.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import uvicorn
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from agent_proxy.sessions import SessionStore
from agent_proxy.telemetry import RequestTelemetry

ROOT = Path(__file__).resolve().parents[2]
PROXY_PORT = 8199
INTERCEPT_PORT = 18199
LAPTOP, DESKTOP, ANTHROPIC = 18434, 18435, 18436
PROXY = f"http://127.0.0.1:{PROXY_PORT}"
GB = 1024**3

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


def ollama_app(models: list[tuple[str, float, str, str]], loaded: dict[str, float], delay: float, machine: dict) -> FastAPI:
    app = FastAPI()

    @app.get("/api/host/metrics")
    async def metrics():
        # Stands in for agent-metrics on the machine, so screenshots don't show the real host.
        wave = (time.time() % 20) / 20
        gpu_total = machine["gpu_gb"] * GB
        return {
            "hostname": machine["hostname"], "platform": "Linux 6.14.0", "timestamp": time.time(),
            "cpu": {"percent": round(18 + 14 * wave + random.random() * 6, 1), "count": machine["cores"]},
            "memory": {"used": int(machine["ram_gb"] * 0.42 * GB), "total": int(machine["ram_gb"] * GB), "percent": 42.0},
            "gpus": [{"index": 0, "name": machine["gpu"], "utilization": round(72 + 20 * wave + random.random() * 6, 1),
                      "memory_used": int(gpu_total * 0.93), "memory_total": int(gpu_total),
                      "temperature": 67 + int(6 * wave), "power_watts": round(280 + 60 * wave, 1)}],
            "gpu_error": None,
        }

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


def serve(app: FastAPI, port: int) -> None:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()


# --------------------------------------------------------------------------- seeded account traffic

def iso(when: datetime) -> str:
    return when.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Seeder:
    def __init__(self, log_dir: Path):
        self.store = SessionStore(log_dir)
        self.clock = datetime.now(timezone.utc) - timedelta(minutes=50)

    def session(self, client: str, project: str, minutes_ago: float, ended: bool = True):
        session = self.store.create({"client": client, "backend": "direct", "project": project, "trace": True,
                                     "tags": {"project_path": f"/home/dev/{project}"}})
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


def seed_accounts(log_dir: Path) -> None:
    seed = Seeder(log_dir)
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
    seed.add("POST", "api.anthropic.com", "/v1/messages", ms=6400, ttft=1240, model="claude-opus-5-5", sent=46000, received=9800,
             usage=(4, 214, 18234, 512), headers=claude_headers,
             response_headers=[["content-type", "text/event-stream"], ["request-id", "req_011DemoRequest"], ["anthropic-ratelimit-unified-status", "allowed"]],
             request={"model": "claude-opus-5-5", "max_tokens": 32000, "stream": True,
                      "system": [{"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."}],
                      "tools": [{"name": name, "input_schema": {"type": "object"}} for name in CLAUDE_TOOLS],
                      "messages": [{"role": "user", "content": [{"type": "text", "text": PROMPT}]}]},
             response=anthropic_events("claude-opus-5-5"))
    seed.add("POST", "api.anthropic.com", "/v1/messages", ms=4100, ttft=980, model="claude-opus-5-5", sent=52000, received=7200,
             usage=(3, 388, 18746, 2210), headers=claude_headers,
             request={"model": "claude-opus-5-5", "max_tokens": 32000, "stream": True, "messages": [{"role": "user", "content": PROMPT}]},
             response=anthropic_events("claude-opus-5-5"))
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


def drive() -> None:
    client = httpx.Client(timeout=600)

    def session(agent: str, model: str, backend: str, project: str) -> str:
        return client.post(f"{PROXY}/api/sessions", json={"client": agent, "model": model, "backend": backend,
                                                           "trace": True, "project": project}).json()["session_id"]

    qwen = session("qwen", "qwen3.6:35b", "laptop", "payments-api")
    opencode = session("opencode", "qwen3.8:latest", "desktop", "web-dashboard")
    claude = session("claude", "qwen3.6:27b-coding", "desktop", "data-pipeline")
    hosted = session("claude-subscription", "sonnet", "anthropic", "payments-api")
    openai_turn(client, qwen, "qwen3.6:35b")
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

    for port in (PROXY_PORT, INTERCEPT_PORT, LAPTOP, DESKTOP, ANTHROPIC):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                sys.exit(f"Port {port} is already in use; is another demo still running?")
    random.seed(7)
    work = Path(tempfile.mkdtemp(prefix="agent-proxy-demo-"))
    logs = work / "logs"
    seed_accounts(logs)
    backends = {
        "proxy": {"listen_host": "127.0.0.1", "listen_port": PROXY_PORT, "url": PROXY},
        "default_backend": "laptop",
        "backends": {
            "laptop": {"type": "ollama", "url": f"http://127.0.0.1:{LAPTOP}", "metrics_url": f"http://127.0.0.1:{LAPTOP}"},
            "desktop": {"type": "ollama", "url": f"http://127.0.0.1:{DESKTOP}", "metrics_url": f"http://127.0.0.1:{DESKTOP}"},
            "anthropic": {"type": "anthropic", "url": f"http://127.0.0.1:{ANTHROPIC}"},
        },
        "intercept": {"enabled": True, "listen_host": "127.0.0.1", "listen_port": INTERCEPT_PORT},
    }
    (work / "backends.yaml").write_text(yaml.safe_dump(backends, sort_keys=False))
    serve(ollama_app([("qwen3.6:35b", 21.1, "qwen35moe", "35.5B"), ("qwen3.6:9b", 6.1, "qwen35", "9.0B")],
                     {"qwen3.6:35b": 1.0}, delay=0.07,
                     machine={"hostname": "ai-laptop", "cores": 16, "ram_gb": 64, "gpu": "NVIDIA RTX 5000 Ada Laptop", "gpu_gb": 32}), LAPTOP)
    serve(ollama_app([("qwen3.8:latest", 19.4, "qwen38", "32.8B"), ("qwen3.6:27b-coding", 17.2, "qwen35", "27.4B"),
                      ("devstral:24b", 14.3, "mistral3", "23.6B"), ("gpt-oss:20b", 13.8, "gptoss", "20.9B")],
                     {"qwen3.8:latest": 1.0, "qwen3.6:27b-coding": 0.82}, delay=0.05,
                     machine={"hostname": "ai-desktop", "cores": 24, "ram_gb": 128, "gpu": "NVIDIA RTX 5090", "gpu_gb": 32}), DESKTOP)
    serve(anthropic_app(), ANTHROPIC)
    env = {**os.environ, "AI_PROXY_BACKENDS": str(work / "backends.yaml"), "AI_PROXY_LOG_DIR": str(logs),
           "AI_PROXY_CA_DIR": str(work / "certs")}
    proxy = subprocess.Popen([sys.executable, "-m", "agent_proxy.app", "--port", str(PROXY_PORT), "--no-build"], env=env, cwd=ROOT)
    try:
        for _ in range(50):
            try:
                if httpx.get(f"{PROXY}/health", timeout=0.5).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        print(f"demo proxy on {PROXY} (data in {work})", flush=True)
        drive()
    except KeyboardInterrupt:
        pass
    finally:
        proxy.terminate()
        proxy.wait(timeout=10)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
