"""Provider accounts' API: people's own subscriptions, where they run, their usage, and choosing one for a task.

See provider_accounts.py for what an account is and who may do what. Everything here is
metadata: no answer carries a credential, except POST /api/sessions/{id}/provider-credential,
which hands a managed token to the launcher of a session the server started, once.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import access, auth, remote
from ..provider_accounts import MODES, PROVIDERS, STATES, WINDOWS, AccountError
from ..runtime import Runtime

# A token or an account's settings are small; anything bigger is refused before it's parsed.
BODY_LIMIT = 16_384


def register(app: FastAPI, runtime: Runtime) -> None:
    accounts = runtime.provider_accounts
    fleet = runtime.fleet

    def identity_of(request: Request, change: bool = False) -> Any:
        if not runtime.remote.trusted(request):
            raise HTTPException(status_code=401, detail="Sign in first.")
        if change and not remote.same_origin(request):
            raise HTTPException(status_code=403, detail="Change provider accounts from the console's own page.")
        return auth.request_identity(request)

    async def body(request: Request) -> dict[str, Any]:
        raw = await request.body()
        if len(raw) > BODY_LIMIT:
            raise HTTPException(status_code=413, detail="That's too big")
        try:
            data = json.loads(raw or b"{}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Send a JSON object") from exc
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Send a JSON object")
        return data

    def failed(exc: AccountError) -> HTTPException:
        return HTTPException(status_code=exc.status, detail=exc.detail)

    @app.get("/api/provider-accounts")
    async def listing(request: Request, machine: str = "") -> dict[str, Any]:
        """The provider accounts you may see (yours, those shared with you to launch, and for admins their tenant's), metadata only."""
        identity = identity_of(request)
        return {
            "accounts": [accounts.public(account, identity) for account in accounts.listed(identity, machine or None)],
            "providers": {name: {key: value for key, value in item.items() if key != "hosts"} for name, item in PROVIDERS.items()},
            "modes": MODES, "states": list(STATES), "windows": list(WINDOWS),
            "vault": {"configured": accounts.vault.configured},
        }

    @app.post("/api/provider-accounts")
    async def create(request: Request) -> JSONResponse:
        """A new provider account of yours; the one you already have with that provider and name answers 200."""
        identity = identity_of(request, change=True)
        try:
            account, made = accounts.create(identity, await body(request))
        except AccountError as exc:
            raise failed(exc) from exc
        return JSONResponse({**accounts.public(account, identity), "existing": not made}, status_code=201 if made else 200)

    @app.post("/api/provider-accounts/plan")
    async def plan(request: Request) -> dict[str, Any]:
        """Which of your subscriptions (agent, machine, account) should take a new task now, best first, and why others can't.

        agents: account agents in the order you prefer them (default: every one); machine: only that one;
        project: a folder that must be inside the machine's launch folders; fallback: local-model agents to
        use when no subscription can (a paid API is never chosen); allow_unknown (default true).
        """
        identity = identity_of(request)
        data = await body(request)
        known = runtime.agents(identity)
        named =[str(item) for item in data.get("agents") or []] or [agent.id for agent in known.values() if agent.subscription]
        agents = [known[item] for item in named if item in known]
        fallback = [known[str(item)] for item in data.get("fallback") or [] if str(item) in known]
        if data.get("machine"):
            machine = fleet.machines.get(str(data["machine"]))
            if machine is None or not fleet_can_see(identity, machine):
                raise HTTPException(status_code=404, detail="No such machine")
            machines = [machine]
        else:
            machines = list(fleet.machines.values())
        return accounts.plan(identity, agents, machines, fallback, allow_unknown=data.get("allow_unknown", True) is not False, project=str(data.get("project") or ""))

    @app.get("/api/provider-accounts/{account_id}")
    async def one(account_id: str, request: Request) -> dict[str, Any]:
        identity = identity_of(request)
        try:
            return accounts.public(accounts.visible(account_id, identity), identity, usage=True)
        except AccountError as exc:
            raise failed(exc) from exc

    @app.patch("/api/provider-accounts/{account_id}")
    async def change(account_id: str, request: Request) -> dict[str, Any]:
        """Its name, plan, priority, who else may launch with it, a declared limit and its thresholds (its owner);
        status active or disabled (its owner, or an admin of its tenant); clear_observations forgets cooldowns."""
        identity = identity_of(request, change=True)
        try:
            account = accounts.update(identity, accounts.visible(account_id, identity), await body(request))
        except AccountError as exc:
            raise failed(exc) from exc
        return accounts.public(account, identity)

    @app.delete("/api/provider-accounts/{account_id}")
    async def delete(account_id: str, request: Request) -> dict[str, Any]:
        identity = identity_of(request, change=True)
        try:
            accounts.delete(identity, accounts.managed(account_id, identity))
        except AccountError as exc:
            raise failed(exc) from exc
        return {"deleted": account_id}

    @app.get("/api/provider-accounts/{account_id}/health")
    async def health(account_id: str, request: Request) -> dict[str, Any]:
        identity = identity_of(request)
        try:
            account = accounts.visible(account_id, identity)
        except AccountError as exc:
            raise failed(exc) from exc
        state, reason = accounts.state(account)
        return {"account_id": account.id, "state": state, "reason": reason, "machines": [accounts.health(account, item.machine_id) for item in accounts.assigned(account)]}

    @app.get("/api/provider-accounts/{account_id}/usage")
    async def usage(account_id: str, request: Request) -> dict[str, Any]:
        """What went through this server for it, by window (observed), and what's left of a limit you declared (an estimate)."""
        identity = identity_of(request)
        try:
            return accounts.usage(accounts.visible(account_id, identity))
        except AccountError as exc:
            raise failed(exc) from exc

    @app.get("/api/provider-accounts/{account_id}/events")
    async def events(account_id: str, request: Request) -> dict[str, Any]:
        """Its audit trail (owner and admins of its tenant): enrollments, assignments, provisioning, revocations, limits seen."""
        identity = identity_of(request)
        try:
            account = accounts.visible(account_id, identity)
        except AccountError as exc:
            raise failed(exc) from exc
        if not accounts.may_suspend(identity, account):
            raise HTTPException(status_code=403, detail="Only its owner and admins see its history")
        return {"events": accounts.events(account)}

    @app.get("/api/provider-accounts/{account_id}/assignments")
    async def assignments(account_id: str, request: Request) -> dict[str, Any]:
        identity = identity_of(request)
        try:
            account = accounts.visible(account_id, identity)
        except AccountError as exc:
            raise failed(exc) from exc
        return {"assignments": [{**item.__dict__, "health": accounts.health(account, item.machine_id)} for item in accounts.assigned(account)]}

    @app.put("/api/provider-accounts/{account_id}/assignments/{machine_id}")
    async def assign(account_id: str, machine_id: str, request: Request) -> dict[str, Any]:
        """Let it run on one of your machines (enabled, and for a token account provisioning_allowed)."""
        identity = identity_of(request, change=True)
        try:
            account = accounts.managed(account_id, identity)
            assignment = accounts.assign(identity, account, machine_id, await body(request))
        except AccountError as exc:
            raise failed(exc) from exc
        return {**assignment.__dict__, "health": accounts.health(account, machine_id)}

    @app.delete("/api/provider-accounts/{account_id}/assignments/{machine_id}")
    async def unassign(account_id: str, machine_id: str, request: Request) -> dict[str, Any]:
        """Stop it running on that machine: new launches there are refused, and a pending token handover is dropped."""
        identity = identity_of(request, change=True)
        try:
            accounts.unassign(identity, accounts.managed(account_id, identity), machine_id)
        except AccountError as exc:
            raise failed(exc) from exc
        return {"unassigned": machine_id}

    @app.post("/api/provider-accounts/{account_id}/enrollments", status_code=201)
    async def enroll(account_id: str, request: Request) -> dict[str, Any]:
        """Register a machine as an authenticated runner for it (agent accounts enroll, on that machine): it's assigned
        there. machine names it, else hostname finds your machine with that name. Nothing secret is sent."""
        identity = identity_of(request, change=True)
        data = await body(request)
        try:
            account = accounts.managed(account_id, identity)
            machine = accounts.find_machine(identity, str(data.get("machine") or "") or None, str(data.get("hostname") or "") or None)
            assignment = accounts.assign(identity, account, machine.id, {"enabled": True}, source="enrollment")
        except AccountError as exc:
            raise failed(exc) from exc
        detected = data.get("detected") if isinstance(data.get("detected"), dict) else {}
        accounts.event(account, "enrolled", identity, machine=machine.id, auth=str(detected.get("auth") or "unknown")[:20], cli_version=str(detected.get("version") or "")[:60])
        account.last_validated_at = runtime.provider_accounts.clock() if detected.get("auth") == "signed_in" else account.last_validated_at
        return {"account": accounts.public(account, identity), "machine": {"id": machine.id, "name": machine.name}, "assignment": assignment.__dict__}

    @app.put("/api/provider-accounts/{account_id}/credential")
    async def credential(account_id: str, request: Request) -> dict[str, Any]:
        """Store its managed token ({"token": ...}), encrypted; replaces any before. Never readable back."""
        identity = identity_of(request, change=True)
        try:
            account = accounts.managed(account_id, identity)
            accounts.set_credential(identity, account, (await body(request)).get("token"))
        except AccountError as exc:
            raise failed(exc) from exc
        return accounts.public(account, identity)

    @app.post("/api/provider-accounts/{account_id}/revoke")
    async def revoke(account_id: str, request: Request) -> dict[str, Any]:
        """Forget its managed token and stop handing it out. Revoke it at the provider too."""
        identity = identity_of(request, change=True)
        try:
            account = accounts.managed(account_id, identity)
            accounts.revoke(identity, account)
        except AccountError as exc:
            raise failed(exc) from exc
        return accounts.public(account, identity)

    @app.post("/api/sessions/{session_id}/provider-credential")
    async def provider_credential(session_id: str, request: Request) -> JSONResponse:
        """The managed token for a session the server launched on an assigned machine, once, for its launcher to put in
        the agent's environment. Only the session's owner (the machine's sign-in), only within the launch's grant."""
        identity = identity_of(request, change=True)
        session = runtime.sessions.get(session_id)
        if session is None or session.ended_at or not access.is_owner(identity, session):
            raise HTTPException(status_code=404, detail="Session not found")
        try:
            env = accounts.deliver(identity, session)
        except AccountError as exc:
            raise failed(exc) from exc
        return JSONResponse({"env": env}, headers={"Cache-Control": "no-store"})

    def fleet_can_see(identity: Any, machine: Any) -> bool:
        from ..fleet import can_see

        return can_see(identity, machine)
