"""Every HTTP request and WebSocket the server received, and how each ended, for site admins.

The Requests page records agents' model calls; this is everything else as well: the console's API
calls, sign-in, agent-machine's fleet socket and tunnels, agent-proxy's tunnels, downloads, and
the ones refused before any route ran (401, 403, 426, a closed WebSocket and its code). It is what
to read when a machine says it's connected and the console says it's offline.

Two parts. Everything since the server started is kept in memory (bounded: the newest ALL_LIMIT,
and the newest FAILED_LIMIT failures apart from them), with running stats. What's worth having
after a restart is also saved in the database's access_log table, through the write queue:
failures, dropped WebSockets and every fleet connection (FLEET_PATH), however it ended. Those are
deleted after the retention setting, or SAVED_DAYS without one. The console's polling, which is
most of the traffic, is never saved.

Query strings are reduced to their names, as an OAuth callback carries its code in one.
"""

from __future__ import annotations

import itertools
import logging
import re
import time
import uuid
from collections import Counter, deque
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select

from .db import schema

LOGGER = logging.getLogger("agent_server")

# The newest requests of all kinds, and, separately, the newest that failed: a busy console's
# polling would otherwise push a machine's refusals out of the first one within minutes.
ALL_LIMIT = 5000
FAILED_LIMIT = 2000
# The start of an error's body: its {"detail": ...}, enough to say why.
DETAIL_LIMIT = 300
# The log's own page polls it; listing those would only bury everything else.
SKIPPED = ("/api/server/access-log", "/assets/")
# A WebSocket closed with one of these by either side ended normally.
NORMAL_CLOSES = {1000, 1001}
# agent-machine's connection to the fleet: saved however it ends, so a machine's history survives restarts.
FLEET_PATH = "/api/machines/connect"
# How long saved entries are kept when the server keeps recordings forever.
SAVED_DAYS = 30
# Running stats: an hour of minutes, the durations of the newest HTTP answers, and at most this many
# distinct failing routes and addresses (a scanner's random paths mustn't grow them without end).
MINUTES = 60
DURATIONS = 2000
GROUPS = 500
# Ids, session ids and other long or numeric path segments, so one route counts as one.
VARIABLE_SEGMENT = re.compile(r"^(?:[0-9a-f-]{16,}|\d+|[A-Za-z0-9_-]{24,})$")


def route(path: str) -> str:
    """The path with its ids replaced, for counting: /session/<id>/v1/... is one route, whatever the session."""
    parts = path.split("/")
    if len(parts) > 2 and parts[1] == "session":
        parts[2] = "{id}"
    return "/".join("{id}" if VARIABLE_SEGMENT.match(part) else part for part in parts)


def answer(entry: dict[str, Any]) -> str:
    """What a request got back, as one label: an HTTP status, or how a WebSocket ended."""
    if entry["kind"] == "http":
        return str(entry["status"]) if entry["status"] is not None else "answering"
    if entry["outcome"] == "open":
        return "open"
    if entry["status"] is not None and entry["status"] != 101:
        # The handshake's answer, and the code the server closed it with, which says why (4401: not signed in).
        return f"refused {entry['status']}" + (f", {entry['close_code']}" if entry["close_code"] is not None else "")
    if entry["close_code"] is None:
        return "closed"
    return f"{entry['close_code']} by {entry['closed_by'] or '?'}"


