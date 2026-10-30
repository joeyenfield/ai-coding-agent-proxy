"""Importing an install from before the database: its files, once, into a new database.

What it reads, where earlier versions kept them:
  config/backends.yaml (or AI_PROXY_BACKENDS), config/models.yaml, config/agents.yaml
  users.json, oidc-browser-key, container-keys.json beside the old access key (certs/)
  the intercept CA (ca.pem, ca-key.pem in certs/ or AI_PROXY_CA_DIR)
  logs/machines.json and every logs/sessions/<id>/ (session.json, requests.jsonl,
  traces/, chat-index.json, terminal.json, terminal.log)
The issuer's issuer.sqlite3 and signing-key.pem are imported by agent-auth itself (issuer.State).

Nothing is deleted: once the server runs from the database, the files can be removed.
Settings and accounts are imported when a database is first set up (config.open_database);
sessions follow, a transaction each, and an interrupted import carries on at the next start.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterator

import yaml
from sqlalchemy import select

from . import Database, kv, pack, schema

LOGGER = logging.getLogger("agent-proxy")
TRACE_BATCH = 200
CHUNK = 64 * 1024


def sources(settings: Any, root: Path | None = None) -> dict[str, Path]:
    """The old files that exist, by what they are."""
    from ..config import ROOT

    base = root or ROOT
    certs = settings.users_path.parent if root is None else base / "certs"
    backends = Path(os.environ["AI_PROXY_BACKENDS"]) if os.getenv("AI_PROXY_BACKENDS") and root is None else base / "config" / "backends.yaml"
    logs = settings.log_dir if root is None else base / "logs"
    ca_dir = Path(os.environ["AI_PROXY_CA_DIR"]) if os.getenv("AI_PROXY_CA_DIR") and root is None else base / "certs"
    found = {
        "backends": backends,
        "models": Path(os.getenv("AI_PROXY_MODELS") or base / "config" / "models.yaml") if root is None else base / "config" / "models.yaml",
        "agents": Path(os.getenv("AI_PROXY_AGENTS") or base / "config" / "agents.yaml") if root is None else base / "config" / "agents.yaml",
        "users": certs / "users.json",
        "browser_key": certs / "oidc-browser-key",
        "container_keys": certs / "container-keys.json",
        "ca_cert": ca_dir / "ca.pem",
        "ca_key": ca_dir / "ca-key.pem",
        "machines": logs / "machines.json",
        "sessions": logs / "sessions",
    }
    return {name: path for name, path in found.items() if path.exists()}


def import_settings(connection: Any, settings: Any, root: Path | None = None) -> list[str]:
    """Settings, accounts, secrets and machines from the old files, into a database being set up."""
    from ..auth import _migrated, write_directory
    from ..certs import store_pair
    from ..config import _replace_backends

    found = sources(settings, root)
    done: list[str] = []
    if "backends" in found:
        document = _yaml(found["backends"])
        proxy = dict(document.get("proxy") or {})
        tls = proxy.get("tls") or {}
        if tls.get("cert") and tls.get("key"):
            # They were relative to the file's folder.
            proxy["tls"] = {key: str((found["backends"].parent / str(tls[key])).resolve()) for key in ("cert", "key")}
        kv.put(connection, schema.settings, "proxy", proxy)
        if document.get("default_backend"):
            kv.put(connection, schema.settings, "default_backend", str(document["default_backend"]))
        intercept = {key: value for key, value in (document.get("intercept") or {}).items() if key != "ca_dir"}
        kv.put(connection, schema.settings, "intercept", intercept or {"enabled": True, "passthrough": []})
        _replace_backends(connection, dict(document.get("backends") or {}))
        kv.put_meta(connection, "backends_imported", True)
        done.append("backends.yaml")
    if "models" in found:
        data = _yaml(found["models"])
        kv.put(connection, schema.settings, "models", {
            "defaults": data.get("defaults") or {}, "profiles": list(data.get("profiles") or []), "aliases": data.get("models") or {},
        })
        done.append("models.yaml")
    if "agents" in found:
        _import_agents(connection, dict(_yaml(found["agents"]).get("agents") or {}))
        done.append("agents.yaml")
    if "users" in found:
        data = json.loads(found["users"].read_text(encoding="utf-8")) or {}
        if "orgs" not in data:
            data = _migrated(data)
        write_directory(connection, data.get("users") or {}, data.get("orgs") or {})
        kv.put_meta(connection, "accounts_version", int(kv.get_meta(connection, "accounts_version", 0) or 0) + 1)
        kv.put_meta(connection, "accounts_initialized", True)
        done.append("users.json")
    if "browser_key" in found:
        kv.put(connection, schema.secrets, "oidc-browser-key", found["browser_key"].read_text(encoding="ascii").strip().encode())
        done.append("oidc-browser-key")
    if "container_keys" in found:
        keys = json.loads(found["container_keys"].read_text(encoding="utf-8")) or {}
        for name, value in keys.items():
            kv.put(connection, schema.secrets, f"container-key:{name}", str(value).encode())
        done.append("container-keys.json")
    if "ca_cert" in found and "ca_key" in found:
        store_pair(connection, found["ca_cert"].read_bytes(), found["ca_key"].read_bytes())
        done.append("the intercept CA")
    if "machines" in found:
        data = json.loads(found["machines"].read_text(encoding="utf-8")) or {}
        for item in data.get("machines") or []:
            if isinstance(item, dict) and item.get("id"):
                kv.upsert(connection, schema.machines, {"id": str(item["id"]), "data": item})
        for machine_id, value in (data.get("removed") or {}).items():
            if isinstance(value, dict):
                kv.upsert(connection, schema.machines_removed, {"id": str(machine_id), "data": value})
        done.append("machines.json")
    if "sessions" in found:
        # Sessions are many and large: config.open_database imports them next, a transaction each.
        kv.put_meta(connection, "sessions_pending", str(found["sessions"]))
    if done:
        kv.put_meta(connection, "imported", {"at": time.time(), "files": done})
        LOGGER.info("imported %s into the database", ", ".join(done))
    return done


def _import_agents(connection: Any, agents: dict[str, Any]) -> None:
    from .. import defaults

    shipped = defaults.agents()
    connection.execute(schema.agents.delete())
    for position, (agent_id, definition) in enumerate(agents.items()):
        builtin = agent_id in shipped
        connection.execute(schema.agents.insert().values(
            id=str(agent_id), position=position, definition=definition, builtin=builtin,
            # One that was never changed from what shipped then follows releases from now on.
            edited=False, updated_at=time.time(),
        ))


def import_sessions(db: Database, folder: Path | None = None) -> int:
    """Every session folder not imported yet; returns how many were."""
    if folder is None:
        with db.read() as connection:
            pending = kv.get_meta(connection, "sessions_pending")
        if not pending:
            return 0
        folder = Path(pending)
    if not folder.is_dir():
        return 0
    with db.read() as connection:
        existing = set(connection.execute(select(schema.sessions.c.id)).scalars())
    count = 0
    started = time.monotonic()
    for directory in sorted(path for path in folder.iterdir() if (path / "session.json").is_file()):
        if directory.name in existing:
            continue
        try:
            import_session(db, directory)
            count += 1
        except Exception:  # noqa: BLE001 - one broken folder mustn't stop the others
            LOGGER.exception("importing session %s failed", directory.name)
        if count and count % 25 == 0:
            LOGGER.info("imported %s sessions (%.0f s)", count, time.monotonic() - started)
    with db.write() as connection:
        kv.remove(connection, schema.meta, "sessions_pending")
    if count:
        LOGGER.info("imported %s sessions from %s in %.0f s; the folder can be removed", count, folder, time.monotonic() - started)
    return count


def import_session(db: Database, directory: Path) -> None:
    from ..sessions import Session

    data = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    session_id = str(data.get("session_id") or directory.name)
    records = list(_records(directory / "requests.jsonl"))
    if not data.get("last_activity_at") and records:
        data["last_activity_at"] = records[-1].get("timestamp")
    stored = {key: value for key, value in data.items() if key in Session.__dataclass_fields__}
    session = Session(**stored)
    with db.write() as connection:
        for record in records:
            kv.upsert(connection, schema.requests, {
                "session_id": session_id, "request_id": str(record.get("request_id") or ""),
                "timestamp": str(record.get("timestamp") or session.started_at), "kind": record.get("kind"),
                "model": record.get("model"), "backend": record.get("backend"),
                "input_tokens": int(record.get("input_tokens") or 0), "output_tokens": int(record.get("output_tokens") or 0),
                "record": record,
            })
        index = directory / "chat-index.json"
        if index.is_file():
            try:
                kv.upsert(connection, schema.chat_index, {"session_id": session_id, "data": json.loads(index.read_text(encoding="utf-8"))})
            except ValueError:
                pass
    traces = sorted((directory / "traces").glob("*.json")) if (directory / "traces").is_dir() else []
    for start in range(0, len(traces), TRACE_BATCH):
        with db.write() as connection:
            for path in traces[start:start + TRACE_BATCH]:
                body = path.read_bytes()
                kv.upsert(connection, schema.traces, {"session_id": session_id, "request_id": path.stem, "size": len(body), "body": pack(body)})
    meta = directory / "terminal.json"
    if meta.is_file():
        with db.write() as connection:
            try:
                kv.upsert(connection, schema.terminals, {"session_id": session_id, "meta": json.loads(meta.read_text(encoding="utf-8"))})
            except ValueError:
                pass
            log = directory / "terminal.log"
            if log.is_file():
                with log.open("rb") as handle:
                    offset = 0
                    while chunk := handle.read(CHUNK):
                        kv.upsert(connection, schema.terminal_chunks, {"session_id": session_id, "offset": offset, "data": chunk})
                        offset += len(chunk)
    # Written last, so a session cut short is imported again next time.
    with db.write() as connection:
        kv.upsert(connection, schema.sessions, {"id": session_id, "started_at": session.started_at, "data": session.stored()})


def _records(path: Path) -> Iterator[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            yield value


def _yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    return value if isinstance(value, dict) else {}
