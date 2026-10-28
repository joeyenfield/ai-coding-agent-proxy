"""MCP servers the fleet's machines share with agents.

A machine's owner lists MCP (Model Context Protocol) servers on it with `agent-machine mcp add`:
a command run over stdio, or a URL. agent-machine serves every one of them as Streamable HTTP on
a loopback port of its own, its bridge (machine/internal/mcp), behind a key it makes for each
run. Its hello names them, with the bridge's port and key; their commands, environments and
URLs stay on the machine.

An agent gets the ones its launch chose (`agent ... --mcp`, or Start an agent), each at
{proxy_url}/session/<id>/mcp/<machine>/<name> on its own machine's agent-proxy, as with model
traffic: the session id is the credential. The server relays each request to the bridge through
the machine's tunnel (tunnels.py) and records it in the session (routes/mcp.py).

Who may use a machine's MCP servers is the fleet's "mcp" feature: its owner, and the
organizations and teams it grants it. Admins need a grant like anyone else, as a server often
holds its owner's tokens. It's checked when the launch chooses them and on every request.
"""

from __future__ import annotations

import re
from typing import Any

from .auth import Identity
from .fleet import Fleet, Machine, allowed


FEATURE = "mcp"
# Every MCP server a user may use, as a launch can name it.
ALL = "all"


def usable(fleet: Fleet, identity: Identity | None, machine: Machine) -> bool:
    return allowed(identity, machine, FEATURE)


def server_id(machine: Machine, name: str) -> str:
    return f"{machine.id}/{name}"


def listed(fleet: Fleet, identity: Identity | None) -> list[dict[str, Any]]:
    """Every MCP server this user may use, on connected machines and the rest, for the console's picker."""
    found = []
    for machine in sorted(fleet.machines.values(), key=lambda item: item.name.lower()):
        if not usable(fleet, identity, machine):
            continue
        for server in (machine.mcp or {}).get("servers") or []:
            found.append({
                "id": server_id(machine, server["name"]), "machine": machine.id, "machine_name": machine.name,
                "connected": fleet.connected(machine.id), **server,
            })
    return found


def choose(fleet: Fleet, identity: Identity | None, wanted: Any) -> list[dict[str, Any]]:
    """The MCP servers a launch names: "all", a machine (by id or name) for all of its servers, or
    "<machine>/<name>". Raises ValueError for one that doesn't exist or isn't this user's to use."""
    if wanted in (None, "", []):
        return []
    if isinstance(wanted, str):
        wanted = [item.strip() for item in wanted.split(",")]
    if not isinstance(wanted, list):
        raise ValueError("Name the MCP servers as a list: machine/name, a machine, or all")
    available = {item["id"]: item for item in listed(fleet, identity)}
    chosen: dict[str, dict[str, Any]] = {}
    for raw in wanted:
        name = str(raw or "").strip()
        if not name:
            continue
        if name == ALL:
            chosen.update({key: item for key, item in available.items() if item["connected"]})
            continue
        machine_part, _, server = name.partition("/")
        machine = _machine(fleet, machine_part)
        if machine is None or not usable(fleet, identity, machine):
            raise ValueError(f"No machine {machine_part} with MCP servers you can use")
        matches = [item for item in available.values() if item["machine"] == machine.id and (not server or item["name"] == server)]
        if not matches:
            raise ValueError(f"{machine.name} shares no MCP server called {server}" if server else f"{machine.name} shares no MCP servers")
        if server and not fleet.connected(machine.id):
            raise ValueError(f"{machine.name} isn't connected, so its MCP server {server} can't be used now")
        chosen.update({item["id"]: item for item in matches if item["connected"] or server})
    return list(chosen.values())


def _machine(fleet: Fleet, value: str) -> Machine | None:
    if value in fleet.machines:
        return fleet.machines[value]
    named = [machine for machine in fleet.machines.values() if machine.name.lower() == value.lower()]
    named.sort(key=lambda machine: not fleet.connected(machine.id))
    return named[0] if named else None


def entry_name(item: dict[str, Any], chosen: list[dict[str, Any]]) -> str:
    """The name an agent knows a server by: its own, or with its machine's when two machines share one of that name."""
    if sum(other["name"] == item["name"] for other in chosen) == 1:
        return item["name"]
    machine = re.sub(r"[^a-z0-9_-]+", "-", item["machine_name"].lower()).strip("-") or item["machine"]
    return f"{machine}-{item['name']}"


def url(proxy_url: str, session_id: str, item: dict[str, Any]) -> str:
    return f"{proxy_url.rstrip('/')}/session/{session_id}/mcp/{item['id']}"


def rpc_label(message: Any) -> str:
    """What a JSON-RPC message does, for the traffic list: "tools/call search_code", "initialize"."""
    if isinstance(message, list):
        labels = [rpc_label(item) for item in message]
        return labels[0] + (f" (+{len(labels) - 1})" if len(labels) > 1 else "") if labels else "batch"
    if not isinstance(message, dict):
        return "message"
    method = str(message.get("method") or ("response" if "id" in message else "message"))
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    detail = params.get("name") or params.get("uri")
    return f"{method} {detail}" if isinstance(detail, str) and detail else method
