"""An organization's usage over a range of days, split by one thing and narrowed by the others.

costs.py says what an organization's model calls cost. This module reads the same records (the
organization's sessions between two UTC days, their model calls that used tokens) and counts usage:
sessions, model calls, cost, tokens and the people who made them, for each day, split by one
dimension (person, model, provider or agent) and narrowed to one value of any of the others. That is
what the console's Usage page drills down with: pick a person, then see their models, and so on.

A split has many values and a chart only so many colours, so the SERIES busiest values keep their
own series and the rest are counted together as OTHER. Sessions and people are counted once in a
total, and once in every series they appear in: a session that used two models is in both.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from . import costs
from .db import Database, schema

DIMENSIONS = ("user", "model", "provider", "agent")
# The values of a split that get a series of their own in each day (the console's palette has eight hues).
SERIES = 8
OTHER = "__other__"
# The values listed with their totals, and the sessions listed, costliest first.
VALUES = 200
SESSIONS = 50


def sessions_of(db: Database, tenant: str, start: str, end: str, user: str | None = None) -> dict[str, dict[str, Any]]:
    """The organization's sessions active in the range (one person's only, with user): whose, which agent, what on."""
    table = schema.sessions
    db.writer.flush()
    with db.read() as connection:
        rows = connection.execute(
            select(table.c.id, table.c.data, table.c.started_at).where(table.c.started_at < end, func.coalesce(table.c.last_activity_at, table.c.started_at) >= start)
        ).all()
    found: dict[str, dict[str, Any]] = {}
    for session_id, data, started_at in rows:
        tags = data.get("tags") if isinstance(data, dict) else None
        if not isinstance(tags, dict) or tags.get("tenant") != tenant:
            continue
        whose = str(tags.get("launched_by") or tags.get("owner") or "")
        if user is None or whose == user:
            found[str(session_id)] = {"user": whose, "agent": str(data.get("client") or ""), "project": str(data.get("project") or ""), "started_at": str(started_at or "")}
    return found


def _cell() -> dict[str, Any]:
    return {"requests": 0, "usd": 0.0, "subscription_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "_sessions": set(), "_users": set()}


def _put(cell: dict[str, Any], session_id: str, user: str, record: dict[str, Any]) -> None:
    cell["requests"] += 1
    cell["input_tokens"] += int(record.get("input_tokens") or 0)
    cell["output_tokens"] += int(record.get("output_tokens") or 0)
    cost = record.get("cost")
    if isinstance(cost, dict):
        # Money spent on a paid API, apart from what a subscription's calls would cost at API prices.
        key = "usd" if cost.get("basis") == "api" else "subscription_usd"
        cell[key] = round(cell[key] + float(cost.get("usd") or 0), 6)
    cell["_sessions"].add(session_id)
    cell["_users"].add(user)


def _out(cell: dict[str, Any]) -> dict[str, Any]:
    return {**{key: value for key, value in cell.items() if not key.startswith("_")}, "sessions": len(cell["_sessions"]), "users": len(cell["_users"])}


def usage(
    db: Database, tenant: str, since: Any, until: Any, by: str = "model", filters: dict[str, str] | None = None, user: str | None = None,
) -> dict[str, Any]:
    """The organization's usage between two days, split by one dimension (one person's only, with user).

    filters narrows it to one value of other dimensions ({"user": "alice"}). Answers the totals, every
    day of the range with its series, the split's values with their totals (slot: the series it has in
    the days, None when it's in OTHER) and the costliest sessions."""
    if by not in DIMENSIONS:
        raise ValueError(f"by is one of {', '.join(DIMENSIONS)}")
    wanted = {key: str(value) for key, value in (filters or {}).items() if key in DIMENSIONS and value is not None}
    start, end, days = costs.day_range(since, until)
    sessions = sessions_of(db, tenant, start, end, user)

    # Each call with what it is in every dimension, narrowed; then the split's values, the busiest first.
    kept: list[tuple[str, str, dict[str, str], dict[str, Any]]] = []
    values: dict[str, dict[str, Any]] = {}
    total = _cell()
    per_session: dict[str, dict[str, Any]] = {}
    for session_id, _, record in costs.records(db, sessions, start, end):
        session = sessions[session_id]
        what = {
            "user": session["user"], "agent": session["agent"],
            "model": str(record.get("upstream_model") or record.get("model") or ""), "provider": str(record.get("backend") or ""),
        }
        if any(what[key] != value for key, value in wanted.items()):
            continue
        kept.append((session_id, str(record.get("timestamp") or "")[:10], what, record))
        for cell in (total, values.setdefault(what[by], _cell()), per_session.setdefault(session_id, _cell())):
            _put(cell, session_id, what["user"], record)
    ranked = sorted(values, key=lambda key: (-values[key]["requests"], key))
    slots = {key: index for index, key in enumerate(ranked[:SERIES])}

    by_day = {day: (_cell(), {}) for day in days}
    for session_id, day, what, record in kept:
        if day not in by_day:
            continue
        whole, series = by_day[day]
        _put(whole, session_id, what["user"], record)
        _put(series.setdefault(what[by] if what[by] in slots else OTHER, _cell()), session_id, what["user"], record)

    costliest = sorted(per_session, key=lambda key: (-(per_session[key]["usd"] + per_session[key]["subscription_usd"]), -per_session[key]["requests"], key))
    return {
        "org": tenant, "since": days[0], "until": days[-1], "currency": "USD", "by": by, "filters": wanted, **_out(total),
        "days": [{"day": day, **_out(whole), "series": {key: _out(cell) for key, cell in series.items()}} for day, (whole, series) in by_day.items()],
        "values": [{"key": key, "slot": slots.get(key), **_out(values[key])} for key in ranked[:VALUES]],
        "more_values": max(0, len(ranked) - VALUES),
        "top_sessions": [
            {"session_id": key, **{name: sessions[key][name] for name in ("user", "agent", "project", "started_at")}, **{name: value for name, value in _out(per_session[key]).items() if name not in ("sessions", "users")}}
            for key in costliest[:SESSIONS]
        ],
    }
