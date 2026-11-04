"""The request log: every HTTP request and WebSocket the server received, saved for site admins.

The access log (access_log.py) keeps everything since the server started in memory, and saves only
failures and fleet connections. This is off until the request_log setting's `enabled` is turned on;
then it saves every request, whoever sent it and however it ended, to
the request_log table for the request_log setting's `days`, so a site admin can see what happened
yesterday as well as now (the console's API log page). Its own routes and the console's static files
are left out (access_log.SKIPPED).

Debug mode (`payloads`) also saves each request's and response's headers and body, up to
PAYLOAD_LIMIT each, in request_log_payloads, kept for `payload_days`. Payloads are for finding out
why something fails, so they are never saved for sign-in (SECRET_PATHS), credential headers are
dropped, and fields named like secrets are redacted from JSON and form bodies (and from a truncated
body's text). Anything else a body holds is saved as it is: turn debug mode on only while it's needed.

Rows are written through the database's write queue, so logging never holds up the request.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode

from sqlalchemy import delete, func, select

from .access_log import _haystack
from .db import pack, schema, unpack

LOGGER = logging.getLogger("agent_server")

# The most of each body kept in debug mode; the rest is counted, not stored.
PAYLOAD_LIMIT = 64 * 1024
# Sign-in and its tokens: never saved, whatever debug mode says.
SECRET_PATHS = ("/api/auth/",)
# Headers that carry credentials, dropped from saved payloads.
SECRET_HEADERS = {
    "authorization", "proxy-authorization", "cookie", "set-cookie", "x-ai-proxy-token", "x-api-key", "api-key",
    "x-ai-proxy-internal", "x-goog-api-key",
}
# A field or parameter whose name says it holds a secret.
SECRET_NAME = re.compile(r"pass(word|phrase)?|secret|token|authori[sz]ation|api[_-]?key|credential|cookie|private[_-]?key|^key$|^code$", re.I)
# The same in a JSON body that couldn't be parsed (it was cut at PAYLOAD_LIMIT): "name": "value".
SECRET_JSON_TEXT = re.compile(
    r'("(?:[^"\\]*(?:pass(?:word|phrase)?|secret|token|authori[sz]ation|api[_-]?key|credential|cookie|private[_-]?key)[^"\\]*|key|code)"\s*:\s*)"(?:[^"\\]|\\.)*"',
    re.I,
)
REDACTED = "[redacted]"
# Kinds of body kept as text; anything else is described by its size and type.
TEXT_TYPES = ("json", "text/", "x-www-form-urlencoded", "xml", "javascript", "x-ndjson", "event-stream")
# The most rows one query returns.
MAX_LIMIT = 500


def redact(value: Any) -> Any:
    """A JSON value with every field named like a secret replaced."""
    if isinstance(value, dict):
        return {key: REDACTED if SECRET_NAME.search(str(key)) and value[key] not in (None, "") else redact(value[key]) for key in value}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def redact_query(query: str) -> str:
    """A query string with the values of parameters named like secrets replaced."""
    pairs = parse_qsl(query, keep_blank_values=True)
    return urlencode([(key, REDACTED if SECRET_NAME.search(key) else value) for key, value in pairs])


def _headers(raw: list[tuple[bytes, bytes]]) -> dict[str, str]:
    kept: dict[str, str] = {}
    for key, value in raw:
        name = key.decode("latin-1").lower()
        if name in SECRET_HEADERS:
            kept[name] = REDACTED
        else:
            kept[name] = (kept[name] + ", " if name in kept else "") + value.decode("latin-1")
    return kept


def body(data: bytes, total: int, content_type: str) -> dict[str, Any]:
    """A saved body: as JSON or text with secrets redacted, or just its size for anything binary."""
    result: dict[str, Any] = {"bytes": total, "truncated": total > len(data)}
    if not total:
        return result
    kind = content_type.lower()
    if kind and not any(part in kind for part in TEXT_TYPES):
        result["omitted"] = f"{kind.split(';')[0]} body"
        return result
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        if result["truncated"]:
            text = data.decode("utf-8", "ignore")
        else:
            result["omitted"] = "binary body"
            return result
    if "x-www-form-urlencoded" in kind and not result["truncated"]:
        result["text"] = redact_query(text)
        return result
    if not result["truncated"] and ("json" in kind or not kind):
        try:
            result["json"] = redact(json.loads(text))
            return result
        except ValueError:
            pass
    result["text"] = SECRET_JSON_TEXT.sub(lambda match: match.group(1) + f'"{REDACTED}"', text)
    return result


class Capture:
    """One request's headers and bodies, as debug mode saves them."""

    def __init__(self, scope: dict[str, Any]) -> None:
        self.request_headers = _headers(scope.get("headers") or [])
        self.query = redact_query(scope.get("query_string", b"").decode("latin-1"))
        self.request_body = bytearray()
        self.request_total = 0
        self.response_headers: dict[str, str] = {}
        self.response_body = bytearray()
        self.response_total = 0

    def request(self, data: bytes) -> None:
        self.request_total += len(data)
        room = PAYLOAD_LIMIT - len(self.request_body)
        if room > 0:
            self.request_body += data[:room]

    def response_start(self, headers: list[tuple[bytes, bytes]]) -> None:
        self.response_headers = _headers(headers)

    def response(self, data: bytes) -> None:
        self.response_total += len(data)
        room = PAYLOAD_LIMIT - len(self.response_body)
        if room > 0:
            self.response_body += data[:room]

    def result(self) -> dict[str, Any]:
        return {
            "request": {
                "query": self.query, "headers": self.request_headers,
                "body": body(bytes(self.request_body), self.request_total, self.request_headers.get("content-type", "")),
            },
            "response": {
                "headers": self.response_headers,
                "body": body(bytes(self.response_body), self.response_total, self.response_headers.get("content-type", "")),
            },
        }


