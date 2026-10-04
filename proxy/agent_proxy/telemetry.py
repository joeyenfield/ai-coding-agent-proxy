from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from .sessions import utc_now


@dataclass
class RequestTelemetry:
    session_id: str
    request_id: str
    client: str
    model: str
    backend: str
    endpoint: str
    request_bytes: int
    context_size: int | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    timestamp: str = field(default_factory=utc_now)
    started: float = field(default_factory=time.perf_counter)
    first_token: float | None = None
    response_bytes: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    prompt_eval_ms: float | None = None
    generation_ms: float | None = None
    status: int = 200
    error_type: str | None = None
    # Ollama reports token counts only in a stream's final chunk. If the stream
    # stops early these let finish() estimate the counts instead of reporting 0.
    streaming: bool = False
    completed: bool = False
    streamed_chunks: int = 0
    estimated_input_tokens: int | None = None

    def saw_content(self) -> None:
        if self.first_token is None:
            self.first_token = time.perf_counter()

    def apply_ollama_stats(self, obj: dict[str, Any]) -> None:
        if (
            obj.get("done") is True
            or obj.get("type") in {"response.completed", "message_stop"}
            or obj.get("usage")
            or any(isinstance(choice, dict) and choice.get("finish_reason") for choice in obj.get("choices") or [])
        ):
            self.completed = True
        self.input_tokens = int(obj.get("prompt_eval_count") or self.input_tokens or 0)
        self.output_tokens = int(obj.get("eval_count") or self.output_tokens or 0)
        if obj.get("prompt_eval_duration") is not None:
            self.prompt_eval_ms = obj["prompt_eval_duration"] / 1_000_000
        if obj.get("eval_duration") is not None:
            self.generation_ms = obj["eval_duration"] / 1_000_000
        usage = obj.get("usage") or (obj.get("response") or {}).get("usage") or (obj.get("message") or {}).get("usage") or {}
        self.input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or self.input_tokens)
        self.output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or self.output_tokens)

    def finish(self) -> dict[str, Any]:
        total_ms = (time.perf_counter() - self.started) * 1000
        ttft_ms = ((self.first_token - self.started) * 1000) if self.first_token else None
        if self.streaming and not self.completed and self.error_type is None:
            self.error_type = "incomplete_stream"
        estimated = False
        if self.output_tokens == 0 and self.streamed_chunks:
            # Ollama streams roughly one token per chunk.
            self.output_tokens = self.streamed_chunks
            estimated = True
        if self.input_tokens == 0 and self.estimated_input_tokens and (estimated or not self.completed):
            self.input_tokens = self.estimated_input_tokens
            estimated = True
        # Backend timings win. OpenAI-compatible Ollama responses sometimes only
        # include counts, so retain useful wall-clock rates as a fallback.
        prompt_ms = self.prompt_eval_ms if self.prompt_eval_ms is not None else ttft_ms
        generation_ms = self.generation_ms
        if generation_ms is None and ttft_ms is not None:
            generation_ms = max(total_ms - ttft_ms, 0)
        prompt_tps = _rate(self.input_tokens, prompt_ms)
        generation_tps = _rate(self.output_tokens, generation_ms)
        return {
            "request_id": self.request_id,
            "timestamp": self.timestamp,
            "session_id": self.session_id,
            "client": self.client,
            "model": self.model,
            "backend": self.backend,
            "endpoint": self.endpoint,
            "request_bytes": self.request_bytes,
            "response_bytes": self.response_bytes,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tokens_estimated": estimated,
            "ttft_ms": _round(ttft_ms),
            "prompt_eval_ms": _round(prompt_ms),
            "generation_ms": _round(generation_ms),
            "total_ms": _round(total_ms),
            "prompt_tps": _round(prompt_tps),
            "generation_tps": _round(generation_tps),
            "status": self.status,
            "error_type": self.error_type,
            "context_size": self.context_size,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
        }


def parse_json_line(line: bytes) -> dict[str, Any] | None:
    line = line.strip()
    if line.startswith(b"data:"):
        line = line[5:].strip()
    if not line or line == b"[DONE]":
        return None
    try:
        value = json.loads(line)
        return value if isinstance(value, dict) else None
    except (ValueError, UnicodeDecodeError):
        return None


RESPONSES_DELTAS = {
    "response.output_text.delta": "content",
    "response.reasoning_text.delta": "reasoning",
    "response.reasoning_summary_text.delta": "reasoning",
    "response.function_call_arguments.delta": "tool",
}


def stream_deltas(obj: dict[str, Any]) -> list[tuple[str, str]]:
    """Return (kind, text) pieces from one streamed object, kind being reasoning, content or tool.

    Understands native Ollama chat/generate chunks, OpenAI chat completion
    chunks and OpenAI Responses events, plus complete (non-streamed) Ollama,
    OpenAI and Anthropic responses.
    """
    deltas: list[tuple[str, str]] = []
    if obj.get("type") == "content_block_delta":
        delta = obj.get("delta") or {}
        _add(deltas, "content", delta.get("text"))
        _add(deltas, "reasoning", delta.get("thinking"))
        _add(deltas, "tool", delta.get("partial_json"))
        return deltas
    if obj.get("type") == "content_block_start":
        block = obj.get("content_block") or {}
        if block.get("type") == "tool_use":
            _add(deltas, "tool", f"{block.get('name', '')}(")
        _add(deltas, "content", block.get("text"))
        _add(deltas, "reasoning", block.get("thinking"))
        return deltas
    kind = RESPONSES_DELTAS.get(str(obj.get("type", "")))
    if kind:
        if obj.get("delta"):
            deltas.append((kind, str(obj["delta"])))
        return deltas
    message = obj.get("message")
    if isinstance(message, dict):
        _add(deltas, "reasoning", message.get("thinking"))
        _add(deltas, "content", message.get("content"))
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            arguments = function.get("arguments", {})
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            deltas.append(("tool", f"{function.get('name', '')}({arguments})\n"))
    elif "response" in obj and isinstance(obj.get("response"), str):
        _add(deltas, "reasoning", obj.get("thinking"))
        _add(deltas, "content", obj.get("response"))
    if obj.get("type") == "message" and isinstance(obj.get("content"), list):
        # A complete Anthropic Messages response.
        for block in obj["content"]:
            if block.get("type") == "text":
                _add(deltas, "content", block.get("text"))
            elif block.get("type") == "tool_use":
                deltas.append(("tool", f"{block.get('name', '')}({json.dumps(block.get('input', {}), ensure_ascii=False)})\n"))
    for choice in obj.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        # Streamed chunks carry "delta"; complete OpenAI responses carry "message".
        delta = choice.get("delta") or choice.get("message") or {}
        _add(deltas, "reasoning", delta.get("reasoning_content") or delta.get("reasoning"))
        _add(deltas, "content", delta.get("content") or choice.get("text"))
        for call in delta.get("tool_calls") or []:
            function = call.get("function") or {}
            _add(deltas, "tool", (f"{function['name']}(" if function.get("name") else "") + (function.get("arguments") or ""))
    return deltas


def has_content(obj: dict[str, Any]) -> bool:
    return bool(stream_deltas(obj))


def _add(deltas: list[tuple[str, str]], kind: str, value: Any) -> None:
    if isinstance(value, str) and value:
        deltas.append((kind, value))


def _rate(tokens: int, duration_ms: float | None) -> float | None:
    return tokens * 1000 / duration_ms if duration_ms and duration_ms > 0 else None


def _round(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None
