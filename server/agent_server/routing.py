"""Full names for resources another server lets this one use over a bridge (bridges.py).

A resource on this server keeps its short name: a backend's name, an agent's id, a machine's
name. A resource that lives on a linked server only ever appears under its full name, which
says where it lives:

    provider:server-b/org-b1/provider-b1
    mcp:server-b/org-b1/gpu-box/mcp-b1
    agent:server-a/org-a1/agent-a1
    machine:server-b/org-b1/gpu-box

server is the other server's mesh id (links.py), a slug chosen once, never its hostname. The
separators are ":" and "/", which no organization, agent, backend, machine or MCP server name
can contain (those allow letters, digits, ".", "_" and "-"), so a full name never matches a
local resource and is never ambiguous. The API carries the parts separately (Name.public);
the text form is for the CLI, logs and the console.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


KINDS = ("provider", "mcp", "agent", "machine")
# What a part of a full name may be: what the names themselves allow (backends, agents and MCP
# servers allow "." and "_", machine names spaces too, so those are refused here).
PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
# A server's mesh id: lower case letters, digits and "-".
MESH_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


@dataclass(frozen=True)
class Name:
    kind: str
    server: str
    org: str
    name: str
    machine: str | None = None

    def text(self) -> str:
        middle = f"{self.machine}/" if self.machine else ""
        return f"{self.kind}:{self.server}/{self.org}/{middle}{self.name}"

    def public(self) -> dict[str, Any]:
        return {"kind": self.kind, "server": self.server, "org": self.org, "machine": self.machine, "name": self.name, "text": self.text()}


def is_full(value: Any) -> bool:
    """Whether value looks like a full name (a remote resource) rather than a local short name."""
    return isinstance(value, str) and ":" in value and value.split(":", 1)[0] in KINDS


def parse(value: Any, kind: str | None = None) -> Name:
    """A full name from its text form. Raises ValueError, saying what's wrong."""
    if not isinstance(value, str) or ":" not in value:
        raise ValueError(f"{value!r} isn't a full name (kind:server/organization/name)")
    found, _, rest = value.partition(":")
    if found not in KINDS:
        raise ValueError(f"{value!r}: the kind must be one of {', '.join(KINDS)}")
    if kind is not None and found != kind:
        raise ValueError(f"{value!r} names a {found}, not a {kind}")
    parts = rest.split("/")
    expected = 4 if found == "mcp" else 3
    if len(parts) != expected:
        shape = "server/organization/machine/name" if found == "mcp" else "server/organization/name"
        raise ValueError(f"{value!r}: a {found} is named {found}:{shape}")
    server, org = parts[0], parts[1]
    if not MESH_ID.fullmatch(server):
        raise ValueError(f"{value!r}: {server!r} isn't a server's mesh id")
    for part in parts[1:]:
        if not PART.fullmatch(part):
            raise ValueError(f"{value!r}: {part!r} isn't a name")
    if found == "mcp":
        return Name(found, server, org, parts[3], machine=parts[2])
    return Name(found, server, org, parts[2])


def full(kind: str, server: str, org: str, name: str, machine: str | None = None) -> str:
    return Name(kind, server, org, name, machine).text()
