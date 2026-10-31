from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import func, select

from .db import Database, kv, pack, schema, unpack


# Session ids appear in URLs and once named folders, so they can't hold path separators or start with a dot.
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Session:
    session_id: str
    client: str
    project: str | None
    model: str | None
    backend: str
    trace: bool = False
    started_at: str = field(default_factory=utc_now)
    ended_at: str | None = None
    exit_status: int | None = None
    tags: dict[str, Any] = field(default_factory=dict)
    request_count: int = 0
    request_sequence: int = 0
    last_activity_at: str | None = None
    mcp_token_hash: str | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("mcp_token_hash")
        return value

    def stored(self) -> dict[str, Any]:
        return asdict(self)


class SessionStore:
    """Sessions and what they recorded: request records, traces, chat indexes (the database's tables).

    Sessions are few and changed on every request, so memory holds them all and is authoritative;
    each change is written behind (db.writer), in order. Reads of records and traces flush first,
    so they see every request that has finished.
    """

    def __init__(self, db: Database):
        self.db = db
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._load_existing()

    def _load_existing(self) -> None:
        with self.db.read() as connection:
            rows = connection.execute(select(schema.sessions.c.data)).all()
        for (data,) in rows:
            try:
                session = Session(**{k: v for k, v in data.items() if k in Session.__dataclass_fields__})
            except TypeError:
                continue
            self._sessions[session.session_id] = session

    def create(self, data: dict[str, Any], *, mcp_token_hash: str | None = None) -> Session:
        session_id = data.get("session_id") or str(uuid.uuid4())
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            raise ValueError("A session id may only contain letters, numbers, '.', '_' and '-'")
        if session_id in self._sessions:
            raise ValueError(f"Session already exists: {session_id}")
        session = Session(
            session_id=session_id,
            client=data.get("client", "unknown"),
            project=data.get("project"),
            model=data.get("model"),
            backend=data.get("backend", "laptop"),
            trace=bool(data.get("trace", False)),
            tags=data.get("tags") or {},
            mcp_token_hash=mcp_token_hash,
        )
        self._sessions[session_id] = session
        self._locks[session_id] = asyncio.Lock()
        self._write_session(session)
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def list(self) -> list[Session]:
        return sorted(self._sessions.values(), key=lambda item: item.started_at, reverse=True)

    async def next_request_id(self, session: Session) -> str:
        lock = self._locks.setdefault(session.session_id, asyncio.Lock())
        async with lock:
            session.request_sequence = max(session.request_sequence, session.request_count) + 1
            session.request_count += 1
            session.last_activity_at = utc_now()
            self._write_session(session)
            return f"{session.request_sequence:06d}"

    def end(self, session: Session, exit_status: int | None = None) -> None:
        session.ended_at = utc_now()
        session.exit_status = exit_status
        self._write_session(session)

    def touch(self, session: Session) -> None:
        """Record that traffic for this session just finished."""
        session.last_activity_at = utc_now()
        self._write_session(session)

    def update_tags(self, session: Session, tags: dict[str, Any]) -> None:
        session.tags = {**session.tags, **tags}
        self._write_session(session)

    def set_trace(self, session: Session, enabled: bool) -> None:
        session.trace = enabled
        self._write_session(session)

    def clear_data(self, session: Session) -> None:
        session_id = session.session_id

        def clear(connection: Any) -> None:
            for table in (schema.requests, schema.traces, schema.chat_index):
                connection.execute(table.delete().where(table.c.session_id == session_id))

        self.db.writer.submit(clear)
        session.request_sequence = max(session.request_sequence, session.request_count)
        session.request_count = 0
        self._write_session(session)
        self.db.writer.flush()

    def delete(self, session: Session) -> None:
        session_id = session.session_id

        def delete(connection: Any) -> None:
            connection.execute(schema.sessions.delete().where(schema.sessions.c.id == session_id))
            for table in SESSION_TABLES:
                connection.execute(table.delete().where(table.c.session_id == session_id))

        self.db.writer.submit(delete)
        self.db.writer.flush()
        self._sessions.pop(session_id, None)
        self._locks.pop(session_id, None)

    def append_request(self, session: Session, record: dict[str, Any]) -> None:
        row = {
            "session_id": session.session_id, "request_id": str(record.get("request_id") or ""),
            "timestamp": str(record.get("timestamp") or utc_now()), "kind": record.get("kind"),
            "model": record.get("model"), "backend": record.get("backend"),
            "input_tokens": int(record.get("input_tokens") or 0), "output_tokens": int(record.get("output_tokens") or 0),
            "record": record,
        }
        self.db.writer.submit(lambda connection: kv.upsert(connection, schema.requests, row))

    def write_trace(self, session: Session, request_id: str, trace: dict[str, Any]) -> None:
        body = json.dumps(trace, ensure_ascii=False).encode("utf-8")
        row = {"session_id": session.session_id, "request_id": request_id, "size": len(body), "body": pack(body)}
        self.db.writer.submit(lambda connection: kv.upsert(connection, schema.traces, row))

    def traces(self, session: Session) -> list[str]:
        """The session's traces, newest first, named as the files they once were (NNNNNN.json)."""
        self.db.writer.flush()
        with self.db.read() as connection:
            ids = connection.execute(
                select(schema.traces.c.request_id).where(schema.traces.c.session_id == session.session_id)
            ).scalars().all()
        return sorted((f"{request_id}.json" for request_id in ids), reverse=True)

    def read_trace(self, session: Session, request_id: str) -> dict[str, Any] | None:
        if not request_id.isdigit():
            return None
        self.db.writer.flush()
        with self.db.read() as connection:
            body = connection.execute(select(schema.traces.c.body).where(
                schema.traces.c.session_id == session.session_id, schema.traces.c.request_id == request_id,
            )).scalar()
        if body is None:
            return None
        try:
            value = json.loads(unpack(body))
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def read_traces(self, session: Session, request_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Several traces at once, by request id; ones that aren't there are left out."""
        wanted = [request_id for request_id in dict.fromkeys(request_ids) if request_id.isdigit()]
        self.db.writer.flush()
        result: dict[str, dict[str, Any]] = {}
        for start in range(0, len(wanted), 100):
            with self.db.read() as connection:
                rows = connection.execute(select(schema.traces.c.request_id, schema.traces.c.body).where(
                    schema.traces.c.session_id == session.session_id, schema.traces.c.request_id.in_(wanted[start:start + 100]),
                )).all()
            for request_id, body in rows:
                try:
                    value = json.loads(unpack(body))
                except ValueError:
                    continue
                if isinstance(value, dict):
                    result[request_id] = value
        return result

    def records(self, session: Session) -> list[dict[str, Any]]:
        """The session's request records, oldest first."""
        self.db.writer.flush()
        with self.db.read() as connection:
            rows = connection.execute(
                select(schema.requests.c.record).where(schema.requests.c.session_id == session.session_id)
                .order_by(schema.requests.c.timestamp, schema.requests.c.request_id)
            ).scalars().all()
        return [record for record in rows if isinstance(record, dict)]

    def telemetry(self, session: Session) -> str:
        """The records as newline-delimited JSON (the download, and what requests.jsonl held)."""
        return "".join(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n" for record in self.records(session))

    def query_requests(
        self, session_ids: Iterable[str] | None = None, *, limit: int | None = None, before: str | None = None,
        model: str | None = None, kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """Request records, newest first, of these sessions (all with None), before a timestamp, of a model or kind."""
        self.db.writer.flush()
        query = select(schema.requests.c.session_id, schema.requests.c.record).order_by(
            schema.requests.c.timestamp.desc(), schema.requests.c.request_id.desc(),
        )
        if session_ids is not None:
            wanted = list(session_ids)
            if not wanted:
                return []
            query = query.where(schema.requests.c.session_id.in_(wanted))
        if before:
            query = query.where(schema.requests.c.timestamp < before)
        if model:
            query = query.where(schema.requests.c.model == model)
        if kind:
            query = query.where(schema.requests.c.kind == kind)
        if limit:
            query = query.limit(limit)
        with self.db.read() as connection:
            rows = connection.execute(query).all()
        return [{**record, "session_id": session_id} for session_id, record in rows if isinstance(record, dict)]

    def traced(self, keys: Iterable[tuple[str, str]]) -> set[tuple[str, str]]:
        """Which of these (session, request) pairs have a trace."""
        wanted = list(keys)
        sessions = {session_id for session_id, _ in wanted}
        if not sessions:
            return set()
        with self.db.read() as connection:
            rows = connection.execute(
                select(schema.traces.c.session_id, schema.traces.c.request_id).where(schema.traces.c.session_id.in_(sessions))
            ).all()
        found = {(session_id, request_id) for session_id, request_id in rows}
        return found & set(wanted)

    def totals(self) -> tuple[int, int]:
        """Input and output tokens of every recorded request."""
        self.db.writer.flush()
        with self.db.read() as connection:
            row = connection.execute(select(
                func.coalesce(func.sum(schema.requests.c.input_tokens), 0), func.coalesce(func.sum(schema.requests.c.output_tokens), 0),
            )).one()
        return int(row[0]), int(row[1])

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        """The newest request records, oldest first."""
        return self.query_requests(limit=limit)[::-1]

    def prune(self, before: str) -> int:
        """Delete the traces, chat indexes and terminal recordings of sessions last active before this timestamp."""
        old = [
            session.session_id for session in self._sessions.values()
            if (session.last_activity_at or session.ended_at or session.started_at) < before
        ]
        if not old:
            return 0

        def prune(connection: Any) -> None:
            for table in (schema.traces, schema.chat_index, schema.terminal_chunks):
                connection.execute(table.delete().where(table.c.session_id.in_(old)))

        self.db.writer.submit(prune)
        self.db.writer.flush()
        return len(old)

    def _write_session(self, session: Session) -> None:
        row = {"id": session.session_id, "started_at": session.started_at, "data": session.stored()}
        self.db.writer.submit(lambda connection: kv.upsert(connection, schema.sessions, row))


# Every table that holds a session's data, by session_id.
SESSION_TABLES = (schema.requests, schema.traces, schema.chat_index, schema.terminals, schema.terminal_chunks)
