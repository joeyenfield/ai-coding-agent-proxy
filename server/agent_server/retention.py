"""What's recorded, and for how long: the server's retention policy, organizations' own, and legal holds.

Site admins set the server's policy (Server settings -> Storage, /api/config/retention): what is
captured, how many days each kind of recording is kept, and the bounds organizations' admins may
set their own within (/api/orgs/<org>/retention). The kinds:

  traces    full requests and responses (and the chat view's index of them)
  terminal  shared terminals' recorded output
  records   request records (who called which model, tokens, timings)
  sessions  the session itself, and everything it recorded
  audit     the audit log (audit.py), which has a floor no organization goes below

Capture: traces off (never recorded), opt_in (per session, --trace: the default) or always;
intercept_bodies (account agents' traffic in traces) and terminal_output on or off. Off means not
written, so there's nothing to delete later.

A session takes its organization's policy when it starts (its server tag `retention`), so changing a
policy doesn't silently stretch or cut what people were told when they began. Deleting is the worker's
hourly job (run), one process at a time: it looks only at sessions whose next deletion is due
(sessions.retention_next), so millions of old sessions cost nothing until one is. A legal hold on an
organization, a person or a session stops every deletion of what it covers until it's lifted.

retention_days, the older single setting, is the server policy's traces (and terminal) days.
"""

from __future__ import annotations

import copy
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import func, or_, select

from .db import Database, kv, schema


KINDS = ("traces", "terminal", "records", "sessions", "audit")
TRACE_CAPTURE = ("off", "opt_in", "always")
HOLD_SCOPES = ("org", "user", "session")
LONGEST = 36_500
# The audit log is kept at least this long whatever anyone sets.
AUDIT_FLOOR = 90
DEFAULT_POLICY: dict[str, Any] = {
    "capture": {"traces": "opt_in", "intercept_bodies": True, "terminal_output": True},
    "allowed_traces": list(TRACE_CAPTURE),
    # Days each kind is kept; None is forever.
    "days": {"traces": None, "terminal": None, "records": None, "sessions": None, "audit": 365},
    # What organizations may set: at most max days (None: no bound), at least min days.
    "max": {"traces": None, "terminal": None, "records": None, "sessions": None, "audit": None},
    "min": {"traces": None, "terminal": None, "records": None, "sessions": None, "audit": AUDIT_FLOOR},
}
NEVER = "9999-12-31T00:00:00.000Z"
BATCH = 2000


class RetentionError(ValueError):
    pass


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _days(value: Any, label: str) -> int | None:
    if value in (None, "", 0):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value) or not 1 <= value <= LONGEST:
        raise RetentionError(f"{label}: a whole number of days from 1 to {LONGEST}, or none (forever)")
    return int(value)


def validate_server(document: Any) -> dict[str, Any]:
    """The server's policy, checked and complete."""
    if not isinstance(document, dict):
        raise RetentionError("Send the policy as {capture, allowed_traces, days, max, min}")
    policy = copy.deepcopy(DEFAULT_POLICY)
    capture = document.get("capture") or {}
    if not isinstance(capture, dict):
        raise RetentionError("capture is {traces, intercept_bodies, terminal_output}")
    allowed = document.get("allowed_traces", policy["allowed_traces"])
    if not isinstance(allowed, list) or not allowed or any(item not in TRACE_CAPTURE for item in allowed):
        raise RetentionError(f"allowed_traces lists some of {', '.join(TRACE_CAPTURE)}")
    policy["allowed_traces"] = [item for item in TRACE_CAPTURE if item in allowed]
    traces = capture.get("traces", policy["capture"]["traces"])
    if traces not in policy["allowed_traces"]:
        raise RetentionError("capture.traces must be one of allowed_traces")
    policy["capture"] = {"traces": traces, "intercept_bodies": bool(capture.get("intercept_bodies", True)), "terminal_output": bool(capture.get("terminal_output", True))}
    for part in ("days", "max", "min"):
        given = document.get(part) or {}
        if not isinstance(given, dict) or set(given) - set(KINDS):
            raise RetentionError(f"{part} is days for some of {', '.join(KINDS)}")
        for kind in KINDS:
            if kind in given:
                policy[part][kind] = _days(given[kind], f"{part}.{kind}")
    if (policy["min"]["audit"] or 0) < AUDIT_FLOOR:
        policy["min"]["audit"] = AUDIT_FLOOR
    for kind in KINDS:
        low, high, days = policy["min"][kind], policy["max"][kind], policy["days"][kind]
        if low and high and low > high:
            raise RetentionError(f"min.{kind} is more than max.{kind}")
        if days is not None and ((low and days < low) or (high and days > high)):
            raise RetentionError(f"days.{kind} is outside min.{kind}..max.{kind}")
        if days is None and high:
            raise RetentionError(f"days.{kind} can't be forever when max.{kind} is {high}")
    if policy["days"]["audit"] is None or policy["days"]["audit"] < AUDIT_FLOOR:
        policy["days"]["audit"] = max(AUDIT_FLOOR, policy["days"]["audit"] or 365)
    return policy


