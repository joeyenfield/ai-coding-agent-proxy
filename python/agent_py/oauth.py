"""OIDC discovery, device authorization and rotating credentials, shared by both CLIs."""

from __future__ import annotations

import contextlib
import os
import sys
import threading
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


# How long before its expiry an access token is refreshed: long enough that a refresh on a slow or
# dropping network (it may take 20 s, and be tried again) still finishes before the server stops
# accepting the old token and closes the sockets it opened. The same as Go's RefreshBefore.
REFRESH_BEFORE = 120
# The margin an access token needs left to be used after a refresh failed (Go's StillValid).
STILL_VALID = 30
# How long after a refresh the issuer couldn't answer before trying again, using the token meanwhile
# (Go's RefreshRetry): retrying at once could present a consumed refresh token several times over, and
# the issuer revokes the whole sign-in on a second reuse.
REFRESH_RETRY = 15.0
_refresh_failed: dict = {}
_refresh_failed_lock = threading.Lock()


def _backing_off(server: str) -> bool:
    with _refresh_failed_lock:
        at = _refresh_failed.get(server)
        return at is not None and time.monotonic() - at < REFRESH_RETRY


def _note_refresh(server: str, failed: bool) -> None:
    with _refresh_failed_lock:
        if failed:
            _refresh_failed[server] = time.monotonic()
        else:
            _refresh_failed.pop(server, None)


def _valid_token(entry) -> str:
    """The stored access token if it has STILL_VALID seconds left, else ""."""
    if entry is not None and entry.access_token and entry.expires_at > time.time() + STILL_VALID:
        return entry.access_token
    return ""

# How old a credentials lock must be before it's taken for one a killed process left behind. Whoever
# holds it does one read, one refresh (at most 20 s) and one write, so a lock this old has no live
# owner; without this, every later sign-in and request would wait and fail until someone deleted it.
STALE_LOCK = 120


def _stale(lock: Path) -> bool:
    try:
        return time.time() - lock.stat().st_mtime > STALE_LOCK
    except OSError:
        return False


@contextlib.contextmanager
def credential_lock(path: Path):
    # The directory name and acquisition rules match Go. A timed-out lock isn't stolen (another
    # process might still be exchanging a rotating refresh credential), only one past STALE_LOCK.
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 30
    while True:
        try:
            lock.mkdir(mode=0o700)
            break
        except FileExistsError:
            if _stale(lock):
                # Left by a process that died holding it: take it over.
                with contextlib.suppress(OSError):
                    lock.rmdir()
                continue
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
    key = common.normalize(server)
    lock = credential_lock(upstream.credentials_path())
    try:
        lock.__enter__()
    except (OAuthError, OSError):
        # Another process holds the lock (a slow refresh): the stored token while it's still good. The
        # file is replaced whole, so reading it unlocked is safe.
        token = _valid_token(upstream.credentials().servers.get(key))
        if token:
            return token
        raise
    try:
        return _locked_access_token(upstream, key)
    finally:
        lock.__exit__(None, None, None)


def _locked_access_token(upstream: common.Upstream, key: str) -> str:
    credentials = upstream.credentials()
    entry = credentials.servers.get(key)
    if not entry or not entry.access_token or not entry.issuer or not entry.client_id:
        raise OAuthError("OAuth sign-in required: run login (legacy passwords and access keys are no longer supported)", "login_required")
    if entry.expires_at > time.time() + REFRESH_BEFORE:
        return entry.access_token
    if _valid_token(entry) and _backing_off(key):
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
            raise OAuthError(f"OAuth refresh revoked: run login again: {error}", "login_required") from error
        failure: Exception = error
    except (OSError, ValueError, common.httpx.HTTPError) as error:
        failure = error
    else:
        failure = None
    if failure is not None:
        # The issuer couldn't answer (down, restarting, slow): carry on with the token while it lasts.
        _note_refresh(key, True)
        if _valid_token(entry):
            return entry.access_token
        raise OAuthError(f"OAuth refresh failed: {failure}", getattr(failure, "code", "")) from failure
    _note_refresh(key, False)
    set_token(entry, token)
    # The old refresh token is spent; losing the new one would mean presenting the old again, which
    # the issuer allows only once. On Windows a reader holding the file can block the replace briefly.
    for attempt in range(10):
        try:
            credentials.save()
            break
        except OSError as error:
            if attempt == 9:
                print(f"warning: couldn't save the renewed OAuth credential: {error}", file=sys.stderr)
            else:
                time.sleep(0.2)
    return entry.access_token


