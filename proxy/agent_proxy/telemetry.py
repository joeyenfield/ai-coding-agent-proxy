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

    def saw_content(self) -> None:
        if self.first_token is None:
            self.first_token = time.perf_counter()

    def apply_ollama_stats(self, obj: dict[str, Any]) -> None:
        self.input_tokens = int(obj.get("prompt_eval_count") or self.input_tokens or 0)
        self.output_tokens = int(obj.get("eval_count") or self.output_tokens or 0)
        if obj.get("prompt_eval_duration") is not None:
            self.prompt_eval_ms = obj["prompt_eval_duration"] / 1_000_000
        if obj.get("eval_duration") is not None:
            self.generation_ms = obj["eval_duration"] / 1_000_000
        usage = obj.get("usage") or (obj.get("response") or {}).get("usage") or {}
        self.input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or self.input_tokens)
        self.output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or self.output_tokens)

    def finish(self) -> dict[str, Any]:
        total_ms = (time.perf_counter() - self.started) * 1000
        ttft_ms = ((self.first_token - self.started) * 1000) if self.first_token else None
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


def has_content(obj: dict[str, Any]) -> bool:
    if obj.get("type") in {"response.output_text.delta", "response.reasoning_summary_text.delta"} and obj.get("delta"):
        return True
    if obj.get("message", {}).get("content"):
        return True
    choices = obj.get("choices") or []
    return any(
        choice.get("delta", {}).get("content") or choice.get("text")
        for choice in choices
        if isinstance(choice, dict)
    )


def _rate(tokens: int, duration_ms: float | None) -> float | None:
    return tokens * 1000 / duration_ms if duration_ms and duration_ms > 0 else None


def _round(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None
