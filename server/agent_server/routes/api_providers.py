"""Management of generic API providers: an organization's at /api/orgs/{org}/providers, every one at /api/config/providers.

A provider is a backend of type "api" (api_providers.py). Each belongs to one organization: a hosted API
or gateway to the one it was added in (its `org` option; none is the root), one on a machine to its
machine's. That organization's admins add, change, test and remove it, and choose which of its teams may
use it (`teams`); site admins can do it in every organization and, at /api/config/providers (SITE_ADMIN_ONLY,
as all of /api/config/), see all of them at once and share one with other organizations whole (`orgs`).
Machine-hosted ones come from adopting a model server a machine found (routes/machines.py) and are only
edited here. A key typed here is stored encrypted and never shown again; the response says only whether
there is one and where it comes from.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import api_providers, auth, remote
from ..api_providers import ProviderError
from ..provider_accounts import VaultError
from ..runtime import Runtime

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")


def register(app: FastAPI, runtime: Runtime) -> None:
    settings = runtime.settings
    keys = runtime.api_keys
    fleet = runtime.fleet
    store = runtime.auth.store

    def described(backend: Any) -> dict[str, Any]:
        catalog = runtime.api_discovery.get(backend.name)
        return {**api_providers.public(backend, keys, settings.backends, runtime.api_health, settings.default_backend),
                "discovered_models": catalog.models if catalog and api_providers.options(backend)["discover"] else [],
                "balance_supported": api_providers.deepseek(backend),
                "tenant": fleet.backend_tenant(backend)}

    async def listing(providers: list[Any], identity: Any) -> dict[str, Any]:
        await asyncio.gather(*(auto_discover(runtime, backend) for backend in providers))
        return {
            "providers": [described(backend) for backend in providers],
            "vault": runtime.provider_accounts.vault.configured,
            "presets": api_providers.PRESETS,
            "protocols": list(api_providers.PROTOCOLS),
            "auth_modes": list(api_providers.AUTH_MODES),
            # Only site admins share a provider with other organizations.
            "share": bool(identity and identity.site_admin),
        }

    def managed(request: Request, org: str) -> Any:
        """The organization's admins, and site admins; anyone else finds no such organization."""
        identity = auth.request_identity(request)
        if store.org(org) is None or not auth.administers(identity, org):
            raise HTTPException(status_code=404, detail="No such organization")
        return identity

    def provider(name: str, org: str | None) -> Any:
        """An api backend, of this organization when one is named."""
        backend = settings.backends.get(name)
        if backend is None or backend.type != "api" or (org is not None and fleet.backend_tenant(backend) != org):
            raise HTTPException(status_code=404, detail="No such API provider")
        return backend

    def changing(request: Request) -> None:
        if not remote.same_origin(request):
            raise HTTPException(status_code=403, detail="Change providers from the console's own page.")

    async def save(name: str, request: Request, org: str | None) -> dict[str, Any]:
        """Add or change one. org: the organization it's changed in (None: the site admins' view of every one)."""
        changing(request)
        identity = auth.request_identity(request)
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
            if current is not None and (current.type != "api" or (org is not None and fleet.backend_tenant(current) != org)):
                # Names are the server's; to anyone but a site admin, another organization's provider is just a name that's taken.
                detail = f"{name} is already a {current.type} backend" if identity.site_admin else f"The name {name} is taken"
                raise HTTPException(status_code=409, detail=detail)
            body = dict(data)
            if org is not None:
                # Added here, it's this organization's; changed here, it stays whose it is.
                body["org"] = org if current is None else api_providers.options(current)["org"]
            elif body.get("org") not in (None, api_providers.ROOT_ORG) and store.org(str(body["org"])) is None:
                raise HTTPException(status_code=400, detail=f"org: there's no organization {body['org']}")
            if not identity.site_admin:
                body["orgs"] = api_providers.options(current)["orgs"] if current is not None else []
            try:
                url, options = api_providers.validate(name, body, current)
            except ProviderError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            # Whose it is: its machine's tenant's, else its owner's.
            tenant = fleet.backend_tenant(current) if current is not None and current.machine else (options["org"] or api_providers.ROOT_ORG)
            record = store.org(tenant) if tenant else None
            known = {team["key"] for team in record["teams"]} if record else set()
            unknown = sorted(set(options["teams"]) - known)
            if unknown:
                raise HTTPException(status_code=400, detail=f"teams: {', '.join(unknown)} isn't a team of {tenant}")
            for other in options["fallback"]:
                target = settings.backends.get(other)
                if target is None or target.type != "api" or (org is not None and fleet.backend_tenant(target) != tenant):
                    raise HTTPException(status_code=400, detail=f"fallback: {other} isn't an API provider" + (f" of {tenant}" if org is not None else ""))
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
        runtime.api_discovery.pop(name, None)
        return described(settings.backends[name])

    async def delete(name: str, request: Request, org: str | None) -> dict[str, Any]:
        changing(request)
        provider(name, org)
        async with runtime.config_lock:
            try:
                settings.remove_backend(name)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            keys.clear(name)
        runtime.api_discovery.pop(name, None)
        return {"ok": True}

    # Every organization's, for site admins.

    @app.get("/api/config/providers")
    async def list_providers(request: Request) -> dict[str, Any]:
        return await listing([backend for backend in settings.backends.values() if backend.type == "api"], auth.request_identity(request))

    @app.put("/api/config/providers/{name}")
    async def save_provider(name: str, request: Request) -> dict[str, Any]:
        return await save(name, request, None)

    @app.delete("/api/config/providers/{name}")
    async def delete_provider(name: str, request: Request) -> dict[str, Any]:
        return await delete(name, request, None)

    @app.post("/api/config/providers/{name}/test")
    async def test_provider(name: str, request: Request) -> dict[str, Any]:
        """Whether the server reaches the provider, and the models it lists (GET <url>/models)."""
        changing(request)
        return await discover(runtime, provider(name, None))

    # Models shared with every organization (Settings.shared_models), for site admins: SITE_ADMIN_ONLY as /api/config/.

    def shareable(backend: Any) -> bool:
        """Only the site's own providers (the root organization's machines and hosted APIs) can be shared with everyone."""
        return backend.type != "link" and fleet.backend_tenant(backend) == auth.ROOT_TENANT

    def shares() -> dict[str, Any]:
        return {
            "shared": settings.shared_models,
            "backends": [
                {"name": backend.name, "type": backend.type, "machine": backend.machine}
                for backend in settings.backends.values() if shareable(backend)
            ],
        }

    @app.get("/api/config/shared-models")
    async def shared_models() -> dict[str, Any]:
        """The models every organization may use, by provider, and the providers that can be shared."""
        return shares()

    @app.put("/api/config/shared-models")
    async def set_shared_models(request: Request) -> dict[str, Any]:
        """Replace the shares: {"shared": {backend: [model, ...]}}, "*" for every model of one; a provider left out isn't shared."""
        changing(request)
        try:
            data = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Send JSON") from exc
        value = data.get("shared") if isinstance(data, dict) else None
        if isinstance(value, dict):
            refused = sorted(name for name in value if name in settings.backends and not shareable(settings.backends[name]))
            if refused:
                raise HTTPException(status_code=400, detail=f"{', '.join(refused)} isn't the site's own provider, so it can't be shared with every organization")
        try:
            settings.set_shared_models(value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return shares()

    # One organization's, for its admins and site admins (changes are in ADMIN_ONLY with the rest of /api/orgs/*).

    @app.get("/api/orgs/{org}/providers")
    async def org_providers(org: str, request: Request) -> dict[str, Any]:
        """The organization's API providers: hosted ones added in it, and those on its machines."""
        identity = managed(request, org)
        return await listing([backend for backend in settings.backends.values() if backend.type == "api" and fleet.backend_tenant(backend) == org], identity)

    @app.put("/api/orgs/{org}/providers/{name}")
    async def save_org_provider(org: str, name: str, request: Request) -> dict[str, Any]:
        managed(request, org)
        return await save(name, request, org)

    @app.delete("/api/orgs/{org}/providers/{name}")
    async def delete_org_provider(org: str, name: str, request: Request) -> dict[str, Any]:
        managed(request, org)
        return await delete(name, request, org)

    @app.post("/api/orgs/{org}/providers/{name}/test")
    async def test_org_provider(org: str, name: str, request: Request) -> dict[str, Any]:
        """Whether the server reaches the provider, and the models it lists."""
        managed(request, org)
        changing(request)
        return await discover(runtime, provider(name, org))

    @app.post("/api/orgs/{org}/providers/{name}/balance")
    async def org_provider_balance(org: str, name: str, request: Request) -> JSONResponse:
        """Read DeepSeek's account balance with this organization's stored provider key."""
        managed(request, org)
        changing(request)
        return JSONResponse(await balance(runtime, provider(name, org)), headers={"Cache-Control": "no-store"})

    @app.post("/api/config/providers/{name}/balance")
    async def provider_balance(name: str, request: Request) -> JSONResponse:
        changing(request)
        return JSONResponse(await balance(runtime, provider(name, None)), headers={"Cache-Control": "no-store"})


async def balance(runtime: Runtime, backend: Any) -> dict[str, Any]:
    """DeepSeek's balance API is account data: explicit admin action only, never polled or persisted."""
    if not api_providers.deepseek(backend):
        raise HTTPException(status_code=400, detail="Balance checks are supported for the official DeepSeek API only")
    key = runtime.api_keys.get(backend, runtime.settings.backends)
    if not key:
        raise HTTPException(status_code=400, detail="Set this provider's API key before checking its balance")
    try:
        client, _ = await upstream(runtime, backend)
        response = await client.get("https://api.deepseek.com/user/balance", headers={"Authorization": f"Bearer {key}"},
                                    timeout=httpx.Timeout(20, connect=api_providers.options(backend)["connect_timeout"]))
        if response.status_code >= 400:
            message = "DeepSeek rejected the configured API key" if response.status_code in {401, 403} else "DeepSeek couldn't return the account balance. Try again later"
            raise HTTPException(status_code=429 if response.status_code == 429 else 502, detail=message)
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("is_available"), bool) or not isinstance(data.get("balance_infos"), list):
            raise ValueError("Invalid balance response")
        rows = []
        for item in data["balance_infos"][:10]:
            if not isinstance(item, dict) or item.get("currency") not in {"USD", "CNY"}:
                raise ValueError("Invalid balance currency")
            row = {"currency": item["currency"]}
            for field in ("total_balance", "granted_balance", "topped_up_balance"):
                value = item.get(field)
                if not isinstance(value, str) or not re.fullmatch(r"-?[0-9]{1,40}(\.[0-9]{1,40})?", value):
                    raise ValueError("Invalid balance amount")
                row[field] = value
            rows.append(row)
        return {"is_available": data["is_available"], "balance_infos": rows}
    except ProviderError as exc:
        raise HTTPException(status_code=503, detail="The server isn't ready to reach DeepSeek") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail="Couldn't reach DeepSeek to check the balance") from exc
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="DeepSeek returned an invalid balance response") from exc


