"""Providers as people use them, and organizations' usage of them: GET /api/providers, /api/orgs/{org}/usage and its limits.

A provider is a backend (a model server on a machine, or a hosted API) or a kind of
subscription; usage_limits.py says how usage is counted and how limits stop someone. Everyone sees
the providers their organization may use with their own usage and limits; an organization's admins
(and site admins) see everyone's in it and set the limits. Members see only their own rows.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request

from .. import api_providers, auth, remote
from ..config import MACHINE_TYPES, PROVIDER_NAMES
from ..provider_accounts import PROVIDERS, WINDOWS
from ..runtime import Runtime
from ..usage_limits import EVERYONE, WINDOW_LABELS, provider_label


def register(app: FastAPI, runtime: Runtime) -> None:
    usage = runtime.usage
    fleet = runtime.fleet
    store = runtime.auth.store

    def identity_of(request: Request) -> Any:
        identity = auth.request_identity(request)
        if identity is None:
            raise HTTPException(status_code=401, detail="Sign in first.")
        return identity

    def providers_for(tenant: str, usable: Any = None) -> list[dict[str, Any]]:
        """Every provider this organization's people can use: its machines' backends, hosted APIs, and the
        subscriptions; with usable (fleet.usable_by), only those one person may (an api provider can be for some teams)."""
        found = []
        for backend in runtime.settings.backends.values():
            owner = fleet.backend_tenant(backend)
            if backend.type == "api":
                # It carries a key: its own organization's, and those it's shared with.
                tenants = fleet.api_tenants(backend)
                if fleet.SHARED not in tenants and tenant not in tenants:
                    continue
            elif owner != fleet.SHARED and owner != tenant:
                continue
            if usable is not None and not usable(backend):
                continue
            machine = fleet.machines.get(backend.machine) if backend.machine else None
            found.append({
                "id": f"backend:{backend.name}", "kind": "local" if backend.type in MACHINE_TYPES else "hosted",
                "type": backend.type, "type_label": PROVIDER_NAMES.get(backend.type, backend.type), "name": backend.name,
                "label": provider_label(f"backend:{backend.name}", runtime.settings, fleet),
                "machine": {"id": machine.id, "name": machine.name, "online": machine.id in fleet.sockets} if machine else None,
                "container": bool(backend.container), "default": backend.name == runtime.settings.default_backend,
            })
        for kind, provider in PROVIDERS.items():
            found.append({
                "id": f"subscription:{kind}", "kind": "subscription", "type": kind, "type_label": provider["label"], "name": kind,
                "label": provider_label(f"subscription:{kind}"), "machine": None, "container": False, "default": False,
            })
        return found

    def zero() -> dict[str, dict[str, int]]:
        return {window: {"requests": 0, "input_tokens": 0, "output_tokens": 0} for window in WINDOWS}

    @app.get("/api/providers")
    async def providers(request: Request) -> dict[str, Any]:
        """The providers you can use, your usage of each and the limits on you there."""
        identity = identity_of(request)
        mine = usage.windows(identity.tenant, identity.user)
        accounts = [account for account in runtime.provider_accounts.listed(identity) if account.owner == identity.user or runtime.provider_accounts.may_use(identity, account)]
        listed = []
        for provider in providers_for(identity.tenant, fleet.usable_by(identity)):
            limits = usage.status(identity.tenant, identity.user, provider["id"])
            reached = [item for item in limits if item["reached"]]
            extra: dict[str, Any] = {}
            if provider["kind"] == "subscription":
                extra["accounts"] = sum(1 for account in accounts if account.provider == provider["type"])
            listed.append({
                **provider, **extra,
                "usage": mine.get((identity.user, provider["id"])) or zero(),
                "limits": limits,
                "blocked": max(reached, key=lambda item: item["until"] or 0) if reached else None,
            })
        return {"providers": listed, "windows": [{"id": window, "label": WINDOW_LABELS[window]} for window in WINDOWS], "user": identity.user, "tenant": identity.tenant}

    def org_visible(request: Request, org: str) -> tuple[Any, bool]:
        """The organization's usage is its admins' (and site admins') to see whole; a member sees their own. Others: 404."""
        identity = identity_of(request)
        if store.org(org) is None or not auth.same_tenant(identity, org):
            raise HTTPException(status_code=404, detail="No such organization")
        return identity, auth.administers(identity, org)

    def report(org: str, identity: Any, manager: bool) -> dict[str, Any]:
        people = [user["name"] for user in store.user_list() if user["tenant"] == org]
        if not manager:
            people = [name for name in people if name == identity.user]
        totals = usage.windows(org, None if manager else identity.user)
        known = providers_for(org)
        ids = {item["id"] for item in known}
        # Usage of a backend that's gone (or moved to another organization) still shows, by its id.
        for _, provider in totals:
            if provider not in ids:
                ids.add(provider)
                known.append({"id": provider, "kind": "removed", "type": "", "type_label": "", "name": provider.split(":", 1)[-1],
                              "label": provider_label(provider, runtime.settings, fleet), "machine": None, "container": False, "default": False})
        limits = usage.tenant_limits(org)
        everyone = {limit.provider for limit in limits if limit.user == EVERYONE}
        pairs = {(user, provider) for user, provider in totals if user in people or manager}
        pairs |= {(limit.user, limit.provider) for limit in limits if limit.user != EVERYONE and limit.user in people}
        pairs |= {(user, provider) for provider in everyone for user in people}
        rows = []
        for user, provider in sorted(pairs):
            status = usage.status(org, user, provider)
            reached = [item for item in status if item["reached"]]
            rows.append({
                "user": user, "provider": provider, "usage": totals.get((user, provider)) or zero(), "limits": status,
                "blocked": max(reached, key=lambda item: item["until"] or 0) if reached else None,
            })
        return {
            "org": org, "manage": manager, "users": people, "providers": known, "rows": rows,
            "limits": [limit.public() for limit in limits] if manager else [],
            "windows": [{"id": window, "label": WINDOW_LABELS[window]} for window in WINDOWS],
        }

    @app.get("/api/orgs/{org}/usage")
    async def org_usage(org: str, request: Request) -> dict[str, Any]:
        """Each person's usage of each provider in every window, the limits on them and who's blocked."""
        identity, manager = org_visible(request, org)
        return report(org, identity, manager)

    def administered(request: Request, org: str) -> Any:
        identity, manager = org_visible(request, org)
        if not manager:
            raise HTTPException(status_code=403, detail="Only an admin of this organization can set its usage limits.")
        if not remote.same_origin(request):
            raise HTTPException(status_code=403, detail="Set usage limits from the console's own page.")
        return identity

    @app.put("/api/orgs/{org}/limits")
    async def set_limit(org: str, request: Request) -> dict[str, Any]:
        """Set one limit: {user ("*" for everyone in the organization), provider, window, tokens}."""
        identity = administered(request, org)
        data = await request.json()
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Send a JSON object")
        user, provider, window = str(data.get("user") or ""), str(data.get("provider") or ""), str(data.get("window") or "")
        if user != EVERYONE and (store.user(user) or {}).get("tenant") != org:
            raise HTTPException(status_code=400, detail=f"{user or 'That'} isn't someone in this organization")
        if provider not in {item["id"] for item in providers_for(org)} and (org, user, provider, window) not in usage.limits:
            raise HTTPException(status_code=400, detail="Choose one of the providers this organization can use")
        try:
            usage.set_limit(org, user, provider, window, data.get("tokens"), identity.user)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return report(org, identity, True)

    @app.delete("/api/orgs/{org}/limits")
    async def remove_limit(org: str, user: str, provider: str, window: str, request: Request) -> dict[str, Any]:
        """Remove one limit (named by user, provider and window in the query)."""
        identity = administered(request, org)
        if not usage.remove_limit(org, user, provider, window):
            raise HTTPException(status_code=404, detail="No such limit")
        return report(org, identity, True)
