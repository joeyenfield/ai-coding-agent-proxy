"""Coding-agent definitions loaded from config/agents.yaml.

Each agent describes how to point a CLI at a proxy session: environment
variables, command-line arguments, settings files and the API protocol it speaks.
Values are templates rendered with the session's endpoints, so supporting another
agent only requires a new YAML entry.

A template whose placeholder has no value for a session (no {context_window} when
the model's context size isn't known, no {files_dir} when the console rather than
a launcher asks) is left out: the env variable, file, mapping key or list item goes,
so the agent falls back to its own default rather than getting an empty value.

Shared MCP servers (mcp.py) a launch chose reach the agent through its mcp block: entry is one
server as the agent's config wants it ({mcp_name}, {mcp_url}), and {mcp_servers} renders as a
mapping of every chosen server's name to its entry, or as nothing without any (so a settings key
that holds it goes too). The block's env, files and args apply only when there are some, and
args_each is rendered once for each server. Those args go where the agent's args have
"{mcp_args}", or else before them (Claude Code's --mcp-config takes every value up to the next option).
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


PROTOCOLS = {"openai-chat", "openai-responses", "anthropic", "ollama"}
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
# Where in an agent's args its shared MCP servers' args go.
MCP_ARGS = "{mcp_args}"
# A template that names a placeholder without a value for this session renders to this, and is left out.
_MISSING = object()


@dataclass
class AgentDefinition:
    id: str
    name: str
    executable: str
    protocol: str
    env: dict[str, Any] = field(default_factory=dict)
    args: list[str] = field(default_factory=list)
    # Settings files the launcher writes for the session, in {files_dir}: name to template (a mapping is written as JSON).
    files: dict[str, Any] = field(default_factory=dict)
    install: str | None = None
    # The install command for each environment (ENVIRONMENTS); install is the one for any other.
    installs: dict[str, str] = field(default_factory=dict)
    homepage: str | None = None
    description: str | None = None
    recommended_for: list[str] = field(default_factory=list)
    notes: str | None = None
    # Route the agent's own HTTPS traffic through the intercept proxy instead of
    # pointing it at a proxy endpoint. Used for agents signed in to hosted accounts.
    intercept: bool = False
    # Backend type this agent's protocol works with: ollama (local models: Ollama,
    # translated and tuned by the proxy, or Strata and Docker Model Runner, passed
    # through) or anthropic (hosted pass-through). Unused for intercept agents.
    backend_type: str = "ollama"
    # How it takes shared MCP servers: entry, and env, files, args and args_each for when it has some.
    mcp: dict[str, Any] = field(default_factory=dict)

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
            # Every environment's install command, so a page can show the one for the computer it's on.
            "installs": {name: self.installs.get(name) or self.install for name in ENVIRONMENTS if self.installs.get(name) or self.install},
            "description": self.description, "recommended_for": self.recommended_for,
            "notes": self.notes, "installed": self.installed_path() is not None,
            "intercept": self.intercept, "needs_model": self.needs_model,
            "backend_type": None if self.intercept else self.backend_type, "route": self.route,
            # Whether a launch can give it shared MCP servers.
            "mcp": bool(self.mcp),
        }

    def render(
        self,
        proxy_url: str,
        session_id: str,
        model: str,
        project: str = ".",
        intercept: dict[str, Any] | None = None,
        files_dir: str | None = None,
        context_window: int | None = None,
        mcp_servers: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Return the environment, command and files used to launch this agent for a session.

        intercept holds the intercept proxy's url, ca_path and bundle_path; it is
        required for agents with intercept: true. files_dir is the folder the launcher
        writes the files to, and context_window the model's context size on the
        session's backend, when known. mcp_servers are the shared MCP servers chosen for
        it, each a name and the URL the agent reaches it at.
        """
        variables: dict[str, Any] = endpoints(proxy_url, session_id)
        variables.update({
            "model": model or "", "project": project, "session_id": session_id, "proxy_url": proxy_url,
            "files_dir": files_dir, "context_window": context_window,
        })
        servers = mcp_servers if self.mcp else None
        each = [{**variables, "mcp_name": item["name"], "mcp_url": item["url"]} for item in servers or ()]
        variables["mcp_servers"] = {
            values["mcp_name"]: _render(self.mcp.get("entry") or {"url": "{mcp_url}"}, values) for values in each
        } or None
        env = {"AI_PROXY_SESSION": session_id}
        extra = self.mcp if servers else {}
        if self.intercept:
            if not intercept or not intercept.get("url") or not intercept.get("ca_path"):
                raise ValueError(f"{self.name} needs the proxy's HTTPS intercept listener, which isn't running.")
            env.update(intercept_env(intercept, session_id))
            variables.update({
                "intercept_url": env["HTTPS_PROXY"], "ca_cert": intercept["ca_path"],
                "ca_bundle": str(intercept.get("bundle_path") or intercept["ca_path"]),
            })
        for key, value in {**self.env, **(extra.get("env") or {})}.items():
            rendered = _render(value, variables)
            if rendered is not _MISSING:
                env[key] = rendered if isinstance(rendered, str) else json.dumps(rendered, separators=(",", ":"))
        files = {}
        for name, value in {**self.files, **(extra.get("files") or {})}.items() if files_dir else ():
            rendered = _render(value, variables)
            if rendered is not _MISSING:
                files[name] = rendered if isinstance(rendered, str) else json.dumps(rendered, indent=2)
        mcp_args = [_render(str(arg), variables) for arg in extra.get("args") or []]
        for values in each if extra else ():
            mcp_args += [_render(str(arg), values) for arg in extra.get("args_each") or []]
        ordered: list[Any] = [] if MCP_ARGS in self.args else list(mcp_args)
        for arg in self.args:
            ordered += mcp_args if arg == MCP_ARGS else [_render(arg, variables)]
        args = [arg for arg in ordered if arg is not _MISSING]
        return {"env": env, "command": [self.executable, *map(str, args)], "files": files}


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


