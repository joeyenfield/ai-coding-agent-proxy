"""The server's settings as one document: for export, import, backups and editing by hand.

The document holds what the old YAML files did (backends.yaml, models.yaml, agents.yaml):
proxy, default_backend, intercept, retention_days, backends, models and agents. The server
never reads it from a file by itself; `agent-server config import` or POST /api/config/import
stores it, and `agent-server config export` or GET /api/config/export writes it out.
"""

from __future__ import annotations

import time
from typing import Any

import yaml
from sqlalchemy import select

from . import defaults
from .agents import parse_agents
from .config import BACKEND_TYPES, Settings, _replace_backends, _validate_http_url, stored_config, validate_models
from .db import kv, schema

KEYS = ("proxy", "default_backend", "intercept", "retention_days", "backends", "models", "agents")


def export(settings: Settings) -> dict[str, Any]:
    db = settings.open_database()
    with db.read() as connection:
        document, models = stored_config(connection)
        agents = {row["id"]: row["definition"] for row in connection.execute(
            select(schema.agents).order_by(schema.agents.c.position, schema.agents.c.id)).mappings()}
    return {
        "proxy": document.get("proxy") or {}, "default_backend": document.get("default_backend"),
        "intercept": document.get("intercept") or {}, "retention_days": document.get("retention_days"),
        "backends": document.get("backends") or {}, "models": models or {}, "agents": agents,
    }


def dump(document: dict[str, Any]) -> str:
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


def parse(text: str) -> dict[str, Any]:
    """A document as YAML (JSON is YAML too)."""
    value = yaml.safe_load(text)
    if not isinstance(value, dict):
        raise ValueError("A settings document is a mapping of proxy, backends, models, agents, ...")
    return value


def apply(settings: Settings, document: dict[str, Any], *, replace: bool = False) -> list[str]:
    """Store the document's parts (all of them with replace; otherwise those it has). Returns what changed.

    Everything is checked before anything is written, and written in one transaction.
    """
    if not isinstance(document, dict):
        raise ValueError("A settings document is a mapping")
    unknown = sorted(set(document) - set(KEYS))
    if unknown:
        raise ValueError(f"Unknown settings: {', '.join(unknown)} (a document has {', '.join(KEYS)})")
    parts = {key: document.get(key) for key in KEYS if key in document or replace}
    checked: dict[str, Any] = {}
    if "proxy" in parts:
        proxy = parts["proxy"] or {}
        if not isinstance(proxy, dict):
            raise ValueError("proxy is a mapping: listen_host, listen_port, url, public_url, tls")
        if proxy.get("url"):
            _validate_http_url(proxy["url"], "proxy.url")
        if proxy.get("public_url"):
            _validate_http_url(proxy["public_url"], "proxy.public_url")
        if "listen_port" in proxy and not 1 <= int(proxy["listen_port"]) <= 65535:
            raise ValueError("proxy.listen_port must be between 1 and 65535")
        checked["proxy"] = proxy
    if "intercept" in parts:
        intercept = parts["intercept"] or {}
        if not isinstance(intercept, dict) or set(intercept) - {"enabled", "passthrough", "listen_host", "listen_port", "ca_dir"}:
            raise ValueError("intercept has enabled, passthrough, listen_host and listen_port")
        intercept.pop("ca_dir", None)
        checked["intercept"] = intercept
    if "backends" in parts:
        backends = parts["backends"] or {}
        if not isinstance(backends, dict):
            raise ValueError("backends is a mapping of names to backends")
        for name, backend in backends.items():
            if not isinstance(backend, dict) or backend.get("type", "ollama") not in BACKEND_TYPES:
                raise ValueError(f"Backend {name}: type is one of {', '.join(sorted(BACKEND_TYPES))}")
            _validate_http_url(backend.get("url"), f"Backend {name}'s url")
        checked["backends"] = backends
    if "default_backend" in parts:
        checked["default_backend"] = str(parts["default_backend"] or "")
    if "retention_days" in parts:
        days = parts["retention_days"]
        if days not in (None, 0) and not 1 <= int(days) <= 36500:
            raise ValueError("retention_days is between 1 and 36500, or empty to keep everything")
        checked["retention_days"] = int(days) if days else None
    if "models" in parts:
        checked["models"] = validate_models(parts["models"] or {})
    if "agents" in parts:
        agents = parts["agents"] or {}
        if not isinstance(agents, dict):
            raise ValueError("agents is a mapping of ids to definitions")
        parse_agents(agents)
        checked["agents"] = agents
    with settings.open_database().write() as connection:
        for key in ("proxy", "default_backend", "intercept", "retention_days", "models"):
            if key in checked:
                kv.put(connection, schema.settings, key, checked[key])
        if "backends" in checked:
            _replace_backends(connection, checked["backends"])
        if "agents" in checked:
            replace_agents(connection, checked["agents"])
        kv.put_meta(connection, "config_seeded", True)
        document_now, models = stored_config(connection)
    reload(settings, document_now, models)
    return sorted(checked)


