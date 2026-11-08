from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import case, func, or_, select

from . import blobs as blob_store
from .cluster import Cluster, LocalCluster
from .db import Database, kv, pack, schema, unpack


# Session ids appear in URLs and once named folders, so they can't hold path separators or start with a dot.
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
# With several processes: sessions active within this many days are listed (older ones are still found by id).
LIST_DAYS = int(os.getenv("AI_PROXY_SESSION_LIST_DAYS") or 30)
# How many sessions a process keeps in memory when it isn't the only one (the rest are read when asked for).
CACHE_LIMIT = 20_000
# How long a shared request counter lives without use; one used again later starts from the database.
COUNTER_TTL = 7 * 24 * 3600
LIST_CACHE_SECONDS = 3.0


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


DOCUMENT_FIELDS = tuple(Session.__dataclass_fields__)


class SessionStore:
    """Sessions and what they recorded: request records, traces, chat indexes (the database's tables).

    One process (single-node mode): memory holds every session and is authoritative; each change is
    written behind (db.writer), in order. Reads of records and traces flush first, so they see every
    request that has finished.

    Several processes (cluster.scaled): the database is authoritative and each process keeps the
    sessions it uses. A new session is written at once, so every process finds it. Changes to a
    session's document (sharing, tags, ending it) are read, changed and written in one transaction
    and announced (session.changed), and every process updates its copy in place, so code holding a
    Session (a terminal viewer, a streaming request) sees sharing narrow straight away. Request ids
    come from a shared counter, and the counters are columns each process only ever raises, so none
    writes another's changes back as it last saw them.

    Traces go to blob storage when there is one (blobs.py): messages once per session, by hash.
    """

    def __init__(self, db: Database, cluster: Cluster | None = None, blobs: blob_store.Blobs | None = None):
        self.db = db
        self.cluster = cluster or LocalCluster()
        self.scaled = self.cluster.scaled
        self.blobs = blobs
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock = threading.RLock()
        self._listed: tuple[float, list[Session]] | None = None
        # Blocks (messages) already stored per session, so each goes to blob storage once from here.
        self._blocks: dict[str, set[str]] = {}
        # Uploads not finished yet, which flush waits for.
        self._pending: set[Any] = set()
        self._uploads = ThreadPoolExecutor(max_workers=int(os.getenv("AI_PROXY_BLOB_THREADS") or 8), thread_name_prefix="blob") if blobs else None
        self.cluster.on("session.changed", lambda data, _: self._refresh(str(data.get("id") or "")))
        self.cluster.on("session.deleted", lambda data, _: self._forget(str(data.get("id") or "")))
        # Others told when this process's copy of a session changed (terminal viewers recheck).
        self.listeners: list[Any] = []
        # Set by the runtime (retention.py): a new session's data with its organization's policy, and whether
        # its tracing may change (raises ValueError when it may not).
        self.stamp: Any = None
        self.trace_check: Any = None
        self._load_existing()

    # Loading

    def _load_existing(self) -> None:
        query = select(schema.sessions)
        if self.scaled:
            # Only recent ones: the rest are read when someone asks for them.
            cutoff = _days_ago(LIST_DAYS)
            query = query.where(or_(schema.sessions.c.last_activity_at >= cutoff, schema.sessions.c.started_at >= cutoff))
        with self.db.read() as connection:
            rows = connection.execute(query).mappings().all()
        for row in rows:
            session = _from_row(row)
            if session is not None:
                self._sessions[session.session_id] = session
        self._trim()

    def _fetch(self, session_id: str) -> Session | None:
        if not session_id or not SESSION_ID.fullmatch(session_id):
            return None
        with self.db.read() as connection:
            row = connection.execute(select(schema.sessions).where(schema.sessions.c.id == session_id)).mappings().first()
        return _from_row(row) if row is not None else None

    def _remember(self, session: Session) -> Session:
        with self._lock:
            known = self._sessions.get(session.session_id)
            if known is not None:
                _copy_into(known, session)
                self._sessions.move_to_end(session.session_id)
                return known
            self._sessions[session.session_id] = session
            self._trim()
            return session

    def _trim(self) -> None:
        if not self.scaled:
            return
        while len(self._sessions) > CACHE_LIMIT:
            # The least recently used ended session goes first; open ones are kept.
            victim = next((key for key, item in self._sessions.items() if item.ended_at is not None), None)
            if victim is None:
                break
            self._sessions.pop(victim, None)
            self._locks.pop(victim, None)

    def _refresh(self, session_id: str) -> None:
        """Another process changed this session: read it again, into the copy code here may be holding."""
        if not session_id:
            return
        with self._lock:
            known = self._sessions.get(session_id)
        if known is None:
            return
        fresh = self._fetch(session_id)
        if fresh is None:
            self._forget(session_id)
            return
        _copy_into(known, fresh)
        self._listed = None
        for listener in list(self.listeners):
            try:
                listener(known)
            except Exception:  # noqa: BLE001
                pass

    def _forget(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)
            self._locks.pop(session_id, None)
        self._listed = None

    # Sessions

    def create(self, data: dict[str, Any], *, mcp_token_hash: str | None = None) -> Session:
        session_id = data.get("session_id") or str(uuid.uuid4())
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            raise ValueError("A session id may only contain letters, numbers, '.', '_' and '-'")
        if self.get(session_id) is not None:
            raise ValueError(f"Session already exists: {session_id}")
        if self.stamp is not None:
            data = self.stamp(data)
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
        with self._lock:
            self._sessions[session_id] = session
            self._locks[session_id] = asyncio.Lock()
        if self.scaled:
            # Written now, not behind: the agent's first request may reach another process.
            with self.db.write() as connection:
                kv.upsert(connection, schema.sessions, _row(session))
            self.cluster.publish("session.changed", {"id": session_id})
            self._listed = None
        else:
            self._write_session(session)
        return session

    def get(self, session_id: str) -> Session | None:
        session = self._sessions.get(session_id)
        if session is not None or not self.scaled:
            return session
        fetched = self._fetch(session_id)
        return self._remember(fetched) if fetched is not None else None

    def list(self) -> list[Session]:
        if not self.scaled:
            return sorted(self._sessions.values(), key=lambda item: item.started_at, reverse=True)
        now = asyncio.get_event_loop().time() if _in_loop() else 0.0
        if self._listed and now and now - self._listed[0] < LIST_CACHE_SECONDS:
            return self._listed[1]
        cutoff = _days_ago(LIST_DAYS)
        with self.db.read() as connection:
            rows = connection.execute(select(schema.sessions).where(
                or_(schema.sessions.c.last_activity_at >= cutoff, schema.sessions.c.started_at >= cutoff)
            )).mappings().all()
        listed = {}
        for row in rows:
            session = _from_row(row)
            if session is None:
                continue
            known = self._sessions.get(session.session_id)
            if known is not None:
                _copy_into(known, session)
                session = known
            listed[session.session_id] = session
        for session in list(self._sessions.values()):
            if session.ended_at is None:
                listed.setdefault(session.session_id, session)
        result = sorted(listed.values(), key=lambda item: item.started_at, reverse=True)
        if now:
            self._listed = (now, result)
        return result

    async def next_request_id(self, session: Session) -> str:
        lock = self._locks.setdefault(session.session_id, asyncio.Lock())
        async with lock:
            if self.scaled:
                sequence, count = self._shared_counts(session)
                session.request_sequence = max(session.request_sequence, sequence)
                session.request_count = max(session.request_count, count)
                session.last_activity_at = utc_now()
                self._write_counters(session)
            else:
                session.request_sequence = max(session.request_sequence, session.request_count) + 1
                session.request_count += 1
                session.last_activity_at = utc_now()
                self._write_session(session)
            return f"{session.request_sequence:06d}"

    def raise_counts(self, session: Session, count: int) -> None:
        """Records that came from elsewhere (a linked server's launch, mirrored here: bridges.py) with their own ids:
        the counters reach the highest, so the session's request count is right and later ids don't collide."""
        if count <= max(session.request_count, session.request_sequence):
            return
        session.request_count = session.request_sequence = count
        session.last_activity_at = utc_now()
        self._write_counters(session)
        if self.scaled:
            base = f"session:{session.session_id}"
            self.cluster.set(f"{base}:seq", str(count), COUNTER_TTL)
            self.cluster.set(f"{base}:count", str(count), COUNTER_TTL)

    def _shared_counts(self, session: Session) -> tuple[int, int]:
        """The next request number and the count, from counters every process shares."""
        base = f"session:{session.session_id}"
        # Seeded from what's recorded the first time (or after the counter expired), then only raised.
        self.cluster.set(f"{base}:seq", str(max(session.request_sequence, session.request_count)), COUNTER_TTL, nx=True)
        self.cluster.set(f"{base}:count", str(session.request_count), COUNTER_TTL, nx=True)
        return self.cluster.incr(f"{base}:seq", 1, COUNTER_TTL), self.cluster.incr(f"{base}:count", 1, COUNTER_TTL)

    def end(self, session: Session, exit_status: int | None = None) -> None:
        ended = utc_now()

        def change(item: Session) -> None:
            item.ended_at = ended
            item.exit_status = exit_status

        self._change(session, change)

    def touch(self, session: Session) -> None:
        """Record that traffic for this session just finished."""
        session.last_activity_at = utc_now()
        if self.scaled:
            self._write_counters(session)
        else:
            self._write_session(session)

    def update_tags(self, session: Session, tags: dict[str, Any]) -> None:
        self._change(session, lambda item: setattr(item, "tags", {**item.tags, **tags}))

    def set_trace(self, session: Session, enabled: bool) -> None:
        if self.trace_check is not None:
            self.trace_check(session, enabled)
        self._change(session, lambda item: setattr(item, "trace", enabled))

    def _change(self, session: Session, mutate: Any, counters: bool = False) -> None:
        """A change to the session's document. With several processes: read, changed and written in one
        transaction (so another process's change in between isn't lost), then announced."""
        if not self.scaled:
            mutate(session)
            self._write_session(session)
            return
        self.db.writer.flush()
        with self.db.write() as connection:
            row = connection.execute(
                select(schema.sessions).where(schema.sessions.c.id == session.session_id).with_for_update()
            ).mappings().first()
            current = _from_row(row) if row is not None else None
            if current is None:
                current = Session(**{key: getattr(session, key) for key in DOCUMENT_FIELDS})
            mutate(current)
            values = _row(current)
            if not counters:
                # Counters only ever go up here; clear_data, which lowers one, passes counters=True.
                for name in ("request_sequence", "request_count"):
                    values[name] = max(int(values[name] or 0), int((row or {}).get(name) or 0))
            kv.upsert(connection, schema.sessions, values)
        _copy_into(session, current)
        if counters:
            self.cluster.set(f"session:{session.session_id}:count", str(session.request_count), COUNTER_TTL)
        self._listed = None
        self.cluster.publish("session.changed", {"id": session.session_id})

    def clear_data(self, session: Session) -> None:
        session_id = session.session_id
        keys = self._blob_keys([session])

        def clear(connection: Any) -> None:
            for table in (schema.requests, schema.traces, schema.chat_index):
                connection.execute(table.delete().where(table.c.session_id == session_id))
            connection.execute(schema.sessions.update().where(schema.sessions.c.id == session_id).values(input_tokens=0, output_tokens=0))

        self.db.writer.submit(clear)
        self.db.writer.flush()
        self._delete_blobs(keys, traces_only=True)

        def reset(item: Session) -> None:
            item.request_sequence = max(item.request_sequence, item.request_count)
            item.request_count = 0

        self._change(session, reset, counters=True)
        self.db.writer.flush()

    def delete(self, session: Session) -> None:
        self.delete_sessions([session.session_id])

    def delete_sessions(self, session_ids: list[str]) -> int:
        """Remove sessions and everything they recorded."""
        if not session_ids:
            return 0
        sessions = [item for item in (self.get(session_id) for session_id in session_ids) if item is not None]
        keys = self._blob_keys(sessions)

        def delete(connection: Any) -> None:
            for start in range(0, len(session_ids), 500):
                chunk = session_ids[start:start + 500]
                connection.execute(schema.sessions.delete().where(schema.sessions.c.id.in_(chunk)))
                for table in SESSION_TABLES:
                    connection.execute(table.delete().where(table.c.session_id.in_(chunk)))

        self.db.writer.submit(delete)
        self.db.writer.flush()
        self._delete_blobs(keys)
        for session_id in session_ids:
            self._forget(session_id)
            self._blocks.pop(session_id, None)
            if self.scaled:
                self.cluster.delete(f"session:{session_id}:seq", f"session:{session_id}:count")
                self.cluster.publish("session.deleted", {"id": session_id})
        return len(session_ids)

    # Recording

    def append_request(self, session: Session, record: dict[str, Any]) -> None:
        input_tokens, output_tokens = int(record.get("input_tokens") or 0), int(record.get("output_tokens") or 0)
        row = {
            "session_id": session.session_id, "request_id": str(record.get("request_id") or ""),
            "timestamp": str(record.get("timestamp") or utc_now()), "kind": record.get("kind"),
            "model": record.get("model"), "backend": record.get("backend"),
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "record": record,
        }
        sessions = schema.sessions

        def write(connection: Any) -> None:
            kv.upsert(connection, schema.requests, row)
            if input_tokens or output_tokens:
                connection.execute(sessions.update().where(sessions.c.id == session.session_id).values(
                    input_tokens=func.coalesce(sessions.c.input_tokens, 0) + input_tokens,
                    output_tokens=func.coalesce(sessions.c.output_tokens, 0) + output_tokens,
                ))

        self.db.writer.submit(write)

    def write_trace(self, session: Session, request_id: str, trace: dict[str, Any]) -> None:
        body = json.dumps(trace, ensure_ascii=False).encode("utf-8")
        if self.blobs is None or self._uploads is None:
            row = {"session_id": session.session_id, "request_id": request_id, "size": len(body), "body": pack(body), "location": None}
            self.db.writer.submit(lambda connection: kv.upsert(connection, schema.traces, row))
            return
        prefix = blob_store.session_prefix(session.tags.get("tenant"), session.session_id, session.started_at)
        location = f"{prefix}/traces/{request_id}.json.gz"
        size = len(body)
        blocks = self._blocks.setdefault(session.session_id, set())
        store = self.blobs

        def upload() -> None:
            def stored(name: str) -> bool:
                return name in blocks

            def put(name: str, data: bytes) -> None:
                store.put(f"{prefix}/blocks/{name}", data)
                blocks.add(name)

            store.put(location, blob_store.pack_trace(trace, stored, put))
            row = {"session_id": session.session_id, "request_id": request_id, "size": size, "body": b"", "location": location}
            self.db.writer.submit(lambda connection: kv.upsert(connection, schema.traces, row))

        self._track(self._uploads.submit(upload))

    def _track(self, future: Any) -> None:
        self._pending.add(future)
        future.add_done_callback(self._pending.discard)
        future.add_done_callback(_log_failure)

    def flush(self) -> None:
        """Wait for what's being written: blob uploads, then the database's queue."""
        if self._uploads is not None:
            # Uploads finish in their own time; a read that must see them waits for them, then the queue they feed.
            from concurrent.futures import wait

            wait(list(self._pending), timeout=60)
        self.db.writer.flush()

    def _trace_body(self, row: Any, session: Session) -> dict[str, Any] | None:
        location = row.get("location") if hasattr(row, "get") else None
        if location and self.blobs is not None:
            data = self.blobs.get(location)
            if data is None:
                return None
            prefix = location.rsplit("/traces/", 1)[0]
            return blob_store.unpack_trace(data, lambda name: self.blobs.get(f"{prefix}/blocks/{name}") if self.blobs else None)
        try:
            value = json.loads(unpack(row["body"]))
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def traces(self, session: Session) -> list[str]:
        """The session's traces, newest first, named as the files they once were (NNNNNN.json)."""
        self.flush()
        with self.db.read() as connection:
            ids = connection.execute(
                select(schema.traces.c.request_id).where(schema.traces.c.session_id == session.session_id)
            ).scalars().all()
        return sorted((f"{request_id}.json" for request_id in ids), reverse=True)

    def read_trace(self, session: Session, request_id: str) -> dict[str, Any] | None:
        if not request_id.isdigit():
            return None
        self.flush()
        with self.db.read() as connection:
            row = connection.execute(select(schema.traces.c.body, schema.traces.c.location).where(
                schema.traces.c.session_id == session.session_id, schema.traces.c.request_id == request_id,
            )).mappings().first()
        if row is None:
            return None
        return self._trace_body(row, session)

    def read_traces(self, session: Session, request_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Several traces at once, by request id; ones that aren't there are left out."""
        wanted = [request_id for request_id in dict.fromkeys(request_ids) if request_id.isdigit()]
        self.flush()
        result: dict[str, dict[str, Any]] = {}
        for start in range(0, len(wanted), 100):
            with self.db.read() as connection:
                rows = connection.execute(select(schema.traces.c.request_id, schema.traces.c.body, schema.traces.c.location).where(
                    schema.traces.c.session_id == session.session_id, schema.traces.c.request_id.in_(wanted[start:start + 100]),
                )).mappings().all()
            values = blob_store.parallel([lambda row=row: self._trace_body(row, session) for row in rows])
            for row, value in zip(rows, values):
                if isinstance(value, dict):
                    result[row["request_id"]] = value
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

    def totals(self) -> dict[str, list[int]]:
        """Input and output tokens of every recorded request, by session (status adds up the ones its caller may see)."""
        self.db.writer.flush()
        sessions = schema.sessions
        query = select(sessions.c.id, func.coalesce(sessions.c.input_tokens, 0), func.coalesce(sessions.c.output_tokens, 0))
        if self.scaled:
            cutoff = _days_ago(LIST_DAYS)
            query = query.where(or_(sessions.c.last_activity_at >= cutoff, sessions.c.started_at >= cutoff))
        with self.db.read() as connection:
            rows = connection.execute(query).all()
        return {str(row[0]): [int(row[1]), int(row[2])] for row in rows if row[1] or row[2]}

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        """The newest request records, oldest first."""
        return self.query_requests(limit=limit)[::-1]

    # Retention

    def inactive_since(self, before: str, limit: int = 5000) -> list[Session]:
        """Sessions last active (or started, or ended) before this timestamp: from memory when it's authoritative
        (one process), else from the database."""
        if not self.scaled:
            with self._lock:
                found = [item for item in self._sessions.values() if (item.last_activity_at or item.ended_at or item.started_at) < before]
            return found[:limit]
        self.db.writer.flush()
        sessions = schema.sessions
        activity = func.coalesce(sessions.c.last_activity_at, sessions.c.started_at)
        with self.db.read() as connection:
            rows = connection.execute(select(sessions).where(activity < before).limit(limit)).mappings().all()
        found = [_from_row(row) for row in rows]
        return [item for item in found if item is not None and (item.last_activity_at or item.ended_at or item.started_at) < before]

    def prune(self, before: str) -> int:
        """Delete the traces, chat indexes and terminal recordings of sessions last active before this timestamp."""
        old = [session for session in self.inactive_since(before, limit=1_000_000)]
        return self.delete_recordings(old, ("traces", "chat", "terminal"))

    def delete_recordings(self, sessions: list[Session], kinds: Iterable[str]) -> int:
        """Delete some of what these sessions recorded: traces (and their chat indexes), chat, terminal output."""
        kinds = set(kinds)
        if not sessions or not kinds:
            return 0
        ids = [session.session_id for session in sessions]
        tables = []
        if "traces" in kinds:
            tables += [schema.traces, schema.chat_index]
        if "chat" in kinds:
            tables.append(schema.chat_index)
        if "terminal" in kinds:
            tables.append(schema.terminal_chunks)
        keys = self._blob_keys(sessions) if self.blobs is not None else []

        def prune(connection: Any) -> None:
            for start in range(0, len(ids), 500):
                for table in dict.fromkeys(tables):
                    connection.execute(table.delete().where(table.c.session_id.in_(ids[start:start + 500])))

        self.db.writer.submit(prune)
        self.db.writer.flush()
        if "traces" in kinds:
            self._delete_blobs(keys, traces_only="terminal" not in kinds)
            for session_id in ids:
                self._blocks.pop(session_id, None)
        elif "terminal" in kinds:
            self._delete_blobs([key + "/terminal" for key in keys])
        return len(ids)

    def _blob_keys(self, sessions: list[Session]) -> list[str]:
        if self.blobs is None:
            return []
        return [blob_store.session_prefix(session.tags.get("tenant"), session.session_id, session.started_at) for session in sessions]

    def _delete_blobs(self, prefixes: list[str], traces_only: bool = False) -> None:
        if self.blobs is None or not prefixes:
            return
        store = self.blobs
        if traces_only:
            calls = [lambda prefix=prefix, part=part: store.delete_prefix(f"{prefix}/{part}") for prefix in prefixes for part in ("traces", "blocks")]
        else:
            calls = [lambda prefix=prefix: store.delete_prefix(prefix) for prefix in prefixes]
        blob_store.parallel(calls)

    def _write_session(self, session: Session) -> None:
        row = _row(session)
        self.db.writer.submit(lambda connection: kv.upsert(connection, schema.sessions, row))

    def _write_counters(self, session: Session) -> None:
        """Raise the session's counters in the database (never lower them: another process may be ahead)."""
        sessions = schema.sessions
        sequence, count, activity = session.request_sequence, session.request_count, session.last_activity_at or ""

        def raised(column: Any, value: Any, empty: Any) -> Any:
            return case((func.coalesce(column, empty) < value, value), else_=column)

        def write(connection: Any) -> None:
            connection.execute(sessions.update().where(sessions.c.id == session.session_id).values(
                request_sequence=raised(sessions.c.request_sequence, sequence, 0),
                request_count=raised(sessions.c.request_count, count, 0),
                last_activity_at=raised(sessions.c.last_activity_at, activity, ""),
            ))

        self.db.writer.submit(write)

    def close(self) -> None:
        if self._uploads is not None:
            self._uploads.shutdown(wait=True)


def _row(session: Session) -> dict[str, Any]:
    return {
        "id": session.session_id, "started_at": session.started_at, "data": session.stored(),
        "request_sequence": session.request_sequence, "request_count": session.request_count,
        "last_activity_at": session.last_activity_at,
    }


def _from_row(row: Any) -> Session | None:
    data = row["data"] if isinstance(row["data"], dict) else {}
    try:
        session = Session(**{key: value for key, value in data.items() if key in Session.__dataclass_fields__})
    except TypeError:
        return None
    # The columns are the counters' truth (another process may have raised them since the document was written).
    for name in ("request_sequence", "request_count"):
        value = row.get(name) if hasattr(row, "get") else None
        if value is not None:
            setattr(session, name, max(int(value), int(getattr(session, name) or 0)))
    activity = row.get("last_activity_at") if hasattr(row, "get") else None
    if activity and (session.last_activity_at or "") < activity:
        session.last_activity_at = activity
    return session


def _copy_into(target: Session, source: Session) -> None:
    for name in DOCUMENT_FIELDS:
        setattr(target, name, getattr(source, name))


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _in_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _log_failure(future: Any) -> None:
    exc = future.exception()
    if exc is not None:
        import logging

        logging.getLogger("agent-proxy").warning("a trace couldn't be stored: %s", exc)


# Every table that holds a session's data, by session_id.
SESSION_TABLES = (schema.requests, schema.traces, schema.chat_index, schema.terminals, schema.terminal_chunks)
