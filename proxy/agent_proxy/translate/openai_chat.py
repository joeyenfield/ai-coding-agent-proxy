"""Translate OpenAI Chat Completions to native Ollama /api/chat and back.

Ollama's own OpenAI-compatible endpoint ignores per-request settings such as
num_ctx, so routing through /api/chat lets model profiles control context size,
sampling, keep_alive and thinking for agents that only speak the OpenAI API.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from ..config import ModelProfile
from .ollama import apply_profile


def openai_to_ollama(payload: dict[str, Any], profile: ModelProfile) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    tool_names: dict[str, str] = {}
    for message in payload.get("messages") or []:
        role = message.get("role", "user")
        if role == "developer":
            role = "system"
        text, images = _content(message.get("content"))
        converted: dict[str, Any] = {"role": role, "content": text}
        if images:
            converted["images"] = images
        if role == "assistant" and message.get("tool_calls"):
            calls = []
            for call in message["tool_calls"]:
                function = call.get("function") or {}
                tool_names[call.get("id", "")] = function.get("name", "")
                calls.append({"function": {
                    "name": function.get("name", ""),
                    "arguments": _arguments(function.get("arguments")),
                }})
            converted["tool_calls"] = calls
        if role == "tool":
            name = message.get("name") or tool_names.get(message.get("tool_call_id", ""))
            if name:
                converted["tool_name"] = name
        messages.append(converted)

    options: dict[str, Any] = {}
    for source, target in (
        ("temperature", "temperature"), ("top_p", "top_p"), ("seed", "seed"),
        ("frequency_penalty", "frequency_penalty"), ("presence_penalty", "presence_penalty"),
        ("top_k", "top_k"), ("min_p", "min_p"),
    ):
        if payload.get(source) is not None:
            options[target] = payload[source]
    max_tokens = payload.get("max_completion_tokens") or payload.get("max_tokens")
    if max_tokens:
        options["num_predict"] = max_tokens
    stop = payload.get("stop")
    if stop:
        options["stop"] = [stop] if isinstance(stop, str) else stop

    result: dict[str, Any] = {
        "model": payload.get("model", ""),
        "messages": messages,
        "stream": bool(payload.get("stream", False)),
    }
    if options:
        result["options"] = options
    tools = [tool for tool in payload.get("tools") or [] if tool.get("type", "function") == "function"]
    if tools:
        result["tools"] = tools
    response_format = payload.get("response_format") or {}
    if response_format.get("type") == "json_object":
        result["format"] = "json"
    elif response_format.get("type") == "json_schema":
        schema = (response_format.get("json_schema") or {}).get("schema")
        if schema:
            result["format"] = schema
    effort = payload.get("reasoning_effort")
    if effort is not None:
        result["think"] = effort not in {"none", "minimal"}
    return apply_profile(result, profile)


def ollama_to_openai(data: dict[str, Any], model: str) -> dict[str, Any]:
    message = data.get("message") or {}
    calls = [_tool_call(call, index) for index, call in enumerate(message.get("tool_calls") or [])]
    reply: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ("" if not calls else None)}
    if message.get("thinking"):
        reply["reasoning_content"] = message["thinking"]
    if calls:
        reply["tool_calls"] = [{key: value for key, value in call.items() if key != "index"} for call in calls]
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": reply, "finish_reason": _finish_reason(data, bool(calls))}],
        "usage": _usage(data),
    }


class OpenAIStreamTranslator:
    """Convert native Ollama stream objects into OpenAI chat.completion.chunk SSE events."""

    def __init__(self, model: str, include_usage: bool):
        self.id = f"chatcmpl-{uuid.uuid4().hex}"
        self.created = int(time.time())
        self.model = model
        self.include_usage = include_usage
        self.sent_role = False
        self.tool_index = 0
        self.finished = False

    def start(self) -> list[bytes]:
        return []

    def ping(self) -> bytes:
        # SSE comment: ignored by clients, but keeps idle connections open.
        return b": keep-alive\n\n"

    def finish(self) -> list[bytes]:
        if self.finished:
            return []
        self.finished = True
        return [self._chunk({}, "stop"), b"data: [DONE]\n\n"]

    def feed(self, obj: dict[str, Any]) -> list[bytes]:
        events: list[bytes] = []
        if obj.get("error"):
            events.append(_sse({"error": {"message": str(obj["error"]), "type": "backend_error"}}))
            return events
        message = obj.get("message") or {}
        delta: dict[str, Any] = {}
        if not self.sent_role:
            delta["role"] = "assistant"
            self.sent_role = True
        if message.get("thinking"):
            delta["reasoning_content"] = message["thinking"]
        if message.get("content"):
            delta["content"] = message["content"]
        if message.get("tool_calls"):
            delta["tool_calls"] = []
            for call in message["tool_calls"]:
                delta["tool_calls"].append(_tool_call(call, self.tool_index))
                self.tool_index += 1
        if set(delta) - {"role"} or (delta and not obj.get("done")):
            events.append(self._chunk(delta, None))
        if obj.get("done"):
            self.finished = True
            events.append(self._chunk({}, _finish_reason(obj, self.tool_index > 0)))
            if self.include_usage:
                events.append(_sse({**self._base(), "choices": [], "usage": _usage(obj)}))
            events.append(b"data: [DONE]\n\n")
        return events

    def _base(self) -> dict[str, Any]:
        return {"id": self.id, "object": "chat.completion.chunk", "created": self.created, "model": self.model}

    def _chunk(self, delta: dict[str, Any], finish_reason: str | None) -> bytes:
        return _sse({**self._base(), "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]})


def _content(content: Any) -> tuple[str, list[str]]:
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    text: list[str] = []
    images: list[str] = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        if part.get("type") in {"text", "input_text"}:
            text.append(part.get("text", ""))
        elif part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if url.startswith("data:") and "," in url:
                images.append(url.split(",", 1)[1])
    return "".join(text), images


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except (TypeError, ValueError):
        return {"value": value}


def _tool_call(call: dict[str, Any], index: int) -> dict[str, Any]:
    function = call.get("function") or {}
    arguments = function.get("arguments", {})
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, separators=(",", ":"))
    return {
        "index": index,
        "id": call.get("id") or f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {"name": function.get("name", ""), "arguments": arguments},
    }


def _finish_reason(data: dict[str, Any], used_tool: bool) -> str:
    if used_tool:
        return "tool_calls"
    return "length" if data.get("done_reason") == "length" else "stop"


def _usage(data: dict[str, Any]) -> dict[str, int]:
    prompt = int(data.get("prompt_eval_count") or 0)
    completion = int(data.get("eval_count") or 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def _sse(data: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(data, separators=(',', ':'), ensure_ascii=False)}\n\n".encode()