async def auto_discover(runtime: Runtime, backend: Any) -> None:
    """Only hosted providers with server-side authentication opt into automatic discovery."""
    configured = api_providers.options(backend)
    if api_providers.external(backend) and configured["enabled"] and configured["discover"] and configured["auth"] != "agent":
        await discover(runtime, backend, force=False)
    elif api_providers.external(backend):
        runtime.api_discovery.pop(backend.name, None)


async def discover(runtime: Runtime, backend: Any, *, force: bool = True) -> dict[str, Any]:
    """Share model checks across concurrent callers; explicit Test connection bypasses the cache."""
    catalog = runtime.api_discovery.setdefault(backend.name, api_providers.ModelDiscovery())
    async with catalog.lock:
        key = runtime.api_keys.get(backend, runtime.settings.backends)
        # A URL, option or key change must not keep another configuration's model list or failed check.
        signature = hashlib.sha256(json.dumps([backend.url, backend.machine, api_providers.options(backend), key], sort_keys=True).encode()).hexdigest()
        if catalog.signature != signature:
            catalog.signature, catalog.expires_at = signature, 0
            catalog.models, catalog.contexts = [], {}
        if not force and catalog.expires_at > time.monotonic():
            return catalog.result
        catalog.result = await _discover(runtime, backend, catalog)
        catalog.expires_at = time.monotonic() + (300 if catalog.result["ok"] else 30)
        return catalog.result


