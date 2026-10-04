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
    # Route the agent's own HTTPS traffic through the intercept proxy instead of
    # pointing it at a proxy endpoint. Used for agents signed in to hosted accounts.
    intercept: bool = False
    # Backend type this agent's protocol works with: ollama (translated and tuned
    # by the proxy) or anthropic (hosted pass-through). Unused for intercept agents.
    backend_type: str = "ollama"

    @property
    def route(self) -> str:
        """account: own sign-in, captured in transit. hosted: hosted API pass-through. ollama: local models."""
        if self.intercept:
            return "account"
        return "hosted" if self.backend_type != "ollama" else "ollama"

    @property
    def needs_model(self) -> bool:
        return "{model}" in json.dumps([self.env, self.args])

    def installed_path(self) -> str | None:
        return shutil.which(self.executable)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "executable": self.executable,
            "protocol": self.protocol, "install": self.install, "homepage": self.homepage,
            "description": self.description, "recommended_for": self.recommended_for,
            "notes": self.notes, "installed": self.installed_path() is not None,
            "intercept": self.intercept, "needs_model": self.needs_model,
            "backend_type": None if self.intercept else self.backend_type, "route": self.route,
        }

    def render(
        self,
        proxy_url: str,
        session_id: str,
        model: str,
        project: str = ".",
        intercept: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return the environment and command used to launch this agent for a session.

        intercept holds the intercept proxy's url, ca_path and bundle_path; it is
        required for agents with intercept: true.
        """
        variables = endpoints(proxy_url, session_id)
        variables.update({"model": model or "", "project": project, "session_id": session_id, "proxy_url": proxy_url})
        env = {"AI_PROXY_SESSION": session_id}
        if self.intercept:
            if not intercept or not intercept.get("url") or not intercept.get("ca_path"):
                raise ValueError(f"{self.name} needs the proxy's HTTPS intercept listener, which isn't running.")
            env.update(intercept_env(intercept, session_id))
            variables.update({
                "intercept_url": env["HTTPS_PROXY"], "ca_cert": intercept["ca_path"],
                "ca_bundle": str(intercept.get("bundle_path") or intercept["ca_path"]),
            })
        for key, value in self.env.items():
            rendered = _render(value, variables)
            env[key] = rendered if isinstance(rendered, str) else json.dumps(rendered, separators=(",", ":"))
        return {"env": env, "command": [self.executable, *(_render(arg, variables) for arg in self.args)]}


def intercept_env(intercept: dict[str, Any], session_id: str) -> dict[str, str]:
    """Environment that sends a process tree's HTTPS through the intercept proxy.

    The session id rides in the proxy URL's user name so the proxy can file
    traffic under the right session. Node reads NODE_EXTRA_CA_CERTS; the bundle
    (system roots plus the local CA) covers curl, git, Python and other tools
    the agent runs, so their traffic is captured instead of failing TLS checks.
    """
    scheme, _, address = str(intercept["url"]).partition("://")
    proxy = f"{scheme}://{session_id}:agent-proxy@{address}"
    bundle = str(intercept.get("bundle_path") or intercept["ca_path"])
    no_proxy = "localhost,127.0.0.1,::1"
    return {
        "HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "https_proxy": proxy, "http_proxy": proxy,
        "NO_PROXY": no_proxy, "no_proxy": no_proxy,
        # Node 24+ only applies the variables above to fetch() when this is set.
        "NODE_USE_ENV_PROXY": "1",
        "NODE_EXTRA_CA_CERTS": str(intercept["ca_path"]),
        "SSL_CERT_FILE": bundle, "REQUESTS_CA_BUNDLE": bundle, "CURL_CA_BUNDLE": bundle, "GIT_SSL_CAINFO": bundle,
    }


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
            intercept=bool(value.get("intercept", False)),
            backend_type=str(value.get("backend_type", "ollama")),
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
