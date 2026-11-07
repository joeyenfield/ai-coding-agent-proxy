"""OIDC browser sign-in, token-cookie renewal, several sign-ins per browser, and account-directory administration.

A browser can be signed in to several accounts at once and switch between them. Each account's
rotating refresh token is in one signed, HttpOnly cookie (ACCOUNTS, sent only to /api/auth), with
which of them is active. The active account's access token is the usual access cookie (auth.COOKIE),
so everything else sees exactly one identity per request: switching is a fresh access token for
another account, and the tenant, role and sharing checks follow whichever one it is.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any

import httpx
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, URLSafeSerializer
from joserfc.errors import JoseError
from starlette.middleware.sessions import SessionMiddleware

from .. import api_providers, auth
from ..db import kv
from ..runtime import Runtime


LOGGER = logging.getLogger("agent-proxy")

# The browser's sign-ins: {"active": user, "accounts": {user: refresh token}}.
ACCOUNTS = "mesh_accounts"
# Where a browser kept its one sign-in's refresh token before it could hold several; read once, then moved into ACCOUNTS.
LEGACY_REFRESH = "mesh_refresh"
# How many accounts one browser keeps signed in (each refresh token is a few dozen bytes of a 4 KB cookie).
MAX_ACCOUNTS = 5
REFRESH_LIFETIME = 30 * 86400


def register(app: FastAPI, runtime: Runtime) -> None:
    accounts = runtime.auth
    settings = runtime.settings
    key = kv.secret(runtime.db, "oidc-browser-key", create=True)
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

    signer = URLSafeSerializer(key, salt="mesh-accounts")
    secure = settings.oidc_issuer.startswith("https://")

    def checked(token: Any) -> tuple[str, int, str]:
        """The access token, its lifetime and the new refresh token from a token response, or 502."""
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
        return access, int(expires), token["refresh_token"]

    def signed_in(request: Request) -> dict[str, Any]:
        """This browser's sign-ins. A missing or tampered cookie is none; an older single sign-in is one with no name yet ("")."""
        found: dict[str, Any] = {"active": None, "accounts": {}}
        value = request.cookies.get(ACCOUNTS)
        if value:
            try:
                data = signer.loads(value)
            except BadSignature:
                data = None
            if isinstance(data, dict) and isinstance(data.get("accounts"), dict):
                held = {str(user): str(secret) for user, secret in data["accounts"].items() if isinstance(secret, str) and secret}
                active = data.get("active") if data.get("active") in held else None
                found = {"active": active, "accounts": held}
        legacy = request.cookies.get(LEGACY_REFRESH)
        if legacy and not found["accounts"]:
            found = {"active": "", "accounts": {"": legacy}}
        return found

    def store(response: Response, held: dict[str, Any]) -> None:
        response.delete_cookie(LEGACY_REFRESH, path="/api/auth")
        if held["accounts"]:
            response.set_cookie(ACCOUNTS, signer.dumps(held), max_age=REFRESH_LIFETIME, httponly=True, secure=secure, samesite="lax", path="/api/auth")
        else:
            response.delete_cookie(ACCOUNTS, path="/api/auth")
        response.headers["Cache-Control"] = "no-store"

    def activate(response: Response, held: dict[str, Any], user: str, token: Any) -> tuple[str, int]:
        """Make user the active account with this token response: its access cookie, its new refresh token.
        Returns the access token and its lifetime in seconds."""
        access, expires, refresh_token = checked(token)
        if user:
            held["accounts"].pop("", None)
        held["accounts"][user] = refresh_token
        held["active"] = user
        response.set_cookie(auth.COOKIE, access, max_age=expires, httponly=True, secure=secure, samesite="lax")
        store(response, held)
        return access, expires

    def listing(held: dict[str, Any]) -> dict[str, Any]:
        return {
            "active": held["active"] or None,
            "accounts": [{"user": user, "active": user == held["active"]} for user in sorted(held["accounts"]) if user],
            "max": MAX_ACCOUNTS,
        }

    def back_to_console(request: Request, reason: str) -> RedirectResponse:
        """A browser sign-in that can't go on: back to the console's sign-in page, which says why.

        reason is "failed", "unavailable" or "too_many"; the console shows its own wording for each.
        """
        target = request.session.pop("return_to", None)
        request.session.clear()
        if target != "/" and target not in settings.oidc_browser_origins:
            target = settings.oidc_browser_origins[0] if settings.oidc_browser_origins else "/"
        response = RedirectResponse(f"{target.rstrip('/')}/?sign_in={reason}", status_code=303)
        response.headers["Cache-Control"] = "no-store"
        return response

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

    async def revoke_secret(secret: str) -> None:
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
                result = await client.post(settings.oidc_issuer + "/revoke", data={"token": secret, "client_id": settings.oidc_console_client})
                result.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=503, detail="Sign-out could not revoke your credential; try again.") from exc

    async def renew(response: Response, held: dict[str, Any], user: str) -> tuple[str, int]:
        """A fresh access token for one of the browser's accounts, which becomes the active one.
        A sign-in the issuer no longer honours is dropped from the browser: 401."""
        try:
            token = await token_request({"grant_type": "refresh_token", "refresh_token": held["accounts"][user]})
            if not user:
                # An older single sign-in: learn whose it is from its new access token.
                user = await asyncio.to_thread(accounts.username_of, checked(token)[0]) or ""
                if not user:
                    raise HTTPException(status_code=401, detail="Your sign-in has expired. Sign in again.")
        except HTTPException as exc:
            if exc.status_code == 401:
                held["accounts"].pop(user, None)
                if held["active"] == user:
                    held["active"] = None
                failed = JSONResponse({"detail": exc.detail, **listing(held)}, status_code=401)
                store(failed, held)
                if held["active"] is None:
                    failed.delete_cookie(auth.COOKIE)
                raise _Answer(failed) from exc
            raise
        return activate(response, held, user, token)

    @app.get("/api/auth/status")
    async def status(request: Request) -> JSONResponse:
        try:
            identity = await asyncio.to_thread(accounts.identify, request)
        except auth.AuthUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        expires = accounts.expires_at(request) if identity else None
        token, via = auth.Auth.request_token(request)
        return JSONResponse({
            "required": True,
            "identity": identity.public() if identity else None,
            # Seconds left on the access token this request carried, for the console's renewal timer.
            "expires_in": max(0, int(expires - time.time())) if expires else None,
            # The page's own access token (from its cookie), for renewing its open sockets in place.
            "access_token": token if identity and via == "cookie" else None,
            "secure": request.url.scheme == "https",
            "issuer": settings.oidc_issuer, "audience": settings.oidc_audience, "client_id": settings.oidc_cli_client,
            # The browser sign-in's callbacks (the issuer knows them anyway). The Android app keeps sign-in in its
            # WebView on these hosts when the console's API isn't the address it was given (split hosting).
            "redirect_uris": settings.oidc_redirect_uris,
            # The console's address for everyone else (proxy.public_url), if one is set: agent-tray
            # opens it beside the loopback one.
            "public_url": settings.public_url,
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
        except (OAuthError, httpx.HTTPError, ValueError):
            return back_to_console(request, "unavailable")
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/api/auth/login")
    async def password_login_removed() -> None:
        raise HTTPException(status_code=410, detail="Password sign-in moved to the OIDC issuer. Start with GET /api/auth/login.")

    @app.get("/api/auth/callback", name="oidc_callback")
    async def callback(request: Request) -> Response:
        # A browser landed here, so failures go back to the console's sign-in page, not a JSON error.
        try:
            token = await oidc.authorize_access_token(request)
        except httpx.HTTPError:
            return back_to_console(request, "unavailable")
        except (OAuthError, JoseError, ValueError):
            # An expired or replayed callback, or one from another browser's sign-in.
            return back_to_console(request, "failed")
        user = str((token.get("userinfo") or {}).get("preferred_username") or "")
        if not user:
            return back_to_console(request, "failed")
        held = signed_in(request)
        named = {name for name in held["accounts"] if name}
        if user not in named and len(named) >= MAX_ACCOUNTS:
            return back_to_console(request, "too_many")
        replaced = held["accounts"].get(user)
        target = request.session.pop("return_to", "/")
        request.session.clear()
        response = RedirectResponse(target if target == "/" or target in settings.oidc_browser_origins else "/")
        try:
            activate(response, held, user, token)
        except HTTPException:
            return back_to_console(request, "failed")
        if replaced:
            # Signed in to the same account again: its older refresh token goes.
            try:
                await revoke_secret(replaced)
            except HTTPException:
                pass
        return response

    # What the issuer's refusals of a console device approval mean for the person in the console.
    DEVICE_REFUSALS = {
        "invalid_user_code": (404, "That code isn't right, or it has expired. Check it, or run the login command again for a new one."),
        "code_already_used": (409, "That code has already been approved or denied."),
        "invalid_token": (403, "Approving a device needs a console sign-in. Sign in to the console again."),
        "rate_limit_exceeded": (429, "Too many tries. Wait a few minutes, then try again."),
    }

    @app.post("/api/auth/device")
    async def approve_device(request: Request) -> dict[str, Any]:
        """Approve or deny an `agent login`/`agent-machine login` code from the console, already signed in."""
        data = await request.json()
        if not isinstance(data, dict) or data.get("action") not in {"check", "approve", "deny"}:
            raise HTTPException(status_code=400, detail="action is check, approve or deny")
        code = str(data.get("user_code") or "").strip()
        if not code or len(code) > 32:
            raise HTTPException(status_code=400, detail="Enter the code the login command shows")
        try:
            response = await asyncio.to_thread(accounts.device_decision, request, code, data["action"])
        except auth.AuthUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        try:
            answer = response.json()
        except ValueError:
            answer = {}
        if response.status_code == 200 and isinstance(answer, dict):
            if "client_id" in answer:
                # Which program asked, in words: the CLI client is agent and agent-machine.
                answer["client"] = "agent or agent-machine" if answer["client_id"] == settings.oidc_cli_client else answer["client_id"]
            return answer
        status, detail = DEVICE_REFUSALS.get(answer.get("error") if isinstance(answer, dict) else None,
                                             (503, "The authentication service couldn't decide that code. Try again."))
        raise HTTPException(status_code=status, detail=detail)

    SETUP_REFUSALS = {
        "invalid_token": (403, "Setup codes need a console sign-in. Sign in to the console again."),
        "too_many_setup_codes": (409, "You have 20 setup codes open already. Revoke one, or wait for one to expire."),
        "invalid_setup_code": (404, "No such setup code: it may have expired or been used up."),
        "invalid_request": (400, "A setup code lasts from 1 minute to 24 hours and allows 1 to 100 sign-ins."),
        "rate_limit_exceeded": (429, "Too many tries. Wait a few minutes, then try again."),
    }

    async def setup_call(request: Request, form: dict[str, str]) -> dict[str, Any]:
        try:
            response = await asyncio.to_thread(accounts.setup_codes, request, form)
        except auth.AuthUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        try:
            answer = response.json()
        except ValueError:
            answer = {}
        if response.status_code == 200 and isinstance(answer, dict):
            return answer
        status, detail = SETUP_REFUSALS.get(answer.get("error") if isinstance(answer, dict) else None,
                                            (503, "The authentication service couldn't do that. Try again."))
        raise HTTPException(status_code=status, detail=detail)

    @app.get("/api/auth/setup-codes")
    async def list_setup_codes(request: Request) -> dict[str, Any]:
        """Your open setup codes (never the codes themselves: id, label, created, expires, uses left)."""
        return await setup_call(request, {"action": "list"})

    @app.post("/api/auth/setup-codes", status_code=201)
    async def create_setup_code(request: Request) -> dict[str, Any]:
        """A setup code for adding machines as you: {"lifetime": seconds, "uses": n, "label": text}. The answer
        holds the code, once. agent-machine login --code (or AI_PROXY_MACHINE_SETUP_CODE) signs in with it."""
        data = await request.json()
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Send a JSON object")
        form = {"action": "create", "lifetime": str(data.get("lifetime") or ""), "uses": str(data.get("uses") or ""),
                "label": str(data.get("label") or "")[:80]}
        return await setup_call(request, form)

    @app.delete("/api/auth/setup-codes/{code_id}")
    async def revoke_setup_code(code_id: str, request: Request) -> dict[str, Any]:
        """Revoke one of your setup codes before it expires; machines it already signed in stay signed in."""
        return await setup_call(request, {"action": "revoke", "id": code_id[:32]})

    @app.post("/api/auth/refresh")
    async def refresh(request: Request) -> Response:
        """A fresh access token for the active account."""
        held = signed_in(request)
        if held["active"] is None:
            raise HTTPException(status_code=401, detail="Sign in first.")
        response = JSONResponse({})
        try:
            access, expires = await renew(response, held, held["active"])
        except _Answer as answer:
            return answer.response
        # The console renews again a little before expires_in runs out, so open streams and terminals never
        # meet an expired cookie.
        # The token itself too: the console sends it over its open sockets ({"type": "token"}) to renew them in place.
        response.body = response.render({"refreshed": True, "expires_in": expires, "access_token": access})
        response.headers["content-length"] = str(len(response.body))
        return response

    @app.get("/api/auth/accounts")
    async def signed_in_accounts(request: Request) -> JSONResponse:
        """The accounts this browser is signed in to, and which is active. Names only: their tokens never leave the cookie."""
        return JSONResponse(listing(signed_in(request)), headers={"Cache-Control": "no-store"})

    @app.post("/api/auth/switch")
    async def switch(request: Request) -> Response:
        """Make another of this browser's accounts the active one: {"user": name}. No password: it's already signed in."""
        data = await request.json()
        user = str((data or {}).get("user") or "") if isinstance(data, dict) else ""
        held = signed_in(request)
        if not user or user not in held["accounts"]:
            raise HTTPException(status_code=404, detail="This browser isn't signed in to that account")
        response = JSONResponse({})
        try:
            _, expires = await renew(response, held, user)
        except _Answer as answer:
            return answer.response
        response.body = response.render({**listing(held), "expires_in": expires})
        response.headers["content-length"] = str(len(response.body))
        return response

    @app.post("/api/auth/logout")
    async def logout(request: Request) -> JSONResponse:
        """Sign out of one account ({"user": name}; the active one without), or of every one ({"all": true}).

        Each one signed out is revoked at the issuer. When the active one goes, the next one left becomes active.
        """
        try:
            data = await request.json()
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        held = signed_in(request)
        everyone = bool(data.get("all"))
        target = str(data.get("user") or "") if data.get("user") else held["active"]
        if not everyone and data.get("user") and target not in held["accounts"]:
            raise HTTPException(status_code=404, detail="This browser isn't signed in to that account")
        leaving = list(held["accounts"]) if everyone else ([target] if target is not None and target in held["accounts"] else [])
        secrets = [held["accounts"][user] for user in leaving]
        if not secrets and request.cookies.get(auth.COOKIE):
            secrets = [request.cookies[auth.COOKIE]]
        for secret in secrets:
            await revoke_secret(secret)
        for user in leaving:
            held["accounts"].pop(user, None)
        request.session.clear()
        response = JSONResponse({})
        if held["active"] in leaving or not held["accounts"]:
            held["active"] = None
            response.delete_cookie(auth.COOKIE)
            for user in sorted(held["accounts"]):
                try:
                    await renew(response, held, user)
                    break
                except _Answer:
                    continue
                except HTTPException:
                    break
        store(response, held)
        response.body = response.render({"identity": None, **listing(held)})
        response.headers["content-length"] = str(len(response.body))
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

    # Organizations (tenants) and users. Everyone signed in sees their own organization and the people in it.
    # Its admins change its teams and users; site admins (the root organization's) also make and remove
    # organizations and set up the users of any (the middleware checks SITE_ADMIN_ONLY and ADMIN_ONLY;
    # these check which organization). Another organization looks missing. Changes answer with the whole directory.

    def directory(request: Request) -> dict[str, Any]:
        viewer = auth.request_identity(request)
        return {"orgs": accounts.store.orgs(viewer), "users": accounts.store.user_list(viewer)}

    def change(request: Request, action: Any, missing: str) -> dict[str, Any]:
        try:
            action()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=missing) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return directory(request)

    def administered(request: Request, org: str) -> None:
        """An organization this user manages; any other answers as missing."""
        if accounts.store.org(org) is None or not auth.administers(auth.request_identity(request), org):
            raise HTTPException(status_code=404, detail="No such organization")

    def user_administered(request: Request, name: str) -> dict[str, Any]:
        record = accounts.store.user(name)
        if record is None or not auth.administers(auth.request_identity(request), record["tenant"]):
            raise HTTPException(status_code=404, detail="No such user")
        return record

    @app.get("/api/orgs")
    async def orgs(request: Request) -> dict[str, Any]:
        return {"orgs": accounts.store.orgs(auth.request_identity(request))}

    @app.post("/api/orgs", status_code=201)
    async def add_org(request: Request) -> dict[str, Any]:
        data = await request.json()
        return change(request, lambda: accounts.store.add_org(str(data.get("id") or ""), data.get("name")), "")

    @app.put("/api/orgs/{org}")
    async def rename_org(org: str, request: Request) -> dict[str, Any]:
        administered(request, org)
        data = await request.json()
        return change(request, lambda: accounts.store.rename_org(org, str(data.get("name") or "")), "No such organization")

    @app.delete("/api/orgs/{org}")
    async def remove_org(org: str, request: Request) -> dict[str, Any]:
        # Nothing of the organization's may pass to a later one of the same name: its API
        # providers (and keys), machines, usage limits and retention policy go with it, and
        # its sessions become site-admin-only. Legal holds must be resolved first.
        if runtime.retention.holds(org):
            raise HTTPException(status_code=400, detail=f"{org} has legal holds; release them before removing the organization")
        owned = [backend.name for backend in settings.backends.values() if api_providers.external(backend) and api_providers.owner(backend) == org]
        if org != auth.ROOT_TENANT and settings.default_backend in owned:
            raise HTTPException(status_code=400, detail=f"{settings.default_backend}, {org}'s API provider, is the default backend; choose another default first")
        identity = auth.request_identity(request)
        answer = change(request, lambda: accounts.store.remove_org(org), "No such organization")
        async with runtime.config_lock:
            for name in owned:
                settings.remove_backend(name)
                runtime.api_keys.clear(name)
                runtime.api_models.pop(name, None)
        await offboard(identity, org, lambda owner, of: of == org,
                       lambda tags: {"tenant": None} if tags.get("tenant") == org else {})

        def clean() -> None:
            for limit in runtime.usage.tenant_limits(org):
                try:
                    runtime.usage.remove_limit(org, limit.user, limit.provider, limit.window)
                except Exception:  # noqa: BLE001
                    LOGGER.exception("removing %s: couldn't remove a usage limit", org)
            runtime.retention.remove_org(org)

        try:
            await asyncio.to_thread(clean)
        except Exception:  # noqa: BLE001
            LOGGER.exception("removing %s: couldn't remove its limits or retention policy", org)
        return answer

    @app.post("/api/orgs/{org}/teams", status_code=201)
    async def add_team(org: str, request: Request) -> dict[str, Any]:
        administered(request, org)
        data = await request.json()
        return change(request, lambda: accounts.store.add_team(org, str(data.get("id") or ""), data.get("name")), "No such organization")

    @app.put("/api/orgs/{org}/members/{name}")
    @app.delete("/api/orgs/{org}/members/{name}")
    async def org_member(org: str, name: str, request: Request) -> dict[str, Any]:
        """Users can't join or leave an organization: they belong to the one they're made in."""
        administered(request, org)
        user_administered(request, name)
        return change(request, lambda: accounts.store.set_membership(org, name, request.method == "PUT"), "No such organization or user")

    @app.put("/api/orgs/{org}/teams/{team}")
    async def rename_team(org: str, team: str, request: Request) -> dict[str, Any]:
        administered(request, org)
        data = await request.json()
        return change(request, lambda: accounts.store.rename_team(org, team, str(data.get("name") or "")), "No such team")

    @app.put("/api/orgs/{org}/teams/{team}/members/{name}")
    @app.delete("/api/orgs/{org}/teams/{team}/members/{name}")
    async def team_member(org: str, team: str, name: str, request: Request) -> dict[str, Any]:
        administered(request, org)
        user_administered(request, name)
        return change(request, lambda: accounts.store.set_membership(org, name, request.method == "PUT", team=team), "No such organization, team or user")

    @app.delete("/api/orgs/{org}/teams/{team}")
    async def remove_team(org: str, team: str, request: Request) -> dict[str, Any]:
        administered(request, org)
        return change(request, lambda: accounts.store.remove_team(org, team), "No such team")

    @app.get("/api/users")
    async def users(request: Request) -> dict[str, Any]:
        return {"users": accounts.store.user_list(auth.request_identity(request))}

    @app.post("/api/users", status_code=201)
    async def add_user(request: Request) -> dict[str, Any]:
        """A user in one organization: an organization admin's own; a site admin names any (the root takes only admins)."""
        identity = auth.request_identity(request)
        data = await request.json()
        named = sorted({str(item) for item in data.get("orgs") or []} | ({str(data["tenant"])} if data.get("tenant") else set()))
        if len(named) > 1:
            raise HTTPException(status_code=400, detail="A user belongs to one organization; name only one")
        tenant = named[0] if named else (None if identity is None or identity.site_admin else identity.tenant)
        if tenant is not None:
            administered(request, tenant)
        return change(request, lambda: accounts.store.add_user(
            str(data.get("name") or ""), str(data.get("password") or ""), data.get("role") or "member",
            list(data.get("teams") or []), tenant=tenant,
        ), "")

    @app.put("/api/users/{name}")
    async def update_user(name: str, request: Request) -> dict[str, Any]:
        user_administered(request, name)
        data = await request.json()

        def update() -> None:
            accounts.store.update_user(
                name, role=data.get("role"), teams=data.get("teams"), orgs=data.get("orgs"),
                password=str(data["password"]) if data.get("password") else None,
            )

        return change(request, update, "No such user")

    async def offboard(identity: Any, what: str, mine: Any, session_change: Any) -> None:
        """What a removed user's or organization's name must not pass to a later one of the
        same name: their machines leave the fleet (allowed back explicitly), their provider
        accounts and stored credentials are deleted, and their sessions are retagged, kept
        under retention. The removal already happened, so nothing here may stop the rest:
        every step is guarded and failures are logged for an admin to finish by hand."""
        for machine_id, machine in list(runtime.fleet.machines.items()):
            if mine(machine.owner, machine.tenant):
                try:
                    await runtime.fleet.remove(machine_id, identity)
                except Exception:  # noqa: BLE001 - logged; the rest still offboards
                    LOGGER.exception("offboarding %s: couldn't remove machine %s", what, machine_id)

        def disown() -> None:
            for account in list(runtime.provider_accounts.accounts.values()):
                if mine(account.owner, account.tenant):
                    try:
                        runtime.provider_accounts.delete(identity, account)
                    except Exception:  # noqa: BLE001
                        LOGGER.exception("offboarding %s: couldn't delete provider account %s", what, account.id)
            for session in runtime.sessions.list():
                cleared = session_change(session.tags)
                if cleared:
                    try:
                        runtime.sessions.update_tags(session, cleared)
                    except Exception:  # noqa: BLE001
                        LOGGER.exception("offboarding %s: couldn't retag session %s", what, session.session_id)

        try:
            await asyncio.to_thread(disown)
        except Exception:  # noqa: BLE001 - even the listing failing mustn't 500 a removal that happened
            LOGGER.exception("offboarding %s: couldn't walk the provider accounts or sessions", what)

    @app.delete("/api/users/{name}")
    async def remove_user(name: str, request: Request) -> dict[str, Any]:
        identity = auth.request_identity(request)
        if identity and identity.user == name:
            raise HTTPException(status_code=400, detail="You can't remove your own account.")
        record = user_administered(request, name)

        def remove() -> None:
            if not accounts.store.remove_user(name):
                raise KeyError(name)

        answer = change(request, remove, "No such user")
        tenant = record["tenant"]

        def disowned(tags: dict[str, Any]) -> dict[str, Any]:
            if tags.get("tenant") != tenant:
                return {}
            return {key: None for key in ("owner", "launched_by") if tags.get(key) == name}

        await offboard(identity, name, lambda owner, of: owner == name and of == tenant, disowned)

        try:
            # Their own limits are keyed by name: a recreated user mustn't start blocked (or raised).
            await asyncio.to_thread(runtime.usage.remove_user, tenant, name)
        except Exception:  # noqa: BLE001
            LOGGER.exception("offboarding %s: couldn't remove its usage limits", name)
        return answer


class _Answer(Exception):
    """A response to send as it is, from inside a helper (a sign-in the issuer no longer honours)."""

    def __init__(self, response: Response):
        super().__init__("answer")
        self.response = response
