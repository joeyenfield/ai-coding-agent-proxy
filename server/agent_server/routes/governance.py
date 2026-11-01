"""Retention policies, legal holds and the audit log (retention.py, audit.py).

  GET|PUT    /api/config/retention        the server's policy (site admins: /api/config/ is SITE_ADMIN_ONLY)
  GET|PUT    /api/config/audit            the audit log's exports and checkpoint interval (site admins)
  GET|PUT|DELETE /api/orgs/{org}/retention an organization's own policy, within the server's (its admins, site admins)
  GET|POST   /api/holds, DELETE /api/holds/{id}   legal holds (admins, for their organization; site admins, any)
  GET        /api/audit                   events: site admins every organization's, admins their own
  GET        /api/audit/export            the same as NDJSON or CSV, for a download
  GET        /api/audit/verify            the chains and checkpoints checked (admins: their own; site admins: all)
  POST       /api/audit/checkpoint        sign the chains' heads now (site admins)

Another organization's policy, holds or events answer 404, as everything of another tenant does.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from .. import auth, remote
from ..retention import RetentionError
from ..runtime import Runtime


def register(app: FastAPI, runtime: Runtime) -> None:
    retention, audit = runtime.retention, runtime.audit

    def identity_of(request: Request) -> Any:
        identity = auth.request_identity(request)
        if identity is None:
            raise HTTPException(status_code=401, detail="Sign in first.")
        return identity

    def changing(request: Request) -> Any:
        if not remote.same_origin(request):
            raise HTTPException(status_code=403, detail="Change this from the console's own page.")
        return identity_of(request)

    def organization(org: str, request: Request) -> Any:
        identity = identity_of(request)
        if runtime.auth.store.org(org) is None or not auth.same_tenant(identity, org):
            raise HTTPException(status_code=404, detail="No such organization")
        return identity

    def admin_for(org: str, request: Request) -> Any:
        identity = organization(org, request)
        if not auth.administers(identity, org):
            raise HTTPException(status_code=403, detail="Only an admin of this organization can change this.")
        return identity

    def audit_scope(request: Request, org: str | None) -> str | None:
        """Whose events someone may read: a site admin anyone's (or one organization's), an admin their own."""
        identity = identity_of(request)
        if not identity.admin:
            raise HTTPException(status_code=403, detail="Only admins read the audit log.")
        if identity.site_admin:
            return org or None
        if org and org != identity.tenant:
            raise HTTPException(status_code=404, detail="No such organization")
        return identity.tenant

    async def body(request: Request) -> Any:
        try:
            return await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Send JSON") from exc

    # Retention

    @app.get("/api/config/retention")
    async def server_retention(request: Request) -> dict[str, Any]:
        """The server's retention and capture policy, and the bounds organizations set theirs within."""
        return await asyncio.to_thread(retention.server)

    @app.put("/api/config/retention")
    async def set_server_retention(request: Request) -> dict[str, Any]:
        identity = changing(request)
        try:
            return await asyncio.to_thread(retention.set_server, await body(request), identity.user)
        except RetentionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/orgs/{org}/retention")
    async def org_retention(org: str, request: Request) -> dict[str, Any]:
        """The policy this organization's sessions start under: the server's, with its own on top."""
        organization(org, request)
        return {**retention.effective(org), "holds": len(retention.holds(org))}

    @app.put("/api/orgs/{org}/retention")
    async def set_org_retention(org: str, request: Request) -> dict[str, Any]:
        identity = admin_for(org, request)
        changing(request)
        try:
            return await asyncio.to_thread(retention.set_org, org, await body(request), identity.user)
        except RetentionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/orgs/{org}/retention")
    async def reset_org_retention(org: str, request: Request) -> dict[str, Any]:
        admin_for(org, request)
        changing(request)
        await asyncio.to_thread(retention.remove_org, org)
        return retention.effective(org)

    # Legal holds

    @app.get("/api/holds")
    async def holds(request: Request, org: str | None = None) -> dict[str, Any]:
        identity = identity_of(request)
        if not identity.admin:
            raise HTTPException(status_code=403, detail="Only admins see legal holds.")
        tenant = (org or None) if identity.site_admin else identity.tenant
        return {"holds": retention.holds(tenant)}

    @app.post("/api/holds")
    async def add_hold(request: Request) -> dict[str, Any]:
        identity = changing(request)
        data = await body(request)
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Send {scope, target, reason, org}")
        org = str(data.get("org") or identity.tenant)
        admin_for(org, request)
        try:
            return await asyncio.to_thread(retention.add_hold, org, str(data.get("scope") or ""), str(data.get("target") or ""), data.get("reason"), identity.user)
        except RetentionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/holds/{hold_id}")
    async def remove_hold(hold_id: str, request: Request) -> dict[str, Any]:
        identity = changing(request)
        hold = next((item for item in retention.holds() if item["id"] == hold_id), None)
        if hold is None or not auth.same_tenant(identity, hold["tenant"]):
            raise HTTPException(status_code=404, detail="No such hold")
        admin_for(hold["tenant"], request)
        await asyncio.to_thread(retention.remove_hold, hold_id)
        return {"removed": hold_id}

    # The audit log

    @app.get("/api/config/audit")
    async def audit_config(request: Request) -> dict[str, Any]:
        key_id, public_key = await asyncio.to_thread(audit.public_key)
        return {**await asyncio.to_thread(audit.config), "key_id": key_id, "public_key": public_key, "backlog": await asyncio.to_thread(audit.backlog)}

    @app.put("/api/config/audit")
    async def set_audit_config(request: Request) -> dict[str, Any]:
        changing(request)
        try:
            return await asyncio.to_thread(audit.set_config, await body(request))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def filters(request: Request) -> dict[str, Any]:
        params = request.query_params

        def number(value: str | None, name: str) -> float | None:
            try:
                return float(value) if value else None
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"{name} is seconds since the epoch") from exc

        # before is a time, or the cursor next_before gave ("<time>~<id>").
        before, _, before_id = (params.get("before") or "").partition("~")
        return {"kind": params.get("type") or None, "actor": params.get("actor") or None, "outcome": params.get("outcome") or None,
                "since": number(params.get("since"), "since"), "before": number(before, "before"), "before_id": before_id or None}

    @app.get("/api/audit")
    async def events(request: Request, org: str | None = None, limit: int = 100) -> dict[str, Any]:
        tenant = audit_scope(request, org)
        found = await asyncio.to_thread(audit.query, tenant, limit=max(1, min(limit, 1000)), **filters(request))
        last = found[-1] if len(found) >= max(1, min(limit, 1000)) else None
        return {"events": found, "next_before": f"{last['time_epoch']!r}~{last['id']}" if last else None}

    @app.get("/api/audit/export")
    async def export(request: Request, org: str | None = None, format: str = "ndjson") -> PlainTextResponse:
        tenant = audit_scope(request, org)
        found = await asyncio.to_thread(audit.query, tenant, limit=5000, **filters(request))
        if format == "csv":
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow(["time", "type", "tenant", "actor", "outcome", "action", "target", "ip", "reason", "seq", "hash"])
            for event in found:
                data = event.get("data") or {}
                writer.writerow([event.get("time"), event["type"], event["tenant"], event.get("actor"), event["outcome"], data.get("action"),
                                 json.dumps(data.get("target") or {}), (data.get("actor") or {}).get("ip"), data.get("reason"), event["chain"]["seq"], event["chain"]["hash"]])
            return PlainTextResponse(out.getvalue(), media_type="text/csv", headers={"content-disposition": "attachment; filename=audit.csv"})
        text = "".join(json.dumps(event, default=str) + "\n" for event in found)
        return PlainTextResponse(text, media_type="application/x-ndjson", headers={"content-disposition": "attachment; filename=audit.ndjson"})

    @app.get("/api/audit/verify")
    async def verify(request: Request, org: str | None = None) -> dict[str, Any]:
        tenant = audit_scope(request, org)
        return await asyncio.to_thread(audit.verify, tenant)

    @app.post("/api/audit/checkpoint")
    async def checkpoint(request: Request) -> dict[str, Any]:
        identity = changing(request)
        if not identity.site_admin:
            raise HTTPException(status_code=403, detail="Only a site admin can do this.")
        record = await asyncio.to_thread(audit.checkpoint)
        return {"checkpoint": record}
