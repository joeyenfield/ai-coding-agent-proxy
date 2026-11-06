"""Usage by person and provider, and the limits that stop someone using a provider once they've used their share.

A provider here is anywhere agents' model calls go, as one thing an organization's admins can
put a limit on:

- each backend: a model server on a machine (Ollama on game-pc, llama.cpp on a phone) or a
  hosted API, named `backend:<backend name>`;
- each kind of subscription, `subscription:claude`, `subscription:codex`, `subscription:copilot`:
  account agents' traffic to that provider's own hosts (provider_accounts.PROVIDERS), whichever
  subscription or sign-in carried it.

Usage is counted by the hour for each (tenant, user, provider) from every recorded model call
(Runtime.complete), and belongs to whoever the session is for: the person who started it from the
console (`launched_by`), else its owner. It counts what this server saw: tokens the provider
reported in its answers (input and output), and model calls.

Limits are an organization's: its admins (and site admins) set them for one person, or for
everyone in it (user "*"), on one provider, over one window (the rolling 5h, 24h, 7d or 30d, or
the calendar month in UTC), in tokens. A person's own limit for a window replaces everyone's for
that window; limits for different windows all apply. Once one is reached, new model calls to that
provider answer 429 with `usage_limit`, and new sessions on it are refused, until the window moves
on or an admin raises it. A call already running finishes: limits are checked before each call,
so a person can pass a limit by the size of their last call.
"""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select

from .db import Database, kv, schema
from .provider_accounts import PROVIDERS, WINDOWS

# The rolling windows in seconds, and the calendar month (UTC): the same ones subscriptions use.
WINDOW_LABELS = {"5h": "the last 5 hours", "24h": "the last 24 hours", "7d": "the last 7 days", "30d": "the last 30 days", "month": "this month (UTC)"}
EVERYONE = "*"
# Hourly buckets are kept this long; checks and reports need at most the last 31 days.
USAGE_DAYS = 62
LOOKBACK = 32 * 86400
PROVIDER_ID = re.compile(r"(backend:[^\x00-\x1f]{1,180}|subscription:(%s))" % "|".join(PROVIDERS))
LARGEST = 10**15
REFUSED = "usage_limit"


@dataclass
class Limit:
    tenant: str
    user: str
    provider: str
    window: str
    tokens: int
    set_by: str | None = None
    set_at: float | None = None

    def public(self) -> dict[str, Any]:
        return {"user": self.user, "provider": self.provider, "window": self.window, "tokens": self.tokens, "set_by": self.set_by, "set_at": self.set_at}


def session_user(session: Any) -> str | None:
    """Whose usage a session's calls are: who started it from the console, else its owner."""
    tags = getattr(session, "tags", None) or {}
    return str(tags.get("launched_by") or tags.get("owner") or "") or None


def subscription_for_host(host: str | None) -> str | None:
    """The subscription kind whose own hosts these are (api.anthropic.com is Claude's), if any."""
    host = (host or "").lower()
    if not host:
        return None
    for kind, provider in PROVIDERS.items():
        if any(host == item or host.endswith("." + item) for item in provider["hosts"]):
            return f"subscription:{kind}"
    return None


def provider_for(record: dict[str, Any]) -> str | None:
    """Which provider a recorded call went to: its backend, or the subscription whose host it reached."""
    if record.get("kind") in {"intercept", "upgrade"}:
        return subscription_for_host(record.get("host"))
    if record.get("kind", "api") != "api":
        return None  # MCP, tunnels: not model calls
    backend = str(record.get("backend") or "")
    return f"backend:{backend}" if backend and backend != "direct" else None


def window_start(window: str, now: float) -> float:
    seconds = WINDOWS[window]
    if seconds:
        return now - seconds
    return datetime.fromtimestamp(now, timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def next_month(now: float) -> float:
    start = datetime.fromtimestamp(now, timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)).timestamp()


# With several processes: how long a process reuses the shared counts it read, and how long they're kept.
SHARED_CACHE_SECONDS = 2.0
SHARED_TTL = 33 * 86400