class AccessLog:
    def __init__(self, db: Any = None, retention_days: Any = None, limit: int = ALL_LIMIT, failed_limit: int = FAILED_LIMIT) -> None:
        self.db = db
        # The retention setting, read when pruning (it can change while the server runs).
        self.retention_days = retention_days or (lambda: None)
        self.entries: deque[dict[str, Any]] = deque(maxlen=limit)
        self.failed: deque[dict[str, Any]] = deque(maxlen=failed_limit)
        # WebSockets still open, however long ago they arrived (a fleet connection lasts days).
        self.open: dict[int, dict[str, Any]] = {}
        # Their scopes, so who and which machine they are shows while they're open (the hello comes after accept).
        self.open_scopes: dict[int, dict[str, Any]] = {}
        self.ids = itertools.count(1)
        self.started_at = time.time()
        self.totals: Counter[str] = Counter()
        self.minutes: dict[int, Counter[str]] = {}
        self.durations: deque[float] = deque(maxlen=DURATIONS)
        self.answers: Counter[str] = Counter()
        self.failing_routes: Counter[tuple[str, str, str]] = Counter()
        self.failing_clients: Counter[str] = Counter()
        self.last_failure: dict[Any, dict[str, Any]] = {}

    def start(self, scope: dict[str, Any]) -> dict[str, Any]:
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers") or []}
        client = scope.get("client")
        query = scope.get("query_string", b"").decode("latin-1")
        entry = {
            "id": next(self.ids),
            "time": time.time(),
            "kind": "websocket" if scope["type"] == "websocket" else "http",
            "method": scope.get("method", "GET") if scope["type"] == "http" else "WS",
            "path": scope.get("path", ""),
            "query": "&".join(sorted({part.split("=", 1)[0] for part in query.split("&") if part})),
            "client": client[0] if client else "",
            # Behind nginx or Cloudflare, who really asked.
            "forwarded_for": headers.get("cf-connecting-ip") or headers.get("x-forwarded-for", ""),
            "host": headers.get("host", ""),
            "user_agent": headers.get("user-agent", "")[:200],
            "program_version": headers.get("x-ai-proxy-version", ""),
            "protocol": headers.get("x-ai-proxy-protocol", ""),
            "user": None,
            "tenant": None,
            # A fleet connection's machine, once its hello named it (routes/machines.py sets it in the scope).
            "machine_id": None,
            "machine_name": None,
            "status": None,
            "close_code": None,
            "close_reason": "",
            "closed_by": None,
            "detail": "",
            "duration_ms": None,
            "outcome": "open",
        }
        self.entries.append(entry)
        if entry["kind"] == "websocket":
            self.open[entry["id"]] = entry
            self.open_scopes[entry["id"]] = scope
        return entry

    @staticmethod
    def identify(entry: dict[str, Any], scope: dict[str, Any]) -> None:
        state = scope.get("state") or {}
        identity = state.get("identity")
        if identity is not None:
            entry["user"] = identity.user
            entry["tenant"] = identity.tenant
        machine = state.get("machine")
        if machine:
            entry["machine_id"], entry["machine_name"] = machine.get("id"), machine.get("name")

    def finish(self, entry: dict[str, Any], scope: dict[str, Any]) -> None:
        self.identify(entry, scope)
        entry["duration_ms"] = round((time.time() - entry["time"]) * 1000, 1)
        entry["outcome"] = outcome(entry)
        self.open.pop(entry["id"], None)
        self.open_scopes.pop(entry["id"], None)
        self._count(entry)
        if entry["outcome"] != "ok":
            self.failed.append(entry)
        if entry["outcome"] != "ok" or entry["path"] == FLEET_PATH:
            self._save(entry)

    def _count(self, entry: dict[str, Any]) -> None:
        kind = entry["outcome"]
        self.totals[kind] += 1
        minute = int(entry["time"] // 60)
        self.minutes.setdefault(minute, Counter())[kind] += 1
        for old in [key for key in self.minutes if key <= minute - MINUTES]:
            del self.minutes[old]
        label = answer(entry)
        self.answers[label] += 1
        if entry["kind"] == "http":
            self.durations.append(entry["duration_ms"])
        if kind == "ok":
            return
        key = (entry["method"], route(entry["path"]), label)
        if key in self.failing_routes or len(self.failing_routes) < GROUPS:
            self.failing_routes[key] += 1
            self.last_failure[key] = entry
        address = entry["forwarded_for"] or entry["client"]
        if address in self.failing_clients or len(self.failing_clients) < GROUPS:
            self.failing_clients[address] += 1
            self.last_failure[address] = entry

    def _save(self, entry: dict[str, Any]) -> None:
        if self.db is None:
            return
        saved = dict(entry)
        row = {
            "id": uuid.uuid4().hex,
            "time": saved["time"],
            "day": datetime.fromtimestamp(saved["time"], timezone.utc).strftime("%Y-%m-%d"),
            "outcome": saved["outcome"],
            "path": saved["path"],
            "search": _haystack(saved),
            "entry": saved,
        }
        try:
            self.db.writer.submit(lambda connection: connection.execute(schema.access_log.insert().values(**row)))
        except Exception:  # noqa: BLE001 - a log entry must never fail the request it describes
            LOGGER.exception("saving an access log entry failed")

    def _refresh_open(self) -> None:
        for key, scope in list(self.open_scopes.items()):
            if key in self.open:
                self.identify(self.open[key], scope)

    def query(self, failed: bool = False, search: str = "", limit: int = 200, before: int | None = None) -> dict[str, Any]:
        self._refresh_open()
        source = self.failed if failed else self.entries
        words = search.lower().split()
        found: list[dict[str, Any]] = []
        for entry in reversed(source):
            if before is not None and entry["id"] >= before:
                continue
            if words and not all(word in _haystack(entry) for word in words):
                continue
            found.append(dict(entry))
            if len(found) >= limit:
                break
        return {"entries": found, "started_at": self.started_at, "totals": self._totals(), "kept": {"all": self.entries.maxlen, "failed": self.failed.maxlen}}

    def _totals(self) -> dict[str, int]:
        return {"ok": self.totals["ok"], "failed": self.totals["failed"], "dropped": self.totals["dropped"], "open_websockets": len(self.open)}

    def stats(self) -> dict[str, Any]:
        """Since the server started: requests a minute for the last hour, answers, the slowest, what fails most and from where, open WebSockets."""
        now = int(time.time() // 60)
        self._refresh_open()
        durations = sorted(self.durations)

        def percentile(share: float) -> float | None:
            return durations[min(len(durations) - 1, int(len(durations) * share))] if durations else None

        def last(key: Any) -> dict[str, Any]:
            entry = self.last_failure.get(key) or {}
            return {"time": entry.get("time"), "user": entry.get("user"), "user_agent": entry.get("user_agent", ""), "program_version": entry.get("program_version", ""), "reason": entry.get("close_reason") or entry.get("detail", "")}

        return {
            "started_at": self.started_at,
            "totals": self._totals(),
            "per_minute": [
                {"minute": minute * 60, **{kind: self.minutes.get(minute, Counter())[kind] for kind in ("ok", "failed", "dropped")}}
                for minute in range(now - MINUTES + 1, now + 1)
            ],
            "answers": [{"answer": label, "count": count} for label, count in self.answers.most_common(20)],
            "durations": {"count": len(durations), "p50": percentile(0.5), "p95": percentile(0.95), "max": durations[-1] if durations else None},
            "failing_routes": [
                {"method": method, "route": path, "answer": label, "count": count, **last((method, path, label))}
                for (method, path, label), count in self.failing_routes.most_common(10)
            ],
            "failing_clients": [{"address": address, "count": count, **last(address)} for address, count in self.failing_clients.most_common(10)],
            "open_websockets": sorted(
                ({"path": entry["path"], "user": entry["user"], "machine_id": entry.get("machine_id"), "machine_name": entry.get("machine_name"), "address": entry["forwarded_for"] or entry["client"], "since": entry["time"], "user_agent": entry["user_agent"], "program_version": entry["program_version"]}
                 for entry in list(self.open.values())),
                key=lambda item: item["since"],
            ),
        }

    # The saved part, read from the database (run these off the event loop).

    def history(self, search: str = "", failed: bool = False, fleet: bool = False, limit: int = 200, before: float | None = None) -> list[dict[str, Any]]:
        if self.db is None:
            return []
        self.db.writer.flush()
        table = schema.access_log
        query = select(table.c.id, table.c.entry).order_by(table.c.time.desc()).limit(limit)
        if before is not None:
            query = query.where(table.c.time < before)
        if failed:
            query = query.where(table.c.outcome != "ok")
        if fleet:
            query = query.where(table.c.path == FLEET_PATH)
        for word in search.lower().split():
            query = query.where(table.c.search.contains(word, autoescape=True))
        with self.db.read() as connection:
            # The in-memory id was only that run's; the saved row's own id is unique.
            return [{**entry, "id": row_id, "saved": True} for row_id, entry in connection.execute(query)]

    def saved_stats(self, days: int = 14, fleet_days: int = 7) -> dict[str, Any]:
        """From the database: what was saved each day, and each machine's fleet connections (by its sign-in and address)."""
        if self.db is None:
            return {"days": [], "fleet": [], "oldest": None, "count": 0}
        self.db.writer.flush()
        table = schema.access_log
        today = datetime.now(timezone.utc)
        first = datetime.fromtimestamp(today.timestamp() - (days - 1) * 86400, timezone.utc).strftime("%Y-%m-%d")
        with self.db.read() as connection:
            counts = connection.execute(
                select(table.c.day, table.c.outcome, func.count()).where(table.c.day >= first).group_by(table.c.day, table.c.outcome)
            ).all()
            oldest, count = connection.execute(select(func.min(table.c.time), func.count())).one()
            connections = connection.execute(
                select(table.c.entry).where(table.c.path == FLEET_PATH, table.c.time >= time.time() - fleet_days * 86400).order_by(table.c.time.desc()).limit(5000)
            ).all()
        by_day: dict[str, Counter[str]] = {}
        for day, kind, number in counts:
            by_day.setdefault(day, Counter())[kind] += number
        day_list = [datetime.fromtimestamp(today.timestamp() - offset * 86400, timezone.utc).strftime("%Y-%m-%d") for offset in range(days - 1, -1, -1)]
        machines: dict[tuple[Any, str], dict[str, Any]] = {}
        for (entry,) in connections:
            address = entry.get("forwarded_for") or entry.get("client", "")
            # By the machine its hello named; a connection refused before its hello, by its sign-in and address.
            key = ("machine", entry["machine_id"]) if entry.get("machine_id") else ("address", entry.get("user"), address)
            item = machines.get(key)
            if item is None:
                # Newest first, so the first one seen is the latest.
                item = machines[key] = {
                    "machine_id": entry.get("machine_id"), "machine_name": entry.get("machine_name"),
                    "user": entry.get("user"), "address": address, "connections": 0, "ok": 0, "failed": 0, "dropped": 0,
                    "connected_ms": 0.0, "last_time": entry.get("time"), "last_answer": answer(entry),
                    "last_reason": entry.get("close_reason") or entry.get("detail", ""), "user_agent": entry.get("user_agent", ""),
                    "program_version": entry.get("program_version", ""),
                }
            item["connections"] += 1
            item[entry.get("outcome", "ok") if entry.get("outcome") in ("ok", "failed", "dropped") else "ok"] += 1
            item["connected_ms"] += entry.get("duration_ms") or 0
        return {
            "days": [{"day": day, **{kind: by_day.get(day, Counter())[kind] for kind in ("ok", "failed", "dropped")}} for day in day_list],
            "fleet": sorted(machines.values(), key=lambda item: -(item["last_time"] or 0)),
            "fleet_days": fleet_days,
            "oldest": oldest,
            "count": count,
            "kept_days": self.kept_days(),
        }

    def kept_days(self) -> int:
        return int(self.retention_days() or SAVED_DAYS)

    def prune(self) -> int:
        """Saved entries older than the retention setting (SAVED_DAYS when recordings are kept forever)."""
        if self.db is None:
            return 0
        cutoff = time.time() - self.kept_days() * 86400
        with self.db.write() as connection:
            return connection.execute(delete(schema.access_log).where(schema.access_log.c.time < cutoff)).rowcount or 0


def outcome(entry: dict[str, Any]) -> str:
    """ok, failed (refused, or an error answer), or dropped (a WebSocket that went away without closing)."""
    if entry["kind"] == "http":
        return "ok" if entry["status"] is not None and entry["status"] < 400 else "failed"
    code = entry["close_code"]
    if entry["status"] is not None and entry["status"] != 101:
        return "failed"  # refused before it was accepted
    if code is None or code in NORMAL_CLOSES:
        return "ok"
    # 1005: closed with no code; 1006: the connection was lost, as when a phone sleeps or changes network.
    if entry["closed_by"] == "client" and code in (1005, 1006):
        return "dropped"
    return "failed"


def _haystack(entry: dict[str, Any]) -> str:
    return " ".join(str(entry.get(key) or "") for key in (
        "method", "path", "client", "forwarded_for", "user", "tenant", "machine_id", "machine_name", "user_agent", "program_version", "status", "close_code", "close_reason", "detail", "outcome",
    )).lower()


class AccessLogMiddleware:
    """The outermost layer: sees what every other one answered, the version and sign-in refusals too."""

    def __init__(self, app: Any, log: AccessLog) -> None:
        self.app = app
        self.log = log

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket") or scope.get("path", "").startswith(SKIPPED):
            await self.app(scope, receive, send)
            return
        # Shared with the inner layers, so the identity AuthMiddleware finds is here afterwards.
        scope.setdefault("state", {})
        entry = self.log.start(scope)
        capture = {"body": False}

        async def logged_send(message: dict[str, Any]) -> None:
            kind = message["type"]
            if kind in ("http.response.start", "websocket.http.response.start"):
                entry["status"] = message["status"]
                # Who it was, already while a long answer (a model's stream) is still going.
                self.log.identify(entry, scope)
                content_type = next((value for key, value in message.get("headers") or [] if key.lower() == b"content-type"), b"")
                capture["body"] = message["status"] >= 400 and (b"json" in content_type or b"text" in content_type)
            elif kind in ("http.response.body", "websocket.http.response.body") and capture["body"]:
                room = DETAIL_LIMIT - len(entry["detail"])
                if room > 0:
                    entry["detail"] += message.get("body", b"")[:room].decode("utf-8", "replace")
            elif kind == "websocket.accept":
                entry["status"] = 101
                self.log.identify(entry, scope)
            elif kind == "websocket.close":
                if entry["close_code"] is None:
                    entry["close_code"] = message.get("code", 1000)
                    entry["close_reason"] = str(message.get("reason") or "")[:DETAIL_LIMIT]
                    entry["closed_by"] = "server"
                if entry["status"] is None:
                    # Closed before it was accepted: the server answers the handshake with 403.
                    entry["status"] = 403
            await send(message)

        async def logged_receive() -> dict[str, Any]:
            message = await receive()
            if message["type"] == "websocket.disconnect" and entry["close_code"] is None:
                entry["close_code"] = message.get("code", 1005)
                entry["close_reason"] = str(message.get("reason") or "")[:DETAIL_LIMIT]
                entry["closed_by"] = "client"
            return message

        try:
            await self.app(scope, logged_receive, logged_send)
        except BaseException as exc:
            if entry["status"] is None or (entry["kind"] == "websocket" and entry["close_code"] is None):
                if entry["kind"] == "http":
                    entry["status"] = 500
                else:
                    entry["close_code"], entry["closed_by"] = 1011, "server"
                entry["detail"] = entry["detail"] or f"{type(exc).__name__}: {exc}"[:DETAIL_LIMIT]
            raise
        finally:
            self.log.finish(entry, scope)
