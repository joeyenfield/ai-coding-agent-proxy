"""OIDC browser sign-in, token-cookie renewal and account-directory administration."""

from __future__ import annotations

import asyncio
import math
from typing import Any

import httpx
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from joserfc.errors import JoseError
from starlette.middleware.sessions import SessionMiddleware

from .. import auth
from ..remote import read_token
from ..runtime import Runtime


def register(app: FastAPI, runtime: Runtime) -> None:
    accounts = runtime.auth
    settings = runtime.settings
    key = read_token(settings.users_path.parent / "oidc-browser-key", create=True)
    app.add_middleware(
        SessionMiddleware, secret_key=key, session_cookie="mesh_oidc_state",
        max_age=600, same_site="lax", https_only=settings.oidc_issuer.startswith("https://"),
    )
    oauth = OAuth()
    oidc = oauth.register(
        "mesh", client_id=settings.oidc_console_client,
        server_metadata_url=settings.oidc_issuer + "/.well-known/openid-configuration",
        client_kwargs={"scope": "openid profile offline_access mesh-api", "code_challenge_method": "S256", "token_endpoint_auth_method": "none", "trust_env": False},
    )

    def set_tokens(response: Response, token: dict[str, Any]) -> None:
        if not isinstance(token, dict):
            raise HTTPException(status_code=502, detail="The authentication service returned an invalid token response")
        access = token.get("access_token")
        expires = token.get("expires_in")
        if not isinstance(access, str) or not access or str(token.get("token_type", "")).lower() != "bearer":
            raise HTTPException(status_code=502, detail="The authentication service did not issue an access token")
        if not isinstance(expires, (int, float)) or not math.isfinite(expires) or expires <= 0:
            raise HTTPException(status_code=502, detail="The authentication service returned an invalid access-token lifetime")
        if not isinstance(token.get("refresh_token"), str) or not token["refresh_token"]:
            raise HTTPException(status_code=502, detail="The authentication service did not issue a rotating refresh token")
        secure = settings.oidc_issuer.startswith("https://")
        response.set_cookie(auth.COOKIE, access, max_age=int(expires), httponly=True, secure=secure, samesite="lax")
        if token.get("refresh_token"):
            response.set_cookie("mesh_refresh", token["refresh_token"], max_age=30 * 86400, httponly=True, secure=secure, samesite="lax", path="/api/auth")
        response.headers["Cache-Control"] = "no-store"

    def clear_tokens(response: Response) -> None:
        response.delete_cookie(auth.COOKIE)
        response.delete_cookie("mesh_refresh", path="/api/auth")
        response.headers["Cache-Control"] = "no-store"

    async def token_request(data: dict[str, str]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
                response = await client.post(settings.oidc_issuer + "/token", data={"client_id": settings.oidc_console_client, **data})
            if response.status_code == 400:
                raise HTTPException(status_code=401, detail="Your sign-in has expired. Sign in again.")
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=503, detail="The authentication service is unavailable") from exc

    @app.get("/api/auth/status")
    async def status(request: Request) -> JSONResponse:
        try:
            identity = await asyncio.to_thread(accounts.identify, request)
        except auth.AuthUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return JSONResponse({
            "required": True,
            "identity": identity.public() if identity else None,
            "secure": request.url.scheme == "https",
            "issuer": settings.oidc_issuer, "audience": settings.oidc_audience, "client_id": settings.oidc_cli_client,
        }, headers={"Cache-Control": "no-store"})

    @app.get("/api/auth/login")
    async def login(request: Request) -> Response:
        target = request.query_params.get("return_to") or (settings.oidc_browser_origins[0] if settings.oidc_browser_origins else "/")
        if target != "/" and target not in settings.oidc_browser_origins:
            raise HTTPException(status_code=400, detail="The console return address is not allowed")
        callback = str(request.url_for("oidc_callback"))
        if callback not in settings.oidc_redirect_uris:
            raise HTTPException(status_code=400, detail="This API's OIDC callback is not registered")
        request.session["return_to"] = target
        try:
            response = await oidc.authorize_redirect(request, callback)
        except (OAuthError, httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=503, detail="The authentication service is unavailable") from exc
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/auth/login")
    async def password_login_removed() -> None:
        raise HTTPException(status_code=410, detail="Password sign-in moved to the OIDC issuer. Start with GET /api/auth/login.")

    @app.get("/api/auth/callback", name="oidc_callback")
    async def callback(request: Request) -> Response:
        try:
            token = await oidc.authorize_access_token(request)
        except (OAuthError, JoseError, httpx.HTTPError, ValueError) as exc:
            request.session.clear()
            raise HTTPException(status_code=400, detail="Sign-in could not be verified. Start sign-in again.") from exc
        if not token.get("userinfo"):
            raise HTTPException(status_code=400, detail="The authentication service did not return a verified OIDC identity")
        target = request.session.pop("return_to", "/")
        request.session.clear()
        response = RedirectResponse(target if target == "/" or target in settings.oidc_browser_origins else "/")
        set_tokens(response, token)
        return response

    @app.post("/api/auth/refresh")
    async def refresh(request: Request) -> Response:
        refresh_token = request.cookies.get("mesh_refresh")
        if not refresh_token:
            raise HTTPException(status_code=401, detail="Sign in first.")
        token = await token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})
        response = JSONResponse({"refreshed": True})
        set_tokens(response, token)
        return response

    @app.post("/api/auth/logout")
    async def logout(request: Request) -> JSONResponse:
        secret = request.cookies.get("mesh_refresh") or request.cookies.get(auth.COOKIE)
        if secret:
            try:
                async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
                    result = await client.post(settings.oidc_issuer + "/revoke", data={"token": secret, "client_id": settings.oidc_console_client})
                    result.raise_for_status()
            except httpx.HTTPError as exc:
                raise HTTPException(status_code=503, detail="Sign-out could not revoke your credential; try again.") from exc
        request.session.clear()
        response = JSONResponse({"identity": None})
        clear_tokens(response)
        return response

    @app.post("/api/auth/token")
    async def token_for_launcher(request: Request) -> dict[str, Any]:
        raise HTTPException(status_code=410, detail="Password grants were removed. Use OIDC device authorization at the issuer.")

    @app.get("/api/auth/tokens")
    async def tokens(request: Request) -> dict[str, Any]:
        raise HTTPException(status_code=410, detail="Launcher tokens were replaced by OAuth access tokens.")

    @app.post("/api/auth/tokens", status_code=201)
    async def create_token(request: Request) -> dict[str, Any]:
        raise HTTPException(status_code=410, detail="Use device authorization or a provisioned service account at the OIDC issuer.")

    @app.delete("/api/auth/tokens/{token_id}")
    async def revoke(token_id: str, request: Request) -> dict[str, Any]:
        raise HTTPException(status_code=410, detail="Revoke OAuth credentials at the issuer's revocation endpoint.")

    @app.post("/api/auth/password")
    async def change_password(request: Request) -> dict[str, Any]:
        raise HTTPException(status_code=410, detail="Password sign-in moved to the issuer. Ask an administrator to reset your account password.")

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
