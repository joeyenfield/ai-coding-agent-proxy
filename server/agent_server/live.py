"""Fan out in-flight request text to UI subscribers as it streams.

Text and payloads are held in memory only while a request is in flight (plus a
short tail of recently finished requests) and are never written to disk;
persisted payloads remain controlled by the session's trace setting.

With several processes (cluster.py), each one's events also go to the others (live.events, batched
every BATCH_SECONDS), but only while some process has a console watching (its heartbeat says how
many): with nobody watching, a token costs nothing. Each process mirrors the others' requests
(without their payloads), so a console sees every process's traffic; a request's details are asked
of the process it runs on (internal.py's /internal/live).
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from typing import Any

from .telemetry import RequestTelemetry


MAX_TEXT = 24_000
# Intercepted sessions make many small requests, so keep a longer tail.
RECENT_LIMIT = 40
RAW_LIMIT = 200
BATCH_SECONDS = 0.1
# How often whether anyone elsewhere watches is looked at again.
WATCH_CHECK_SECONDS = 1.0


class LiveHub:
    def __init__(self, enabled: bool = True, cluster: Any = None):
        self.enabled = enabled
        self.subscribers: set[asyncio.Queue[dict[str, Any] | None]] = set()
        self.active: dict[str, dict[str, Any]] = {}
        self.recent: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # Kept apart from the entries so large payloads never go out with stream events.
        self.payloads: dict[str, dict[str, Any]] = {}
        self.cluster = cluster
        self._outbox: list[dict[str, Any]] = []
        self._watched: tuple[float, bool] = (0.0, False)
        if cluster is not None:
            cluster.beacons["live"] = lambda: len(self.subscribers)
            cluster.on("live.events", self._remote)

    @property
    def pod(self) -> str | None:
        return self.cluster.pod_id if self.cluster is not None else None

    def _watching_elsewhere(self) -> bool:
        if self.cluster is None or not self.cluster.scaled:
            return False
        now = time.monotonic()
        if now - self._watched[0] >= WATCH_CHECK_SECONDS:
            watching = any(int(info.get("live") or 0) > 0 for pod, info in self.cluster.pods().items() if pod != self.cluster.pod_id)
            self._watched = (now, watching)
        return self._watched[1]

    async def run(self) -> None:
        """Send this process's events to the others, a batch at a time (started by the app's lifespan)."""
        while True:
            await asyncio.sleep(BATCH_SECONDS)
            if self._outbox and self.cluster is not None:
                events, self._outbox = self._outbox, []
                self.cluster.publish("live.events", {"events": events})

    def _remote(self, data: dict[str, Any], sender: str) -> None:
        """Another process's events: mirrored here (no payloads) and passed to this process's consoles."""
        for event in data.get("events") or []:
            if not isinstance(event, dict):
                continue
            kind, key = event.get("type"), str(event.get("key") or "")
            if kind == "start" and isinstance(event.get("request"), dict):
                entry = {**event["request"], "pod": sender}
                self.active[entry["key"]] = entry
            elif kind == "summary" and key in self.active:
                self.active[key]["summary"] = event.get("summary")
            elif kind == "delta" and key in self.active:
                entry = self.active[key]
                if entry.get("first_token_at") is None:
                    entry["first_token_at"] = time.time()
                text_kind = str(event.get("kind") or "content")
                if text_kind in ("reasoning", "content", "tool"):
                    entry[text_kind] = (entry[text_kind] + str(event.get("text") or ""))[-MAX_TEXT:]
                    entry["chunks"][text_kind] = event.get("chunks") or entry["chunks"][text_kind] + 1
            elif kind == "end":
                entry = self.active.pop(key, None)
                if entry is not None:
                    entry.update({"done": True, "ended_at": time.time(), "record": event.get("record")})
                    self.recent[key] = entry
                    while len(self.recent) > RECENT_LIMIT:
                        old_key, _ = self.recent.popitem(last=False)
                        self.payloads.pop(old_key, None)
            self._fanout(event)

    def elsewhere(self, key: str) -> str | None:
        """The other process a live (or recently finished) request ran on, if it isn't this one."""
        entry = self.active.get(key) or self.recent.get(key)
        pod = (entry or {}).get("pod")
        return pod if pod and pod != self.pod else None

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
            "endpoint": telemetry.endpoint, "kind": telemetry.kind, "method": telemetry.method,
            "host": telemetry.host, "started_at": time.time(), "first_token_at": None,
            "reasoning": "", "content": "", "tool": "",
            "chunks": {"reasoning": 0, "content": 0, "tool": 0},
            "summary": None,
            "done": False,
            "pod": self.pod,
        }
        self.active[key] = entry
        self.payloads[key] = {"request": None, "upstream": None, "http": None, "raw": deque(maxlen=RAW_LIMIT), "raw_total": 0}
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

    def http(self, key: str, exchange: dict[str, Any]) -> None:
        """Headers and status of an intercepted exchange (credentials already redacted)."""
        payload = self.payloads.get(key)
        if payload is not None and self.enabled:
            payload["http"] = exchange

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

    def details(self, key: str, local: bool = False) -> dict[str, Any] | None:
        """Everything known about one live or recently finished request (here: one another process ran is asked of it)."""
        entry = self.active.get(key) or self.recent.get(key)
        payload = self.payloads.get(key)
        if entry is None or payload is None:
            return None
        return {
            **entry,
            "enabled": self.enabled,
            "request": payload["request"],
            "upstream": payload["upstream"],
            "http": payload["http"],
            "raw": list(payload["raw"]),
            "raw_total": payload["raw_total"],
        }

    def close(self) -> None:
        """End every live stream, so a restart doesn't wait on them; the console reconnects."""
        for queue in list(self.subscribers):
            self.subscribers.discard(queue)
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(None)

    def clear_session(self, session_id: str) -> None:
        for key in [key for key, entry in self.recent.items() if entry["session_id"] == session_id]:
            self.recent.pop(key)
            self.payloads.pop(key, None)

    def _publish(self, event: dict[str, Any]) -> None:
        self._fanout(event)
        if self._watching_elsewhere():
            self._outbox.append(event)

    def _fanout(self, event: dict[str, Any]) -> None:
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