class RequestLog:
    def __init__(self, db: Any, settings: Callable[[], dict[str, Any]]) -> None:
        self.db = db
        # The request_log setting, read on every request (it can change while the server runs).
        self.settings = settings

    def enabled(self) -> bool:
        return self.db is not None and bool(self.settings().get("enabled"))

    def capture(self, scope: dict[str, Any]) -> Capture | None:
        """A capture for this request's payloads, when debug mode is on and the request may be kept."""
        if scope["type"] != "http" or not self.enabled() or not self.settings().get("payloads"):
            return None
        if scope.get("path", "").startswith(SECRET_PATHS):
            return None
        return Capture(scope)

    def save(self, entry: dict[str, Any], capture: Capture | None = None) -> None:
        if not self.enabled():
            return
        row_id = uuid.uuid4().hex
        saved = {key: value for key, value in entry.items() if key != "id"}
        row = {
            "id": row_id, "time": saved["time"], "method": saved["method"], "path": saved["path"][:2000],
            "status": saved["status"], "outcome": saved["outcome"], "user": saved["user"],
            "search": _haystack(saved) + " " + saved.get("query", "").lower(), "entry": saved, "payload": capture is not None,
        }
        payload = None
        if capture is not None:
            try:
                payload = {"id": row_id, "time": saved["time"], "data": pack(json.dumps(capture.result()).encode("utf-8"))}
            except Exception:  # noqa: BLE001 - a payload that can't be stored mustn't lose the entry
                LOGGER.exception("packing a request log payload failed")
                row["payload"] = False

        def write(connection: Any) -> None:
            connection.execute(schema.request_log.insert().values(**row))
            if payload is not None:
                connection.execute(schema.request_log_payloads.insert().values(**payload))

        try:
            self.db.writer.submit(write)
        except Exception:  # noqa: BLE001 - a log entry must never fail the request it describes
            LOGGER.exception("saving a request log entry failed")

    # Reading (run these off the event loop).

    def query(
        self, search: str = "", method: str = "", status: str = "", user: str = "",
        limit: int = 100, before: float | None = None, since: float | None = None,
    ) -> list[dict[str, Any]]:
        """Saved requests, newest first. status: 2xx, 3xx, 4xx or 5xx (an HTTP status class), failed, or websocket."""
        if self.db is None:
            return []
        self.db.writer.flush()
        table = schema.request_log
        query = select(table.c.id, table.c.entry, table.c.payload).order_by(table.c.time.desc()).limit(max(1, min(limit, MAX_LIMIT)))
        if before is not None:
            query = query.where(table.c.time < before)
        if since is not None:
            query = query.where(table.c.time >= since)
        if method:
            query = query.where(table.c.method == method.upper())
        if user:
            query = query.where(table.c.user == user)
        if status in ("1xx", "2xx", "3xx", "4xx", "5xx"):
            low = int(status[0]) * 100
            query = query.where(table.c.status >= low, table.c.status < low + 100, table.c.method != "WS")
        elif status == "failed":
            query = query.where(table.c.outcome != "ok")
        elif status == "websocket":
            query = query.where(table.c.method == "WS")
        elif status:
            raise ValueError("status is 2xx, 3xx, 4xx, 5xx, failed or websocket")
        for word in search.lower().split():
            query = query.where(table.c.search.contains(word, autoescape=True))
        with self.db.read() as connection:
            return [{**entry, "id": row_id, "payload": bool(payload)} for row_id, entry, payload in connection.execute(query)]

    def get(self, row_id: str) -> dict[str, Any] | None:
        """One saved request, with its payloads when debug mode kept them (and they haven't expired)."""
        if self.db is None:
            return None
        self.db.writer.flush()
        with self.db.read() as connection:
            row = connection.execute(select(schema.request_log.c.entry, schema.request_log.c.payload).where(schema.request_log.c.id == row_id)).first()
            if row is None:
                return None
            data = connection.execute(select(schema.request_log_payloads.c.data).where(schema.request_log_payloads.c.id == row_id)).scalar()
        entry, had_payload = row
        return {
            **entry, "id": row_id, "payload": bool(had_payload),
            "payloads": json.loads(unpack(data)) if data else None,
        }

    def summary(self) -> dict[str, Any]:
        """What's saved: how many requests and payloads, the oldest of each, and the settings."""
        settings = dict(self.settings())
        if self.db is None:
            return {"count": 0, "oldest": None, "payloads": 0, "oldest_payload": None, "settings": settings}
        self.db.writer.flush()
        with self.db.read() as connection:
            oldest, count = connection.execute(select(func.min(schema.request_log.c.time), func.count()).select_from(schema.request_log)).one()
            oldest_payload, payloads = connection.execute(
                select(func.min(schema.request_log_payloads.c.time), func.count()).select_from(schema.request_log_payloads)
            ).one()
        return {"count": count, "oldest": oldest, "payloads": payloads, "oldest_payload": oldest_payload, "settings": settings}

    def prune(self) -> int:
        """Requests older than `days` and payloads older than `payload_days` (whichever is sooner, for payloads)."""
        if self.db is None:
            return 0
        settings = self.settings()
        now = time.time()
        days = int(settings.get("days") or 1)
        payload_days = min(days, int(settings.get("payload_days") or 1))
        with self.db.write() as connection:
            removed = connection.execute(delete(schema.request_log).where(schema.request_log.c.time < now - days * 86400)).rowcount or 0
            connection.execute(delete(schema.request_log_payloads).where(schema.request_log_payloads.c.time < now - payload_days * 86400))
        return removed
