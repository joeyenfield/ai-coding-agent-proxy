"""The server's database: PostgreSQL in production, SQLite for development, tests and checkouts.

Everything agent-server keeps lives here: its configuration, accounts, secrets, the fleet,
and every session's records, traces and terminal output. SQLAlchemy Core keeps one set of
queries for both dialects; nothing outside this package writes dialect-specific SQL.

Hot-path writes (a request's record, its trace, terminal output, session counters, machine
updates) go through Writer: one thread, one ordered queue, short batched transactions, so
the event loop never waits on a database round trip. Readers that must see those writes
call Writer.flush() first.
"""

from __future__ import annotations

import gzip
import logging
import queue
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine, make_url

LOGGER = logging.getLogger("agent-proxy")

# The PostgreSQL schemas: the API's tables and the issuer's (agent-auth), each with its own role.
API_SCHEMA = "mesh"
AUTH_SCHEMA = "mesh_auth"


def sqlite_url(path: Path) -> str:
    """A SQLite file's URL (Windows drive letters and spaces included), readable as it is."""
    return "sqlite:///" + Path(path).resolve().as_posix()


def redacted(url: str) -> str:
    """The URL with its password hidden, for logs and `agent-server db info`."""
    return make_url(url).render_as_string(hide_password=True)


def pack(value: bytes) -> bytes:
    """Traces and similar large documents are stored gzipped: they're JSON, and compress about tenfold."""
    return gzip.compress(value, compresslevel=5)


def unpack(value: bytes | None) -> bytes:
    return gzip.decompress(value) if value else b""


class Database:
    """One engine, its schemas (in PostgreSQL) and the background writer."""

    def __init__(self, url: str, *, readonly: bool = False):
        self.url = url
        parsed = make_url(url)
        self.sqlite = parsed.get_backend_name() == "sqlite"
        self.readonly = readonly
        options: dict[str, Any] = {"future": True}
        if self.sqlite:
            database = parsed.database or ""
            if database and database != ":memory:" and not database.startswith("file:"):
                Path(database).parent.mkdir(parents=True, exist_ok=True)
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
        else:
            options["pool_pre_ping"] = True
            options["pool_size"] = 5
            options["max_overflow"] = 10
        engine = create_engine(parsed, **options)
        # SQLite has no schemas: both sets of tables live in the file's main schema.
        translate = {API_SCHEMA: None, AUTH_SCHEMA: None} if self.sqlite else {API_SCHEMA: API_SCHEMA, AUTH_SCHEMA: AUTH_SCHEMA}
        self.engine: Engine = engine.execution_options(schema_translate_map=translate)
        if self.sqlite:
            _sqlite_events(engine, readonly)
        self.writer = Writer(self)

    # Transactions

    @contextmanager
    def write(self) -> Iterator[Connection]:
        """A read-write transaction. In SQLite it takes the write lock at once (BEGIN IMMEDIATE),
        so two writers wait for each other rather than failing on upgrade."""
        with self.engine.connect().execution_options(immediate=True) as connection:
            with connection.begin():
                yield connection

    @contextmanager
    def read(self) -> Iterator[Connection]:
        with self.engine.connect() as connection:
            with connection.begin():
                yield connection

    def size(self) -> int | None:
        """The database's size in bytes, for the server's stats page."""
        try:
            if self.sqlite:
                database = make_url(self.url).database or ""
                return sum(Path(database + suffix).stat().st_size for suffix in ("", "-wal") if Path(database + suffix).exists())
            with self.read() as connection:
                return int(connection.execute(text("SELECT pg_database_size(current_database())")).scalar_one())
        except Exception:  # noqa: BLE001 - stats are best effort
            return None

    def close(self) -> None:
        self.writer.close()
        self.engine.dispose()


def _sqlite_events(engine: Engine, readonly: bool) -> None:
    @event.listens_for(engine, "connect")
    def connect(dbapi_connection: Any, _: Any) -> None:
        # pysqlite's own transaction handling is turned off so BEGIN can be chosen below.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        if not readonly:
            cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        if readonly:
            cursor.execute("PRAGMA query_only=ON")
        cursor.close()

    @event.listens_for(engine, "begin")
    def begin(connection: Connection) -> None:
        connection.exec_driver_sql("BEGIN IMMEDIATE" if connection.get_execution_options().get("immediate") else "BEGIN")


class Writer:
    """Writes that mustn't hold up a request: run in order on one thread, a batch per transaction."""

    BATCH = 500

    def __init__(self, db: Database):
        self.db = db
        self.queue: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self.thread: threading.Thread | None = None
        self.closed = False
        self.lock = threading.Lock()

    def submit(self, work: Callable[[Connection], Any]) -> None:
        if self.closed or threading.current_thread() is self.thread:
            # After shutdown (a request finishing late) or from inside a batch: write it now.
            self._run([work])
            return
        self._start()
        self.queue.put(work)

    def flush(self, timeout: float = 60) -> None:
        """Wait until everything submitted so far is written."""
        if self.thread is None or self.closed or threading.current_thread() is self.thread:
            return
        done = threading.Event()
        self.queue.put(done)
        done.wait(timeout)

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            thread = self.thread
            if thread is not None:
                self.queue.put(None)
                thread.join(60)
            self.closed = True

    def _start(self) -> None:
        if self.thread is None:
            with self.lock:
                if self.thread is None and not self.closed:
                    self.thread = threading.Thread(target=self._loop, name="db-writer", daemon=True)
                    self.thread.start()

    def _loop(self) -> None:
        while True:
            item = self.queue.get()
            batch: list[Any] = [item]
            while len(batch) < self.BATCH:
                try:
                    batch.append(self.queue.get_nowait())
                except queue.Empty:
                    break
            stop = None in batch
            work = [entry for entry in batch if callable(entry)]
            if work:
                self._run(work)
            for entry in batch:
                if isinstance(entry, threading.Event):
                    entry.set()
            if stop:
                return

    def _run(self, work: list[Callable[[Connection], Any]]) -> None:
        try:
            with self.db.write() as connection:
                for item in work:
                    item(connection)
            return
        except Exception:  # noqa: BLE001 - retried one by one below
            if len(work) == 1:
                LOGGER.exception("database write failed")
                return
        # One bad write mustn't lose the rest of its batch.
        for item in work:
            try:
                with self.db.write() as connection:
                    item(connection)
            except Exception:  # noqa: BLE001
                LOGGER.exception("database write failed")
