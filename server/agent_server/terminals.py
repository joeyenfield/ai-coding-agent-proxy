"""Agent terminals the launcher shares with the proxy, for watching and typing into from the console.

The launcher runs the agent in a pseudo-terminal and streams everything it
prints here (client/internal/terminal in the Go client). The proxy keeps the recent history in
memory, records it in the database (terminal_chunks, written a chunk at a time:
every CHUNK bytes or FLUSH_SECONDS), and fans it out to viewers: each new viewer
gets the history replayed, then live output. Input from a viewer goes back to the
launcher, which types it into the agent.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from .db import kv, schema
from .sessions import Session, SessionStore, utc_now


# Replayed to each new viewer; enough for a long agent session's scrollback.
HISTORY_LIMIT = 4 * 1024 * 1024
# The recording stops growing past this; the in-memory history keeps going.
LOG_LIMIT = 64 * 1024 * 1024
# Output is recorded in chunks of about this size, or what came in this long.
CHUNK = 64 * 1024
FLUSH_SECONDS = 1.0
# A viewer that falls this far behind is disconnected and catches up by reconnecting.
VIEWER_BACKLOG = 512
INPUT_LIMIT = 64 * 1024
BRACKETED_PASTE = re.compile(rb"\x1b\[\?2004([hl])")
# An agent quiet this long, with no model request in flight, is waiting for its user.
QUIET_SECONDS = 5.0
# How much of the end of the output is read for a question on screen.
SCREEN_TAIL = 6 * 1024
# Escape sequences (CSI, OSC, two-byte) and other control characters, for reading the screen as text.
ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]|[\x00-\x08\x0b-\x1f\x7f]")
# What agents ask when they need a yes or no: Claude Code's and Codex's permission prompts, Aider's (Y)es/(N)o, plain y/n.
QUESTION = re.compile(
    r"do you want to|would you like to|allow (?:this|once|always)|approve|\((?:y/n|yes/no)\)|\[y/n\]|\(y\)es|"
    r"press enter to (?:continue|confirm)|waiting for (?:your )?(?:approval|confirmation|input)",
    re.IGNORECASE,
)


@dataclass
class Terminal:
    session_id: str
    cols: int = 80
    rows: int = 24
    command: str | None = None
    started_at: str = field(default_factory=utc_now)
    last_output_at: str | None = None
    ended_at: str | None = None
    exit_status: int | None = None
    # Bytes received since the agent started; the launcher resumes from here after a reconnect.
    total: int = 0
    # Bytes the launcher had already dropped when it reconnected; the recording lacks them.
    skipped: int = 0
    log_full: bool = False
    # Bytes recorded (or waiting in pending to be), and when pending was last written.
    logged: int = 0
    pending: bytearray = field(default_factory=bytearray)
    flushed_at: float = field(default_factory=time.monotonic)
    # Whether the agent asked for bracketed paste, so multi-line input can be sent as one paste.
    bracketed_paste: bool = False
    history: bytearray = field(default_factory=bytearray)
    host: Any = None
    # Who each viewer is, so they can be dropped when the session stops being shared with them.
    watchers: dict[asyncio.Queue, Any] = field(default_factory=dict)
    # Viewers type into the agent concurrently; their messages go to the launcher one at a time.
    host_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    viewers: set[asyncio.Queue] = field(default_factory=set)

    @property
    def connected(self) -> bool:
        return self.host is not None

    def public(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id, "cols": self.cols, "rows": self.rows, "command": self.command,
            "started_at": self.started_at, "last_output_at": self.last_output_at,
            "ended_at": self.ended_at, "exit_status": self.exit_status,
            "connected": self.connected, "viewers": len(self.viewers), "bytes": self.total,
        }

    def meta(self) -> dict[str, Any]:
        return {
            "cols": self.cols, "rows": self.rows, "command": self.command, "started_at": self.started_at,
            "last_output_at": self.last_output_at, "ended_at": self.ended_at, "exit_status": self.exit_status,
            "total": self.total, "skipped": self.skipped, "log_full": self.log_full, "bracketed_paste": self.bracketed_paste,
        }


class TerminalHub:
    def __init__(self, sessions: SessionStore):
        self.sessions = sessions
        self.terminals: dict[str, Terminal] = {}

    def get(self, session: Session) -> Terminal | None:
        """The session's terminal, loaded from its log when it ran before the proxy started."""
        terminal = self.terminals.get(session.session_id)
        if terminal is None:
            terminal = self._load(session)
            if terminal is not None:
                self.terminals[session.session_id] = terminal
        return terminal

    def summaries(self) -> list[dict[str, Any]]:
        """Every session with a shared terminal, newest first, without loading histories."""
        result = []
        stored = self._metas()
        for session in self.sessions.list():
            terminal = self.terminals.get(session.session_id)
            if terminal is not None:
                result.append(terminal.public())
                continue
            meta = stored.get(session.session_id)
            if meta is not None:
                result.append({**_public_meta(session, meta), "connected": False, "viewers": 0})
        return result

    def attach(self, session: Session, host: Any, hello: dict[str, Any]) -> Terminal:
        terminal = self.get(session) or Terminal(session.session_id)
        if terminal.host is not None and terminal.host is not host:
            # A reconnecting launcher replaces a connection the proxy hasn't noticed is dead.
            asyncio.ensure_future(_close_quietly(terminal.host))
        terminal.host = host
        terminal.cols, terminal.rows = _size(hello, terminal)
        terminal.command = str(hello.get("command") or terminal.command or "") or None
        terminal.ended_at = None
        terminal.exit_status = None
        self.terminals[session.session_id] = terminal
        self._save(terminal)
        self._broadcast(terminal, {"type": "state", "terminal": terminal.public()})
        return terminal

    def detach(self, terminal: Terminal, host: Any) -> None:
        if terminal.host is not host:
            return
        terminal.host = None
        self._flush(terminal)
        self._save(terminal)
        self._broadcast(terminal, {"type": "state", "terminal": terminal.public()})

    def output(self, terminal: Terminal, data: bytes) -> None:
        terminal.total += len(data)
        terminal.last_output_at = utc_now()
        terminal.history += data
        if len(terminal.history) > HISTORY_LIMIT:
            del terminal.history[: len(terminal.history) - HISTORY_LIMIT]
        modes = BRACKETED_PASTE.findall(data)
        if modes:
            terminal.bracketed_paste = modes[-1] == b"h"
        self._append_log(terminal, data)
        self._broadcast(terminal, data)

    def gap(self, terminal: Terminal, offset: int) -> None:
        """The launcher no longer has bytes up to offset; skip ahead to it."""
        if offset > terminal.total:
            terminal.skipped += offset - terminal.total
            terminal.total = offset
            self._save(terminal)

    def resize(self, terminal: Terminal, message: dict[str, Any]) -> None:
        terminal.cols, terminal.rows = _size(message, terminal)
        self._save(terminal)
        self._broadcast(terminal, {"type": "resize", "cols": terminal.cols, "rows": terminal.rows})

    def exited(self, terminal: Terminal, status: Any) -> None:
        terminal.exit_status = status if isinstance(status, int) else None
        terminal.ended_at = utc_now()
        self._flush(terminal)
        self._save(terminal)
        self._broadcast(terminal, {"type": "state", "terminal": terminal.public()})

    def attention(self, terminal: Terminal, in_flight: int, now: datetime | None = None) -> dict[str, Any] | None:
        """Whether the agent is waiting for its user, and the question on its screen if it asks one.

        It waits once it has printed nothing for QUIET_SECONDS with no model request in
        flight: agents animate a spinner while they work, and are silent at their prompt.
        "question" means the end of its output asks something (a permission prompt), "idle"
        that it's at its prompt. A guess from the screen, not something agents report.
        """
        if terminal.host is None or terminal.ended_at is not None or in_flight > 0:
            return None
        since = terminal.last_output_at or terminal.started_at
        quiet = ((now or datetime.now(timezone.utc)) - _parse_time(since)).total_seconds() if since else QUIET_SECONDS
        if quiet < QUIET_SECONDS:
            return None
        question = screen_question(bytes(terminal.history[-SCREEN_TAIL:]))
        return {"kind": "question" if question else "idle", "since": since, "question": question}

    def subscribe(self, terminal: Terminal, identity: Any = None) -> tuple[bytes, asyncio.Queue]:
        # No await between the snapshot and joining, so no output falls between them.
        queue: asyncio.Queue = asyncio.Queue(maxsize=VIEWER_BACKLOG)
        terminal.viewers.add(queue)
        terminal.watchers[queue] = identity
        return bytes(terminal.history), queue

    def unsubscribe(self, terminal: Terminal, queue: asyncio.Queue) -> None:
        terminal.viewers.discard(queue)
        terminal.watchers.pop(queue, None)

    def drop_viewers(self, session_id: str, still_allowed: Any) -> None:
        """Disconnect viewers who may no longer see this session."""
        terminal = self.terminals.get(session_id)
        if terminal is None:
            return
        for queue, identity in list(terminal.watchers.items()):
            if identity is not None and not still_allowed(identity):
                self.unsubscribe(terminal, queue)
                _replace_with_stop(queue)

    async def send_input(self, terminal: Terminal, message: dict[str, Any]) -> str | None:
        """Type a viewer's input into the agent. Returns why it couldn't, or None."""
        if terminal.host is None:
            return "The agent's terminal isn't connected." if terminal.ended_at is None else "The agent has exited."
        parts = input_parts(message, terminal.bracketed_paste)
        if not parts:
            return None
        try:
            async with terminal.host_lock:
                await terminal.host.send_text(json.dumps({"type": "input", "parts": parts}))
        except Exception:  # noqa: BLE001 - the host's handler notices the dead connection and detaches it
            return "The agent's terminal isn't connected."
        return None

    async def close_all(self) -> None:
        """Before a restart: launchers reconnect and resend what's missing, viewers reconnect too."""
        for terminal in self.terminals.values():
            self._flush(terminal)
            if terminal.host is not None:
                await _close_quietly(terminal.host, 1012)
            for queue in list(terminal.viewers):
                self.unsubscribe(terminal, queue)
                _replace_with_stop(queue)

    def forget(self, session_id: str) -> None:
        terminal = self.terminals.pop(session_id, None)
        if terminal is not None:
            # A forgotten (deleted) session's output isn't wanted any more.
            terminal.pending.clear()
        if terminal and terminal.host is not None:
            asyncio.ensure_future(_close_quietly(terminal.host))

    def _broadcast(self, terminal: Terminal, item: bytes | dict[str, Any]) -> None:
        for queue in list(terminal.viewers):
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                # Too far behind: drop it; the viewer reconnects and gets a fresh replay.
                terminal.viewers.discard(queue)
                _replace_with_stop(queue)

    def _append_log(self, terminal: Terminal, data: bytes) -> None:
        if terminal.log_full:
            return
        if terminal.logged + len(data) > LOG_LIMIT:
            self._flush(terminal)
            terminal.log_full = True
            self._save(terminal)
            return
        terminal.pending += data
        terminal.logged += len(data)
        if len(terminal.pending) >= CHUNK or time.monotonic() - terminal.flushed_at >= FLUSH_SECONDS:
            self._flush(terminal)

    def flush_quiet(self) -> None:
        """Record output an agent printed before going quiet: chunks are otherwise written as more arrives."""
        now = time.monotonic()
        for terminal in list(self.terminals.values()):
            if terminal.pending and now - terminal.flushed_at >= FLUSH_SECONDS:
                self._flush(terminal)

    def _flush(self, terminal: Terminal) -> None:
        """Record the output waiting in pending as one chunk."""
        terminal.flushed_at = time.monotonic()
        if not terminal.pending:
            return
        row = {"session_id": terminal.session_id, "offset": terminal.logged - len(terminal.pending), "data": bytes(terminal.pending)}
        terminal.pending.clear()
        self.sessions.db.writer.submit(lambda connection: kv.upsert(connection, schema.terminal_chunks, row))

    def _save(self, terminal: Terminal) -> None:
        row = {"session_id": terminal.session_id, "meta": terminal.meta()}
        self.sessions.db.writer.submit(lambda connection: kv.upsert(connection, schema.terminals, row))

    def _metas(self) -> dict[str, dict[str, Any]]:
        db = self.sessions.db
        db.writer.flush()
        with db.read() as connection:
            return {session_id: meta for session_id, meta in connection.execute(select(schema.terminals.c.session_id, schema.terminals.c.meta)) if isinstance(meta, dict)}

    def _load(self, session: Session) -> Terminal | None:
        db = self.sessions.db
        db.writer.flush()
        chunks = schema.terminal_chunks
        with db.read() as connection:
            meta = connection.execute(select(schema.terminals.c.meta).where(schema.terminals.c.session_id == session.session_id)).scalar()
            if not isinstance(meta, dict):
                return None
            last = connection.execute(
                select(chunks.c.offset, func.length(chunks.c.data)).where(chunks.c.session_id == session.session_id)
                .order_by(chunks.c.offset.desc()).limit(1)
            ).first()
            size = int(last[0]) + int(last[1]) if last else 0
            # The chunks holding the last HISTORY_LIMIT bytes (a chunk is at most about CHUNK plus one frame).
            rows = connection.execute(
                select(chunks.c.offset, chunks.c.data).where(chunks.c.session_id == session.session_id, chunks.c.offset >= size - HISTORY_LIMIT - 4 * CHUNK)
                .order_by(chunks.c.offset)
            ).all()
        terminal = Terminal(session.session_id)
        for key in ("cols", "rows", "command", "started_at", "last_output_at", "ended_at", "exit_status", "total", "skipped", "log_full", "bracketed_paste"):
            if key in meta:
                setattr(terminal, key, meta[key])
        if terminal.ended_at is None and session.ended_at:
            # The launcher closed the session but never reported the exit (proxy restarted meanwhile).
            terminal.ended_at, terminal.exit_status = session.ended_at, session.exit_status
        history = b"".join(bytes(data) for _, data in rows)
        terminal.history = bytearray(history[-HISTORY_LIMIT:])
        terminal.logged = size
        if not terminal.log_full:
            # The meta is only rewritten on events, so after a crash the recording is the better count.
            terminal.total = max(terminal.total, size + terminal.skipped)
        return terminal


