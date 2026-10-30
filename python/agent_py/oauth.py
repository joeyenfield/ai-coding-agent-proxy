"""OIDC discovery, device authorization and rotating credentials, shared by both CLIs."""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from . import common


class OAuthError(RuntimeError):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


def secure_url(raw: str) -> None:
    parsed = urlparse(raw)
    if not parsed.netloc or parsed.username is not None or parsed.password is not None or parsed.fragment or parsed.query or (
        parsed.scheme != "https" and not (parsed.scheme == "http" and common.is_loopback_url(raw))
    ):
        raise OAuthError("OAuth requires HTTPS (HTTP is allowed only on loopback)")


def issuer_endpoint(issuer: str, endpoint: str) -> None:
    secure_url(issuer)
    secure_url(endpoint)
    a, b = urlparse(issuer), urlparse(endpoint)
    if (a.scheme.lower(), a.netloc.lower()) != (b.scheme.lower(), b.netloc.lower()):
        raise OAuthError("OAuth endpoint is outside the issuer origin")


def discovery(http, issuer: str) -> dict:
    secure_url(issuer)
    response = http.get(issuer.rstrip("/") + "/.well-known/openid-configuration")
    if response.status_code != 200:
        raise OAuthError(f"OIDC discovery returned HTTP {response.status_code}")
    document = response.json()
    if document.get("issuer") != issuer:
        raise OAuthError("OIDC discovery issuer mismatch")
    for key in ("device_authorization_endpoint", "token_endpoint", "userinfo_endpoint", "revocation_endpoint"):
        issuer_endpoint(issuer, document.get(key, ""))
    return document


def token_request(http, endpoint: str, form: dict) -> dict:
    response = http.post(endpoint, data=form)
    try:
        value = response.json()
    except ValueError as error:
        raise OAuthError(f"invalid OAuth response (HTTP {response.status_code})") from error
    if value.get("error"):
        raise OAuthError(f"OAuth: {value['error']}", str(value["error"]))
    if response.status_code != 200 or not value.get("access_token") or str(value.get("token_type", "")).lower() != "bearer" or int(value.get("expires_in", 0)) <= 0:
        raise OAuthError(f"invalid OAuth token response (HTTP {response.status_code})")
    return value


def set_token(entry: common.ServerEntry, token: dict) -> None:
    entry.access_token = token["access_token"]
    entry.refresh_token = token.get("refresh_token") or entry.refresh_token
    entry.expires_at = int(time.time()) + int(token["expires_in"])
    entry.token = entry.token_id = ""


@contextlib.contextmanager
def credential_lock(path: Path):
    # The directory name and acquisition rules match Go. Never steal a lock while
    # another process might still be exchanging a rotating refresh credential.
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 30
    while True:
        try:
            lock.mkdir(mode=0o700)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise OAuthError("credentials are locked; another sign-in or refresh is in progress (remove a stale credentials.json.lock only after stopping clients)")
            time.sleep(0.05)
        except PermissionError:
            # On Windows, creating the directory while another process is removing it is refused
            # as access denied, not as already existing: that's the lock being released, so wait.
            if os.name != "nt" or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)
    try:
        yield
    finally:
        lock.rmdir()


def access_token(upstream: common.Upstream, server: str) -> str:
    secure_url(server)
    name = "AI_PROXY_TOKEN" if upstream.use_env else "AI_PROXY_MACHINE_TOKEN"
    if os.environ.get(name):
        return os.environ[name]
    with credential_lock(upstream.credentials_path()):
        credentials = upstream.credentials()
        entry = credentials.servers.get(common.normalize(server))
        if not entry or not entry.access_token or not entry.issuer or not entry.client_id:
            raise OAuthError("OAuth sign-in required: run login (legacy passwords and access keys are no longer supported)", "login_required")
        if entry.expires_at > time.time() + 30:
            return entry.access_token
        if not entry.refresh_token:
            raise OAuthError("OAuth access token expired: run login again", "login_required")
        issuer_endpoint(entry.issuer, entry.token_endpoint)
        try:
            with common.client(20) as http:
                token = token_request(http, entry.token_endpoint, {
                    "grant_type": "refresh_token", "refresh_token": entry.refresh_token, "client_id": entry.client_id,
                })
        except OAuthError as error:
            if error.code == "invalid_grant":
                entry.access_token = entry.refresh_token = ""
                entry.expires_at = 0
                credentials.save()
            raise OAuthError(f"OAuth refresh failed; run login if revoked: {error}", "login_required" if error.code == "invalid_grant" else error.code) from error
        set_token(entry, token)
        credentials.save()
        return entry.access_token


