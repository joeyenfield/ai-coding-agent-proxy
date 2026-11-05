"""Generic API providers: backends of type "api", which speak an agent's API themselves.

An api backend is any server that speaks one or more of OpenAI Chat Completions, OpenAI
Responses and Anthropic Messages: a hosted API (OpenAI, Anthropic, Groq, DeepSeek, ...), a
gateway (OpenRouter, LiteLLM Proxy) or, on a fleet machine, a local server (LM Studio, vLLM,
SGLang). Requests are passed through as they are (compat._forward); this module holds what
is particular to them:

- the options kept with the backend (`Backend.options`, the backends table's options column):
  protocols, authentication mode, model aliases, extra headers, timeouts, priority, fallback;
- the upstream key, which never enters the settings: an environment variable the operator
  names (AI_PROXY_API_KEY_*), or a key a site admin typed, encrypted by the provider accounts'
  Vault (AI_PROXY_VAULT_KEYS) in provider_credentials under "backend:<name>", bound to the
  provider's origin so it is never sent to an address it wasn't given for;
- the network policy for providers that aren't on a machine: they're reached directly from the
  server, so only public addresses, unless the operator lists a private host in
  AI_PROXY_API_PRIVATE_HOSTS (a LiteLLM Proxy beside the server, say). Redirects are never
  followed. A model server on a private network belongs on a machine (agent-machine), whose
  tunnel and grants keep it to the machine's tenant;
- passive health (each process's own observations of its requests) and presets.
"""

from __future__ import annotations

import ipaddress
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend
from sqlalchemy import select

from .db import Database, schema

PROTOCOLS = ("openai-chat", "openai-responses", "anthropic")
# How the server authenticates to the provider. agent: the agent's own Authorization or x-api-key
# goes through (it brings its own credentials); the others replace whatever the agent sent.
AUTH_MODES = ("none", "bearer", "x-api-key", "api-key", "agent")
CATEGORIES = ("machine", "hosted", "gateway")
KEY_ENV_PREFIX = "AI_PROXY_API_KEY_"
ANTHROPIC_VERSION = "2023-06-01"
# Agents' own credential headers: dropped unless auth is "agent".
CREDENTIAL_HEADERS = {"authorization", "x-api-key", "api-key"}
# Extra headers may not carry credentials or change how the request is framed or routed.
_SECRET_PARTS = ("auth", "cookie", "token", "secret", "api-key", "apikey", "session", "password", "key")
_FRAMING = {"host", "content-length", "content-type", "connection", "transfer-encoding", "te", "upgrade", "accept-encoding", "expect", "forwarded", "via"}
HEADER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")
MODEL_NAME = re.compile(r"[^\s\x00-\x1f]{1,200}")
MAX_MODELS = 500
MAX_HEADERS = 20

DEFAULT_OPTIONS: dict[str, Any] = {
    "category": "hosted",
    "protocols": ["openai-chat"],
    "auth": "bearer",
    "key_env": None,
    "anthropic_url": None,
    "models": {},
    "discover": True,
    "headers": {},
    "connect_timeout": 10.0,
    "verify_tls": True,
    "api_version": None,
    "priority": 0,
    "fallback": [],
    "enabled": True,
    "orgs": [],
}

