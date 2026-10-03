from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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

    def public(self) -> dict[str, Any]:
        return asdict(self)


class SessionStore:
    def __init__(self, log_dir: Path):
        self.root = log_dir / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._load_existing()

    def _load_existing(self) -> None:
        for path in self.root.glob("*/session.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                session = Session(**{k: v for k, v in data.items() if k in Session.__dataclass_fields__})
                self._sessions[session.session_id] = session
            except (OSError, ValueError, TypeError):
                continue

    def create(self, data: dict[str, Any]) -> Session:
        session_id = data.get("session_id") or str(uuid.uuid4())
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
            self._write_session(session)
            return f"{session.request_sequence:06d}"

    def end(self, session: Session, exit_status: int | None = None) -> None:
        session.ended_at = utc_now()
        session.exit_status = exit_status
        self._write_session(session)

    def set_trace(self, session: Session, enabled: bool) -> None:
        session.trace = enabled
        self._write_session(session)

    def clear_data(self, session: Session) -> None:
        directory = self._checked_directory(session)
        trace_dir = directory / "traces"
        if trace_dir.exists():
            shutil.rmtree(trace_dir)
        (directory / "requests.jsonl").unlink(missing_ok=True)
        session.request_sequence = max(session.request_sequence, session.request_count)
        session.request_count = 0
        self._write_session(session)

    def delete(self, session: Session) -> None:
        directory = self._checked_directory(session)
        if directory.exists():
            shutil.rmtree(directory)
        self._sessions.pop(session.session_id, None)
        self._locks.pop(session.session_id, None)

    def _checked_directory(self, session: Session) -> Path:
        directory = self.root / session.session_id
        if directory.is_symlink() or directory.resolve().parent != self.root.resolve():
            raise ValueError("Invalid session directory")
        return directory

    def append_request(self, session: Session, record: dict[str, Any]) -> None:
        directory = self._directory(session)
        with (directory / "requests.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")

    def write_trace(self, session: Session, request_id: str, trace: dict[str, Any]) -> None:
        trace_dir = self._directory(session) / "traces"
        trace_dir.mkdir(exist_ok=True)
        _atomic_json(trace_dir / f"{request_id}.json", trace)

    def traces(self, session: Session) -> list[str]:
        trace_dir = self._directory(session) / "traces"
        return sorted((path.name for path in trace_dir.glob("*.json")), reverse=True)

    def read_trace(self, session: Session, request_id: str) -> dict[str, Any] | None:
        if not request_id.isdigit():
            return None
        path = self._directory(session) / "traces" / f"{request_id}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def telemetry(self, session: Session) -> str:
        path = self._directory(session) / "requests.jsonl"
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def _directory(self, session: Session) -> Path:
        directory = self.root / session.session_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _write_session(self, session: Session) -> None:
        _atomic_json(self._directory(session) / "session.json", session.public())


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)