class UsageLimits:
    """Counting and limits. With several processes (cluster.py) every call is also counted in a shared hourly
    counter per (tenant, user, provider), which the checks read (seeded once from the database, taking the
    larger count of each hour, so nothing is counted twice), and a changed limit is announced (usage.limits)."""

    def __init__(self, db: Database, clock: Callable[[], float] = time.time, cluster: Any = None):
        self.db = db
        self.clock = clock
        self.cluster = cluster
        self.lock = threading.Lock()
        self.limits: dict[tuple[str, str, str, str], Limit] = {}
        self._shared: dict[tuple[str, str, str], tuple[float, dict[int, int]]] = {}
        if cluster is not None:
            cluster.on("usage.limits", lambda data, _: self.reload_limits())
        # Recent hourly token counts, for checking limits without the database: only for the
        # (tenant, user, provider) a limit applies to, loaded the first time one is checked.
        self._recent: dict[tuple[str, str, str], dict[int, int]] = {}
        self.reload_limits()

    @property
    def scaled(self) -> bool:
        return bool(self.cluster is not None and self.cluster.scaled)

    def reload_limits(self) -> None:
        limits = {}
        with self.db.read() as connection:
            for row in connection.execute(select(schema.usage_limits)).mappings():
                limit = Limit(row["tenant"], row["user"], row["provider"], row["window"], int(row["tokens"]), row["set_by"], row["set_at"])
                limits[(limit.tenant, limit.user, limit.provider, limit.window)] = limit
        self.limits = limits

    # Counting

    def observe(self, session: Any, record: dict[str, Any]) -> None:
        """One recorded call: added to its person's usage of its provider."""
        if record.get("error_type") == REFUSED:
            return
        provider = provider_for(record)
        user = session_user(session)
        tenant = str((session.tags or {}).get("tenant") or "")
        if not provider or not user or not tenant:
            return
        input_tokens, output_tokens = int(record.get("input_tokens") or 0), int(record.get("output_tokens") or 0)
        model_call = bool(record.get("model")) or input_tokens + output_tokens > 0
        if not model_call:
            return
        bucket = int(self.clock() // 3600 * 3600)
        keys = {"tenant": tenant, "user": user, "provider": provider, "bucket": bucket}
        amounts = {"requests": 1, "input_tokens": input_tokens, "output_tokens": output_tokens}
        self.db.writer.submit(lambda connection: kv.increment(connection, schema.provider_usage, keys, amounts))
        if self.scaled and input_tokens + output_tokens:
            try:
                self.cluster.hincrby(f"usage:{tenant}:{user}:{provider}", str(bucket), input_tokens + output_tokens, SHARED_TTL)
            except Exception:  # noqa: BLE001 - counted in the database either way; the next seed catches up
                pass
            self._shared.pop((tenant, user, provider), None)
        with self.lock:
            recent = self._recent.get((tenant, user, provider))
            if recent is not None:
                recent[bucket] = recent.get(bucket, 0) + input_tokens + output_tokens

    def _tokens(self, tenant: str, user: str, provider: str) -> dict[int, int]:
        if self.scaled:
            return self._shared_tokens(tenant, user, provider)
        return self._local_tokens(tenant, user, provider)

    def _from_database(self, tenant: str, user: str, provider: str) -> dict[int, int]:
        self.db.writer.flush()
        table = schema.provider_usage
        since = int(self.clock() - LOOKBACK)
        with self.db.read() as connection:
            rows = connection.execute(
                select(table.c.bucket, table.c.input_tokens, table.c.output_tokens)
                .where((table.c.tenant == tenant) & (table.c.user == user) & (table.c.provider == provider) & (table.c.bucket >= since))
            ).all()
        return {int(bucket): int(a) + int(b) for bucket, a, b in rows}

    def _shared_tokens(self, tenant: str, user: str, provider: str) -> dict[int, int]:
        """Every process's count, from the shared counters (seeded from the database the first time)."""
        key = (tenant, user, provider)
        cached = self._shared.get(key)
        now = self.clock()
        if cached and time.monotonic() - cached[0] < SHARED_CACHE_SECONDS:
            return dict(cached[1])
        name = f"usage:{tenant}:{user}:{provider}"
        found = self.cluster.hgetall(name)
        if "seeded" not in found:
            # Calls before the counter existed are only in the database; calls since, in both. The larger
            # count of each hour is the right one (the database's lags by the writers' queues).
            stored = self._from_database(tenant, user, provider)
            merged = {str(bucket): str(max(tokens, int(found.get(str(bucket)) or 0))) for bucket, tokens in stored.items()}
            self.cluster.hset(name, {**merged, "seeded": "1"}, SHARED_TTL)
            found = self.cluster.hgetall(name)
        cutoff = now - LOOKBACK
        buckets = {int(bucket): int(value) for bucket, value in found.items() if bucket.isdigit() and int(bucket) >= cutoff}
        self._shared[key] = (time.monotonic(), buckets)
        return dict(buckets)

    def _local_tokens(self, tenant: str, user: str, provider: str) -> dict[int, int]:
        key = (tenant, user, provider)
        with self.lock:
            recent = self._recent.get(key)
        if recent is None:
            self.db.writer.flush()
            table = schema.provider_usage
            since = int(self.clock() - LOOKBACK)
            with self.db.read() as connection:
                rows = connection.execute(
                    select(table.c.bucket, table.c.input_tokens, table.c.output_tokens)
                    .where((table.c.tenant == tenant) & (table.c.user == user) & (table.c.provider == provider) & (table.c.bucket >= since))
                ).all()
            loaded = {int(bucket): int(a) + int(b) for bucket, a, b in rows}
            with self.lock:
                # A call observed while reading is in both, or in neither: keep what's in memory if it got there first.
                recent = self._recent.setdefault(key, loaded)
        cutoff = self.clock() - LOOKBACK
        with self.lock:
            for bucket in [bucket for bucket in recent if bucket < cutoff]:
                del recent[bucket]
            return dict(recent)

    # Limits

    def applicable(self, tenant: str, user: str, provider: str) -> list[Limit]:
        """The limits on this person for this provider: their own for a window, else everyone's for it."""
        found = []
        for window in WINDOWS:
            limit = self.limits.get((tenant, user, provider, window)) or self.limits.get((tenant, EVERYONE, provider, window))
            if limit is not None:
                found.append(limit)
        return found

    def status(self, tenant: str, user: str, provider: str, limits: list[Limit] | None = None) -> list[dict[str, Any]]:
        """Each applicable limit with what's used of it, and whether it's reached (and until when)."""
        limits = self.applicable(tenant, user, provider) if limits is None else limits
        if not limits:
            return []
        now = self.clock()
        buckets = self._tokens(tenant, user, provider)
        answers = []
        for limit in limits:
            start = window_start(limit.window, now)
            # A bucket counts when its hour overlaps the window: rolling windows are whole hours.
            inside = sorted((bucket, tokens) for bucket, tokens in buckets.items() if bucket + 3600 > start)
            used = sum(tokens for _, tokens in inside)
            reached = used >= limit.tokens
            until = None
            if reached:
                if WINDOWS[limit.window]:
                    # When enough of the oldest hours leave the window to bring it under the limit.
                    left = used
                    for bucket, tokens in inside:
                        left -= tokens
                        if left < limit.tokens:
                            until = bucket + 3600 + WINDOWS[limit.window]
                            break
                else:
                    until = next_month(now)
            answers.append({
                **limit.public(), "inherited": limit.user != user, "used": used, "remaining": max(0, limit.tokens - used),
                "percent_used": min(999, math.floor(used * 100 / limit.tokens)) if limit.tokens else 100,
                "reached": reached, "until": until,
            })
        return answers

    def blocked(self, tenant: str | None, user: str | None, provider: str | None) -> dict[str, Any] | None:
        """The limit this person has reached on this provider, if any (the one that frees up last)."""
        if not tenant or not user or not provider:
            return None
        limits = self.applicable(tenant, user, provider)
        if not limits:
            return None
        reached = [item for item in self.status(tenant, user, provider, limits) if item["reached"]]
        if not reached:
            return None
        return max(reached, key=lambda item: item["until"] or 0)

    def blocked_session(self, session: Any, provider: str | None) -> dict[str, Any] | None:
        return self.blocked(str((session.tags or {}).get("tenant") or ""), session_user(session), provider)

    def set_limit(self, tenant: str, user: str, provider: str, window: str, tokens: Any, by: str | None) -> Limit:
        if window not in WINDOWS:
            raise ValueError(f"window must be one of {', '.join(WINDOWS)}")
        if not PROVIDER_ID.fullmatch(provider or ""):
            raise ValueError("provider must be backend:<name> or subscription:claude|codex|copilot")
        if isinstance(tokens, bool) or not isinstance(tokens, (int, float)) or tokens != int(tokens) or not 0 <= tokens <= LARGEST:
            raise ValueError("tokens must be a whole number, 0 or more (0 blocks the provider)")
        limit = Limit(tenant, user, provider, window, int(tokens), by, self.clock())
        values = {"tenant": tenant, "user": user, "provider": provider, "window": window, "tokens": limit.tokens, "set_by": by, "set_at": limit.set_at}
        with self.db.write() as connection:
            kv.upsert(connection, schema.usage_limits, values)
        self.limits[(tenant, user, provider, window)] = limit
        if self.cluster is not None:
            self.cluster.publish("usage.limits", {"tenant": tenant})
        return limit

    def remove_limit(self, tenant: str, user: str, provider: str, window: str) -> bool:
        table = schema.usage_limits
        with self.db.write() as connection:
            connection.execute(table.delete().where(
                (table.c.tenant == tenant) & (table.c.user == user) & (table.c.provider == provider) & (table.c.window == window)
            ))
        removed = self.limits.pop((tenant, user, provider, window), None) is not None
        if self.cluster is not None:
            self.cluster.publish("usage.limits", {"tenant": tenant})
        return removed

    def remove_user(self, tenant: str, user: str) -> None:
        """Every limit on one person, as the database holds them (not this process's cache): a
        removed user's name must not pass a limit to a later account of the same name."""
        table = schema.usage_limits
        with self.db.write() as connection:
            connection.execute(table.delete().where((table.c.tenant == tenant) & (table.c.user == user)))
        # A fresh dict, never a mutation another thread could be iterating.
        self.limits = {key: limit for key, limit in self.limits.items() if not (limit.tenant == tenant and limit.user == user)}
        if self.cluster is not None:
            self.cluster.publish("usage.limits", {"tenant": tenant})

    def tenant_limits(self, tenant: str) -> list[Limit]:
        return sorted((limit for limit in self.limits.values() if limit.tenant == tenant), key=lambda item: (item.provider, item.user != EVERYONE, item.user, list(WINDOWS).index(item.window)))

    # Reports

    def windows(self, tenant: str, user: str | None = None) -> dict[tuple[str, str], dict[str, dict[str, int]]]:
        """Each (user, provider)'s requests and tokens in every window, from the database."""
        self.db.writer.flush()
        now = self.clock()
        starts = {name: window_start(name, now) for name in WINDOWS}
        table = schema.provider_usage
        query = select(table.c.user, table.c.provider, table.c.bucket, table.c.requests, table.c.input_tokens, table.c.output_tokens).where(
            (table.c.tenant == tenant) & (table.c.bucket >= int(min(starts.values()) - 3600))
        )
        if user is not None:
            query = query.where(table.c.user == user)
        totals: dict[tuple[str, str], dict[str, dict[str, int]]] = {}
        with self.db.read() as connection:
            for name, provider, bucket, requests, input_tokens, output_tokens in connection.execute(query).all():
                windows = totals.setdefault((name, provider), {window: {"requests": 0, "input_tokens": 0, "output_tokens": 0} for window in WINDOWS})
                for window, start in starts.items():
                    if bucket + 3600 > start:
                        counted = windows[window]
                        counted["requests"] += int(requests)
                        counted["input_tokens"] += int(input_tokens)
                        counted["output_tokens"] += int(output_tokens)
        return totals

    def prune(self) -> None:
        cutoff = int(self.clock() - USAGE_DAYS * 86400)
        table = schema.provider_usage
        self.db.writer.submit(lambda connection: connection.execute(table.delete().where(table.c.bucket < cutoff)))


def refusal(blocked: dict[str, Any], provider_label: str, now: float) -> tuple[str, dict[str, Any], dict[str, str]]:
    """What an agent is told when a limit stops its call: a message, a body both OpenAI and Anthropic clients read, and Retry-After."""
    when = ""
    headers: dict[str, str] = {}
    if blocked.get("until"):
        at = datetime.fromtimestamp(blocked["until"], timezone.utc)
        when = f" It frees up at {at.strftime('%Y-%m-%d %H:%M')} UTC."
        headers["retry-after"] = str(max(1, int(blocked["until"] - now)))
    message = (
        f"Usage limit reached: {blocked['used']:,} of {blocked['tokens']:,} tokens used on {provider_label} in "
        f"{WINDOW_LABELS[blocked['window']]}.{when} Ask an admin of your organization to raise it."
    )
    body = {"type": "error", "error": {"type": "rate_limit_error", "code": REFUSED, "message": message}, "detail": message}
    return message, body, headers


def provider_label(provider: str, settings: Any = None, fleet: Any = None) -> str:
    """A provider id as people read it: a backend's name (and its machine), or the subscription's."""
    if provider.startswith("subscription:"):
        kind = provider.split(":", 1)[1]
        return f"{PROVIDERS[kind]['label']} subscription" if kind in PROVIDERS else provider
    name = provider.split(":", 1)[1] if ":" in provider else provider
    backend = settings.backends.get(name) if settings is not None else None
    machine = fleet.machines.get(backend.machine) if backend is not None and backend.machine and fleet is not None else None
    return f"{name} on {machine.name}" if machine is not None and getattr(machine, "name", None) else name

