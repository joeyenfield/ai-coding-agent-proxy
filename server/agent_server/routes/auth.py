"""Signing in to the proxy: the console's sign-in, and tokens for launchers on other machines."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import auth
from ..runtime import Runtime


def register(app: FastAPI, runtime: Runtime) -> None:
    accounts = runtime.auth

    def client_of(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    async def credentials(request: Request) -> tuple[str, str, dict[str, Any]]:
        """Check a username and password, counting failures per client."""
        client = client_of(request)
        if not accounts.allowed_attempt(client):
            raise HTTPException(status_code=429, detail="Too many failed sign-ins. Wait ten minutes and try again.")
        data = await request.json()
        user = str(data.get("username") or "").strip() if isinstance(data, dict) else ""
        password = str(data.get("password") or "") if isinstance(data, dict) else ""
        if not accounts.enabled:
            raise HTTPException(status_code=400, detail="Sign-in is off: this proxy has no accounts. Add one with `agent-server user add <name>`.")
        user = user.lower()
        if not user or not password or not accounts.check_password(user, password):
            accounts.failed(client)
            raise HTTPException(status_code=401, detail="That username and password don't match.")
        accounts.succeeded(client)
        return user, password, data

    def signed_in(request: Request) -> auth.Identity:
        identity = auth.request_identity(request)
        if identity is None or identity.via not in {"cookie", "token"}:
            raise HTTPException(status_code=401, detail="Sign in with your account first.")
        return identity

    @app.get("/api/auth/status")
    async def status(request: Request) -> dict[str, Any]:
        identity = accounts.identify(request) if accounts.enabled else None
        return {
            "required": accounts.enabled,
            "identity": identity.public() if identity else None,
            "secure": request.url.scheme == "https",
        }

    @app.post("/api/auth/login")
    async def login(request: Request) -> JSONResponse:
        user, _, _ = await credentials(request)
        secret = accounts.store.start_session(user, request.headers.get("user-agent", ""))
        response = JSONResponse({"identity": {"user": user, "via": "cookie"}})
        response.set_cookie(
            auth.COOKIE, secret, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax",
            secure=request.url.scheme == "https",
        )
        return response

    @app.post("/api/auth/logout")
    async def logout(request: Request) -> JSONResponse:
        secret = request.cookies.get(auth.COOKIE)
        if secret:
            accounts.store.end_session(secret)
        response = JSONResponse({"identity": None})
        response.delete_cookie(auth.COOKIE)
        return response

    @app.post("/api/auth/token")
    async def token_for_launcher(request: Request) -> dict[str, Any]:
        """`agent login`: a username and password in, a launcher token out."""
        user, _, data = await credentials(request)
        token_id, secret = accounts.store.create_token(user, str(data.get("label") or "launcher"))
        return {"id": token_id, "token": secret, "user": user}

    @app.get("/api/auth/tokens")
    async def tokens(request: Request) -> dict[str, Any]:
        identity = signed_in(request)
        return {"tokens": accounts.store.tokens(identity.user)}

    @app.post("/api/auth/tokens", status_code=201)
    async def create_token(request: Request) -> dict[str, Any]:
        identity = signed_in(request)
        data = await request.json()
        token_id, secret = accounts.store.create_token(identity.user, str((data or {}).get("label") or "launcher"))
        return {"id": token_id, "token": secret}

    @app.delete("/api/auth/tokens/{token_id}")
    async def revoke(token_id: str, request: Request) -> dict[str, Any]:
        identity = signed_in(request)
        if not accounts.store.revoke_token(identity.user, token_id):
            raise HTTPException(status_code=404, detail="No such token")
        return {"revoked": token_id}

    @app.post("/api/auth/password")
    async def change_password(request: Request) -> dict[str, Any]:
        identity = signed_in(request)
        data = await request.json()
        if not accounts.store.verify(identity.user, str(data.get("current") or "")):
            raise HTTPException(status_code=403, detail="Your current password doesn't match.")
        try:
            accounts.store.set_password(identity.user, str(data.get("new") or ""))
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc) if isinstance(exc, ValueError) else "Only stored accounts can change their password here.") from exc
        return {"changed": True}

    # The organization: everyone signed in can see it; only admins change it (the middleware checks).

    def org_or_400() -> None:
        if not accounts.enabled:
            raise HTTPException(status_code=400, detail="This proxy has no accounts, so no organization yet.")

    @app.get("/api/org")
    async def org() -> dict[str, Any]:
        org_or_400()
        return accounts.store.org()

    @app.put("/api/org")
    async def rename(request: Request) -> dict[str, Any]:
        org_or_400()
        accounts.store.rename_org(str((await request.json()).get("name") or ""))
        return accounts.store.org()

    @app.post("/api/org/teams", status_code=201)
    async def add_team(request: Request) -> dict[str, Any]:
        org_or_400()
        data = await request.json()
        try:
            accounts.store.add_team(str(data.get("id") or ""), data.get("name"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return accounts.store.org()

    @app.delete("/api/org/teams/{team}")
    async def remove_team(team: str) -> dict[str, Any]:
        org_or_400()
        try:
            accounts.store.remove_team(team)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="No such team") from exc
        return accounts.store.org()

    @app.post("/api/org/users", status_code=201)
    async def add_user(request: Request) -> dict[str, Any]:
        org_or_400()
        data = await request.json()
        try:
            accounts.store.add_user(str(data.get("name") or ""), str(data.get("password") or ""), data.get("role") or "member", list(data.get("teams") or []))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return accounts.store.org()

    @app.put("/api/org/users/{name}")
    async def update_user(name: str, request: Request) -> dict[str, Any]:
        org_or_400()
        data = await request.json()
        try:
            accounts.store.update_user(name, role=data.get("role"), teams=data.get("teams"))
            if data.get("password"):
                accounts.store.set_password(name, str(data["password"]))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="No such user") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return accounts.store.org()

    @app.delete("/api/org/users/{name}")
    async def remove_user(name: str, request: Request) -> dict[str, Any]:
        org_or_400()
        identity = auth.request_identity(request)
        if identity and identity.user == name:
            raise HTTPException(status_code=400, detail="You can't remove your own account.")
        try:
            if not accounts.store.remove_user(name):
                raise HTTPException(status_code=404, detail="No such user")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return accounts.store.org()
