"""An organization's costs: GET /api/orgs/{org}/costs, POST /api/orgs/{org}/costs/recompute and GET /api/prices.

costs.py adds up what an organization's model calls cost between two days and prices them again.
The organization's admins (and site admins) see everyone's in it and may recompute; a member sees
only the cost of their own sessions. Another organization's answers 404. The price table itself is
the server's (site admins change it at /api/config/prices); anyone signed in may read it here.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from .. import auth, costs, remote
from ..runtime import Runtime


def register(app: FastAPI, runtime: Runtime) -> None:
    store = runtime.auth.store

    def visible(request: Request, org: str) -> tuple[Any, bool]:
        """The tenant first: the caller, and whether they see the whole organization's costs or only their own."""
        identity = auth.request_identity(request)
        if identity is None:
            raise HTTPException(status_code=401, detail="Sign in first.")
        if store.org(org) is None or not auth.same_tenant(identity, org):
            raise HTTPException(status_code=404, detail="No such organization")
        return identity, auth.administers(identity, org)

    @app.get("/api/prices")
    async def prices(request: Request) -> dict[str, Any]:
        """The price table cost estimates are made with, for anyone signed in to read: dollars for a million
        tokens by model name pattern, first match first. editable says whether the caller may change it
        (site admins, at /api/config/prices)."""
        identity = auth.request_identity(request)
        return {
            "currency": "USD", "prices": runtime.settings.prices(), "custom": runtime.settings.model_prices is not None,
            "editable": bool(identity and identity.site_admin),
        }

    @app.get("/api/orgs/{org}/costs")
    async def org_costs(org: str, request: Request, since: str, until: str) -> dict[str, Any]:
        """What the organization's model calls cost between two UTC days (since and until, YYYY-MM-DD, both
        included): usd (hosted APIs, paid by the token), subscription_usd (subscriptions' calls at API prices),
        how many calls were priced and how many had no price, and the same for each day, model, person and
        provider. Its admins get everyone's (manage: true); a member only their own sessions'."""
        identity, manager = visible(request, org)
        try:
            found = await asyncio.to_thread(costs.report, runtime.db, org, since, until, None if manager else identity.user)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {**found, "manage": manager}

    @app.post("/api/orgs/{org}/costs/recompute")
    async def recompute(org: str, request: Request) -> dict[str, Any]:
        """Price the organization's recorded model calls between two days again, at the prices of now:
        {since, until}. A call with no price gets one if there is one now, and a call priced before gets
        today's. Answers how many calls were read, how many changed, and the totals before and after."""
        identity, manager = visible(request, org)
        if not manager:
            raise HTTPException(status_code=403, detail="Only an admin of this organization can recompute its costs.")
        if not remote.same_origin(request):
            raise HTTPException(status_code=403, detail="Recompute costs from the console's own page.")
        try:
            data = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Send JSON") from exc
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail='Send {"since": "YYYY-MM-DD", "until": "YYYY-MM-DD"}')
        try:
            answer, changed = await asyncio.to_thread(costs.recompute, runtime.db, runtime.reprice, org, data.get("since"), data.get("until"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # On the event loop, which owns the recent requests it holds.
        runtime.repriced(changed)
        return answer