# Starting points for the console. verified: this protocol behaviour was checked against the
# provider's own documentation and a mock of it in the tests; the rest are documented as untested.
PRESETS: list[dict[str, Any]] = [
    {"id": "openai", "label": "OpenAI API", "url": "https://api.openai.com/v1", "anthropic_url": None,
     "protocols": ["openai-chat", "openai-responses"], "auth": "bearer", "category": "hosted", "api_version": None, "headers": {},
     "notes": "Chat Completions and Responses. Codex needs Responses.", "verified": True},
    {"id": "anthropic", "label": "Anthropic API (server key)", "url": "https://api.anthropic.com/v1", "anthropic_url": "https://api.anthropic.com",
     "protocols": ["anthropic"], "auth": "x-api-key", "category": "hosted", "api_version": None, "headers": {},
     "notes": "Messages with the server's key. For agents' own keys or claude.ai logins, keep the anthropic pass-through backend.", "verified": True},
    {"id": "openrouter", "label": "OpenRouter", "url": "https://openrouter.ai/api/v1", "anthropic_url": None,
     "protocols": ["openai-chat"], "auth": "bearer", "category": "gateway", "api_version": None,
     "headers": {"X-Title": "AI Control Mesh"}, "notes": "Model ids look like anthropic/claude-sonnet-4.5. Tool calling depends on the model.", "verified": False},
    {"id": "litellm", "label": "LiteLLM Proxy", "url": "https://litellm.example.com/v1", "anthropic_url": "https://litellm.example.com",
     "protocols": ["openai-chat", "openai-responses", "anthropic"], "auth": "bearer", "category": "gateway", "api_version": None, "headers": {},
     "notes": "Use a LiteLLM virtual key. Turn off the protocols your LiteLLM version doesn't serve. A proxy on a private network needs AI_PROXY_API_PRIVATE_HOSTS.", "verified": False},
    {"id": "groq", "label": "Groq", "url": "https://api.groq.com/openai/v1", "anthropic_url": None,
     "protocols": ["openai-chat"], "auth": "bearer", "category": "hosted", "api_version": None, "headers": {}, "notes": "", "verified": False},
    {"id": "together", "label": "Together AI", "url": "https://api.together.xyz/v1", "anthropic_url": None,
     "protocols": ["openai-chat"], "auth": "bearer", "category": "hosted", "api_version": None, "headers": {}, "notes": "", "verified": False},
    {"id": "fireworks", "label": "Fireworks AI", "url": "https://api.fireworks.ai/inference/v1", "anthropic_url": None,
     "protocols": ["openai-chat"], "auth": "bearer", "category": "hosted", "api_version": None, "headers": {}, "notes": "", "verified": False},
    {"id": "deepseek", "label": "DeepSeek", "url": "https://api.deepseek.com/v1", "anthropic_url": "https://api.deepseek.com/anthropic",
     "protocols": ["openai-chat", "anthropic"], "auth": "bearer", "category": "hosted", "api_version": None, "headers": {},
     "notes": "DeepSeek's Anthropic-compatible API is at /anthropic.", "verified": False},
    {"id": "azure-openai", "label": "Azure OpenAI (v1 API)", "url": "https://RESOURCE.openai.azure.com/openai/v1", "anthropic_url": None,
     "protocols": ["openai-chat", "openai-responses"], "auth": "api-key", "category": "hosted", "api_version": None, "headers": {},
     "notes": "The model is your deployment's name. For the older per-deployment API, use .../openai/deployments/<deployment> and set the API version.", "verified": False},
]


class ProviderError(ValueError):
    pass


# Options


def options(backend: Any) -> dict[str, Any]:
    """The backend's api options with defaults filled in."""
    return {**DEFAULT_OPTIONS, **(getattr(backend, "options", None) or {})}


def speaks(backend: Any, protocol: str | None) -> bool:
    return protocol is None or protocol in options(backend)["protocols"]


def enabled(backend: Any) -> bool:
    return bool(options(backend)["enabled"])


def external(backend: Any) -> bool:
    """Reached directly from the server (not through a machine): a hosted API or gateway."""
    return getattr(backend, "type", None) == "api" and not getattr(backend, "machine", None)


