from __future__ import annotations

import json
import uuid
from typing import Any

from ..config import ModelProfile
from .ollama import apply_profile


def anthropic_to_ollama(payload: dict[str, Any], profile: ModelProfile) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    system = payload.get("system")
    if system:
        if isinstance(system, list):
            system = "".join(part.get("text", "") for part in system if isinstance(part, dict))
        messages.append({"role": "system", "content": system})
    tool_names: dict[str, str] = {}
    for message in payload.get("messages", []):
        content = message.get("content", "")
        if not isinstance(content, list):
            messages.append({"role": message.get("role", "user"), "content": content})
            continue
        text = "".join(part.get("text", "") for part in content if part.get("type") == "text")
        images = [
            part["source"]["data"] for part in content
            if part.get("type") == "image" and (part.get("source") or {}).get("type") == "base64"
        ]
        tool_uses = [part for part in content if part.get("type") == "tool_use"]
        tool_results = [part for part in content if part.get("type") == "tool_result"]
        # Tool results must directly follow the assistant turn that requested them.
        for part in tool_results:
            result_content = part.get("content", "")
            if isinstance(result_content, list):
                result_content = "".join(
                    block.get("text", "") for block in result_content if block.get("type") == "text"
                )
            result: dict[str, Any] = {"role": "tool", "content": result_content}
            name = tool_names.get(part.get("tool_use_id", ""))
            if name:
                result["tool_name"] = name
            messages.append(result)
        if text or tool_uses or images:
            converted: dict[str, Any] = {"role": message.get("role", "user"), "content": text}
            if images:
                converted["images"] = images
            if tool_uses:
                converted["tool_calls"] = [
                    {"function": {"name": part.get("name", ""), "arguments": part.get("input", {})}}
                    for part in tool_uses
                ]
                for part in tool_uses:
                    tool_names[part.get("id", "")] = part.get("name", "")
            messages.append(converted)
    result: dict[str, Any] = {
        "model": payload.get("model", ""),
        "messages": messages,
        "stream": bool(payload.get("stream", False)),
        "options": {"num_predict": payload.get("max_tokens", 1024)},
    }
    for source, target in (("temperature", "temperature"), ("top_p", "top_p"), ("top_k", "top_k")):
        if source in payload:
            result["options"][target] = payload[source]
    if payload.get("stop_sequences"):
        result["options"]["stop"] = payload["stop_sequences"]
    thinking = payload.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") in {"enabled", "disabled"}:
        result["think"] = thinking["type"] == "enabled"
    if payload.get("tools"):
        result["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            }
            for tool in payload["tools"]
            if tool.get("name")
        ]
    return apply_profile(result, profile)


def ollama_to_anthropic(data: dict[str, Any], model: str) -> dict[str, Any]:
    message = data.get("message", {})
    content: list[dict[str, Any]] = []
    if message.get("thinking"):
        content.append({"type": "thinking", "thinking": message["thinking"], "signature": THINKING_SIGNATURE})
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        function = call.get("function", {})
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {"value": arguments}
        content.append({
            "type": "tool_use", "id": f"toolu_{uuid.uuid4().hex}",
            "name": function.get("name", ""), "input": arguments,
        })
    return {
        "id": f"msg_{uuid.uuid4().hex}", "type": "message", "role": "assistant", "model": model,
        "content": content,
        "stop_reason": stop_reason(data, bool(message.get("tool_calls"))),
        "stop_sequence": None,
        "usage": {"input_tokens": data.get("prompt_eval_count", 0), "output_tokens": data.get("eval_count", 0)},
    }


def stop_reason(data: dict[str, Any], used_tool: bool) -> str:
    if used_tool:
        return "tool_use"
    return "max_tokens" if data.get("done_reason") == "length" else "end_turn"


