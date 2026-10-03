from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Backend:
    name: str
    url: str
    type: str = "ollama"


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8181
    proxy_url: str = "http://127.0.0.1:8181"
    log_dir: Path = field(default_factory=lambda: ROOT / "logs")
    backends_file: Path = field(default_factory=lambda: ROOT / "config" / "backends.yaml")
    models_file: Path = field(default_factory=lambda: ROOT / "config" / "models.yaml")
    default_backend: str = "laptop"
    backends: dict[str, Backend] = field(default_factory=dict)
    models: dict[str, dict[str, Any]] = field(default_factory=dict)

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
        )
        settings.default_backend = os.getenv(
            "AI_PROXY_DEFAULT_BACKEND", backend_data.get("default_backend", "laptop")
        )
        settings.backends = {
            name: Backend(name=name, **value)
            for name, value in backend_data.get("backends", {}).items()
        }
        model_data = _read_yaml(settings.models_file)
        settings.models = model_data.get("models", {})
        return settings

    def backend(self, name: str | None) -> Backend:
        selected = name or self.default_backend
        try:
            backend = self.backends[selected]
        except KeyError as exc:
            choices = ", ".join(sorted(self.backends)) or "none configured"
            raise ValueError(f"Unknown backend '{selected}'. Available: {choices}") from exc
        if backend.type != "ollama":
            raise ValueError(f"Unsupported backend type: {backend.type}")
        return backend

    def model_name(self, name: str) -> str:
        return self.models.get(name, {}).get("ollama_model", name)

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
                name: {"type": backend.type, "url": backend.url}
                for name, backend in self.backends.items()
            },
        }

    def update_network_config(self, value: dict[str, Any]) -> dict[str, Any]:
        """Validate, persist, and apply editable network settings."""
        normalized = _validate_network_config(value)
        document = _read_yaml(self.backends_file)
        document.update(normalized)
        temporary = self.backends_file.with_suffix(self.backends_file.suffix + ".tmp")
        self.backends_file.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )
        temporary.replace(self.backends_file)

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
    if not isinstance(raw_backends, dict) or not raw_backends:
        raise ValueError("At least one backend is required")
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
        if backend_type != "ollama":
            raise ValueError(f"Backend '{name}' has unsupported type '{backend_type}'")
        backends[name] = {
            "type": backend_type,
            "url": _validate_http_url(raw_backend.get("url"), f"Backend '{name}' URL"),
        }
    default_backend = str(value.get("default_backend", "")).strip()
    if default_backend not in backends:
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