def validate(name: str, value: Any, current: Any | None) -> tuple[str, dict[str, Any]]:
    """The url and options of an api backend as a site admin sent them, checked; raises ProviderError.

    A machine-hosted one keeps the address its machine gave it."""
    if not isinstance(value, dict):
        raise ProviderError("A provider is an object")
    unknown = sorted(set(value) - set(DEFAULT_OPTIONS) - {"url", "type", "name", "machine", "key", "clear_key", "has_key", "key_source", "health", "default"})
    if unknown:
        raise ProviderError(f"Providers have no {', '.join(unknown)}")
    machine = getattr(current, "machine", None)
    previous = options(current) if current is not None and getattr(current, "type", None) == "api" else {}
    merged = {**DEFAULT_OPTIONS, **previous, **{key: item for key, item in value.items() if key in DEFAULT_OPTIONS}}
    if machine:
        url = current.url.rstrip("/")
        if value.get("url") and value["url"].rstrip("/") != url:
            raise ProviderError(f"{name} is on a machine; its address comes from the machine")
        merged["category"] = "machine"
    else:
        url = check_url(value.get("url") or (current.url if current is not None else ""), f"{name}'s URL")
        if merged["category"] == "machine":
            raise ProviderError("Machine-hosted providers are added from the machine that runs them (Machines page)")
    if merged["category"] not in CATEGORIES:
        raise ProviderError(f"category is one of {', '.join(CATEGORIES)}")
    protocols = merged["protocols"]
    if not isinstance(protocols, list) or not protocols or any(item not in PROTOCOLS for item in protocols):
        raise ProviderError(f"protocols is a list of {', '.join(PROTOCOLS)}")
    merged["protocols"] = [item for item in PROTOCOLS if item in protocols]
    if merged["auth"] not in AUTH_MODES:
        raise ProviderError(f"auth is one of {', '.join(AUTH_MODES)}")
    key_env = merged["key_env"] or None
    if key_env is not None and (not isinstance(key_env, str) or not re.fullmatch(KEY_ENV_PREFIX + r"[A-Z0-9_]{1,64}", key_env)):
        # Only variables meant for it: any other would let the settings send the server's own secrets upstream.
        raise ProviderError(f"key_env names an environment variable starting with {KEY_ENV_PREFIX} (capitals, digits and _)")
    merged["key_env"] = key_env
    anthropic_url = merged["anthropic_url"] or None
    if anthropic_url is not None:
        anthropic_url = check_url(anthropic_url, f"{name}'s Anthropic URL") if not machine else str(anthropic_url).rstrip("/")
        if origin(anthropic_url) != origin(url):
            # One origin per provider: its key, its network check and its tunnel all go by it.
            raise ProviderError("The Anthropic URL must be on the same scheme, host and port as the URL")
    merged["anthropic_url"] = anthropic_url
    merged["models"] = _models(merged["models"])
    merged["headers"] = _headers(merged["headers"])
    for key in ("discover", "verify_tls", "enabled"):
        if not isinstance(merged[key], bool):
            raise ProviderError(f"{key} is true or false")
    try:
        timeout = float(merged["connect_timeout"])
    except (TypeError, ValueError) as exc:
        raise ProviderError("connect_timeout is a number of seconds") from exc
    if not 0.5 <= timeout <= 120:
        raise ProviderError("connect_timeout is between 0.5 and 120 seconds")
    merged["connect_timeout"] = timeout
    version = merged["api_version"] or None
    if version is not None and (not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", version)):
        raise ProviderError("api_version is a version such as 2024-10-21")
    merged["api_version"] = version
    priority = merged["priority"]
    if isinstance(priority, bool) or not isinstance(priority, int) or not -1000 <= priority <= 1000:
        raise ProviderError("priority is a whole number between -1000 and 1000")
    fallback = merged["fallback"]
    if not isinstance(fallback, list) or any(not isinstance(item, str) for item in fallback) or name in fallback or len(fallback) > 5:
        raise ProviderError("fallback lists up to five other providers")
    merged["fallback"] = list(dict.fromkeys(fallback))
    orgs = merged["orgs"]
    if not isinstance(orgs, list) or any(not isinstance(item, str) or not re.fullmatch(r"\*|[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", item) for item in orgs):
        raise ProviderError('orgs lists organizations, or ["*"] for every one')
    merged["orgs"] = sorted(set(orgs))
    return url, merged


def _models(value: Any) -> dict[str, str]:
    """Aliases agents use mapped to the provider's ids; a list means each is its own alias."""
    if isinstance(value, list):
        value = {str(item): str(item) for item in value}
    if not isinstance(value, dict) or len(value) > MAX_MODELS:
        raise ProviderError(f"models maps the names agents use to the provider's model ids (up to {MAX_MODELS})")
    models: dict[str, str] = {}
    for alias, target in value.items():
        target = target or alias
        if not isinstance(alias, str) or not isinstance(target, str) or not MODEL_NAME.fullmatch(alias) or not MODEL_NAME.fullmatch(target):
            raise ProviderError(f"Model {alias!r}: names are up to 200 characters without spaces")
        models[alias] = target
    return models


def _headers(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > MAX_HEADERS:
        raise ProviderError(f"headers maps up to {MAX_HEADERS} header names to values")
    headers: dict[str, str] = {}
    for name, item in value.items():
        lowered = str(name).lower()
        if not HEADER_NAME.fullmatch(str(name)) or lowered in _FRAMING or lowered.startswith(("x-ai-proxy", "proxy-", "x-forwarded")):
            raise ProviderError(f"Header {name!r} can't be set")
        if any(part in lowered for part in _SECRET_PARTS):
            raise ProviderError(f"Header {name!r} looks like a credential: use auth and the provider's key instead, which are never shown or exported")
        if not isinstance(item, str) or len(item) > 500 or any(ord(character) < 32 for character in item):
            raise ProviderError(f"Header {name!r}: a value is one line of up to 500 characters")
        headers[str(name)] = item
    return headers


def check_url(value: Any, label: str) -> str:
    """An external provider's address: https (http only for a host the operator allowed), no credentials, query or fragment."""
    url = str(value or "").strip().rstrip("/")
    try:
        parts = urlsplit(url)
        parts.port
    except ValueError as exc:
        raise ProviderError(f"{label} isn't a valid URL") from exc
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ProviderError(f"{label} must be an http:// or https:// URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ProviderError(f"{label} can't hold credentials, a query or a fragment: use auth for the key and api_version for Azure")
    allowed = private_hosts().allows(parts.hostname)
    if parts.scheme == "http" and not allowed:
        raise ProviderError(f"{label}: a hosted provider is reached over https (http only for a host in AI_PROXY_API_PRIVATE_HOSTS)")
    try:
        address = ipaddress.ip_address(parts.hostname.strip("[]"))
    except ValueError:
        address = None
    if address is not None and not _public(address) and not allowed:
        raise ProviderError(f"{label}: {parts.hostname} is a private address. Model servers on a private network are added from the machine that runs them")
    return url


def origin(url: str) -> str:
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{parts.scheme}://{(parts.hostname or '').lower()}:{port}"


# Routing a request


def protocol_for(upstream_path: str) -> str | None:
    """Which protocol an agent's request is, by the path compat forwards it to."""
    if upstream_path.startswith("/v1/messages"):
        return "anthropic"
    if upstream_path == "/v1/responses" or upstream_path.startswith("/v1/responses/"):
        return "openai-responses"
    if upstream_path in {"/v1/chat/completions", "/v1/completions", "/v1/embeddings"}:
        return "openai-chat"
    return None


def target(base: str, backend: Any, upstream_path: str) -> str:
    """The upstream URL: base is where the backend's url is reached (a tunnel or the url itself).

    The url is the OpenAI base (what comes before /chat/completions, usually ending in /v1);
    Anthropic Messages go to anthropic_url + /v1/messages, else to the url without its /v1."""
    configured = options(backend)
    if upstream_path.startswith("/v1/messages"):
        anthropic = configured["anthropic_url"]
        if anthropic:
            path = urlsplit(anthropic).path.rstrip("/")
            root = base[: len(base) - len(urlsplit(backend.url).path.rstrip("/"))] if urlsplit(backend.url).path.rstrip("/") else base
            return root + path + upstream_path
        return (base[:-3] if base.endswith("/v1") else base) + upstream_path
    return base + upstream_path[len("/v1"):]


def query(backend: Any, params: Any) -> list[tuple[str, str]]:
    """The agent's query, plus api-version for Azure's older API."""
    items = [(key, value) for key, value in (params.multi_items() if hasattr(params, "multi_items") else list(params or []))
             if key.lower() != "api-version"]
    version = options(backend)["api_version"]
    if version:
        items.append(("api-version", version))
    return items


def upstream_model(backend: Any, model: str) -> str:
    return options(backend)["models"].get(model, model)


def headers(backend: Any, cleaned: dict[str, str], key: str | None, protocol: str | None) -> dict[str, str]:
    """The request's headers for the provider: the agent's (already cleaned by compat), with the server's
    authentication in place of the agent's unless auth is "agent", and the provider's extra headers."""
    configured = options(backend)
    mode = configured["auth"]
    result = dict(cleaned) if mode == "agent" else {name: value for name, value in cleaned.items() if name.lower() not in CREDENTIAL_HEADERS}
    for name, value in configured["headers"].items():
        result = {existing: item for existing, item in result.items() if existing.lower() != name.lower()}
        result[name] = value
    if mode != "agent" and mode != "none":
        if not key:
            raise ProviderError(f"{backend.name} has no key: set it on Server settings -> Providers")
        if mode == "bearer":
            result["Authorization"] = f"Bearer {key}"
        elif mode == "x-api-key":
            result["x-api-key"] = key
        else:
            result["api-key"] = key
    if protocol == "anthropic" and not any(name.lower() == "anthropic-version" for name in result):
        result["anthropic-version"] = ANTHROPIC_VERSION
    return result


def timeout(backend: Any) -> httpx.Timeout:
    return httpx.Timeout(600, connect=options(backend)["connect_timeout"], pool=30)


# Keys


class ApiKeys:
    """Providers' upstream keys: an environment variable the provider names, or one stored encrypted.

    Stored keys are decrypted once and kept in memory; reload() (another process changed one) reads them again."""

    def __init__(self, db: Database, vault: Any):
        self.db = db
        self.vault = vault
        self._keys: dict[str, str] = {}
        self._loaded = False
        self.listeners: list[Any] = []

    @staticmethod
    def _id(name: str) -> str:
        return f"backend:{name}"

    @staticmethod
    def _aad(name: str, url: str) -> bytes:
        # Bound to the provider and its origin: moved to another address, the key doesn't decrypt.
        return f"backend:{name}|{origin(url)}".encode()

    def reload(self) -> None:
        self._loaded = False
        self._keys = {}

    def _load(self, backends: dict[str, Any]) -> None:
        if self._loaded:
            return
        keys: dict[str, str] = {}
        table = schema.provider_credentials
        with self.db.read() as connection:
            rows = connection.execute(select(table).where(table.c.account_id.like("backend:%"))).mappings().all()
        for row in rows:
            name = row["account_id"][len("backend:"):]
            backend = backends.get(name)
            if backend is None:
                continue
            try:
                keys[name] = self.vault.decrypt(row["key_version"], row["nonce"], row["ciphertext"], self._aad(name, backend.url))
            except Exception:  # noqa: BLE001 - a key for another address, or an unknown vault key: as if none
                continue
        self._keys, self._loaded = keys, True

    def source(self, backend: Any, backends: dict[str, Any]) -> str | None:
        """Where the key comes from: a stored one wins (a site admin typed it last), then key_env."""
        self._load(backends)
        if backend.name in self._keys:
            return "vault"
        configured = options(backend)
        return "env" if configured["key_env"] and os.environ.get(configured["key_env"], "").strip() else None

    def get(self, backend: Any, backends: dict[str, Any]) -> str | None:
        self._load(backends)
        if backend.name in self._keys:
            return self._keys[backend.name]
        configured = options(backend)
        value = os.environ.get(configured["key_env"], "").strip() if configured["key_env"] else ""
        return value or None

    def set(self, name: str, url: str, key: str) -> None:
        key = key.strip()
        if not key or len(key) > 4000 or any(ord(character) < 32 for character in key):
            raise ProviderError("The key is one line of up to 4000 characters")
        version, nonce, ciphertext = self.vault.encrypt(key, self._aad(name, url))
        table = schema.provider_credentials
        with self.db.write() as connection:
            connection.execute(table.delete().where(table.c.account_id == self._id(name)))
            connection.execute(table.insert().values(account_id=self._id(name), key_version=version, nonce=nonce, ciphertext=ciphertext, created_at=time.time()))
        self._keys[name] = key
        self._changed()

    def clear(self, name: str) -> None:
        table = schema.provider_credentials
        with self.db.write() as connection:
            connection.execute(table.delete().where(table.c.account_id == self._id(name)))
        self._keys.pop(name, None)
        self._changed()

    def stored(self, name: str) -> bool:
        table = schema.provider_credentials
        with self.db.read() as connection:
            return connection.execute(select(table.c.account_id).where(table.c.account_id == self._id(name))).first() is not None

    def rotate(self, backends: dict[str, Any]) -> int:
        """Re-encrypt every stored provider key with the newest vault key; returns how many."""
        table = schema.provider_credentials
        done = 0
        with self.db.write() as connection:
            for row in connection.execute(select(table).where(table.c.account_id.like("backend:%"))).mappings().all():
                name = row["account_id"][len("backend:"):]
                backend = backends.get(name)
                if backend is None or row["key_version"] == self.vault.current:
                    continue
                aad = self._aad(name, backend.url)
                plaintext = self.vault.decrypt(row["key_version"], row["nonce"], row["ciphertext"], aad)
                version, nonce, ciphertext = self.vault.encrypt(plaintext, aad)
                connection.execute(table.update().where(table.c.account_id == row["account_id"]).values(key_version=version, nonce=nonce, ciphertext=ciphertext))
                done += 1
        return done

    def _changed(self) -> None:
        for listener in list(self.listeners):
            try:
                listener("api_keys")
            except Exception:  # noqa: BLE001 - a listener mustn't undo a saved key
                pass


# Network policy for external providers


@dataclass
class PrivateHosts:
    """AI_PROXY_API_PRIVATE_HOSTS: hosts (exact, or *.suffix) and networks (CIDR) external providers may be on
    though they aren't public. The operator's choice, in the environment, never a setting a console admin changes."""

    names: set[str] = field(default_factory=set)
    suffixes: tuple[str, ...] = ()
    networks: list[Any] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str) -> "PrivateHosts":
        result = cls()
        suffixes = []
        for item in re.split(r"[,\s]+", text.strip()):
            item = item.strip().lower()
            if not item:
                continue
            if "/" in item:
                try:
                    result.networks.append(ipaddress.ip_network(item, strict=False))
                except ValueError:
                    continue
            elif item.startswith("*."):
                suffixes.append(item[1:])
            else:
                result.names.add(item.strip("[]"))
        result.suffixes = tuple(suffixes)
        return result

    def allows(self, host: str, address: str | None = None) -> bool:
        host = host.lower().strip("[]")
        if host in self.names or (self.suffixes and host.endswith(self.suffixes)):
            return True
        for value in (host, address):
            try:
                ip = ipaddress.ip_address(value) if value else None
            except ValueError:
                ip = None
            if ip is not None and any(ip in network for network in self.networks):
                return True
        return False


def private_hosts() -> PrivateHosts:
    return PrivateHosts.parse(os.environ.get("AI_PROXY_API_PRIVATE_HOSTS", ""))


def _public(ip: Any) -> bool:
    return ip.is_global and not ip.is_reserved and not ip.is_multicast


class DestinationDenied(OSError):
    """An external provider resolved to an address the server mustn't reach for it."""


class ProviderNetworkBackend:
    """Resolves an external provider's host and connects only to public addresses (or allowed private ones),
    pinned to the address checked, so a DNS answer can't change between the check and the connection."""

    def __init__(self, allowed: PrivateHosts | None = None) -> None:
        self.backend = AutoBackend()
        self.allowed = allowed

    async def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None, socket_options: Any = None) -> Any:
        from .intercept import _resolve_host

        allowed = self.allowed if self.allowed is not None else private_hosts()
        try:
            addresses = [str(ipaddress.ip_address(host.strip("[]")))]
        except ValueError:
            addresses = await _resolve_host(host, port, timeout)
        if not addresses:
            raise OSError(f"Couldn't resolve {host}")
        for address in addresses:
            if not _public(ipaddress.ip_address(address)) and not allowed.allows(host, address):
                raise DestinationDenied(f"{host} resolves to a private or reserved address ({address}); list it in AI_PROXY_API_PRIVATE_HOSTS to allow it")
        last_error: OSError | None = None
        for address in addresses:
            try:
                return await self.backend.connect_tcp(address, port, timeout, local_address, socket_options)
            except OSError as exc:
                last_error = exc
        raise OSError(f"Couldn't connect to {host}:{port}") from last_error

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options: Any = None) -> Any:
        raise DestinationDenied("Providers aren't reached over Unix sockets")

    async def sleep(self, seconds: float) -> None:
        await self.backend.sleep(seconds)