def validate_org(document: Any, server: dict[str, Any]) -> dict[str, Any]:
    """An organization's own policy: only what it changes, within the server's bounds."""
    if not isinstance(document, dict):
        raise RetentionError("Send the policy as {capture, days}")
    result: dict[str, Any] = {}
    capture = document.get("capture") or {}
    if not isinstance(capture, dict):
        raise RetentionError("capture is {traces, intercept_bodies, terminal_output}")
    checked_capture: dict[str, Any] = {}
    if "traces" in capture:
        if capture["traces"] not in server["allowed_traces"]:
            raise RetentionError(f"traces: this server allows {', '.join(server['allowed_traces'])}")
        checked_capture["traces"] = capture["traces"]
    for name in ("intercept_bodies", "terminal_output"):
        if name in capture:
            checked_capture[name] = bool(capture[name])
    if checked_capture:
        result["capture"] = checked_capture
    days = document.get("days") or {}
    if not isinstance(days, dict) or set(days) - set(KINDS):
        raise RetentionError(f"days is days for some of {', '.join(KINDS)}")
    checked_days: dict[str, Any] = {}
    for kind, value in days.items():
        value = _days(value, f"days.{kind}")
        low, high = server["min"][kind], server["max"][kind]
        # Forever is allowed only without a most; a number must be within the server's bounds.
        if (value is None and high) or (value is not None and high and value > high):
            raise RetentionError(f"days.{kind}: this server keeps it at most {high} days")
        if value is not None and low and value < low:
            raise RetentionError(f"days.{kind}: this server keeps it at least {low} days")
        checked_days[kind] = value
    if checked_days:
        result["days"] = checked_days
    return result