async def _discover(runtime: Runtime, backend: Any, catalog: api_providers.ModelDiscovery) -> dict[str, Any]:
    """Ask the provider for its models: a connectivity test and model discovery at once. The result is kept
    as this process's health and model catalog, retaining the last good list on transient failures."""
    started = time.perf_counter()
    status: int | None = None
    try:
        client, base = await upstream(runtime, backend)
        key = runtime.api_keys.get(backend, runtime.settings.backends)
        headers = api_providers.headers(backend, {}, key, None)
        # DeepSeek publishes this at /models, independently of a saved OpenAI /v1 base.
        url = "https://api.deepseek.com/models" if api_providers.deepseek(backend) else api_providers.target(base, backend, "/v1/models")
        response = await client.get(
            url, headers=headers,
            params=api_providers.query(backend, []), timeout=httpx.Timeout(20, connect=api_providers.options(backend)["connect_timeout"]),
        )
        status = response.status_code
        latency = (time.perf_counter() - started) * 1000
        body = response.json() if status < 400 else None
        # Some gateways answer an error with 200 and an error body: that is a failure, not a reachable provider.
        if status >= 400 or (isinstance(body, dict) and body.get("error")):
            error = _error(response)
            runtime.api_health.record(backend.name, status, latency, error)
            return {"ok": False, "status": status, "latency_ms": round(latency, 1), "models": None, "error": error}
        listed = body.get("data")
        entries = {item["id"]: item for item in listed if isinstance(item, dict) and isinstance(item.get("id"), str)
                   and api_providers.MODEL_NAME.fullmatch(item["id"])} if isinstance(listed, list) else None
        models = sorted(entries)[: api_providers.MAX_MODELS] if entries is not None else None
        if entries is not None:
            catalog.contexts = {}
            for model in models:
                item = entries[model]
                size = item.get("context_window", item.get("context_length"))
                if isinstance(size, int) and not isinstance(size, bool) and size > 0:
                    catalog.contexts[model] = size
    except ProviderError as exc:
        runtime.api_health.record(backend.name, None, None, str(exc))
        return {"ok": False, "status": None, "latency_ms": None, "models": None, "error": str(exc)}
    except (httpx.HTTPError, OSError, ValueError, AttributeError) as exc:
        latency = (time.perf_counter() - started) * 1000
        error = str(exc) or type(exc).__name__
        runtime.api_health.record(backend.name, status, latency, error)
        return {"ok": False, "status": status, "latency_ms": round(latency, 1), "models": None, "error": error}
    error = None if models is not None else "It answered, but not with a model list"
    # A provider that answered is reachable even if its answer isn't a model list (some gateways use
    # another shape): health is about the connection, while error says what the check couldn't read.
    runtime.api_health.record(backend.name, status, latency, None)
    if models is not None:
        catalog.models = models
    return {"ok": True, "status": status, "latency_ms": round(latency, 1), "models": models, "error": error}


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