# Where an agent can be installed: an install command can differ for each.
ENVIRONMENTS = ("linux", "macos", "windows", "wsl")


def _install(value: Any) -> dict[str, Any]:
    """An agent's install: one command, or one per environment with "default" (or the Linux one) for the rest.

    WSL falls back to the Linux command, since it's Linux inside.
    """
    if isinstance(value, dict):
        installs = {name: str(value[name]) for name in ENVIRONMENTS if value.get(name)}
        if "wsl" not in installs and "linux" in installs:
            installs["wsl"] = installs["linux"]
        default = value.get("default") or installs.get("linux") or next(iter(installs.values()), None)
        return {"install": str(default) if default else None, "installs": installs}
    return {"install": str(value) if value else None, "installs": {}}


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
            files=_files(agent_id, value.get("files")),
            **_install(value.get("install")),
            homepage=value.get("homepage"),
            description=value.get("description"),
            recommended_for=list(value.get("recommended_for") or []),
            notes=value.get("notes"),
            intercept=bool(value.get("intercept", False)),
            backend_type=str(value.get("backend_type", "ollama")),
            mcp=_mcp(agent_id, value.get("mcp")),
        )
    return agents


def _files(agent_id: Any, value: Any) -> dict[str, Any]:
    files = dict(value or {})
    for name in files:
        # Plain names only: the launcher writes them inside its own folder for the session.
        if not FILE_NAME.fullmatch(str(name)):
            raise ValueError(f"Agent '{agent_id}' has a file named '{name}': use letters, numbers, '.', '_' and '-'")
    return {str(name): content for name, content in files.items()}


def _mcp(agent_id: Any, value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if not isinstance(value, dict) or set(value) - {"entry", "env", "files", "args", "args_each"}:
        raise ValueError(f"Agent '{agent_id}' has an mcp block with other keys than entry, env, files, args and args_each")
    return {**value, "files": _files(agent_id, value.get("files"))}


def _render(value: Any, variables: dict[str, Any]) -> Any:
    """A template with the session's values: a string exactly one placeholder keeps the value's type (a number stays one)."""
    if isinstance(value, str):
        whole = PLACEHOLDER.fullmatch(value)
        if whole and whole.group(1) in variables:
            replacement = variables[whole.group(1)]
            return _MISSING if replacement is None else replacement
        for key, replacement in variables.items():
            if "{" + key + "}" in value:
                if replacement is None:
                    return _MISSING
                value = value.replace("{" + key + "}", str(replacement))
        return value
    if isinstance(value, dict):
        rendered = {_render(str(key), variables): _render(item, variables) for key, item in value.items()}
        return {key: item for key, item in rendered.items() if key is not _MISSING and item is not _MISSING}
    if isinstance(value, list):
        return [item for item in (_render(item, variables) for item in value) if item is not _MISSING]
    return value