def input_parts(message: dict[str, Any], bracketed_paste: bool) -> list[str]:
    """What to type for a viewer message, as pieces the launcher sends with a short pause between.

    keys: raw key sequences (arrows, Esc, Ctrl+C, typed characters).
    text (+ enter): a message for the agent. Enter goes separately, because TUIs
    such as Claude Code read text and Enter arriving together as a paste, not a submit.
    """
    parts: list[str] = []
    keys = message.get("keys")
    if isinstance(keys, str) and keys:
        parts.append(keys[:INPUT_LIMIT])
    text = message.get("text")
    if isinstance(text, str) and text:
        text = text[:INPUT_LIMIT].replace("\r\n", "\n")
        if "\n" in text and bracketed_paste:
            text = f"\x1b[200~{text}\x1b[201~"
        parts.append(text)
    if message.get("enter"):
        parts.append("\r")
    return parts


def screen_question(tail: bytes) -> str | None:
    """The last line of the agent's output that asks the user something, as plain text."""
    text = ESCAPES.sub("", tail.decode("utf-8", errors="ignore").replace("\r\n", "\n").replace("\r", "\n"))
    lines = [line.strip(" │|╭╮╰╯─") for line in text.split("\n")]
    # Only the end of the screen: a question scrolled further up has been answered.
    for line in reversed([line for line in lines if line][-12:]):
        if QUESTION.search(line):
            return line[:200]
    return None


