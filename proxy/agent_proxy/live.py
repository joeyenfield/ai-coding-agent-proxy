"""Fan out in-flight request text to UI subscribers as it streams.

Text is held in memory only while a request is in flight (plus a short tail
of recently finished requests) and is never written to disk; persisted
payloads remain controlled by the session's trace setting.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any

from .telemetry import RequestTelemetry


MAX_TEXT = 24_000
RECENT_LIMIT = 8


class LiveHub:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.subscribers: set[asyncio.Queue[dict[str, Any] | None]] = set()
        self.active: dict[str, dict[str, Any]] = {}
        self.recent: OrderedDict[str, dict[str, Any]] = OrderedDict()

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
            "done": False,
        }
        self.active[key] = entry
        self._publish({"type": "start", "request": entry})

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
            self.recent.popitem(last=False)
        self._publish({"type": "end", "key": key, "record": record})

    def clear_session(self, session_id: str) -> None:
        for key in [key for key, entry in self.recent.items() if entry["session_id"] == session_id]:
            self.recent.pop(key)

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
