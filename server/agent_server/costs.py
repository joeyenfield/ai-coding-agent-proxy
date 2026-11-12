"""An organization's costs over a range of days: what its model calls cost, and working them out again.

A request's cost is worked out once, when it's recorded (pricing.py, Runtime._price), and kept in its
record. This module reads those records for one organization (its sessions carry the server's `tenant`
tag) between two days and adds them up: in all, by day, by model, by person and by provider, with the
models that had no price. `recompute` prices the same records again at the prices of now, which is how
a price added or corrected later reaches traffic already recorded.

Days are UTC days, as records' timestamps are. Only model calls that used tokens are read: nothing
else can have a cost.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable, Iterable

from sqlalchemy import func, or_, select

from .db import Database, schema
from .usage_limits import session_user

# A range's days are all in the answer (the chart's bars), so it has an end.
MAX_DAYS = 366
# Session ids in one query, and records rewritten in one transaction.
CHUNK = 400


def day_range(since: Any, until: Any) -> tuple[str, str, list[str]]:
    """The range as timestamps' bounds (from the first day, to before the day after the last) and its days."""
    try:
        first, last = date.fromisoformat(str(since)), date.fromisoformat(str(until))
    except ValueError as exc:
        raise ValueError("since and until are days, as YYYY-MM-DD") from exc
    if last < first:
        raise ValueError("until is before since")
    days = (last - first).days + 1
    if days > MAX_DAYS:
        raise ValueError(f"Choose a range of {MAX_DAYS} days or less")
    return first.isoformat(), (last + timedelta(days=1)).isoformat(), [(first + timedelta(days=offset)).isoformat() for offset in range(days)]


def sessions_of(db: Database, tenant: str, start: str, end: str, user: str | None = None) -> dict[str, str]:
    """The organization's sessions that were active in the range, each with whose it is (one person's only, with user)."""
    table = schema.sessions
    db.writer.flush()
    with db.read() as connection:
        rows = connection.execute(
            select(table.c.id, table.c.data).where(table.c.started_at < end, func.coalesce(table.c.last_activity_at, table.c.started_at) >= start)
        ).all()
    found: dict[str, str] = {}
    for session_id, data in rows:
        tags = data.get("tags") if isinstance(data, dict) else None
        if not isinstance(tags, dict) or tags.get("tenant") != tenant:
            continue
        whose = str(tags.get("launched_by") or tags.get("owner") or "")
        if user is None or whose == user:
            found[str(session_id)] = whose
    return found


def records(db: Database, session_ids: Iterable[str], start: str, end: str) -> list[tuple[str, str, dict[str, Any]]]:
    """These sessions' model calls in the range: (session, request, record), oldest first."""
    table = schema.requests
    wanted = list(session_ids)
    found: list[tuple[str, str, dict[str, Any]]] = []
    for offset in range(0, len(wanted), CHUNK):
        with db.read() as connection:
            rows = connection.execute(
                select(table.c.session_id, table.c.request_id, table.c.record).where(
                    table.c.session_id.in_(wanted[offset:offset + CHUNK]), table.c.timestamp >= start, table.c.timestamp < end,
                    or_(table.c.input_tokens > 0, table.c.output_tokens > 0),
                )
            ).all()
        found.extend((str(session_id), str(request_id), record) for session_id, request_id, record in rows if isinstance(record, dict))
    found.sort(key=lambda item: (str(item[2].get("timestamp") or ""), item[0], item[1]))
    return found


def _empty() -> dict[str, Any]:
    return {"requests": 0, "priced": 0, "unpriced": 0, "usd": 0.0, "subscription_usd": 0.0, "input_tokens": 0, "output_tokens": 0}


def _add(total: dict[str, Any], record: dict[str, Any]) -> None:
    total["requests"] += 1
    total["input_tokens"] += int(record.get("input_tokens") or 0)
    total["output_tokens"] += int(record.get("output_tokens") or 0)
    cost = record.get("cost")
    if isinstance(cost, dict):
        total["priced"] += 1
        # Money spent on a paid API, apart from what a subscription's calls would cost at API prices.
        key = "usd" if cost.get("basis") == "api" else "subscription_usd"
        total[key] = round(total[key] + float(cost.get("usd") or 0), 6)
    elif record.get("unpriced"):
        total["unpriced"] += 1


def _ranked(groups: dict[str, dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """Groups as a list, the costliest first."""
    return sorted(({name: key, **value} for key, value in groups.items()), key=lambda item: (-(item["usd"] + item["subscription_usd"]), -item["requests"], str(item[name])))


def summary(found: list[tuple[str, str, dict[str, Any]]], owners: dict[str, str], days: list[str]) -> dict[str, Any]:
    """The records added up: in all, for each day of the range, and by model, person and provider."""
    total = _empty()
    by_day = {day: _empty() for day in days}
    models: dict[str, dict[str, Any]] = {}
    users: dict[str, dict[str, Any]] = {}
    providers: dict[str, dict[str, Any]] = {}
    sessions: set[str] = set()
    for session_id, _, record in found:
        sessions.add(session_id)
        _add(total, record)
        day = str(record.get("timestamp") or "")[:10]
        if day in by_day:
            _add(by_day[day], record)
        _add(models.setdefault(str(record.get("upstream_model") or record.get("model") or ""), _empty()), record)
        _add(users.setdefault(owners.get(session_id, ""), _empty()), record)
        _add(providers.setdefault(str(record.get("backend") or ""), _empty()), record)
    return {
        "currency": "USD", **total, "sessions": len(sessions),
        "days": [{"day": day, **value} for day, value in by_day.items()],
        "models": _ranked(models, "model"), "users": _ranked(users, "user"), "providers": _ranked(providers, "provider"),
    }


def report(db: Database, tenant: str, since: Any, until: Any, user: str | None = None) -> dict[str, Any]:
    """The organization's costs between two days (one person's, with user)."""
    start, end, days = day_range(since, until)
    owners = sessions_of(db, tenant, start, end, user)
    return {"org": tenant, "since": days[0], "until": days[-1], **summary(records(db, owners, start, end), owners, days)}


def recompute(
    db: Database, reprice: Callable[[dict[str, Any]], dict[str, Any] | None], tenant: str, since: Any, until: Any,
) -> tuple[dict[str, Any], dict[tuple[str, str], dict[str, Any]]]:
    """Price the organization's model calls in the range again, at the prices of now, and keep what changed.

    reprice (Runtime.reprice) gives a record as it would be priced now, or None when nothing changes.
    Answers what was looked at, what changed and the totals before and after, and the changed records by
    (session, request) for the caller's own copies."""
    start, end, days = day_range(since, until)
    owners = sessions_of(db, tenant, start, end)
    found = records(db, owners, start, end)
    before, after = _empty(), _empty()
    changed: list[tuple[str, str, dict[str, Any]]] = []
    for session_id, request_id, record in found:
        _add(before, record)
        priced = reprice(record)
        _add(after, priced or record)
        if priced is not None:
            changed.append((session_id, request_id, priced))
    table = schema.requests
    for offset in range(0, len(changed), CHUNK):
        with db.write() as connection:
            for session_id, request_id, record in changed[offset:offset + CHUNK]:
                connection.execute(table.update().where(table.c.session_id == session_id, table.c.request_id == request_id).values(record=record))
    answer = {
        "org": tenant, "since": days[0], "until": days[-1], "currency": "USD",
        "requests": len(found), "changed": len(changed), "sessions": len({session_id for session_id, _, _ in found}),
        "before": before, "after": after,
    }
    return answer, {(session_id, request_id): record for session_id, request_id, record in changed}