class AnthropicStreamTranslator:
    """Convert native Ollama stream objects into Anthropic Messages SSE events.

    Reasoning is forwarded as thinking blocks so agents see progress while the
    model thinks; otherwise a long reasoning phase looks like a stalled request.
    """

    def __init__(self, model: str):
        self.model = model
        self.message_id = f"msg_{uuid.uuid4().hex}"
        self.next_index = 0
        self.open_block: str | None = None
        self.used_tool = False
        self.last: dict[str, Any] = {}

    def start(self) -> list[bytes]:
        message = {
            "id": self.message_id, "type": "message", "role": "assistant", "model": self.model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
        return [_sse("message_start", {"type": "message_start", "message": message})]

    def ping(self) -> bytes:
        return _sse("ping", {"type": "ping"})

    def feed(self, obj: dict[str, Any]) -> list[bytes]:
        self.last = obj
        if obj.get("error"):
            return [_sse("error", {"type": "error", "error": {"type": "api_error", "message": str(obj["error"])}})]
        events: list[bytes] = []
        message = obj.get("message") or {}
        if message.get("thinking"):
            events += self._open("thinking")
            events.append(self._delta({"type": "thinking_delta", "thinking": message["thinking"]}))
        if message.get("content"):
            events += self._open("text")
            events.append(self._delta({"type": "text_delta", "text": message["content"]}))
        for call in message.get("tool_calls") or []:
            self.used_tool = True
            events += self._close()
            index = self.next_index
            self.next_index += 1
            function = call.get("function", {})
            arguments = function.get("arguments", {})
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, separators=(",", ":"))
            events += [
                _sse("content_block_start", {"type": "content_block_start", "index": index, "content_block": {
                    "type": "tool_use", "id": f"toolu_{uuid.uuid4().hex}", "name": function.get("name", ""), "input": {},
                }}),
                _sse("content_block_delta", {"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": arguments}}),
                _sse("content_block_stop", {"type": "content_block_stop", "index": index}),
            ]
        return events

    def finish(self) -> list[bytes]:
        events = self._close()
        if self.next_index == 0:
            # Always return at least one content block.
            events += self._open("text") + self._close()
        return events + [
            _sse("message_delta", {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason(self.last, self.used_tool), "stop_sequence": None},
                "usage": {
                    "input_tokens": int(self.last.get("prompt_eval_count") or 0),
                    "output_tokens": int(self.last.get("eval_count") or 0),
                },
            }),
            _sse("message_stop", {"type": "message_stop"}),
        ]

    def _open(self, kind: str) -> list[bytes]:
        if self.open_block == kind:
            return []
        events = self._close()
        self.open_block = kind
        block = {"type": "thinking", "thinking": ""} if kind == "thinking" else {"type": "text", "text": ""}
        events.append(_sse("content_block_start", {"type": "content_block_start", "index": self.next_index, "content_block": block}))
        return events

    def _delta(self, delta: dict[str, Any]) -> bytes:
        return _sse("content_block_delta", {"type": "content_block_delta", "index": self.next_index, "delta": delta})

    def _close(self) -> list[bytes]:
        if self.open_block is None:
            return []
        events = []
        if self.open_block == "thinking":
            events.append(self._delta({"type": "signature_delta", "signature": THINKING_SIGNATURE}))
        events.append(_sse("content_block_stop", {"type": "content_block_stop", "index": self.next_index}))
        self.open_block = None
        self.next_index += 1
        return events


# Anthropic signs thinking blocks so they can be verified when sent back. Local
# models have nothing to sign; the proxy ignores thinking blocks on the way in.
THINKING_SIGNATURE = "agent-proxy-local"


def estimate_tokens(payload: dict[str, Any]) -> int:
    """Rough count for /v1/messages/count_tokens; Ollama has no tokenizer endpoint."""
    text = json.dumps({key: payload.get(key) for key in ("system", "messages", "tools", "input", "prompt")}, ensure_ascii=False)
    return max(1, len(text) // 4)


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'), ensure_ascii=False)}\n\n".encode()
