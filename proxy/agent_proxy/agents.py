"""Coding-agent definitions loaded from config/agents.yaml.

Each agent describes how to point a CLI at a proxy session: environment
variables, command-line arguments and the API protocol it speaks. Values are
templates rendered with the session's endpoints, so supporting another agent
only requires a new YAML entry.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


PROTOCOLS = {"openai-chat", "openai-responses", "anthropic", "ollama"}


@dataclass
class AgentDefinition:
    id: str
    name: str
    executable: str
    protocol: str
    env: dict[str, Any] = field(default_factory=dict)
    args: list[str] = field(default_factory=list)
    install: str | None = None
    homepage: str | None = None
    description: str | None = None
    recommended_for: list[str] = field(default_factory=list)
    notes: str | None = None

    def installed_path(self) -> str | None:
        return shutil.which(self.executable)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "executable": self.executable,
            "protocol": self.protocol, "install": self.install, "homepage": self.homepage,
            "description": self.description, "recommended_for": self.recommended_for,
            "notes": self.notes, "installed": self.installed_path() is not None,
        }

    def render(self, proxy_url: str, session_id: str, model: str, project: str = ".") -> dict[str, Any]:
        """Return the environment and command used to launch this agent for a session."""
        variables = endpoints(proxy_url, session_id)
        variables.update({"model": model, "project": project, "session_id": session_id, "proxy_url": proxy_url})
        env = {"AI_PROXY_SESSION": session_id}
        for key, value in self.env.items():
            rendered = _render(value, variables)
            env[key] = rendered if isinstance(rendered, str) else json.dumps(rendered, separators=(",", ":"))
        return {"env": env, "command": [self.executable, *(_render(arg, variables) for arg in self.args)]}


def endpoints(proxy_url: str, session_id: str) -> dict[str, str]:
    base = f"{proxy_url.rstrip('/')}/session/{session_id}"
    return {"openai_url": f"{base}/v1", "anthropic_url": f"{base}/anthropic", "ollama_url": base}


def load_agents(path: Path) -> dict[str, AgentDefinition]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    agents: dict[str, AgentDefinition] = {}
    for agent_id, value in (data.get("agents") or {}).items():
        if not isinstance(value, dict):
            raise ValueError(f"Agent '{agent_id}' must be a mapping")
        protocol = value.get("protocol", "openai-chat")
        if protocol not in PROTOCOLS:
            raise ValueError(f"Agent '{agent_id}' has unsupported protocol '{protocol}'")
        agents[str(agent_id)] = AgentDefinition(
            id=str(agent_id),
            name=value.get("name", str(agent_id)),
            executable=value.get("executable", str(agent_id)),
            protocol=protocol,
            env=dict(value.get("env") or {}),
            args=[str(arg) for arg in value.get("args") or []],
            install=value.get("install"),
            homepage=value.get("homepage"),
            description=value.get("description"),
            recommended_for=list(value.get("recommended_for") or []),
            notes=value.get("notes"),
        )
    return agents


def _render(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, str):
        for key, replacement in variables.items():
            value = value.replace("{" + key + "}", replacement)
        return value
    if isinstance(value, dict):
        return {_render(str(key), variables): _render(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [_render(item, variables) for item in value]
    return value