def _parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _size(message: dict[str, Any], terminal: Terminal) -> tuple[int, int]:
    def bounded(value: Any, fallback: int) -> int:
        try:
            return min(max(int(value), 2), 1000)
        except (TypeError, ValueError):
            return fallback
    return bounded(message.get("cols"), terminal.cols), bounded(message.get("rows"), terminal.rows)


def _public_meta(session: Session, meta: dict[str, Any]) -> dict[str, Any]:
    ended_at, exit_status = meta.get("ended_at"), meta.get("exit_status")
    if ended_at is None and session.ended_at:
        ended_at, exit_status = session.ended_at, session.exit_status
    return {
        "session_id": session.session_id, "cols": meta.get("cols", 80), "rows": meta.get("rows", 24),
        "command": meta.get("command"), "started_at": meta.get("started_at"),
        "last_output_at": meta.get("last_output_at"), "ended_at": ended_at,
        "exit_status": exit_status, "bytes": meta.get("total", 0),
    }


def _replace_with_stop(queue: asyncio.Queue) -> None:
    while not queue.empty():
        queue.get_nowait()
    queue.put_nowait(None)


async def _close_quietly(websocket: Any, code: int = 4409) -> None:
    try:
        await websocket.close(code=code)
    except Exception:  # noqa: BLE001
        pass
