"""The audit log: who did what to what, and whether it worked. Never what was said to a model, never a secret.

Every change to the server or an organization (accounts, settings, agents, machines and who may use
them, launches and terminals, containers, subscriptions and their tokens' hand-overs, usage limits,
sharing, retention and legal holds), every sign-in and sign-out through the console, every refusal of
an admin route or of the HTTPS interception, and every look at someone else's recordings is an
event (AuditMiddleware decides which from the request, and the code records a few itself).

Integrity: each organization's events are a hash chain (seq, prev, hash) in audit_events, which the
database refuses to change and lets retention alone delete (migrate.py's triggers and audit_guard).
A new event locks its chain's head (audit_chains), so every process appends in one order. Each hour
the worker signs a checkpoint of every chain's head with the server's audit key (Ed25519, in the
secrets table) and keeps it, in blob storage too when there is one (with S3 Object Lock, write once).
`agent-server audit verify` recomputes the chains and checks them against the checkpoints.

Exports (Server settings -> Audit, /api/config/audit): OTLP logs over HTTP (for an OpenTelemetry
Collector, and through it Splunk, Elastic, Sentinel, Datadog and the like), syslog (RFC 5424, TLS),
a signed webhook, and NDJSON files in blob storage. Each has a cursor, so an export that was down
catches up; the backlog is a metric (agent_server_audit_backlog). Exports' secrets come from the
environment: an export names the variable (token_env), never the value.

Events are CloudEvents (type mesh.audit.<category>.<action>) with an OCSF-like body: tenant, actor
(user, via, ip, user agent), action, target, outcome, reason, and the request, session and machine.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import socket
import ssl
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from sqlalchemy import and_, func, or_, select, text

from .db import Database, kv, schema


LOGGER = logging.getLogger("agent-proxy")
SIGNING_KEY = "audit-signing-key"
GENESIS = "0" * 64
EXPORT_KINDS = ("otlp", "syslog", "webhook", "blob")
EXPORT_BATCH = 500
# Fields of a request's body kept in its event, as sent: names and settings, never a password or token.
SAFE_FIELDS = {
    "visibility", "access", "team", "org", "role", "enabled", "window", "tokens", "provider", "user", "scope", "target",
    "reason", "name", "display_name", "days", "capture", "allowed_traces", "retention_days", "trace", "ended", "agent",
    "project", "model", "backend", "status", "action", "op", "teams", "orgs", "admin", "mcp", "account", "engine",
    "image", "gpus", "kind", "provisioning_allowed", "warning_percent", "stop_percent", "machine_id", "variant",
}
CATEGORIES = (
    ("/api/auth/", "authentication"), ("/api/config", "configuration"), ("/api/server", "configuration"),
    ("/api/orgs", "accounts"), ("/api/users", "accounts"), ("/api/machines", "machines"),
    ("/api/containers", "execution"), ("/api/terminals", "execution"), ("/api/provider-accounts", "subscriptions"),
    ("/api/providers", "usage"), ("/api/holds", "data"), ("/api/audit", "data"), ("/api/sessions", "sessions"),
    ("/api/agents", "sessions"), ("/api/proxy", "interception"),
)
VERBS = {"POST": "create", "PUT": "update", "PATCH": "update", "DELETE": "delete", "GET": "view"}


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def chain_hash(prev: str, body: dict[str, Any]) -> str:
    return hashlib.sha256(prev.encode() + canonical(body)).hexdigest()


def category_of(path: str) -> str:
    if path.endswith("/provider-credential"):
        return "subscriptions"
    if path.endswith("/retention") or path.startswith("/api/config/retention"):
        return "data"
    for prefix, category in CATEGORIES:
        if path.startswith(prefix):
            return category
    return "api"


class Audit:
    """Appending events, reading them, checkpoints, verifying and exporting (see the module docstring)."""

    def __init__(self, db: Database, settings: Any = None, blobs: Any = None, cluster: Any = None, clock: Callable[[], float] = time.time):
        self.db = db
        self.settings = settings
        self.blobs = blobs
        self.cluster = cluster
        self.clock = clock
        self._key: Ed25519PrivateKey | None = None
        self._lock = threading.Lock()
        self._backlog: tuple[float, int] = (0.0, 0)
        self.client = httpx.Client(timeout=15, trust_env=False)

    # Appending

    def event(
        self, kind: str, *, tenant: str | None, actor: str | None = None, via: str | None = None, ip: str | None = None,
        user_agent: str | None = None, action: str = "", target: dict[str, Any] | None = None, outcome: str = "success",
        reason: str | None = None, request_id: str | None = None, session_id: str | None = None, machine_id: str | None = None,
        changes: dict[str, Any] | None = None, status: int | None = None,
    ) -> dict[str, Any]:
        """The event as it's kept (before its place in the chain)."""
        now = self.clock()
        return {
            "id": str(uuid.uuid4()), "time": now, "type": f"mesh.audit.{kind}", "tenant": tenant or "root", "actor": actor, "outcome": outcome,
            "event": {
                "specversion": "1.0", "source": f"agent-server/{self.cluster.pod_id if self.cluster is not None else 'local'}",
                "time": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "data": {
                    "tenant": tenant or "root", "actor": {"user": actor, "via": via, "ip": ip, "user_agent": (user_agent or "")[:300] or None},
                    "action": action, "target": target or {}, "outcome": outcome, "reason": reason, "status": status,
                    "request_id": request_id, "session_id": session_id, "machine_id": machine_id, "changes": changes or {},
                },
            },
        }

    def record(self, entry: dict[str, Any], now: bool = False) -> None:
        """Append it: now (in this call, before an admin's change is answered) or behind (the write queue)."""
        if now:
            with self.db.write() as connection:
                self._append(connection, entry)
        else:
            self.db.writer.submit(lambda connection: self._append(connection, entry))

    def _append(self, connection: Any, entry: dict[str, Any]) -> None:
        chains = schema.audit_chains
        tenant = entry["tenant"]
        kv.upsert_absent(connection, chains, {"tenant": tenant, "seq": 0, "head": GENESIS})
        row = connection.execute(select(chains.c.seq, chains.c.head).where(chains.c.tenant == tenant).with_for_update()).first()
        seq, prev = int(row[0]) + 1, str(row[1])
        body = {key: entry[key] for key in ("id", "time", "type", "tenant", "actor", "outcome", "event")}
        body["seq"] = seq
        digest = chain_hash(prev, body)
        connection.execute(schema.audit_events.insert().values(
            tenant=tenant, seq=seq, id=entry["id"], time=entry["time"], type=entry["type"], actor=entry["actor"],
            outcome=entry["outcome"], event=entry["event"], prev=prev, hash=digest,
        ))
        connection.execute(chains.update().where(chains.c.tenant == tenant).values(seq=seq, head=digest))

    # Reading

    def query(self, tenant: str | None = None, *, kind: str | None = None, actor: str | None = None, outcome: str | None = None,
              since: float | None = None, before: float | None = None, before_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        self.db.writer.flush()
        table = schema.audit_events
        query = select(table).order_by(table.c.time.desc(), table.c.id.desc()).limit(max(1, min(limit, 5000)))
        if tenant is not None:
            query = query.where(table.c.tenant == tenant)
        if kind:
            query = query.where(table.c.type.startswith(kind if kind.startswith("mesh.audit.") else f"mesh.audit.{kind}"))
        if actor:
            query = query.where(table.c.actor == actor)
        if outcome:
            query = query.where(table.c.outcome == outcome)
        if since is not None:
            query = query.where(table.c.time >= since)
        if before is not None:
            # (time, id) is the order: events sharing the page's last time aren't skipped.
            query = query.where(or_(table.c.time < before, and_(table.c.time == before, table.c.id < before_id))) if before_id else query.where(table.c.time < before)
        with self.db.read() as connection:
            rows = connection.execute(query).mappings().all()
        return [public(row) for row in rows]

    # Checkpoints and verifying

    def key(self) -> Ed25519PrivateKey:
        with self._lock:
            if self._key is None:
                with self.db.write() as connection:
                    pem = kv.get(connection, schema.secrets, SIGNING_KEY)
                    if pem is None:
                        pem = Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
                        kv.put(connection, schema.secrets, SIGNING_KEY, pem)
                self._key = serialization.load_pem_private_key(bytes(pem), password=None)  # type: ignore[assignment]
            return self._key  # type: ignore[return-value]

    def public_key(self) -> tuple[str, str]:
        """(key id, the public key as base64) for checking checkpoints elsewhere."""
        raw = self.key().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return hashlib.sha256(raw).hexdigest()[:16], base64.b64encode(raw).decode()

    def checkpoint(self) -> dict[str, Any] | None:
        """Sign every chain's head now; None when nothing was recorded since the last one."""
        self.db.writer.flush()
        with self.db.read() as connection:
            heads = {row[0]: [int(row[1]), str(row[2])] for row in connection.execute(select(schema.audit_chains.c.tenant, schema.audit_chains.c.seq, schema.audit_chains.c.head))}
            last = connection.execute(select(schema.audit_checkpoints.c.heads).order_by(schema.audit_checkpoints.c.time.desc()).limit(1)).scalar()
        if not heads or heads == last:
            return None
        now = self.clock()
        key_id, _ = self.public_key()
        document = {"time": now, "heads": heads, "key_id": key_id}
        signature = base64.b64encode(self.key().sign(canonical(document))).decode()
        record = {"id": uuid.uuid4().hex, "time": now, "heads": heads, "signature": signature, "key_id": key_id, "location": None}
        if self.blobs is not None:
            location = f"audit/checkpoints/{datetime.fromtimestamp(now, timezone.utc).strftime('%Y/%m/%d/%H%M%S')}-{record['id'][:8]}.json"
            try:
                self.blobs.put(location, canonical({**document, "signature": signature}))
                record["location"] = location
            except Exception as exc:  # noqa: BLE001 - kept in the database either way
                LOGGER.warning("audit checkpoint couldn't go to blob storage: %s", exc)
        with self.db.write() as connection:
            connection.execute(schema.audit_checkpoints.insert().values(**record))
        return record

    def verify(self, tenant: str | None = None) -> dict[str, Any]:
        """Recompute each chain and check the checkpoints' signatures and heads. {"ok", "tenants": {...}, "problems": [...]}"""
        self.db.writer.flush()
        problems: list[str] = []
        results: dict[str, Any] = {}
        table = schema.audit_events
        with self.db.read() as connection:
            tenants = [tenant] if tenant else [row[0] for row in connection.execute(select(schema.audit_chains.c.tenant))]
            checkpoints = connection.execute(select(schema.audit_checkpoints).order_by(schema.audit_checkpoints.c.time)).mappings().all()
            chains = {row[0]: (int(row[1]), str(row[2])) for row in connection.execute(select(schema.audit_chains.c.tenant, schema.audit_chains.c.seq, schema.audit_chains.c.head))}
            for name in tenants:
                previous: str | None = None
                count = 0
                first = None
                hashes: dict[int, str] = {}
                for row in connection.execute(select(table).where(table.c.tenant == name).order_by(table.c.seq)).mappings():
                    count += 1
                    first = first if first is not None else int(row["seq"])
                    if previous is not None and row["prev"] != previous:
                        problems.append(f"{name}: event {row['seq']} doesn't follow the one before it")
                    body = {key: row[key] for key in ("id", "time", "type", "tenant", "actor", "outcome", "event")}
                    body["seq"] = int(row["seq"])
                    if chain_hash(str(row["prev"]), body) != row["hash"]:
                        problems.append(f"{name}: event {row['seq']} was changed after it was recorded")
                    previous = str(row["hash"])
                    hashes[int(row["seq"])] = str(row["hash"])
                head = chains.get(name)
                if head and count and hashes.get(head[0]) != head[1]:
                    problems.append(f"{name}: the chain's head isn't its last event")
                results[name] = {"events": count, "first": first, "head": head[0] if head else None}
                for checkpoint in checkpoints:
                    seq_head = (checkpoint["heads"] or {}).get(name)
                    if seq_head and int(seq_head[0]) in hashes and hashes[int(seq_head[0])] != seq_head[1]:
                        problems.append(f"{name}: event {seq_head[0]} differs from the checkpoint of {_when(checkpoint['time'])}")
        public_key = self.key().public_key()
        for checkpoint in checkpoints:
            document = {"time": checkpoint["time"], "heads": checkpoint["heads"], "key_id": checkpoint["key_id"]}
            try:
                public_key.verify(base64.b64decode(checkpoint["signature"]), canonical(document))
            except Exception:  # noqa: BLE001
                problems.append(f"the checkpoint of {_when(checkpoint['time'])} isn't signed by this server's audit key")
        return {"ok": not problems, "tenants": results, "checkpoints": len(checkpoints), "problems": problems}

    # Retention

    def prune(self, policy_for: Callable[[str], dict[str, Any]], now: datetime) -> int:
        """Delete each organization's events older than its audit days (never under the floor), unless held."""
        from .retention import AUDIT_FLOOR

        removed = 0
        with self.db.read() as connection:
            tenants = [row[0] for row in connection.execute(select(schema.audit_chains.c.tenant))]
        for tenant in tenants:
            days = max(AUDIT_FLOOR, int((policy_for(tenant)["days"].get("audit") or 0) or 365))
            cutoff = now.timestamp() - days * 86400
            holds = getattr(self, "held", None)
            if holds is not None and holds(tenant):
                continue
            with self.db.write() as connection:
                connection.execute(schema.audit_guard.update().where(schema.audit_guard.c.id == 1).values(allow=1))
                result = connection.execute(schema.audit_events.delete().where(and_(schema.audit_events.c.tenant == tenant, schema.audit_events.c.time < cutoff)))
                connection.execute(schema.audit_guard.update().where(schema.audit_guard.c.id == 1).values(allow=0))
            removed += int(result.rowcount or 0)
        return removed

    # Exports

    def exports(self) -> list[dict[str, Any]]:
        config = self.config()
        return [item for item in config.get("exports") or [] if isinstance(item, dict) and item.get("enabled", True)]

    def config(self) -> dict[str, Any]:
        with self.db.read() as connection:
            value = kv.get(connection, schema.settings, "audit")
        return value if isinstance(value, dict) else {"exports": [], "checkpoint_minutes": 60}

    def set_config(self, document: Any) -> dict[str, Any]:
        config = validate_config(document)
        with self.db.write() as connection:
            kv.put(connection, schema.settings, "audit", config)
        return config

    def export_once(self) -> int:
        """Send each export what it hasn't had yet, a batch at a time; how many events went out."""
        sent = 0
        for export in self.exports():
            name = export["name"]
            with self.db.read() as connection:
                cursor = kv.get_meta(connection, f"audit_export:{name}") or {"time": 0.0, "id": ""}
            events = self._after(cursor)
            if not events:
                continue
            try:
                deliver(export, events, self.blobs, self.client)
            except Exception as exc:  # noqa: BLE001 - tried again next round; the backlog shows it
                LOGGER.warning("audit export %s failed: %s", name, exc)
                continue
            last = events[-1]
            with self.db.write() as connection:
                kv.put_meta(connection, f"audit_export:{name}", {"time": last["time_epoch"], "id": last["id"], "at": self.clock()})
            sent += len(events)
        self._backlog = (0.0, 0)
        return sent

    def _after(self, cursor: dict[str, Any], limit: int = EXPORT_BATCH) -> list[dict[str, Any]]:
        table = schema.audit_events
        since, last_id = float(cursor.get("time") or 0), str(cursor.get("id") or "")
        with self.db.read() as connection:
            rows = connection.execute(
                select(table).where(or_(table.c.time > since, and_(table.c.time == since, table.c.id > last_id)))
                .order_by(table.c.time, table.c.id).limit(limit)
            ).mappings().all()
        return [public(row) for row in rows]

    def backlog(self) -> int:
        """Events the slowest export hasn't sent yet (cached for half a minute: it's a metric)."""
        at, count = self._backlog
        if time.monotonic() - at < 30:
            return count
        exports = self.exports()
        if not exports:
            self._backlog = (time.monotonic(), 0)
            return 0
        slowest = None
        with self.db.read() as connection:
            for export in exports:
                cursor = kv.get_meta(connection, f"audit_export:{export['name']}") or {"time": 0.0}
                slowest = float(cursor.get("time") or 0) if slowest is None else min(slowest, float(cursor.get("time") or 0))
            count = int(connection.execute(select(func.count()).select_from(schema.audit_events).where(schema.audit_events.c.time > (slowest or 0))).scalar() or 0)
        self._backlog = (time.monotonic(), count)
        return count

    def close(self) -> None:
        self.client.close()


def public(row: Any) -> dict[str, Any]:
    """An event as the console, exports and SIEMs get it: a CloudEvent with its place in the chain."""
    event = dict(row["event"] or {})
    return {
        **event, "id": row["id"], "type": row["type"], "tenant": row["tenant"], "actor": row["actor"], "outcome": row["outcome"],
        "chain": {"seq": int(row["seq"]), "prev": row["prev"], "hash": row["hash"]}, "time_epoch": float(row["time"]),
    }


def validate_config(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise ValueError("Send {exports: [...], checkpoint_minutes}")
    minutes = document.get("checkpoint_minutes", 60)
    if not isinstance(minutes, int) or isinstance(minutes, bool) or not 5 <= minutes <= 1440:
        raise ValueError("checkpoint_minutes is from 5 to 1440")
    exports = []
    names = set()
    for item in document.get("exports") or []:
        if not isinstance(item, dict):
            raise ValueError("Each export is {name, kind, url, ...}")
        name = str(item.get("name") or "").strip()
        if not name or not name.replace("-", "").replace("_", "").isalnum() or len(name) > 40 or name in names:
            raise ValueError("Each export has a name of its own: letters, numbers, - and _")
        names.add(name)
        kind = item.get("kind")
        if kind not in EXPORT_KINDS:
            raise ValueError(f"{name}: kind is one of {', '.join(EXPORT_KINDS)}")
        url = str(item.get("url") or "").strip()
        scheme = urlsplit(url).scheme
        allowed = {"otlp": ("http", "https"), "webhook": ("https", "http"), "syslog": ("syslog+tls", "syslog"), "blob": ("", "blob")}[kind]
        if kind != "blob" and scheme not in allowed:
            raise ValueError(f"{name}: a {kind} export's url starts with {' or '.join(item + '://' for item in allowed)}")
        token_env = item.get("token_env")
        if token_env is not None and (not isinstance(token_env, str) or not token_env.replace("_", "").isalnum()):
            raise ValueError(f"{name}: token_env names an environment variable")
        tenants = item.get("tenants")
        if tenants is not None and (not isinstance(tenants, list) or not all(isinstance(value, str) for value in tenants)):
            raise ValueError(f"{name}: tenants is a list of organizations")
        exports.append({
            "name": name, "kind": kind, "url": url, "token_env": token_env, "tenants": tenants,
            "enabled": bool(item.get("enabled", True)), "prefix": str(item.get("prefix") or "audit/export")[:200],
        })
    return {"exports": exports, "checkpoint_minutes": minutes}


def deliver(export: dict[str, Any], events: list[dict[str, Any]], blobs: Any, client: httpx.Client) -> None:
    """Send one batch to one export; raises when it didn't get there."""
    tenants = export.get("tenants")
    if tenants:
        events = [event for event in events if event["tenant"] in tenants]
    if not events:
        return
    token = os.getenv(export["token_env"]) if export.get("token_env") else None
    kind = export["kind"]
    if kind == "otlp":
        headers = {"content-type": "application/json"}
        if token:
            headers["authorization"] = token if " " in token else f"Bearer {token}"
        url = export["url"].rstrip("/")
        url = url if url.endswith("/v1/logs") else url + "/v1/logs"
        response = client.post(url, content=json.dumps(otlp(events)), headers=headers)
        response.raise_for_status()
    elif kind == "webhook":
        body = json.dumps({"events": events}, default=str).encode()
        headers = {"content-type": "application/json"}
        if token:
            headers["x-mesh-signature"] = "sha256=" + hmac.new(token.encode(), body, hashlib.sha256).hexdigest()
        response = client.post(export["url"], content=body, headers=headers)
        response.raise_for_status()
    elif kind == "syslog":
        syslog(export["url"], events)
    else:
        if blobs is None:
            raise RuntimeError("A blob export needs blob storage (AI_PROXY_BLOB_URL)")
        first = events[0]
        stamp = datetime.fromtimestamp(first["time_epoch"], timezone.utc).strftime("%Y/%m/%d/%H%M%S")
        lines = "".join(json.dumps(event, default=str) + "\n" for event in events)
        blobs.put(f"{export.get('prefix') or 'audit/export'}/{stamp}-{first['id'][:8]}.ndjson", lines.encode())


def otlp(events: list[dict[str, Any]]) -> dict[str, Any]:
    """OTLP/HTTP JSON logs: one record a event, its attributes flat for searching."""
    def attribute(key: str, value: Any) -> dict[str, Any]:
        return {"key": key, "value": {"stringValue": "" if value is None else str(value)}}

    records = []
    for event in events:
        data = event.get("data") or {}
        records.append({
            "timeUnixNano": str(int(event["time_epoch"] * 1e9)), "severityText": "INFO" if event["outcome"] == "success" else "WARN",
            "body": {"stringValue": json.dumps(event, default=str)},
            "attributes": [
                attribute("event.name", event["type"]), attribute("event.id", event["id"]), attribute("mesh.tenant", event["tenant"]),
                attribute("user.name", event.get("actor")), attribute("event.outcome", event["outcome"]),
                attribute("source.address", (data.get("actor") or {}).get("ip")), attribute("mesh.action", data.get("action")),
                attribute("mesh.target", json.dumps(data.get("target") or {})), attribute("mesh.chain.seq", (event.get("chain") or {}).get("seq")),
            ],
        })
    return {"resourceLogs": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "agent-server"}}]}, "scopeLogs": [{"scope": {"name": "mesh.audit"}, "logRecords": records}]}]}


