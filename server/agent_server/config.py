from __future__ import annotations

import copy
import fnmatch
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select

from . import defaults
from .db import Database, kv, migrate, schema, sqlite_url


def _find_root() -> Path:
    """Locate the checkout (or install folder) that holds VERSION, logs/, ui/dist and benchmarks/."""
    configured = os.getenv("AI_PROXY_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    for candidate in (Path(__file__).resolve().parents[2], Path.cwd(), *Path.cwd().parents):
        if (candidate / "VERSION").is_file() and (candidate / "server").is_dir():
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
    """A backend from its stored row or an imported document. Fields from older versions (metrics_url: machines report their own load now) are dropped."""
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
    """The HTTPS forward proxy that records traffic to public destinations."""

    enabled: bool = True
    # Agents reach it through the agent-proxy on their own machine, which tunnels here, so
    # the server listens itself only when listen_port is set (tests, the demo). Kept on
    # loopback by default; direct connections still require OAuth and a controllable session.
    listen_host: str = "127.0.0.1"
    listen_port: int | None = None
    # Host patterns tunnelled without decryption (recorded as a connection only).
    passthrough: list[str] = field(default_factory=list)
    # Where the CA's certificate and bundle are written for agents (its key stays in the database); None: log_dir/ca.
    ca_dir: Path | None = None

    @classmethod
    def load(cls, data: dict[str, Any]) -> "InterceptSettings":
        port = os.getenv("AI_PROXY_INTERCEPT_PORT") or data.get("listen_port")
        return cls(
            enabled=os.getenv("AI_PROXY_INTERCEPT", "1" if data.get("enabled", True) else "0") != "0",
            listen_host=os.getenv("AI_PROXY_INTERCEPT_HOST", str(data.get("listen_host", "127.0.0.1"))),
            listen_port=int(port) if port not in (None, "") else None,
            passthrough=[str(item) for item in data.get("passthrough") or []],
            ca_dir=Path(os.environ["AI_PROXY_CA_DIR"]).expanduser().resolve() if os.getenv("AI_PROXY_CA_DIR") else None,
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
    # AI_PROXY_LOG_DIR applies to Settings built in code too, so a test run never opens the real database.
    log_dir: Path = field(default_factory=lambda: Path(os.getenv("AI_PROXY_LOG_DIR") or ROOT / "logs").resolve())
    # The database everything is kept in (AI_PROXY_DATABASE_URL); without one, a SQLite file in log_dir.
    database_url: str | None = field(default=None, repr=False)
    ui_dir: Path = field(default_factory=lambda: ROOT / "ui" / "dist")
    # The builds the server hands out (tools/dist's dist/<version>/), see downloads.py.
    dist_dir: Path = field(default_factory=lambda: ROOT / "dist")
    # The documentation (docs/: Markdown pages and their screenshots), which the console shows under /help.
    docs_dir: Path = field(default_factory=lambda: ROOT / "docs")
    # Legacy key location anchors the account/container-state directory, not authentication.
    # Settings built in code (tests) keep it under log_dir; see remote_key_path.
    remote_key_file: Path | None = None
    # HTTPS for the console and API (the stored proxy setting's tls, or AI_PROXY_TLS_CERT/KEY): certificate and key files.
    tls_cert: Path | None = None
    tls_key: Path | None = None
    # The public console address (proxy.public_url), ahead of discovered LAN addresses.
    public_url: str | None = None
    console_origins: list[str] = field(default_factory=list)
    oidc_issuer: str = "https://auth.aicontrolmesh.com"
    oidc_audience: str = "https://api.aicontrolmesh.com"
    oidc_console_client: str = "mesh-console"
    oidc_cli_client: str = "mesh-cli"
    oidc_redirect_uris: list[str] = field(default_factory=lambda: ["https://api.aicontrolmesh.com/api/auth/callback"])
    oidc_browser_origins: list[str] = field(default_factory=lambda: ["https://console.aicontrolmesh.com"])
    oidc_introspection_client: str = "mesh-api"
    oidc_introspection_secret: str = field(default="", repr=False)
    oidc_state_dir: Path | None = None
    serve_ui: bool = True
    default_backend: str = "laptop"
    backends: dict[str, Backend] = field(default_factory=dict)
    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    model_defaults: dict[str, Any] = field(default_factory=dict)
    model_profiles: list[dict[str, Any]] = field(default_factory=list)
    # Off unless loaded from the database, so Settings() built in code never opens a port.
    intercept: "InterceptSettings" = field(default_factory=lambda: InterceptSettings(enabled=False))
    # Agent definitions for Settings built in code (tests): None means the shipped ones (defaults.agents()).
    agents: dict[str, dict[str, Any]] | None = field(default=None, repr=False)
    # Traces and terminal recordings of sessions older than this many days are deleted; None keeps them.
    retention_days: int | None = None
    # The open database (open_database). Settings.load() reads its settings from it; Settings built
    # in code write theirs into it instead, so a test says what it wants and the database follows.
    db: Database | None = field(default=None, repr=False, compare=False)
    from_database: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        issuer = urlparse(self.oidc_issuer)
        if issuer.scheme != "https" and not (issuer.scheme == "http" and issuer.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError("The OIDC issuer must use HTTPS (HTTP is allowed only on loopback for development)")
        if not issuer.hostname or issuer.username or issuer.password or issuer.path or issuer.query or issuer.fragment:
            raise ValueError("The OIDC issuer must be an exact origin without credentials, path, query or fragment")
        ids = [self.oidc_console_client, self.oidc_cli_client, self.oidc_introspection_client]
        if not all(ids) or len(set(ids)) != 3:
            raise ValueError("OIDC client identifiers must be nonempty and distinct")
        for value in self.oidc_browser_origins + self.oidc_redirect_uris:
            parsed = urlparse(value)
            if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or "*" in value or (
                parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})
            ):
                raise ValueError("OIDC browser origins and callbacks must be exact HTTPS URLs (loopback HTTP is allowed for development)")
            if value in self.oidc_browser_origins and parsed.path:
                raise ValueError("OIDC console return origins must not contain a path")
        for origin in self.console_origins:
            parsed = urlparse(origin)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or "*" in origin:
                raise ValueError(f"Console origins must be exact HTTP(S) origins without a path: {origin!r}")

    @classmethod
    def load(cls, database: bool = True) -> "Settings":
        """The environment's settings, then (with database) those stored in the database, the environment winning.

        agent-auth passes database=False: it needs only the environment and reads accounts on its own.
        """
        settings = cls(
            log_dir=Path(os.getenv("AI_PROXY_LOG_DIR", str(ROOT / "logs"))).resolve(),
            database_url=os.getenv("AI_PROXY_DATABASE_URL") or None,
            ui_dir=Path(os.getenv("AI_PROXY_UI_DIR", str(ROOT / "ui" / "dist"))).resolve(),
            dist_dir=Path(os.getenv("AI_PROXY_DIST_DIR", str(ROOT / "dist"))).resolve(),
            docs_dir=Path(os.getenv("AI_PROXY_DOCS_DIR", str(ROOT / "docs"))).resolve(),
            remote_key_file=Path(os.getenv("AI_PROXY_REMOTE_KEY_FILE", str(ROOT / "certs" / "remote-access-key"))).resolve(),
            console_origins=[origin.strip() for origin in os.getenv("AI_PROXY_CONSOLE_ORIGINS", "").split(",") if origin.strip()],
            oidc_issuer=os.getenv("AI_PROXY_OIDC_ISSUER", "https://auth.aicontrolmesh.com"),
            oidc_audience=os.getenv("AI_PROXY_OIDC_AUDIENCE", "https://api.aicontrolmesh.com"),
            oidc_console_client=os.getenv("AI_PROXY_OIDC_CONSOLE_CLIENT", "mesh-console"),
            oidc_cli_client=os.getenv("AI_PROXY_OIDC_CLI_CLIENT", "mesh-cli"),
            oidc_redirect_uris=[value.strip() for value in os.getenv("AI_PROXY_OIDC_REDIRECT_URIS", "https://api.aicontrolmesh.com/api/auth/callback").split(",") if value.strip()],
            oidc_browser_origins=[value.strip() for value in os.getenv("AI_PROXY_OIDC_BROWSER_ORIGINS", "https://console.aicontrolmesh.com").split(",") if value.strip()],
            oidc_introspection_client=os.getenv("AI_PROXY_OIDC_INTROSPECTION_CLIENT", "mesh-api"),
            oidc_introspection_secret=os.getenv("AI_PROXY_OIDC_INTROSPECTION_SECRET", ""),
            oidc_state_dir=Path(os.environ["AI_PROXY_OIDC_STATE_DIR"]).expanduser().resolve() if os.getenv("AI_PROXY_OIDC_STATE_DIR") else None,
            serve_ui=os.getenv("AI_PROXY_SERVE_UI", "1") != "0",
        )
        settings.from_database = True
        if database:
            settings.open_database()
        else:
            settings._configure({}, {})
        return settings

    @property
    def database_address(self) -> str:
        return self.database_url or os.getenv("AI_PROXY_DATABASE_URL") or sqlite_url(self.log_dir / "agent-server.sqlite3")

    def open_database(self) -> Database:
        """The database, opened and brought up to date. Loaded settings read theirs from it; built ones write theirs."""
        if self.db is not None and not self.db.writer.closed:
            return self.db
        self.db = Database(self.database_address)
        migrate.migrate(self.db)
        document: dict[str, Any] = {}
        models: dict[str, Any] = {}
        with self.db.write() as connection:
            if self.from_database:
                if not kv.get_meta(connection, "config_seeded"):
                    # A new database for an install from before it: its files come in first.
                    from .db import legacy

                    legacy.import_settings(connection, self)
                seed(connection)
                sync_builtin_agents(connection)
                document, models = stored_config(connection)
            else:
                self._store_config(connection)
        if self.from_database:
            from .db import legacy

            legacy.import_sessions(self.db)
            self._configure(document, models)
        return self.db

    def _configure(self, document: dict[str, Any], models: dict[str, Any]) -> None:
        """Apply stored settings (document is shaped as the old backends.yaml was), environment overrides on top."""
        proxy_data = document.get("proxy") or {}
        configured_host = str(proxy_data.get("listen_host", "127.0.0.1"))
        configured_port = int(proxy_data.get("listen_port", 8181))
        configured_url = str(proxy_data.get("url") or f"http://{configured_host}:{configured_port}").rstrip("/")
        environment_host = os.getenv("AI_PROXY_HOST")
        environment_port = os.getenv("AI_PROXY_PORT")
        self.host = environment_host or configured_host
        self.port = int(environment_port or configured_port)
        environment_url = os.getenv("AI_PROXY_URL")
        if environment_url:
            effective_url = environment_url
        elif environment_host or environment_port:
            client_host = "127.0.0.1" if self.host in {"0.0.0.0", "::"} else self.host
            effective_url = f"http://{client_host}:{self.port}"
        else:
            effective_url = configured_url
        self.proxy_url = effective_url.rstrip("/")
        self.default_backend = os.getenv("AI_PROXY_DEFAULT_BACKEND") or str(document.get("default_backend") or defaults.DEFAULT_BACKEND)
        self.backends = {name: _backend(name, value) for name, value in (document.get("backends") or {}).items()}
        self.intercept = InterceptSettings.load(document.get("intercept") or {})
        tls = proxy_data.get("tls") or {}
        cert, key = os.getenv("AI_PROXY_TLS_CERT") or tls.get("cert"), os.getenv("AI_PROXY_TLS_KEY") or tls.get("key")
        if cert and key:
            self.tls_cert, self.tls_key = Path(str(cert)).expanduser().resolve(), Path(str(key)).expanduser().resolve()
        self.public_url = str(os.getenv("AI_PROXY_PUBLIC_URL") or proxy_data.get("public_url") or "").rstrip("/") or None
        self.models = dict(models.get("aliases") or {})
        self.model_defaults = dict(models.get("defaults") or {})
        self.model_profiles = list(models.get("profiles") or [])
        retention = document.get("retention_days")
        self.retention_days = int(retention) if retention else None

    def _store_config(self, connection: Any) -> None:
        """Settings built in code: their values become the database's."""
        proxy: dict[str, Any] = {"listen_host": self.host, "listen_port": self.port, "url": self.proxy_url}
        if self.public_url:
            proxy["public_url"] = self.public_url
        if self.tls_cert and self.tls_key:
            proxy["tls"] = {"cert": str(self.tls_cert), "key": str(self.tls_key)}
        kv.put(connection, schema.settings, "proxy", proxy)
        kv.put(connection, schema.settings, "default_backend", self.default_backend)
        kv.put(connection, schema.settings, "intercept", self.intercept.public())
        kv.put(connection, schema.settings, "models", self.models_document())
        kv.put(connection, schema.settings, "retention_days", self.retention_days)
        _replace_backends(connection, {name: backend.public() for name, backend in self.backends.items()})
        definitions = defaults.agents() if self.agents is None else self.agents
        connection.execute(schema.agents.delete())
        for position, (agent_id, definition) in enumerate(definitions.items()):
            connection.execute(schema.agents.insert().values(
                id=agent_id, position=position, definition=definition, builtin=self.agents is None, edited=False, updated_at=time.time(),
            ))
        kv.put_meta(connection, "config_seeded", True)

    def models_document(self) -> dict[str, Any]:
        """Ollama model tuning as it's stored and edited: defaults, profiles (first match wins), aliases."""
        return {"defaults": self.model_defaults, "profiles": self.model_profiles, "aliases": self.models}

    def update_models(self, value: Any) -> dict[str, Any]:
        """Validate, store and apply the model tuning document."""
        normalized = validate_models(value)
        with self.open_database().write() as connection:
            kv.put(connection, schema.settings, "models", normalized)
        self.model_defaults, self.model_profiles, self.models = normalized["defaults"], normalized["profiles"], normalized["aliases"]
        return self.models_document()

    @property
    def remote_key_path(self) -> Path:
        return self.remote_key_file or self.log_dir / "remote" / "access-key"

    def upcoming_listen(self) -> tuple[str, int]:
        """The listen address stored now, which a restart will use (environment overrides win)."""
        proxy = self._setting("proxy") or {}
        host = os.getenv("AI_PROXY_HOST") or str(proxy.get("listen_host", self.host))
        try:
            port = int(os.getenv("AI_PROXY_PORT") or proxy.get("listen_port", self.port))
        except (TypeError, ValueError):
            port = self.port
        return host, port

    @property
    def users_path(self) -> Path:
        """Where versions before the database kept their accounts: read once, by the legacy import (db/legacy.py).
        first-user-password is written beside it."""
        return self.remote_key_path.parent / "users.json"

    @property
    def container_keys_path(self) -> Path:
        """Where versions before the database kept their Strata containers' keys: read once, by the legacy import."""
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
        raise ValueError(f"No {backend_type} backend is configured (Server settings, or a machine's model servers)")

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
            layers.append((f"aliases.{name}", alias))
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
                "public_url": self.public_url,
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
        with self.open_database().write() as connection:
            # Keep proxy settings the page doesn't edit, such as tls.
            proxy_section = {**(kv.get(connection, schema.settings, "proxy") or {}), **normalized["proxy"]}
            kv.put(connection, schema.settings, "proxy", proxy_section)
            kv.put(connection, schema.settings, "default_backend", normalized["default_backend"])
            _replace_backends(connection, normalized["backends"])

        proxy = normalized["proxy"]
        self.host = proxy["listen_host"]
        self.port = proxy["listen_port"]
        self.proxy_url = proxy["url"]
        if "public_url" in proxy:
            self.public_url = proxy["public_url"]
        self.default_backend = normalized["default_backend"]
        self.backends = {
            name: Backend(name=name, **backend)
            for name, backend in normalized["backends"].items()
        }
        return self.network_config()

    def save_backend(self, name: str, value: dict[str, Any]) -> Backend:
        """Add or replace one backend, leaving the rest as they are."""
        backend = _backend(name, value)
        with self.open_database().write() as connection:
            position = connection.execute(select(schema.backends.c.position).where(schema.backends.c.name == name)).scalar()
            if position is None:
                position = (connection.execute(select(schema.backends.c.position).order_by(schema.backends.c.position.desc())).scalar() or 0) + 1
            kv.upsert(connection, schema.backends, {"name": name, "position": position, **_backend_row(backend)})
        self.backends = {**self.backends, name: backend}
        return backend

    def remove_backend(self, name: str) -> None:
        if name == self.default_backend:
            raise ValueError(f"{name} is the default backend; choose another default in Settings first")
        with self.open_database().write() as connection:
            connection.execute(schema.backends.delete().where(schema.backends.c.name == name))
        self.backends = {key: value for key, value in self.backends.items() if key != name}

    def _setting(self, key: str) -> Any:
        with self.open_database().read() as connection:
            return kv.get(connection, schema.settings, key)

    def set_retention(self, days: Any) -> int | None:
        """How long sessions' traces and terminal recordings are kept, in days (None: forever)."""
        if days in (None, "", 0):
            value = None
        else:
            try:
                value = int(days)
            except (TypeError, ValueError) as exc:
                raise ValueError("Retention is a number of days") from exc
            if not 1 <= value <= 36500:
                raise ValueError("Keep recordings for between 1 and 36500 days, or forever")
        with self.open_database().write() as connection:
            kv.put(connection, schema.settings, "retention_days", value)
        self.retention_days = value
        return value


def _backend_row(backend: Backend) -> dict[str, Any]:
    return {"type": backend.type, "url": backend.url, "machine": backend.machine, "container": backend.container}


def _replace_backends(connection: Any, backends: dict[str, dict[str, Any]]) -> None:
    connection.execute(schema.backends.delete())
    for position, (name, value) in enumerate(backends.items()):
        connection.execute(schema.backends.insert().values(name=name, position=position, **_backend_row(_backend(name, value))))


def stored_config(connection: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """The stored network settings (shaped as backends.yaml was) and model tuning."""
    document: dict[str, Any] = {
        key: kv.get(connection, schema.settings, key)
        for key in ("proxy", "default_backend", "intercept", "retention_days")
    }
    rows = connection.execute(select(schema.backends).order_by(schema.backends.c.position, schema.backends.c.name)).mappings()
    document["backends"] = {
        row["name"]: {key: row[key] for key in ("type", "url", "machine", "container") if row[key] is not None}
        for row in rows
    }
    return document, kv.get(connection, schema.settings, "models") or {}


def seed(connection: Any) -> None:
    """A new database starts with the shipped settings (defaults.py); it's done once."""
    if kv.get_meta(connection, "config_seeded"):
        return
    # Each setting an import didn't bring.
    for key, value in (
        ("proxy", dict(defaults.PROXY)), ("default_backend", defaults.DEFAULT_BACKEND), ("intercept", dict(defaults.INTERCEPT)),
        ("models", defaults.models()), ("retention_days", defaults.RETENTION_DAYS),
    ):
        if kv.get(connection, schema.settings, key) is None:
            kv.put(connection, schema.settings, key, value)
    if kv.get(connection, schema.meta, "backends_imported") is None and connection.execute(select(schema.backends.c.name)).first() is None:
        _replace_backends(connection, defaults.BACKENDS)
    kv.put_meta(connection, "config_seeded", True)


def sync_builtin_agents(connection: Any) -> None:
    """Shipped agents follow the release until an admin edits them; one an admin deleted stays deleted."""
    shipped = defaults.agents()
    deleted = set(kv.get_meta(connection, "deleted_builtin_agents") or [])
    rows = {row["id"]: row for row in connection.execute(select(schema.agents)).mappings()}
    end = max((row["position"] for row in rows.values()), default=-1)
    for agent_id, definition in shipped.items():
        row = rows.get(agent_id)
        if row is None:
            if agent_id in deleted:
                continue
            end += 1
            connection.execute(schema.agents.insert().values(
                id=agent_id, position=end, definition=definition, builtin=True, edited=False, updated_at=time.time(),
            ))
        elif row["builtin"] and not row["edited"] and row["definition"] != definition:
            connection.execute(schema.agents.update().where(schema.agents.c.id == agent_id).values(definition=definition, updated_at=time.time()))
    for agent_id, row in rows.items():
        if row["builtin"] and not row["edited"] and agent_id not in shipped:
            connection.execute(schema.agents.delete().where(schema.agents.c.id == agent_id))


def validate_models(value: Any) -> dict[str, Any]:
    """The model tuning document: defaults and aliases are mappings, profiles a list with a match each."""
    if not isinstance(value, dict) or set(value) - {"defaults", "profiles", "aliases"}:
        raise ValueError("Model tuning has defaults, profiles and aliases")
    defaults_value = value.get("defaults") or {}
    profiles = value.get("profiles") or []
    aliases = value.get("aliases") or {}
    if not isinstance(defaults_value, dict) or not isinstance(aliases, dict) or not isinstance(profiles, list):
        raise ValueError("defaults and aliases are mappings, profiles a list")
    for layer in [defaults_value, *profiles, *aliases.values()]:
        if not isinstance(layer, dict):
            raise ValueError("Each profile and alias is a mapping")
        if not isinstance(layer.get("options") or {}, dict):
            raise ValueError("options is a mapping of Ollama options")
    for profile in profiles:
        if not profile.get("match") or not isinstance(profile["match"], (str, list)):
            raise ValueError(f"Profile {profile.get('name') or '?'} needs match: a pattern, or a list of them")
    for name, alias in aliases.items():
        if not isinstance(alias.get("ollama_model", name), str):
            raise ValueError(f"Alias {name}: ollama_model is a model name")
    return {"defaults": defaults_value, "profiles": profiles, "aliases": {str(name): alias for name, alias in aliases.items()}}


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
    public: dict[str, Any] = {}
    if "public_url" in proxy:
        # The console's address people use, ahead of the addresses the server finds itself; empty clears it.
        public["public_url"] = _validate_http_url(proxy["public_url"], "Public URL") if proxy["public_url"] else None

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
            **public,
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
