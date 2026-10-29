"""Signing in to the proxy: the console's sign-in, tokens for launchers on other machines, and the organizations and users."""

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

    # Organizations and users: everyone signed in sees their own organizations and the people in them,
    # admins see and change everything (the middleware checks ADMIN_ONLY). Changes answer with the whole directory.

    def accounts_or_400() -> None:
        if not accounts.enabled:
            raise HTTPException(status_code=400, detail="This proxy has no accounts, so no organizations yet.")

    def directory(request: Request) -> dict[str, Any]:
        viewer = auth.request_identity(request)
        return {"orgs": accounts.store.orgs(viewer), "users": accounts.store.user_list(viewer)}

    def change(request: Request, action: Any, missing: str) -> dict[str, Any]:
        accounts_or_400()
        try:
            action()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=missing) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return directory(request)

    @app.get("/api/orgs")
    async def orgs(request: Request) -> dict[str, Any]:
        accounts_or_400()
        return {"orgs": accounts.store.orgs(auth.request_identity(request))}

    @app.post("/api/orgs", status_code=201)
    async def add_org(request: Request) -> dict[str, Any]:
        data = await request.json()
        return change(request, lambda: accounts.store.add_org(str(data.get("id") or ""), data.get("name")), "")

    @app.put("/api/orgs/{org}")
    async def rename_org(org: str, request: Request) -> dict[str, Any]:
        data = await request.json()
        return change(request, lambda: accounts.store.rename_org(org, str(data.get("name") or "")), "No such organization")

    @app.delete("/api/orgs/{org}")
    async def remove_org(org: str, request: Request) -> dict[str, Any]:
        return change(request, lambda: accounts.store.remove_org(org), "No such organization")

    @app.post("/api/orgs/{org}/teams", status_code=201)
    async def add_team(org: str, request: Request) -> dict[str, Any]:
        data = await request.json()
        return change(request, lambda: accounts.store.add_team(org, str(data.get("id") or ""), data.get("name")), "No such organization")

    @app.put("/api/orgs/{org}/members/{name}")
    @app.delete("/api/orgs/{org}/members/{name}")
    async def org_member(org: str, name: str, request: Request) -> dict[str, Any]:
        return change(request, lambda: accounts.store.set_membership(org, name, request.method == "PUT"), "No such organization or user")

    @app.put("/api/orgs/{org}/teams/{team}")
    async def rename_team(org: str, team: str, request: Request) -> dict[str, Any]:
        data = await request.json()
        return change(request, lambda: accounts.store.rename_team(org, team, str(data.get("name") or "")), "No such team")

    @app.put("/api/orgs/{org}/teams/{team}/members/{name}")
    @app.delete("/api/orgs/{org}/teams/{team}/members/{name}")
    async def team_member(org: str, team: str, name: str, request: Request) -> dict[str, Any]:
        return change(request, lambda: accounts.store.set_membership(org, name, request.method == "PUT", team=team), "No such organization, team or user")

    @app.delete("/api/orgs/{org}/teams/{team}")
    async def remove_team(org: str, team: str, request: Request) -> dict[str, Any]:
        return change(request, lambda: accounts.store.remove_team(org, team), "No such team")

    @app.get("/api/users")
    async def users(request: Request) -> dict[str, Any]:
        accounts_or_400()
        return {"users": accounts.store.user_list(auth.request_identity(request))}

    @app.post("/api/users", status_code=201)
    async def add_user(request: Request) -> dict[str, Any]:
        data = await request.json()
        return change(request, lambda: accounts.store.add_user(
            str(data.get("name") or ""), str(data.get("password") or ""), data.get("role") or "member",
            list(data.get("teams") or []), list(data.get("orgs") or []),
        ), "")

    @app.put("/api/users/{name}")
    async def update_user(name: str, request: Request) -> dict[str, Any]:
        data = await request.json()

        def update() -> None:
            accounts.store.update_user(
                name, role=data.get("role"), teams=data.get("teams"), orgs=data.get("orgs"),
                password=str(data["password"]) if data.get("password") else None,
            )

        return change(request, update, "No such user")

    @app.delete("/api/users/{name}")
    async def remove_user(name: str, request: Request) -> dict[str, Any]:
        identity = auth.request_identity(request)
        if identity and identity.user == name:
            raise HTTPException(status_code=400, detail="You can't remove your own account.")

        def remove() -> None:
            if not accounts.store.remove_user(name):
                raise KeyError(name)

        return change(request, remove, "No such user")