def reject_access_token(upstream: common.Upstream, server: str) -> None:
    name = "AI_PROXY_TOKEN" if upstream.use_env else "AI_PROXY_MACHINE_TOKEN"
    if os.environ.get(name):
        return
    with credential_lock(upstream.credentials_path()):
        credentials = upstream.credentials()
        entry = credentials.servers.get(common.normalize(server))
        if entry:
            entry.expires_at = 0
            credentials.save()


def device_login(server: str, output, program: str = "agent") -> common.ServerEntry:
    secure_url(server)
    with common.client(20) as http:
        response = http.get(server + "/api/auth/status", headers=common.identify({}, program))
        if response.status_code != 200:
            raise OAuthError(f"auth status returned HTTP {response.status_code}")
        status = response.json()
        if not status.get("required") or not status.get("issuer") or not status.get("client_id"):
            raise OAuthError("server must require OIDC; legacy local/password/key sign-in is unsupported")
        issuer, client_id = status["issuer"], status["client_id"]
        document = discovery(http, issuer)
        response = http.post(document["device_authorization_endpoint"], data={
            "client_id": client_id, "scope": "openid profile offline_access mesh-api",
        })
        if response.status_code != 200:
            raise OAuthError(f"device authorization returned HTTP {response.status_code}")
        device = response.json()
        if not device.get("device_code") or not device.get("user_code") or int(device.get("expires_in", 0)) <= 0:
            raise OAuthError("invalid OAuth device authorization response")
        uri = device.get("verification_uri", "")
        parsed = urlparse(uri)
        issuer_endpoint(issuer, urlunparse(parsed._replace(query="")))
        print(f"Open {uri} and enter code {device['user_code']} to sign in.", file=output, flush=True)
        interval = int(device.get("interval") or 5)
        if interval <= 0:
            interval = 5
        deadline = time.monotonic() + int(device["expires_in"])
        while True:
            if time.monotonic() + interval > deadline:
                raise OAuthError("OAuth device authorization expired; run login again")
            time.sleep(interval)
            try:
                token = token_request(http, document["token_endpoint"], {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device["device_code"], "client_id": client_id,
                })
            except OAuthError as error:
                if error.code == "authorization_pending":
                    continue
                if error.code == "slow_down":
                    interval += 5
                    continue
                raise
            entry = common.ServerEntry(server, issuer=issuer, client_id=client_id,
                                       token_endpoint=document["token_endpoint"], revocation_endpoint=document["revocation_endpoint"])
            set_token(entry, token)
            response = http.get(document["userinfo_endpoint"], headers={"Authorization": f"Bearer {token['access_token']}"})
            if response.status_code != 200:
                raise OAuthError("OAuth userinfo failed")
            user = response.json()
            if not user.get("sub"):
                raise OAuthError("OAuth userinfo failed")
            entry.user = user.get("preferred_username") or user.get("username") or user["sub"]
            return entry


def revoke(entry: common.ServerEntry) -> None:
    if not entry.refresh_token:
        return
    issuer_endpoint(entry.issuer, entry.revocation_endpoint)
    with common.client(20) as http:
        response = http.post(entry.revocation_endpoint, data={
            "token": entry.refresh_token, "token_type_hint": "refresh_token", "client_id": entry.client_id,
        })
    if response.status_code != 200:
        raise OAuthError(f"OAuth revocation returned HTTP {response.status_code}")
