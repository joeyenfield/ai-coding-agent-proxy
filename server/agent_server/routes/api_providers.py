"""Site admins' management of generic API providers: /api/config/providers (auth.SITE_ADMIN_ONLY, as all of /api/config/).

A provider is a backend of type "api" (api_providers.py). Hosted APIs and gateways are added
here by URL; machine-hosted ones come from adopting a model server a machine found (routes/machines.py)
and are only edited here. A key typed here is stored encrypted and never shown again; the response
says only whether there is one and where it comes from.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request

from .. import api_providers, remote
from ..api_providers import ProviderError
from ..provider_accounts import VaultError
from ..runtime import Runtime

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")


def register(app: FastAPI, runtime: Runtime) -> None:
    settings = runtime.settings
    keys = runtime.api_keys

    def described(backend: Any) -> dict[str, Any]:
        return api_providers.public(backend, keys, settings.backends, runtime.api_health, settings.default_backend)

    def provider(name: str) -> Any:
        backend = settings.backends.get(name)
        if backend is None or backend.type != "api":
            raise HTTPException(status_code=404, detail="No such API provider")
        return backend

    def changing(request: Request) -> None:
        if not remote.same_origin(request):
            raise HTTPException(status_code=403, detail="Change providers from the console's own page.")

    @app.get("/api/config/providers")
    async def list_providers() -> dict[str, Any]:
        return {
            "providers": [described(backend) for backend in settings.backends.values() if backend.type == "api"],
            "vault": runtime.provider_accounts.vault.configured,
            "presets": api_providers.PRESETS,
            "protocols": list(api_providers.PROTOCOLS),
            "auth_modes": list(api_providers.AUTH_MODES),
        }

    @app.put("/api/config/providers/{name}")
    async def save_provider(name: str, request: Request) -> dict[str, Any]:
        changing(request)
        if not NAME.fullmatch(name):
            raise HTTPException(status_code=400, detail="A provider's name has letters, numbers, _ and -")
        try:
            data = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Send the provider as JSON") from exc
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="A provider is an object")
        key = data.get("key")
        if key is not None and not isinstance(key, str):
            raise HTTPException(status_code=400, detail="key is the provider's key, as text")
        async with runtime.config_lock:
            current = settings.backends.get(name)
            if current is not None and current.type != "api":
                raise HTTPException(status_code=409, detail=f"{name} is already a {current.type} backend")
            try:
                url, options = api_providers.validate(name, data, current)
            except ProviderError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            for other in options["fallback"]:
                target = settings.backends.get(other)
                if target is None or target.type != "api":
                    raise HTTPException(status_code=400, detail=f"fallback: {other} isn't an API provider")
            if key and not runtime.provider_accounts.vault.configured:
                raise HTTPException(status_code=400, detail="Storing a key needs the server's vault key (AI_PROXY_VAULT_KEYS); or name an environment variable in key_env")
            moved = current is not None and api_providers.origin(current.url) != api_providers.origin(url)
            settings.save_backend(name, {"type": "api", "url": url, "machine": getattr(current, "machine", None), "options": options})
            try:
                if key:
                    keys.set(name, url, key)
                elif data.get("clear_key") or moved:
                    # A stored key is only ever sent to the address it was given for.
                    keys.clear(name)
            except (ProviderError, VaultError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return described(settings.backends[name])

    @app.delete("/api/config/providers/{name}")
    async def delete_provider(name: str, request: Request) -> dict[str, Any]:
        changing(request)
        provider(name)
        async with runtime.config_lock:
            try:
                settings.remove_backend(name)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            keys.clear(name)
        runtime.api_models.pop(name, None)
        return {"ok": True}

    @app.post("/api/config/providers/{name}/test")
    async def test_provider(name: str, request: Request) -> dict[str, Any]:
        """Whether the server reaches the provider, and the models it lists (GET <url>/models)."""
        changing(request)
        backend = provider(name)
        return await discover(runtime, backend)


async def discover(runtime: Runtime, backend: Any) -> dict[str, Any]:
    """Ask the provider for its models: a connectivity test and model discovery at once. The result is kept
    as the provider's health and its discovered models (runtime.api_models)."""
    started = time.perf_counter()
    status: int | None = None
    try:
        client, base = await upstream(runtime, backend)
        key = runtime.api_keys.get(backend, runtime.settings.backends)
        headers = api_providers.headers(backend, {}, key, None)
        response = await client.get(
            api_providers.target(base, backend, "/v1/models"), headers=headers,
            params=api_providers.query(backend, []), timeout=httpx.Timeout(20, connect=api_providers.options(backend)["connect_timeout"]),
        )
        status = response.status_code
        latency = (time.perf_counter() - started) * 1000
        if status >= 400:
            error = _error(response)
            runtime.api_health.record(backend.name, status, latency, error)
            return {"ok": False, "status": status, "latency_ms": round(latency, 1), "models": None, "error": error}
        listed = response.json().get("data")
        models = sorted({str(item["id"]) for item in listed if isinstance(item, dict) and isinstance(item.get("id"), str)}) if isinstance(listed, list) else None
    except ProviderError as exc:
        return {"ok": False, "status": None, "latency_ms": None, "models": None, "error": str(exc)}
    except (httpx.HTTPError, OSError, ValueError, AttributeError) as exc:
        latency = (time.perf_counter() - started) * 1000
        error = str(exc) or type(exc).__name__
        runtime.api_health.record(backend.name, status, latency, error)
        return {"ok": False, "status": status, "latency_ms": round(latency, 1), "models": None, "error": error}
    runtime.api_health.record(backend.name, status, latency, None)
    if models is not None:
        runtime.api_models[backend.name] = models[: api_providers.MAX_MODELS]
    return {"ok": True, "status": status, "latency_ms": round(latency, 1), "models": models,
            "error": None if models is not None else "It answered, but not with a model list"}


async def upstream(runtime: Runtime, backend: Any) -> tuple[httpx.AsyncClient, str]:
    """The client and base URL for an api backend: through its machine like any model server, or for a hosted
    one the public-only client (api_providers.ProviderTransport)."""
    if api_providers.external(backend):
        client = runtime.api_clients.get(bool(api_providers.options(backend)["verify_tls"]))
        if client is None:
            raise ProviderError("The server isn't ready to reach providers yet")
        return client, backend.url.rstrip("/")
    assert runtime.client
    return runtime.client, await runtime.tunnels.base_url(backend)


def _error(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:300] or f"HTTP {response.status_code}"
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return str(error or body)[:300]
