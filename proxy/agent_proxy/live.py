"""Fan out in-flight request text to UI subscribers as it streams.

Text and payloads are held in memory only while a request is in flight (plus a
short tail of recently finished requests) and are never written to disk;
persisted payloads remain controlled by the session's trace setting.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from typing import Any

from .telemetry import RequestTelemetry


MAX_TEXT = 24_000
RECENT_LIMIT = 8
RAW_LIMIT = 200


class LiveHub:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.subscribers: set[asyncio.Queue[dict[str, Any] | None]] = set()
        self.active: dict[str, dict[str, Any]] = {}
        self.recent: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # Kept apart from the entries so large payloads never go out with stream events.
        self.payloads: dict[str, dict[str, Any]] = {}

    def subscribe(self) -> asyncio.Queue[dict[str, Any] | None]:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=2000)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any] | None]) -> None:
        self.subscribers.discard(queue)

    def snapshot(self, session_id: str | None = None) -> dict[str, Any]:
        def keep(entry: dict[str, Any]) -> bool:
            return session_id is None or entry["session_id"] == session_id

        return {
            "enabled": self.enabled,
            "active": [entry for entry in self.active.values() if keep(entry)],
            "recent": [entry for entry in list(self.recent.values())[::-1] if keep(entry)],
        }

    def start(self, key: str, telemetry: RequestTelemetry) -> None:
        entry = {
            "key": key, "session_id": telemetry.session_id, "request_id": telemetry.request_id,
            "client": telemetry.client, "model": telemetry.model, "backend": telemetry.backend,
            "endpoint": telemetry.endpoint, "started_at": time.time(), "first_token_at": None,
            "reasoning": "", "content": "", "tool": "",
            "chunks": {"reasoning": 0, "content": 0, "tool": 0},
            "summary": None,
            "done": False,
        }
        self.active[key] = entry
        self.payloads[key] = {"request": None, "upstream": None, "raw": deque(maxlen=RAW_LIMIT), "raw_total": 0}
        self._publish({"type": "start", "request": entry})

    def attach(self, key: str, request: Any, upstream: Any = None) -> None:
        """Record what the agent sent and what the proxy forwarded, once both are known."""
        entry = self.active.get(key)
        if entry is None:
            return
        entry["summary"] = summarize(request, upstream)
        if self.enabled:
            self.payloads[key].update(request=request, upstream=upstream)
        self._publish({"type": "summary", "key": key, "summary": entry["summary"]})

    def raw(self, key: str, obj: dict[str, Any]) -> None:
        payload = self.payloads.get(key)
        if payload is None:
            return
        payload["raw_total"] += 1
        if self.enabled:
            payload["raw"].append(obj)

    def delta(self, key: str, kind: str, text: str) -> None:
        entry = self.active.get(key)
        if entry is None:
            return
        if entry["first_token_at"] is None:
            entry["first_token_at"] = time.time()
        if self.enabled:
            entry[kind] = (entry[kind] + text)[-MAX_TEXT:]
        entry["chunks"][kind] += 1
        self._publish({
            "type": "delta", "key": key, "kind": kind,
            "text": text if self.enabled else "", "chunks": entry["chunks"][kind],
        })

    def end(self, key: str, record: dict[str, Any]) -> None:
        entry = self.active.pop(key, None)
        if entry is None:
            return
        entry.update({"done": True, "ended_at": time.time(), "record": record})
        self.recent[key] = entry
        while len(self.recent) > RECENT_LIMIT:
            old_key, _ = self.recent.popitem(last=False)
            self.payloads.pop(old_key, None)
        self._publish({"type": "end", "key": key, "record": record})

    def details(self, key: str) -> dict[str, Any] | None:
        """Everything known about one live or recently finished request."""
        entry = self.active.get(key) or self.recent.get(key)
        payload = self.payloads.get(key)
        if entry is None or payload is None:
            return None
        return {
            **entry,
            "enabled": self.enabled,
            "request": payload["request"],
            "upstream": payload["upstream"],
            "raw": list(payload["raw"]),
            "raw_total": payload["raw_total"],
        }

    def clear_session(self, session_id: str) -> None:
        for key in [key for key, entry in self.recent.items() if entry["session_id"] == session_id]:
            self.recent.pop(key)
            self.payloads.pop(key, None)

    def _publish(self, event: dict[str, Any]) -> None:
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A stalled client; drop it so it reconnects and resyncs from a snapshot.
                self.subscribers.discard(queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)


def summarize(request: Any, upstream: Any) -> dict[str, Any]:
    """Small facts about a request that are cheap to send with every live update."""
    request = request if isinstance(request, dict) else {}
    upstream = upstream if isinstance(upstream, dict) else {}
    sent = upstream or request
    options = sent.get("options") or {}
    messages = request.get("messages")
    if messages is None and isinstance(request.get("input"), list):
        messages = request["input"]
    system = request.get("system") or request.get("instructions")
    if system is None and isinstance(messages, list):
        system = next((item.get("content") for item in messages if isinstance(item, dict) and item.get("role") in {"system", "developer"}), None)
    tools = request.get("tools") or []
    return {
        "messages": len(messages) if isinstance(messages, list) else None,
        "tools": len(tools) if isinstance(tools, list) else 0,
        "tool_names": [_tool_name(tool) for tool in tools[:40]] if isinstance(tools, list) else [],
        "system_chars": len(system) if isinstance(system, str) else len(str(system)) if system else 0,
        "stream": bool(request.get("stream")),
        "ollama_model": sent.get("model"),
        "num_ctx": options.get("num_ctx"),
        "num_predict": options.get("num_predict") or request.get("max_tokens") or request.get("max_completion_tokens"),
        "temperature": options.get("temperature", request.get("temperature")),
        "top_p": options.get("top_p", request.get("top_p")),
        "top_k": options.get("top_k"),
        "think": sent.get("think"),
        "keep_alive": sent.get("keep_alive"),
        "translated": bool(upstream) and upstream is not request,
    }


def _tool_name(tool: Any) -> str:
    if not isinstance(tool, dict):
        return "?"
    return str(tool.get("name") or (tool.get("function") or {}).get("name") or tool.get("type") or "?")
