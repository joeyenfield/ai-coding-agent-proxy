from __future__ import annotations

import copy
import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


def _find_root() -> Path:
    """Locate the repository root that holds config/, logs/ and benchmarks/."""
    configured = os.getenv("AI_PROXY_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    for candidate in (Path(__file__).resolve().parents[2], Path.cwd(), *Path.cwd().parents):
        if (candidate / "config" / "backends.yaml").exists():
            return candidate
    return Path.cwd()


ROOT = _find_root()
# Session backend for agents whose traffic goes to their vendor through the intercept proxy.
DIRECT_BACKEND = "direct"
# Where the agent-proxy on each agent machine (the Go client) listens by default, loopback
# only: its API and model endpoints, and the HTTPS proxy account agents use. Both carry
# everything to the server. (8181 is the server's own default without a config.)
LOCAL_PROXY_PORT = 8184
LOCAL_INTERCEPT_PORT = 8183
LOCAL_PROXY_URL = f"http://127.0.0.1:{LOCAL_PROXY_PORT}"
# ollama: Ollama servers, translated and tuned by the proxy. strata: a local Strata
# server, and docker: Docker Model Runner; both speak the agents' APIs themselves and
# are passed through. anthropic: hosted pass-through.
BACKEND_TYPES = {"ollama", "strata", "docker", "anthropic"}
# Agent protocols the pass-through local servers speak natively.
NATIVE_PROTOCOLS = {
    "strata": {"openai-chat", "openai-responses", "anthropic"},
    "docker": {"openai-chat", "openai-responses", "anthropic", "ollama"},
}
# Model servers run on the fleet's machines: a backend of these types is added from a machine
# (a model server it reports, or a container it runs), never by URL alone. Hosted APIs are the exception.
MACHINE_TYPES = {"ollama", "strata", "docker"}
PROVIDER_NAMES = {"ollama": "Ollama", "strata": "Strata", "docker": "Docker Model Runner", "anthropic": "Anthropic API"}


def serves(backend: "Backend", backend_type: str, protocol: str | None = None) -> bool:
    """Whether an agent declared for backend_type, speaking protocol, can use this backend.

    Agents declared for Ollama are local-model agents; Strata and Docker Model
    Runner serve them too when they speak the agent's protocol. Only Strata lacks
    Ollama's native /api.
    """
    if backend.type == backend_type:
        return True
    spoken = NATIVE_PROTOCOLS.get(backend.type)
    return backend_type == "ollama" and spoken is not None and (protocol is None or protocol in spoken)


@dataclass(frozen=True)
class Backend:
    name: str
    url: str
    type: str = "ollama"
    # The fleet machine that runs it (fleet.py). Local model servers come from machines;
    # only hosted APIs, and backends set up before machines did this, have none.
    machine: str | None = None
    # A container its machine runs for this backend (see containers.py); None for model servers run another way.
    container: dict[str, Any] | None = field(default=None, compare=False, hash=False)

    def public(self) -> dict[str, Any]:
        value: dict[str, Any] = {"type": self.type, "url": self.url}
        if self.machine:
            value["machine"] = self.machine
        if self.container:
            value["container"] = self.container
        return value


def _backend(name: str, value: dict[str, Any]) -> Backend:
    """A backend from backends.yaml. Fields from older versions (metrics_url: machines report their own load now) are dropped."""
    return Backend(name=name, **{key: item for key, item in value.items() if key in Backend.__dataclass_fields__ and key != "name"})


@dataclass
class ModelProfile:
    """Ollama settings the proxy applies to a model before forwarding."""

    ollama_model: str
    options: dict[str, Any] = field(default_factory=dict)
    keep_alive: str | int | None = None
    think: bool | str | None = None
    translate_openai: bool = True
    matched: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "ollama_model": self.ollama_model,
            "options": self.options,
            "keep_alive": self.keep_alive,
            "think": self.think,
            "translate_openai": self.translate_openai,
            "matched": self.matched,
        }