def syslog(url: str, events: list[dict[str, Any]]) -> None:
    """RFC 5424 over TCP (RFC 6587 octet counting), TLS with syslog+tls:// (RFC 5425)."""
    parts = urlsplit(url)
    host, port = parts.hostname or "", parts.port or (6514 if parts.scheme == "syslog+tls" else 514)
    raw = socket.create_connection((host, port), timeout=15)
    try:
        stream = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if parts.scheme == "syslog+tls" else raw
        hostname = socket.gethostname()
        for event in events:
            severity = 6 if event["outcome"] == "success" else 4
            stamp = datetime.fromtimestamp(event["time_epoch"], timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            message = f"<{13 * 8 + severity}>1 {stamp} {hostname} agent-server - {event['type']} - {json.dumps(event, default=str)}".encode()
            stream.sendall(str(len(message)).encode() + b" " + message)
    finally:
        raw.close()


def _when(seconds: float) -> str:
    return datetime.fromtimestamp(float(seconds), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class AuditMiddleware:
    """Records what requests did that the audit log keeps (see the module docstring), from the request and its answer.

    Changes are written before their answer goes out (so an answered change is always in the log); refusals
    and looks at others' recordings behind it. Bodies are read (up to 64 KB) for their field names and the
    SAFE_FIELDS values only."""

    def __init__(self, app: Any, runtime: Any):
        self.app = app
        self.runtime = runtime

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/") or (scope.get("state") or {}).get("internal"):
            await self.app(scope, receive, send)
            return
        method, path = scope.get("method", "GET"), scope["path"]
        changing = method in ("POST", "PUT", "PATCH", "DELETE") and not path.startswith(QUIET)
        looking = method == "GET" and (path.startswith(("/api/sessions/", "/api/terminals/")) or path.startswith(SIGN_IN))
        body = bytearray()

        async def receiving() -> Any:
            message = await receive()
            if changing and message.get("type") == "http.request" and len(body) < 65536:
                body.extend(message.get("body", b"")[: 65536 - len(body)])
            return message

        status: dict[str, int] = {}
        entry: dict[str, Any] = {}

        async def sending(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status["code"] = int(message["status"])
                entry.update(self._entry(scope, method, path, status["code"], bytes(body), changing, looking) or {})
                if entry and changing and status["code"] < 400:
                    # Recorded before the change is answered.
                    import asyncio

                    await asyncio.to_thread(self.runtime.audit.record, entry, True)
                    entry.clear()
            await send(message)

        await self.app(scope, receiving, sending)
        if entry:
            self.runtime.audit.record(entry)

    def _entry(self, scope: dict[str, Any], method: str, path: str, code: int, body: bytes, changing: bool, looking: bool) -> dict[str, Any] | None:
        identity = (scope.get("state") or {}).get("identity")
        refused = code in (401, 403, 407)
        if path.startswith(SIGN_IN):
            kind = "authentication.signin" if path.startswith("/api/auth/callback") else "authentication.signout"
        elif refused and (changing or code == 403):
            kind = f"{category_of(path)}.refused"
        elif changing and code < 500:
            kind = f"{category_of(path)}.{_action(method, path)}"
        elif looking and code < 400 and identity is not None and _others_recording(self.runtime, identity, path):
            kind = "data.viewed"
        else:
            return None
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers") or []}
        client = scope.get("client") or ("", 0)
        target = _target(path)
        changes: dict[str, Any] = {}
        if body:
            try:
                sent = json.loads(body)
            except ValueError:
                sent = None
            if isinstance(sent, dict):
                changes = {"fields": sorted(str(key) for key in sent)[:50], **{key: sent[key] for key in sent if key in SAFE_FIELDS}}
        tenant = getattr(identity, "tenant", None)
        if target.get("kind") == "orgs" and target.get("id"):
            tenant = target["id"]
        outcome = "success" if code < 400 else "refused" if refused or code in (404, 409, 429) else "failure"
        return self.runtime.audit.event(
            kind, tenant=tenant, actor=getattr(identity, "user", None), via=getattr(identity, "via", None), ip=str(client[0]) or None,
            user_agent=headers.get("user-agent"), action=f"{method} {_route(path)}", target=target, outcome=outcome,
            reason=None if code < 400 else f"HTTP {code}", request_id=headers.get("x-request-id"),
            session_id=target.get("id") if target.get("kind") in ("sessions", "terminals") else None,
            machine_id=target.get("id") if target.get("kind") == "machines" else None, changes=changes, status=code,
        )


# Changes too frequent and too small to be worth an event: token refreshes, the console's own sign-ins state.
QUIET = ("/api/auth/refresh", "/api/auth/switch", "/api/auth/accounts", "/api/auth/login", "/api/auth/status")
SIGN_IN = ("/api/auth/callback", "/api/auth/logout")


def _action(method: str, path: str) -> str:
    last = path.rstrip("/").rsplit("/", 1)[-1]
    if last == "provider-credential":
        return "token_delivered"
    if path.startswith("/api/auth/device"):
        return "device_decision"
    if last == "sessions" and method == "POST":
        return "started"
    if last.isalpha() and last not in ("api",) and method == "POST" and len(path.split("/")) > 4:
        return last.replace("-", "_")
    return VERBS.get(method, method.lower())


def _target(path: str) -> dict[str, Any]:
    parts = [part for part in path.split("/") if part][1:]
    if not parts:
        return {}
    target: dict[str, Any] = {"kind": parts[0]}
    if len(parts) > 1:
        target["id"] = parts[1]
    if len(parts) > 2:
        target["part"] = "/".join(parts[2:])
    return target


def _route(path: str) -> str:
    """The path with its ids folded, as the access log does (access_log.route)."""
    try:
        from .access_log import route

        return route(path)
    except Exception:  # noqa: BLE001
        return path


def _others_recording(runtime: Any, identity: Any, path: str) -> bool:
    """A look at a session's recordings (traces, chat, records, terminal) by someone it isn't theirs."""
    parts = path.split("/")
    if len(parts) < 5 or parts[4] not in ("traces", "chat", "telemetry", "requests", "trace"):
        return False
    session = runtime.sessions.get(parts[3])
    if session is None:
        return False
    owner = session.tags.get("launched_by") or session.tags.get("owner")
    return owner != identity.user