def reload(settings: Settings, document: dict[str, Any], models: dict[str, Any]) -> None:
    """Apply what's stored to the running server's settings (the listen address waits for a restart)."""
    host, port = settings.host, settings.port
    settings._configure(document, models)
    settings.host, settings.port = host, port


def replace_agents(connection: Any, agents: dict[str, Any]) -> None:
    """These agents and no others. A shipped one keeps following releases only while unchanged."""
    shipped = defaults.agents()
    connection.execute(schema.agents.delete())
    for position, (agent_id, definition) in enumerate(agents.items()):
        builtin = agent_id in shipped
        connection.execute(schema.agents.insert().values(
            id=agent_id, position=position, definition=definition, builtin=builtin,
            edited=builtin and definition != shipped[agent_id], updated_at=time.time(),
        ))
    kv.put_meta(connection, "deleted_builtin_agents", sorted(set(shipped) - set(agents)))


def put_agent(settings: Settings, agent_id: str, definition: Any) -> dict[str, Any]:
    """Add or change one agent; a shipped one stops following releases until it's reset."""
    if not isinstance(definition, dict):
        raise ValueError("An agent's definition is a mapping (name, executable, protocol, env, args, ...)")
    parse_agents({agent_id: definition})
    shipped = defaults.agents()
    with settings.open_database().write() as connection:
        row = connection.execute(select(schema.agents).where(schema.agents.c.id == agent_id)).mappings().first()
        if row is None:
            position = (connection.execute(select(schema.agents.c.position).order_by(schema.agents.c.position.desc())).scalar() or 0) + 1
            builtin = agent_id in shipped
        else:
            position, builtin = row["position"], row["builtin"]
        kv.upsert(connection, schema.agents, {
            "id": agent_id, "position": position, "definition": definition, "builtin": builtin,
            "edited": builtin and definition != shipped.get(agent_id), "updated_at": time.time(),
        })
        _undelete(connection, agent_id)
    return {"id": agent_id, "definition": definition, "builtin": builtin, "edited": builtin and definition != shipped.get(agent_id)}


def delete_agent(settings: Settings, agent_id: str) -> bool:
    with settings.open_database().write() as connection:
        row = connection.execute(select(schema.agents.c.builtin).where(schema.agents.c.id == agent_id)).first()
        if row is None:
            return False
        connection.execute(schema.agents.delete().where(schema.agents.c.id == agent_id))
        if row[0]:
            # A shipped agent stays deleted through upgrades (reset brings it back).
            deleted = set(kv.get_meta(connection, "deleted_builtin_agents") or [])
            kv.put_meta(connection, "deleted_builtin_agents", sorted(deleted | {agent_id}))
    return True


def reset_agent(settings: Settings, agent_id: str) -> dict[str, Any]:
    """A shipped agent back to this release's definition, following releases again."""
    shipped = defaults.agents()
    if agent_id not in shipped:
        raise KeyError(agent_id)
    with settings.open_database().write() as connection:
        position = connection.execute(select(schema.agents.c.position).where(schema.agents.c.id == agent_id)).scalar()
        if position is None:
            position = (connection.execute(select(schema.agents.c.position).order_by(schema.agents.c.position.desc())).scalar() or 0) + 1
        kv.upsert(connection, schema.agents, {
            "id": agent_id, "position": position, "definition": shipped[agent_id], "builtin": True, "edited": False, "updated_at": time.time(),
        })
        _undelete(connection, agent_id)
    return {"id": agent_id, "definition": shipped[agent_id], "builtin": True, "edited": False}


def _undelete(connection: Any, agent_id: str) -> None:
    deleted = set(kv.get_meta(connection, "deleted_builtin_agents") or [])
    if agent_id in deleted:
        kv.put_meta(connection, "deleted_builtin_agents", sorted(deleted - {agent_id}))