class Retention:
    """The policies, the holds, stamping new sessions and the hourly deletion (run)."""

    def __init__(self, db: Database, settings: Any, sessions: Any = None, cluster: Any = None, clock: Callable[[], float] = time.time):
        self.db = db
        self.settings = settings
        self.sessions = sessions
        self.cluster = cluster
        self.clock = clock
        self._lock = threading.Lock()
        self._orgs: dict[str, dict[str, Any]] = {}
        self._holds: dict[str, dict[str, Any]] = {}
        # Other things that delete by policy (the audit log): callables(policy_for_tenant, now) -> count.
        self.extra: list[Callable[[Callable[[str], dict[str, Any]], datetime], int]] = []
        self.reload()
        if cluster is not None:
            cluster.on("retention.changed", lambda data, _: self.reload())

    # Policies

    def reload(self) -> None:
        with self.db.read() as connection:
            orgs = {row["tenant"]: row["policy"] for row in connection.execute(select(schema.retention_policies)).mappings() if isinstance(row["policy"], dict)}
            holds = {row["id"]: dict(row) for row in connection.execute(select(schema.legal_holds)).mappings()}
        with self._lock:
            self._orgs, self._holds = orgs, holds

    def server(self) -> dict[str, Any]:
        stored = None
        try:
            with self.db.read() as connection:
                stored = kv.get(connection, schema.settings, "retention")
        except Exception:  # noqa: BLE001 - a database from before the table: the defaults
            stored = None
        policy = copy.deepcopy(DEFAULT_POLICY)
        if isinstance(stored, dict):
            try:
                policy = validate_server(stored)
            except RetentionError:
                pass
        # The older single setting is the traces' (and terminal output's, and the chat index's) days.
        legacy = getattr(self.settings, "retention_days", None)
        if not (isinstance(stored, dict) and "traces" in (stored.get("days") or {})):
            policy["days"]["traces"] = legacy
        if not (isinstance(stored, dict) and "terminal" in (stored.get("days") or {})):
            policy["days"]["terminal"] = legacy
        return policy

    def set_server(self, document: Any, by: str | None) -> dict[str, Any]:
        policy = validate_server(document)
        with self.db.write() as connection:
            kv.put(connection, schema.settings, "retention", {**policy, "set_by": by, "set_at": self.clock()})
        # The older setting follows the traces' days, for what still reads it (the storage page, the access log).
        self.settings.set_retention(policy["days"]["traces"])
        self._changed()
        return self.server()

    def org(self, tenant: str) -> dict[str, Any] | None:
        with self._lock:
            return copy.deepcopy(self._orgs.get(tenant))

    def set_org(self, tenant: str, document: Any, by: str | None) -> dict[str, Any]:
        policy = validate_org(document, self.server())
        with self.db.write() as connection:
            kv.upsert(connection, schema.retention_policies, {"tenant": tenant, "policy": policy, "set_by": by, "set_at": self.clock()})
        with self._lock:
            self._orgs[tenant] = policy
        self._changed()
        return self.effective(tenant)

    def remove_org(self, tenant: str) -> bool:
        with self.db.write() as connection:
            removed = connection.execute(schema.retention_policies.delete().where(schema.retention_policies.c.tenant == tenant)).rowcount
        with self._lock:
            self._orgs.pop(tenant, None)
        self._changed()
        return bool(removed)

    def effective(self, tenant: str | None) -> dict[str, Any]:
        """The policy a session of this organization runs under: the server's, with the organization's on top."""
        server = self.server()
        own = self.org(tenant or "") or {}
        capture = {**server["capture"], **(own.get("capture") or {})}
        if capture["traces"] not in server["allowed_traces"]:
            capture["traces"] = server["capture"]["traces"]
        days = dict(server["days"])
        for kind, value in (own.get("days") or {}).items():
            low, high = server["min"][kind], server["max"][kind]
            if value is None:
                value = None if not high else high
            if value is not None:
                value = max(value, low or value)
                value = min(value, high or value)
            days[kind] = value
        return {"capture": capture, "days": days, "own": own, "server": {
            "allowed_traces": server["allowed_traces"], "max": server["max"], "min": server["min"],
            # What "the server's" is, for a setting the organization doesn't change.
            "capture": server["capture"], "days": server["days"],
        }}

    def _changed(self) -> None:
        # Every session is looked at again under the new policy (their next deletion may have moved).
        sessions = schema.sessions
        self.db.writer.submit(lambda connection: connection.execute(sessions.update().values(retention_next=None)))
        if self.cluster is not None:
            self.cluster.publish("retention.changed", {})

    # New sessions

    def stamp(self, data: dict[str, Any]) -> dict[str, Any]:
        """A new session's data with its organization's policy (tags.retention) and its trace capture applied."""
        tags = dict(data.get("tags") or {})
        policy = self.effective(tags.get("tenant"))
        tags["retention"] = {"days": policy["days"], "capture": policy["capture"]}
        data = {**data, "tags": tags}
        mode = policy["capture"]["traces"]
        if mode == "off":
            data["trace"] = False
        elif mode == "always":
            data["trace"] = True
        return data

    def check_trace(self, session: Any, enabled: bool) -> None:
        """Raises RetentionError when the session's policy doesn't let its tracing change that way."""
        mode = ((session.tags.get("retention") or {}).get("capture") or {}).get("traces") or self.effective(session.tags.get("tenant"))["capture"]["traces"]
        if enabled and mode == "off":
            raise RetentionError("This organization doesn't record traces")
        if not enabled and mode == "always":
            raise RetentionError("This organization records every session's traces")

    def captures(self, session: Any, what: str) -> bool:
        """Whether the session's policy records intercept_bodies or terminal_output."""
        capture = (session.tags.get("retention") or {}).get("capture") if session is not None else None
        if not isinstance(capture, dict) or what not in capture:
            capture = self.effective(session.tags.get("tenant") if session is not None else None)["capture"]
        return bool(capture.get(what, True))

    # Legal holds

    def holds(self, tenant: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            found = [dict(item) for item in self._holds.values() if tenant is None or item["tenant"] == tenant]
        return sorted(found, key=lambda item: -float(item.get("set_at") or 0))

    def add_hold(self, tenant: str, scope: str, target: str, reason: str | None, by: str | None) -> dict[str, Any]:
        if scope not in HOLD_SCOPES:
            raise RetentionError(f"scope is one of {', '.join(HOLD_SCOPES)}")
        target = str(target or "").strip()[:200]
        if not target:
            raise RetentionError("Name what's held: the organization, the person or the session")
        if scope == "org" and target != tenant:
            raise RetentionError("An organization's hold is on that organization")
        row = {"id": secrets.token_hex(8), "tenant": tenant, "scope": scope, "target": target, "reason": (reason or "")[:2000] or None, "set_by": by, "set_at": self.clock()}
        with self.db.write() as connection:
            connection.execute(schema.legal_holds.insert().values(**row))
        with self._lock:
            self._holds[row["id"]] = row
        if self.cluster is not None:
            self.cluster.publish("retention.changed", {})
        return row

    def remove_hold(self, hold_id: str) -> dict[str, Any] | None:
        with self._lock:
            hold = self._holds.get(hold_id)
        if hold is None:
            return None
        with self.db.write() as connection:
            connection.execute(schema.legal_holds.delete().where(schema.legal_holds.c.id == hold_id))
        with self._lock:
            self._holds.pop(hold_id, None)
        self._changed()
        return hold

    def held(self, session: Any) -> bool:
        tags = session.tags or {}
        tenant, person = tags.get("tenant"), tags.get("launched_by") or tags.get("owner")
        with self._lock:
            holds = list(self._holds.values())
        for hold in holds:
            if hold["tenant"] != tenant and hold["scope"] != "session":
                continue
            if (hold["scope"] == "org" and hold["target"] == tenant) or (hold["scope"] == "user" and hold["target"] == person) \
                    or (hold["scope"] == "session" and hold["target"] == session.session_id):
                return True
        return False

    def tenant_held(self, tenant: str) -> bool:
        with self._lock:
            return any(hold["scope"] == "org" and hold["target"] == tenant for hold in self._holds.values())

    # Deleting

    def _shortest(self) -> int | None:
        values = []
        server = self.server()
        for policy in [server["days"], *((own.get("days") or {}) for own in list(self._orgs.values()))]:
            values += [days for kind, days in policy.items() if kind != "audit" and days]
        return min(values) if values else None

    def run(self, now: datetime | None = None) -> dict[str, int]:
        """Delete what's past its policy. Returns how many sessions had each kind deleted."""
        now = now or datetime.now(timezone.utc)
        done = {kind: 0 for kind in KINDS}
        if self.sessions is not None:
            done.update(self._sessions(now))
        for extra in self.extra:
            done["audit"] += extra(lambda tenant: self.effective(tenant), now)
        return done

    def _sessions(self, now: datetime) -> dict[str, int]:
        done = {kind: 0 for kind in ("traces", "terminal", "records", "sessions")}
        shortest = self._shortest()
        store = self.sessions
        store.db.writer.flush()
        table = schema.sessions
        activity = func.coalesce(table.c.last_activity_at, table.c.started_at)
        due = or_(table.c.retention_next <= _iso(now))
        if shortest:
            due = or_(due, (table.c.retention_next.is_(None)) & (activity < _iso(now - timedelta(days=shortest))))
        while True:
            with self.db.read() as connection:
                rows = connection.execute(select(table).where(due).limit(BATCH)).mappings().all()
            if not rows:
                return done
            from .sessions import _from_row

            plans: dict[tuple[str, ...], list[Any]] = {}
            later: dict[str, str] = {}
            for row in rows:
                session = _from_row(row)
                if session is None:
                    continue
                if session.ended_at is None and not (session.last_activity_at or session.started_at) < _iso(now - timedelta(days=1)):
                    later[session.session_id] = _iso(now + timedelta(days=1))
                    continue
                if self.held(session):
                    later[session.session_id] = _iso(now + timedelta(days=1))
                    continue
                kinds, next_due = self._due(session, now)
                if kinds:
                    plans.setdefault(tuple(sorted(kinds)), []).append(session)
                later[session.session_id] = next_due
            for kinds, sessions in plans.items():
                if "sessions" in kinds:
                    store.delete_sessions([item.session_id for item in sessions])
                    done["sessions"] += len(sessions)
                    for item in sessions:
                        later.pop(item.session_id, None)
                    continue
                recordings = [kind for kind in kinds if kind in ("traces", "terminal")]
                if recordings:
                    store.delete_recordings(sessions, recordings)
                    for kind in recordings:
                        done[kind] += len(sessions)
                if "records" in kinds:
                    ids = [item.session_id for item in sessions]

                    def delete_records(connection: Any, ids: list[str] = ids) -> None:
                        connection.execute(schema.requests.delete().where(schema.requests.c.session_id.in_(ids)))

                    store.db.writer.submit(delete_records)
                    done["records"] += len(sessions)
            if later:
                pairs = list(later.items())

                def mark(connection: Any, pairs: list[tuple[str, str]] = pairs) -> None:
                    for session_id, when in pairs:
                        connection.execute(table.update().where(table.c.id == session_id).values(retention_next=when))

                store.db.writer.submit(mark)
            store.db.writer.flush()
            if len(rows) < BATCH:
                return done

    def _due(self, session: Any, now: datetime) -> tuple[set[str], str]:
        """What of this session to delete now, and when the next deletion of it will be due."""
        stamped = (session.tags.get("retention") or {}).get("days")
        days = stamped if isinstance(stamped, dict) else self.effective(session.tags.get("tenant"))["days"]
        activity = _parse(session.last_activity_at or session.ended_at or session.started_at) or now
        kinds: set[str] = set()
        upcoming: list[datetime] = []
        for kind in ("traces", "terminal", "records", "sessions"):
            value = days.get(kind)
            if not value:
                continue
            expires = activity + timedelta(days=int(value))
            if expires <= now:
                kinds.add(kind)
            else:
                upcoming.append(expires)
        if "sessions" in kinds:
            return {"sessions"}, NEVER
        # A kind already deleted would come round again; done ones are those whose time has passed.
        return kinds, _iso(min(upcoming)) if upcoming else NEVER


def seed_from_environment(settings: Any) -> bool:
    """AI_PROXY_RETENTION_SEED (JSON): the server policy for a new install (Helm's retention values), once."""
    import json
    import os

    text = os.getenv("AI_PROXY_RETENTION_SEED", "").strip()
    if not text:
        return False
    db = settings.open_database()
    with db.read() as connection:
        if kv.get(connection, schema.settings, "retention") is not None or kv.get_meta(connection, "retention_seeded"):
            return False
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise RetentionError(f"AI_PROXY_RETENTION_SEED isn't JSON: {exc}") from exc
    Retention(db, settings).set_server(document, "AI_PROXY_RETENTION_SEED")
    with db.write() as connection:
        kv.put_meta(connection, "retention_seeded", True)
    return True