class ProviderTransport(httpx.AsyncHTTPTransport):
    def __init__(self, verify: bool = True) -> None:
        super().__init__(trust_env=False, verify=verify)
        # httpx's TLS settings, with the checked, pinned connections underneath (as intercept.InterceptTransport).
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=self._pool._ssl_context, max_connections=200, max_keepalive_connections=50,
            keepalive_expiry=30.0, network_backend=ProviderNetworkBackend(),
        )


# Health: what this process saw of each provider's requests and tests (memory only; each process its own).


@dataclass
class Health:
    seen: dict[str, dict[str, Any]] = field(default_factory=dict)

    def record(self, name: str, status: int | None, latency_ms: float | None, error: str | None) -> None:
        ok = error is None and status is not None and status < 500 and status != 429
        self.seen[name] = {"ok": ok, "status": status, "at": time.time(), "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
                           "error": (error or None) and str(error)[:300]}

    def get(self, name: str) -> dict[str, Any] | None:
        return self.seen.get(name)


def public(backend: Any, keys: ApiKeys, backends: dict[str, Any], health: Health, default: str) -> dict[str, Any]:
    """An api backend for the console: everything but its key, which is never shown again."""
    configured = options(backend)
    source = keys.source(backend, backends)
    return {
        "name": backend.name, "type": "api", "url": backend.url, "machine": backend.machine,
        **{key: configured[key] for key in DEFAULT_OPTIONS},
        "category": "machine" if backend.machine else configured["category"],
        "has_key": source is not None, "key_source": source,
        "health": health.get(backend.name), "default": backend.name == default,
    }


def exported(value: dict[str, Any]) -> dict[str, Any]:
    """Options as `config export` writes them: only what differs from the defaults."""
    return {key: item for key, item in value.items() if key in DEFAULT_OPTIONS and item != DEFAULT_OPTIONS[key]}