def reject_access_token(upstream: common.Upstream, server: str, token: str = "") -> None:
    """Forces the next access_token() to refresh after a server invalidated a credential early. With
    token (the one a socket was opened with), only while it's still the stored one: several sockets
    opened with one token are closed together when it expires; the first refreshes, and the others
    mustn't throw that new token away and refresh again each. Go's RejectAccessTokenIf."""
    name = "AI_PROXY_TOKEN" if upstream.use_env else "AI_PROXY_MACHINE_TOKEN"
    if os.environ.get(name):
        return
    with credential_lock(upstream.credentials_path()):
        credentials = upstream.credentials()
        entry = credentials.servers.get(common.normalize(server))
        if entry and (not token or entry.access_token == token):
            entry.expires_at = 0
            credentials.save()


# A setup code from the console for a sign-in without a browser: what a container is given, since
# nobody is there to approve a device code.
SETUP_CODE_ENV = "AI_PROXY_MACHINE_SETUP_CODE"
AGENT_SETUP_CODE_ENV = "AI_PROXY_SETUP_CODE"

# The issuer refusing the setup code itself, which only a new one fixes.
INVALID_SETUP_CODE = "The setup code isn't valid: it may have expired or been used up. Create a new one on the console's Machines page."


def approve_with_setup_code(http, issuer: str, user_code: str, setup_code: str) -> None:
    """Approves the device authorization user_code as the setup code's owner, at the issuer's
    /device/setup. Its errors never contain the setup code."""
    endpoint = issuer.rstrip("/") + "/device/setup"
    issuer_endpoint(issuer, endpoint)
    # A transport error names the URL, never the form, so it can't carry the code.
    response = http.post(endpoint, data={"user_code": user_code, "setup_code": setup_code})
    try:
        answer = response.json()
    except ValueError:
        answer = {}
    if not isinstance(answer, dict):
        answer = {}
    setup_code_error(response.status_code, answer.get("approved") is True, str(answer.get("error") or ""), str(answer.get("error_description") or ""))


def setup_code_error(status: int, approved: bool, code: str, description: str) -> None:
    """Raises what an answer from /device/setup means for the person signing in (Go's setupCodeError)."""
    if status == 200 and approved:
        return
    if code == "invalid_setup_code":
        raise OAuthError(INVALID_SETUP_CODE, code)
    if code == "invalid_user_code":
        raise OAuthError("the issuer didn't accept this sign-in's device code (it may have expired); run login again", code)
    if status == 409 or code == "code_already_used":
        raise OAuthError("this sign-in's device code was already approved or used; run login again", code)
    if status == 429:
        raise OAuthError("too many setup code attempts from this address; wait a few minutes and try again", code)
    if code and description:
        raise OAuthError(f"setup code sign-in failed: {code} ({description})", code)
    if code:
        raise OAuthError(f"setup code sign-in failed: {code}", code)
    raise OAuthError(f"setup code sign-in failed (HTTP {status})")


def device_login(server: str, output, program: str = "agent", setup_code: str = "") -> common.ServerEntry:
    """Signs in with OAuth device authorization: approved by a person in a browser, or by setup_code
    when it isn't empty."""
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
        approved = False
        if setup_code:
            # The setup code stands in for the person at the browser; it's a bearer secret, so it's never printed.
            approve_with_setup_code(http, issuer, device["user_code"], setup_code)
            print("Approved with a setup code.", file=output, flush=True)
            approved = True
        else:
            print(f"Open {uri} and enter code {device['user_code']} to sign in.", file=output, flush=True)
        interval = int(device.get("interval") or 5)
        if interval <= 0:
            interval = 5
        deadline = time.monotonic() + int(device["expires_in"])
        while True:
            if time.monotonic() + interval > deadline:
                raise OAuthError("OAuth device authorization expired; run login again")
            # Once a setup code has approved it the first poll succeeds: no need to wait for it.
            if approved:
                approved = False
            else:
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