@dataclass
class InterceptSettings:
    """The HTTPS forward proxy that records traffic agents send to hosted services."""

    enabled: bool = True
    # Agents reach it through the agent-proxy on their own machine, which tunnels here, so
    # the server listens itself only when listen_port is set (tests, the demo). Kept on
    # loopback by default: the listener is an open forward proxy for whoever can reach it.
    listen_host: str = "127.0.0.1"
    listen_port: int | None = None
    # Host patterns tunnelled without decryption (recorded as a connection only).
    passthrough: list[str] = field(default_factory=list)
    ca_dir: Path = field(default_factory=lambda: ROOT / "certs")

    @classmethod
    def load(cls, data: dict[str, Any]) -> "InterceptSettings":
        port = os.getenv("AI_PROXY_INTERCEPT_PORT") or data.get("listen_port")
        return cls(
            enabled=os.getenv("AI_PROXY_INTERCEPT", "1" if data.get("enabled", True) else "0") != "0",
            listen_host=os.getenv("AI_PROXY_INTERCEPT_HOST", str(data.get("listen_host", "127.0.0.1"))),
            listen_port=int(port) if port not in (None, "") else None,
            passthrough=[str(item) for item in data.get("passthrough") or []],
            ca_dir=Path(os.getenv("AI_PROXY_CA_DIR", str(data.get("ca_dir") or ROOT / "certs"))).expanduser().resolve(),
        )

    def tunnelled(self, host: str) -> bool:
        lowered = host.lower()
        return any(fnmatch.fnmatchcase(lowered, pattern.lower()) for pattern in self.passthrough)

    def public(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled, "listen_host": self.listen_host,
            "listen_port": self.listen_port, "passthrough": self.passthrough,
        }


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8181
    proxy_url: str = "http://127.0.0.1:8181"
    log_dir: Path = field(default_factory=lambda: ROOT / "logs")
    backends_file: Path = field(default_factory=lambda: ROOT / "config" / "backends.yaml")
    models_file: Path = field(default_factory=lambda: ROOT / "config" / "models.yaml")
    agents_file: Path = field(default_factory=lambda: ROOT / "config" / "agents.yaml")
    ui_dir: Path = field(default_factory=lambda: ROOT / "ui" / "dist")
    # The builds the server hands out (tools/dist's dist/<version>/), see downloads.py.
    dist_dir: Path = field(default_factory=lambda: ROOT / "dist")
    # The documentation (docs/: Markdown pages and their screenshots), which the console shows under /help.
    docs_dir: Path = field(default_factory=lambda: ROOT / "docs")
    # Access key for watching and typing into agents' terminals from other devices.
    # Settings built in code (tests) keep it under log_dir; see remote_key_path.
    remote_key_file: Path | None = None
    # HTTPS for the console and API (proxy.tls in backends.yaml): certificate and key files.
    tls_cert: Path | None = None
    tls_key: Path | None = None
    # Where other devices reach the console (proxy.public_url), such as a DNS name the
    # router forwards here. Pairing links use it ahead of this computer's own addresses.
    public_url: str | None = None
    default_backend: str = "laptop"
    backends: dict[str, Backend] = field(default_factory=dict)
    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    model_defaults: dict[str, Any] = field(default_factory=dict)
    model_profiles: list[dict[str, Any]] = field(default_factory=list)
    # Off unless loaded from backends.yaml, so Settings() built in code never opens a port.
    intercept: "InterceptSettings" = field(default_factory=lambda: InterceptSettings(enabled=False))

    @classmethod
    def load(cls) -> "Settings":
        backends_file = Path(
            os.getenv("AI_PROXY_BACKENDS", str(ROOT / "config" / "backends.yaml"))
        ).resolve()
        backend_data = _read_yaml(backends_file)
        proxy_data = backend_data.get("proxy") or {}
        configured_host = str(proxy_data.get("listen_host", "127.0.0.1"))
        configured_port = int(proxy_data.get("listen_port", 8181))
        configured_url = str(
            proxy_data.get("url") or f"http://{configured_host}:{configured_port}"
        ).rstrip("/")
        environment_host = os.getenv("AI_PROXY_HOST")
        environment_port = os.getenv("AI_PROXY_PORT")
        effective_host = environment_host or configured_host
        effective_port = int(environment_port or configured_port)
        environment_url = os.getenv("AI_PROXY_URL")
        if environment_url:
            effective_url = environment_url
        elif environment_host or environment_port:
            client_host = "127.0.0.1" if effective_host in {"0.0.0.0", "::"} else effective_host
            effective_url = f"http://{client_host}:{effective_port}"
        else:
            effective_url = configured_url
        settings = cls(
            host=effective_host,
            port=effective_port,
            proxy_url=effective_url.rstrip("/"),
            log_dir=Path(os.getenv("AI_PROXY_LOG_DIR", str(ROOT / "logs"))).resolve(),
            backends_file=backends_file,
            models_file=Path(os.getenv("AI_PROXY_MODELS", str(ROOT / "config" / "models.yaml"))).resolve(),
            agents_file=Path(os.getenv("AI_PROXY_AGENTS", str(ROOT / "config" / "agents.yaml"))).resolve(),
            ui_dir=Path(os.getenv("AI_PROXY_UI_DIR", str(ROOT / "ui" / "dist"))).resolve(),
            dist_dir=Path(os.getenv("AI_PROXY_DIST_DIR", str(ROOT / "dist"))).resolve(),
            docs_dir=Path(os.getenv("AI_PROXY_DOCS_DIR", str(ROOT / "docs"))).resolve(),
            remote_key_file=Path(os.getenv("AI_PROXY_REMOTE_KEY_FILE", str(ROOT / "certs" / "remote-access-key"))).resolve(),
        )
        settings.default_backend = os.getenv(
            "AI_PROXY_DEFAULT_BACKEND", backend_data.get("default_backend", "laptop")
        )
        settings.backends = {
            name: _backend(name, value)
            for name, value in backend_data.get("backends", {}).items()
        }
        settings.intercept = InterceptSettings.load(backend_data.get("intercept") or {})
        tls = proxy_data.get("tls") or {}
        if tls.get("cert") and tls.get("key"):
            settings.tls_cert = (backends_file.parent / str(tls["cert"])).resolve()
            settings.tls_key = (backends_file.parent / str(tls["key"])).resolve()
        settings.public_url = str(proxy_data.get("public_url") or "").rstrip("/") or None
        model_data = _read_yaml(settings.models_file)
        settings.models = model_data.get("models") or {}
        settings.model_defaults = model_data.get("defaults") or {}
        settings.model_profiles = list(model_data.get("profiles") or [])
        return settings

    @property
    def remote_key_path(self) -> Path:
        return self.remote_key_file or self.log_dir / "remote" / "access-key"

    def upcoming_listen(self) -> tuple[str, int]:
        """The listen address in backends.yaml now, which a restart will use (environment overrides win)."""
        proxy = _read_yaml(self.backends_file).get("proxy") or {}
        host = os.getenv("AI_PROXY_HOST") or str(proxy.get("listen_host", self.host))
        try:
            port = int(os.getenv("AI_PROXY_PORT") or proxy.get("listen_port", self.port))
        except (TypeError, ValueError):
            port = self.port
        return host, port

    @property
    def users_path(self) -> Path:
        """Accounts, sign-ins and launcher tokens (hashed), kept beside the access key."""
        return self.remote_key_path.parent / "users.json"

    @property
    def container_keys_path(self) -> Path:
        """API keys the proxy generated for its Strata containers, kept beside the access key."""
        return self.remote_key_path.parent / "container-keys.json"

    def backend(self, name: str | None) -> Backend:
        selected = name or self.default_backend
        try:
            backend = self.backends[selected]
        except KeyError as exc:
            choices = ", ".join(sorted(self.backends)) or "none configured"
            raise ValueError(f"Unknown backend '{selected}'. Available: {choices}") from exc
        if backend.type not in BACKEND_TYPES:
            raise ValueError(f"Unsupported backend type: {backend.type}")
        return backend

    def backend_for(self, backend_type: str, name: str | None = None, protocol: str | None = None) -> Backend:
        """The named backend, checked against what the agent needs, or the best one that serves it.

        Prefers the default backend when it serves the agent, then a backend of
        exactly the declared type, then any other that serves it (Strata or
        Docker Model Runner for a local-model agent).
        """
        if name:
            backend = self.backend(name)
            if not serves(backend, backend_type, protocol):
                reason = (
                    f" (it speaks native Ollama, which {PROVIDER_NAMES[backend.type]} doesn't have)"
                    if backend.type in NATIVE_PROTOCOLS and backend_type == "ollama" else ""
                )
                raise ValueError(f"Backend '{name}' is {backend.type}; this agent needs a backend of type {backend_type}{reason}")
            return backend
        default = self.backends.get(self.default_backend)
        if default and serves(default, backend_type, protocol):
            return default
        candidates = [backend for backend in self.backends.values() if serves(backend, backend_type, protocol)]
        if candidates:
            return min(candidates, key=lambda item: item.type != backend_type)
        raise ValueError(f"No {backend_type} backend is configured in {self.backends_file.name}")

    def model_name(self, name: str) -> str:
        return (self.models.get(name) or {}).get("ollama_model", name)

    def profile(self, name: str) -> ModelProfile:
        """Resolve defaults, matching profiles, then the model alias, in that order."""
        alias = self.models.get(name) or {}
        ollama_model = alias.get("ollama_model", name)
        layers = [("defaults", self.model_defaults)]
        lowered = ollama_model.lower()
        for profile in self.model_profiles:
            patterns = profile.get("match") or []
            if isinstance(patterns, str):
                patterns = [patterns]
            if any(fnmatch.fnmatchcase(lowered, str(pattern).lower()) for pattern in patterns):
                layers.append((str(profile.get("name") or patterns[0]), profile))
                break
        if alias:
            layers.append((f"models.{name}", alias))
        result = ModelProfile(ollama_model=ollama_model)
        for label, layer in layers:
            if not layer:
                continue
            result.matched.append(label)
            result.options.update(copy.deepcopy(layer.get("options") or {}))
            for key in ("keep_alive", "think", "translate_openai"):
                if key in layer:
                    setattr(result, key, layer[key])
        return result

    def network_config(self) -> dict[str, Any]:
        """Return the editable network configuration."""
        return {
            "proxy": {
                "listen_host": self.host,
                "listen_port": self.port,
                "url": self.proxy_url,
            },
            "default_backend": self.default_backend,
            "backends": {
                name: backend.public()
                for name, backend in self.backends.items()
            },
        }

    def update_network_config(self, value: dict[str, Any]) -> dict[str, Any]:
        """Validate, persist, and apply editable network settings.

        Local model servers come from the fleet's machines, so here they can be kept
        or removed, not added or pointed somewhere else: that happens
        from a machine (fleet adoption, or the containers API). Their machine and container
        blocks are kept from the current backends. Hosted APIs can be added and edited.
        Without "backends", the backends stay as they are.
        """
        if isinstance(value, dict) and "backends" not in value:
            value = {**value, "backends": {name: backend.public() for name, backend in self.backends.items()}}
        normalized = _validate_network_config(value)
        for name, backend in normalized["backends"].items():
            if backend["type"] not in MACHINE_TYPES:
                continue
            current = self.backends.get(name)
            if current is None or current.type != backend["type"] or current.url.rstrip("/") != backend["url"]:
                raise ValueError(
                    f"Backend '{name}': {PROVIDER_NAMES[backend['type']]} servers are added from the machine that runs them "
                    "(Machines page), not by URL"
                )
            if current.machine:
                backend["machine"] = current.machine
            if current.container:
                backend["container"] = current.container
        document = _read_yaml(self.backends_file)
        # Keep proxy settings the page doesn't edit, such as tls.
        proxy_section = {**(document.get("proxy") or {}), **normalized["proxy"]}
        document.update(normalized)
        document["proxy"] = proxy_section
        self._write_backends_file(document)

        proxy = normalized["proxy"]
        self.host = proxy["listen_host"]
        self.port = proxy["listen_port"]
        self.proxy_url = proxy["url"]
        self.default_backend = normalized["default_backend"]
        self.backends = {
            name: Backend(name=name, **backend)
            for name, backend in normalized["backends"].items()
        }
        return self.network_config()

    def save_backend(self, name: str, value: dict[str, Any]) -> Backend:
        """Add or replace one backend in backends.yaml, leaving the rest of the file as it is."""
        document = _read_yaml(self.backends_file)
        backends = dict(document.get("backends") or {})
        backends[name] = value
        document["backends"] = backends
        self._write_backends_file(document)
        backend = _backend(name, value)
        self.backends = {**self.backends, name: backend}
        return backend

    def remove_backend(self, name: str) -> None:
        if name == self.default_backend:
            raise ValueError(f"{name} is the default backend; choose another default in Settings first")
        document = _read_yaml(self.backends_file)
        backends = dict(document.get("backends") or {})
        backends.pop(name, None)
        document["backends"] = backends
        self._write_backends_file(document)
        self.backends = {key: value for key, value in self.backends.items() if key != name}

    def _write_backends_file(self, document: dict[str, Any]) -> None:
        temporary = self.backends_file.with_suffix(self.backends_file.suffix + ".tmp")
        self.backends_file.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        temporary.replace(self.backends_file)


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _validate_network_config(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Configuration must be an object")
    proxy = value.get("proxy")
    if not isinstance(proxy, dict):
        raise ValueError("proxy must be an object")
    host = str(proxy.get("listen_host", "")).strip()
    if not host or any(character.isspace() for character in host):
        raise ValueError("Proxy listen host is invalid")
    try:
        port = int(proxy.get("listen_port"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Proxy listen port must be a number") from exc
    if not 1 <= port <= 65535:
        raise ValueError("Proxy listen port must be between 1 and 65535")
    proxy_url = _validate_http_url(proxy.get("url"), "Proxy URL")

    raw_backends = value.get("backends")
    # None at all is fine: agents on their own accounts need none, and machines add the rest.
    if not isinstance(raw_backends, dict):
        raise ValueError("backends must be an object")
    backends: dict[str, dict[str, str]] = {}
    for raw_name, raw_backend in raw_backends.items():
        name = str(raw_name).strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
            raise ValueError(
                f"Backend name '{name}' may only contain letters, numbers, _ and -"
            )
        if not isinstance(raw_backend, dict):
            raise ValueError(f"Backend '{name}' must be an object")
        backend_type = str(raw_backend.get("type", "ollama")).strip()
        if backend_type not in BACKEND_TYPES:
            raise ValueError(f"Backend '{name}' has unsupported type '{backend_type}'")
        backends[name] = {
            "type": backend_type,
            "url": _validate_http_url(raw_backend.get("url"), f"Backend '{name}' URL"),
        }
    default_backend = str(value.get("default_backend", "")).strip()
    if default_backend not in backends and (backends or default_backend):
        raise ValueError("Default backend must match a configured backend")
    return {
        "proxy": {
            "listen_host": host,
            "listen_port": port,
            "url": proxy_url,
        },
        "default_backend": default_backend,
        "backends": backends,
    }


def _validate_http_url(value: Any, label: str) -> str:
    url = str(value or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{label} must be a valid http:// or https:// URL")
    return url
