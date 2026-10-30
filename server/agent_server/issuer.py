"""Standalone Authlib OIDC issuer; accounts are read-only, credentials live in SQLite."""
from __future__ import annotations

import argparse
import base64
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit

from authlib.oauth2.rfc6749 import AuthorizationServer, OAuth2Request
from authlib.oauth2.rfc6749.errors import InvalidGrantError, InvalidScopeError, OAuth2Error
from authlib.oauth2.rfc6749.grants import AuthorizationCodeGrant, ClientCredentialsGrant, RefreshTokenGrant
from authlib.oauth2.rfc6749.requests import OAuth2Payload
from authlib.oauth2.rfc7636 import CodeChallenge
from authlib.oauth2.rfc8628 import DeviceCodeGrant
from authlib.oauth2.rfc8628.errors import ExpiredTokenError, SlowDownError
from authlib.oidc.core import UserInfo
from authlib.oidc.core.grants import OpenIDCode
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from joserfc.jwk import RSAKey

from . import issuer_pages as pages
from .auth import Auth, UserStore
from .config import Settings

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
SCOPES = "openid profile offline_access mesh-api"
ACCESS_TTL = 300
FAMILY_TTL = 30 * 86400
CODE_TTL = 120
DEVICE_TTL = 600
COOKIE = "__Host-mesh_auth_transaction"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class IssuerError(Exception):
    def __init__(self, error: str, status: int = 400):
        self.error, self.status = error, status


class State:
    """A transaction spans grant validation, consumption and token persistence."""

    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        path = directory / "issuer.sqlite3"
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("CREATE TABLE IF NOT EXISTS records(kind TEXT, key TEXT, data TEXT NOT NULL, PRIMARY KEY(kind,key))")
        self.lock = threading.RLock()
        self.next_cleanup = 0.0
        key_path = directory / "signing-key.pem"
        # Serialize first-time key creation across issuer processes as well.
        with self.transaction():
            if not key_path.exists():
                key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
                pending = directory / "signing-key.pending"
                fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "wb") as file:
                    file.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
                    file.flush()
                    os.fsync(file.fileno())
                pending.replace(key_path)
            os.chmod(key_path, 0o600)
            self.signing_key = RSAKey.import_key(key_path.read_bytes())
        self.kid = self.signing_key.thumbprint()
        self.signing_key.ensure_kid()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                now = time.time()
                if now >= self.next_cleanup:
                    self.db.execute("DELETE FROM records WHERE kind != 'client' AND json_extract(data, '$.exp') <= ?", (now - DEVICE_TTL,))
                    self.next_cleanup = now + 300
                yield
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            else:
                self.db.execute("COMMIT")

    def get(self, kind, key):
        row = self.db.execute("SELECT data FROM records WHERE kind=? AND key=?", (kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, kind, key, data):
        self.db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?)", (kind, key, json.dumps(data)))

    def delete(self, kind, key):
        self.db.execute("DELETE FROM records WHERE kind=? AND key=?", (kind, key))


class Payload(OAuth2Payload):
    def __init__(self, data):
        self._data = data

    @property
    def data(self):
        return self._data

    @property
    def datalist(self):
        return defaultdict(list, {key: [value] for key, value in self._data.items()})


class ProtocolRequest(OAuth2Request):
    def __init__(self, issuer, method, path, data, headers=None):
        # Authlib permits HTTP only when AUTHLIB_INSECURE_TRANSPORT is globally
        # enabled. Use its HTTPS validation internally; external loopback HTTP
        # is independently restricted by the issuer configuration.
        super().__init__(method, "https://issuer.invalid" + path, headers=headers or {})
        self.payload = Payload(data)
        self.issuer = issuer

    @property
    def form(self):
        return self.payload.data if self.method == "POST" else {}

    @property
    def args(self):
        return self.payload.data if self.method == "GET" else {}


class Client:
    def __init__(self, client_id, grants, scopes=SCOPES, redirects=(), secret_hash=None, user=None, version=None):
        self.client_id, self.grants, self.scopes = client_id, grants, scopes
        self.redirects, self.secret_hash = redirects, secret_hash
        self.user, self.version = user, version

    def get_client_id(self):
        return self.client_id

    def get_default_redirect_uri(self):
        return None

    def check_redirect_uri(self, uri):
        return uri in self.redirects

    def check_client_secret(self, secret):
        return bool(self.secret_hash and hmac.compare_digest(self.secret_hash, digest(secret)))

    def check_endpoint_auth_method(self, method, endpoint):
        return method == ("client_secret_basic" if self.secret_hash else "none")

    def check_response_type(self, response_type):
        return response_type == "code" and "authorization_code" in self.grants

    def check_grant_type(self, grant_type):
        return grant_type in self.grants

    def get_allowed_scope(self, scope):
        scope = scope or self.scopes
        if not set(scope.split()).issubset(self.scopes.split()):
            raise InvalidScopeError()
        return scope


class Credential(SimpleNamespace):
    def get_redirect_uri(self):
        return self.redirect_uri

    def get_scope(self):
        return self.scope

    def get_nonce(self):
        return getattr(self, "nonce", None)

    def get_auth_time(self):
        return getattr(self, "auth_time", None)

    def get_acr(self):
        return None

    def get_amr(self):
        return ["pwd"]

    def check_client(self, client):
        return self.client_id == client.client_id

    def get_client_id(self):
        return self.client_id

    def get_user_code(self):
        return self.user_code_hash

    def is_expired(self):
        return self.exp <= time.time()


class CodeGrant(AuthorizationCodeGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = ["none"]

    def save_authorization_code(self, code, request):
        service = self.server.service
        data = dict(request.payload.data)
        nonce = data.get("nonce")
        if nonce:
            key = request.client.client_id + ":" + digest(nonce)
            if service.state.get("nonce", key):
                raise InvalidGrantError()
            service.state.put("nonce", key, {"exp": time.time() + FAMILY_TTL})
        data.update(client_id=request.client.client_id, user=request.user.name, version=request.user.version,
                    exp=time.time() + CODE_TTL, auth_time=int(time.time()))
        service.state.put("code", digest(code), data)

    def query_authorization_code(self, code, client):
        data = self.server.service.state.get("code", digest(code))
        if not data or data["client_id"] != client.client_id or data["exp"] <= time.time():
            return None
        return Credential(key=digest(code), **data)

    def delete_authorization_code(self, code):
        self.server.service.state.delete("code", code.key)

    def authenticate_user(self, code):
        return self.server.service.bound_user(code.user, code.version)


class RefreshGrant(RefreshTokenGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = ["none"]
    INCLUDE_NEW_REFRESH_TOKEN = True

    def authenticate_refresh_token(self, token):
        service = self.server.service
        key = digest(token)
        data = service.state.get("refresh", key)
        if not data or data["client_id"] != self.request.client.client_id:
            return None
        if data["iss"] != service.issuer or data["aud"] != service.settings.oidc_audience:
            return None
        family = service.state.get("family", data["family"])
        if not family or family["revoked"] or family["exp"] <= time.time():
            return None
        if data.get("used"):
            family["revoked"] = True
            service.state.put("family", data["family"], family)
            return None
        if not service.bound_user(data["user"], data["version"]):
            return None
        return Credential(key=key, **data)

    def authenticate_user(self, token):
        return self.server.service.bound_user(token.user, token.version)

    def revoke_old_credential(self, token):
        data = vars(token).copy()
        key = data.pop("key")
        data["used"] = True
        self.server.service.state.put("refresh", key, data)


class ServiceGrant(ClientCredentialsGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = ["client_secret_basic"]

    def validate_token_request(self):
        super().validate_token_request()
        client = self.request.client
        user = self.server.service.bound_user(client.user, client.version)
        record = self.server.service.auth.store.user(client.user)
        if not user or not record or record["role"] != "member":
            raise InvalidGrantError()
        self.request.user = user


class DeviceGrant(DeviceCodeGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = ["none"]

    def query_device_credential(self, code):
        data = self.server.service.state.get("device", digest(code))
        if not data or data.get("used"):
            return None
        return Credential(key=digest(code), **data)

    def validate_device_credential(self, credential):
        if credential.is_expired():
            raise ExpiredTokenError()
        if self.should_slow_down(credential):
            raise SlowDownError()
        return super().validate_device_credential(credential)

    def query_user_grant(self, code):
        service = self.server.service
        approval = service.state.get("approval", code)
        if not approval:
            return None
        user = service.bound_user(approval["user"], approval["version"])
        if not user:
            raise InvalidGrantError()
        return user, approval["approved"]

    def should_slow_down(self, credential):
        # The superclass invokes this a second time for a pending grant.
        if getattr(credential, "_polled", False):
            return False
        credential._polled = True
        data = self.server.service.state.get("device", credential.key)
        now = time.time()
        early = data["last_poll"] and now < data["last_poll"] + data["interval"]
        if early:
            data["interval"] += 5
        data["last_poll"] = now
        self.server.service.state.put("device", credential.key, data)
        return bool(early)


class S256(CodeChallenge):
    SUPPORTED_CODE_CHALLENGE_METHOD = ["S256"]

    def validate_code_challenge(self, grant, redirect_uri):
        data = grant.request.payload.data
        if not data.get("code_challenge") or data.get("code_challenge_method") != "S256":
            from authlib.oauth2.rfc6749.errors import InvalidRequestError
            raise InvalidRequestError("PKCE S256 is required")
        return super().validate_code_challenge(grant, redirect_uri)


class OIDC(OpenIDCode):
    def __init__(self, service):
        super().__init__(require_nonce=True)
        self.service = service

    def exists_nonce(self, nonce, request):
        return bool(self.service.state.get("nonce", request.payload.client_id + ":" + digest(nonce)))

    def resolve_client_private_key(self, client):
        return self.service.state.signing_key

    def get_client_claims(self, client):
        return {"iss": self.service.issuer, "aud": [client.client_id], "exp": int(time.time()) + ACCESS_TTL}

    def get_encode_header(self, client):
        return {"alg": "RS256", "kid": self.service.state.kid}

    def generate_user_info(self, user, scope):
        return UserInfo(sub=user.sub, preferred_username=user.name)


class Server(AuthorizationServer):
    def __init__(self, service):
        super().__init__(SCOPES.split())
        self.service = service
        self.register_grant(CodeGrant, [S256(required=True), OIDC(service)])
        self.register_grant(RefreshGrant)
        self.register_grant(DeviceGrant)
        self.register_grant(ServiceGrant)
        self.register_token_generator("default", self.issue)

    def create_oauth2_request(self, request):
        return request

    def handle_response(self, status, body, headers):
        if isinstance(body, dict):
            return JSONResponse(body, status_code=status, headers=dict(headers))
        return Response(body or "", status_code=status, headers=dict(headers))

    def send_signal(self, *args, **kwargs):
        pass

    def query_client(self, client_id):
        return self.service.client(client_id)

    def issue(self, grant_type, client, user=None, scope=None, expires_in=None, include_refresh_token=True):
        scope = client.get_allowed_scope(scope)
        token = {"access_token": secrets.token_urlsafe(32), "token_type": "Bearer",
                 "expires_in": ACCESS_TTL, "scope": scope}
        if include_refresh_token and client.check_grant_type("refresh_token") and "offline_access" in scope.split():
            token["refresh_token"] = secrets.token_urlsafe(48)
        return token

    def save_token(self, token, request):
        service = self.service
        user = request.user
        if not user:
            client = request.client
            user = service.bound_user(client.user, client.version)
        if not user:
            raise InvalidGrantError()
        old = request.refresh_token
        family_id = old.family if old else secrets.token_urlsafe(24)
        family = service.state.get("family", family_id)
        if not family:
            family = {"exp": time.time() + FAMILY_TTL, "revoked": False}
            service.state.put("family", family_id, family)
        data = {"client_id": request.client.client_id, "scope": token["scope"],
                "client_version": request.client.secret_hash or "",
                "user": user.name, "version": user.version, "sub": user.sub,
                "family": family_id, "exp": int(time.time()) + ACCESS_TTL,
                "iss": service.issuer, "aud": service.settings.oidc_audience}
        service.state.put("access", digest(token["access_token"]), data)
        if "refresh_token" in token:
            service.state.put("refresh", digest(token["refresh_token"]), {**data, "exp": family["exp"], "used": False})
        if request.credential:
            data = service.state.get("device", request.credential.key)
            data["used"] = True
            service.state.put("device", request.credential.key, data)


class Issuer:
    def __init__(self, settings):
        self.settings = settings
        self.issuer = settings.oidc_issuer.rstrip("/")
        self.cookie = COOKIE if self.issuer.startswith("https:") else "mesh_auth_transaction"
        parsed = urlsplit(self.issuer)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("OIDC issuer must be HTTPS (HTTP is allowed only for explicit loopback development)")
        if not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("OIDC issuer must be an origin")
        if not settings.oidc_introspection_secret:
            raise ValueError("AI_PROXY_OIDC_INTROSPECTION_SECRET is required")
        ids = [settings.oidc_console_client, settings.oidc_cli_client, settings.oidc_introspection_client]
        if len(set(ids)) != 3:
            raise ValueError("OIDC built-in client IDs must be distinct")
        for uri in settings.oidc_redirect_uris:
            target = urlsplit(uri)
            if target.fragment or target.username or not target.netloc or (
                target.scheme != "https" and not (target.scheme == "http" and target.hostname in {"localhost", "127.0.0.1", "::1"})
            ):
                raise ValueError("OIDC callback must be explicit HTTPS or loopback HTTP")
        self.state = State(Path(settings.oidc_state_dir or settings.users_path.parent / "oidc"))
        self.auth = Auth(UserStore(settings.users_path))
        self.server = Server(self)
        self.attempts = defaultdict(deque)
        # Authlib's grant debug statements include complete issued token dictionaries.
        for name in ("authorization_code", "refresh_token", "client_credentials"):
            logging.getLogger("authlib.oauth2.rfc6749.grants." + name).disabled = True
        logging.getLogger("authlib.oauth2.rfc8628.device_code").disabled = True

    def bound_user(self, name, version=None):
        current = self.auth.account_version(name) if name else None
        if not current or (version is not None and not hmac.compare_digest(current, version)):
            return None
        return SimpleNamespace(name=name, version=current, sub=digest(name + ":" + current))

    def client(self, client_id):
        settings = self.settings
        if client_id == settings.oidc_console_client:
            return Client(client_id, ["authorization_code", "refresh_token"], redirects=settings.oidc_redirect_uris)
        if client_id == settings.oidc_cli_client:
            return Client(client_id, [DEVICE_GRANT, "refresh_token"])
        if client_id == settings.oidc_introspection_client:
            return Client(client_id, [], scopes="", secret_hash=digest(settings.oidc_introspection_secret))
        data = self.state.get("client", client_id)
        return Client(client_id, ["client_credentials"], **data) if data else None

    def provision(self, client_id, user, scope="mesh-api"):
        if not re.fullmatch(r"[a-zA-Z0-9._-]{1,100}", client_id) or self.client(client_id):
            raise ValueError("Client ID is invalid or already exists")
        account = self.bound_user(user)
        record = self.auth.store.user(user)
        if not account or not record or record["role"] != "member":
            raise ValueError("Service clients require an existing member account, never an administrator")
        if scope != "mesh-api":
            raise ValueError("Service clients may request only mesh-api")
        secret = secrets.token_urlsafe(48)
        self.state.put("client", client_id, {"secret_hash": digest(secret), "user": user,
                                          "version": account.version, "scopes": scope})
        return secret

    def rate(self, key, limit=30, window=600):
        now = time.time()
        attempts = self.attempts[key]
        while attempts and attempts[0] < now - window:
            attempts.popleft()
        if len(attempts) >= limit:
            raise IssuerError("rate_limit_exceeded", 429)
        attempts.append(now)

    def authenticate(self, request, data, introspect=False):
        basic = request.headers.get("authorization")
        client_id = data.get("client_id")
        if basic:
            if not basic.startswith("Basic ") or client_id or data.get("client_secret"):
                raise IssuerError("invalid_client", 401)
            try:
                decoded = base64.b64decode(basic[6:], validate=True).decode()
                client_id, secret = decoded.split(":", 1)
            except (ValueError, UnicodeError):
                raise IssuerError("invalid_client", 401) from None
            client = self.client(client_id)
            if not client or not client.check_client_secret(secret):
                raise IssuerError("invalid_client", 401)
        else:
            client = self.client(client_id)
            if not client or client.secret_hash or data.get("client_secret"):
                raise IssuerError("invalid_client", 401)
        if introspect and client.client_id != self.settings.oidc_introspection_client:
            raise IssuerError("invalid_client", 401)
        return client

    def active(self, token):
        data = self.state.get("access", digest(token))
        if not data or data["exp"] <= time.time() or not self.bound_user(data["user"], data["version"]):
            return None
        if data["iss"] != self.issuer or data["aud"] != self.settings.oidc_audience:
            return None
        family = self.state.get("family", data["family"])
        client = self.client(data["client_id"])
        if not family or family["revoked"] or family["exp"] <= time.time() or not client:
            return None
        if data.get("client_version", "") != (client.secret_hash or ""):
            return None
        if client.user:
            record = self.auth.store.user(client.user)
            if not self.bound_user(client.user, client.version) or not record or record["role"] != "member":
                return None
        return data

    def authorize_request(self, data):
        client = self.client(data.get("client_id"))
        if not client or not client.check_redirect_uri(data.get("redirect_uri")):
            raise IssuerError("invalid_request")
        if not data.get("state") or not data.get("nonce"):
            raise IssuerError("invalid_request")
        if data.get("response_mode") not in (None, "query"):
            raise IssuerError("invalid_request")
        protocol = ProtocolRequest(self, "GET", "/authorize", data)
        protocol.consent_grant = self.server.get_consent_grant(protocol)
        return protocol

    def browser_origin(self, request):
        if request.headers.get("origin") != self.issuer:
            raise IssuerError("invalid_origin", 403)

    def transaction_cookie(self, key):
        # One cookie per form, named after its transaction: a second sign-in page (another tab,
        # the console starting over) must not replace the first page's cookie and break it.
        return f"{self.cookie}_{key[:16]}"

    def csrf(self, request, data):
        self.browser_origin(request)
        key = digest(data.get("transaction", ""))
        saved = self.state.get("transaction", key)
        cookie = request.cookies.get(self.transaction_cookie(key), "")
        if not saved or saved["exp"] <= time.time() or not cookie or not hmac.compare_digest(key, digest(cookie)):
            raise IssuerError("invalid_transaction", 403)
        return key, saved

    def page(self, kind, data=None):
        """A fresh sign-in or device form, with its transaction and its own cookie."""
        transaction = secrets.token_urlsafe(32)
        key = digest(transaction)
        self.state.put("transaction", key, {"kind": kind, "data": data or {}, "exp": time.time() + 600})
        response = pages.device(transaction) if kind == "device" else pages.sign_in(transaction)
        response.set_cookie(self.transaction_cookie(key), transaction, max_age=600, httponly=True,
                            secure=self.issuer.startswith("https:"), samesite="lax", path="/")
        return response

    def finish(self, response, key):
        """A form's transaction is spent: forget it and its cookie."""
        self.state.delete("transaction", key)
        response.delete_cookie(self.transaction_cookie(key), path="/", secure=self.issuer.startswith("https:"))
        return response


async def parameters(request: Request):
    if request.method == "GET":
        raw = request.url.query
        if len(raw) > 16384:
            raise IssuerError("invalid_request")
    else:
        if request.url.query:
            raise IssuerError("invalid_request")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
            raise IssuerError("invalid_request", 415)
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > 16384:
                raise IssuerError("invalid_request", 413)
        try:
            raw = body.decode("utf-8")
        except UnicodeError:
            raise IssuerError("invalid_request") from None
    try:
        if re.search(r"%(?![0-9a-fA-F]{2})", raw):
            raise ValueError("Malformed percent escape")
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=40, errors="strict")
    except (ValueError, UnicodeError):
        raise IssuerError("invalid_request") from None
    data = {}
    for key, value in pairs:
        if key in data or len(value) > 4096:
            raise IssuerError("invalid_request")
        data[key] = value
    return data


def create_auth_app(settings: Settings | None = None) -> FastAPI:
    """Create the separately hosted issuer, never mount it into the API app."""
    service = Issuer(settings or Settings.load())
    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            try:
                service.auth.client.close()
            finally:
                service.state.db.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.issuer = service
    # Browsers apply form-action to the redirect after a sign-in too, so the registered
    # callbacks' origins must be allowed or the console never receives its code.
    form_targets = " ".join(dict.fromkeys(
        f"{part.scheme}://{part.netloc}" for part in map(urlsplit, service.settings.oidc_redirect_uris)))
    # style-src 'self' is for /sign-in.css only; the pages have no scripts.
    policy = f"default-src 'none'; style-src 'self'; form-action 'self' {form_targets}; frame-ancestors 'none'; base-uri 'none'"
    # Where a browser page sends someone to start over: the console starts a fresh sign-in.
    console = service.settings.oidc_browser_origins[0] if service.settings.oidc_browser_origins else None

    @app.middleware("http")
    async def credential_headers(request, call_next):
        origin = request.headers.get("origin")
        cors_path = request.url.path in {"/token", "/revoke", "/device_authorization", "/userinfo"}
        if cors_path and origin and origin not in {
            service.issuer, *service.settings.oidc_browser_origins,
        }:
            return JSONResponse({"error": "invalid_origin"}, status_code=403, headers={"Cache-Control": "no-store"})
        if request.method == "OPTIONS" and cors_path and origin in service.settings.oidc_browser_origins:
            requested = {name.strip().lower() for name in request.headers.get("access-control-request-headers", "").split(",") if name.strip()}
            if request.headers.get("access-control-request-method") not in {"GET", "POST"} or not requested.issubset({"authorization", "content-type"}):
                return JSONResponse({"error": "invalid_request"}, status_code=403, headers={"Cache-Control": "no-store"})
            response = Response(status_code=204, headers={"Access-Control-Allow-Methods": "GET, POST",
                                                         "Access-Control-Allow-Headers": "authorization, content-type"})
        else:
            response = await call_next(request)
        # same-origin, not no-referrer: under no-referrer browsers send `Origin: null` with the
        # sign-in forms, which browser_origin must refuse. Other sites still get no referrer.
        response.headers.update({"Cache-Control": "no-store", "Pragma": "no-cache",
                                 "Referrer-Policy": "same-origin", "X-Content-Type-Options": "nosniff",
                                 "Content-Security-Policy": policy})
        if origin in service.settings.oidc_browser_origins and cors_path:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
        return response

    @app.exception_handler(IssuerError)
    async def issuer_error(request, error):
        # People see /authorize and /device in a browser: explain, and offer a way to start over.
        if request.url.path == "/authorize":
            return pages.problem(error.error, error.status, again=console, again_label="Back to the console")
        if request.url.path == "/device":
            return pages.problem(error.error, error.status, again="/device", again_label="Enter a code again")
        headers = {"WWW-Authenticate": 'Basic realm="issuer"'} if error.status == 401 else {}
        return JSONResponse({"error": error.error}, status_code=error.status, headers=headers)

    @app.get("/sign-in.css")
    def stylesheet():
        return Response(pages.STYLESHEET, media_type="text/css")

    @app.exception_handler(OAuth2Error)
    async def oauth_error(request, error):
        return JSONResponse(dict(error.get_body()), status_code=error.status_code)

    # For container healthchecks (compose.yaml, quickstart/); says nothing about accounts.
    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/.well-known/openid-configuration")
    def discovery():
        base = service.issuer
        return {"issuer": base, "authorization_endpoint": base + "/authorize", "token_endpoint": base + "/token",
                "device_authorization_endpoint": base + "/device_authorization", "userinfo_endpoint": base + "/userinfo",
                "introspection_endpoint": base + "/introspect", "revocation_endpoint": base + "/revoke",
                "jwks_uri": base + "/.well-known/jwks.json", "response_types_supported": ["code"],
                "response_modes_supported": ["query"], "grant_types_supported": ["authorization_code", "refresh_token", DEVICE_GRANT, "client_credentials"],
                "subject_types_supported": ["public"], "id_token_signing_alg_values_supported": ["RS256"],
                "token_endpoint_auth_methods_supported": ["none", "client_secret_basic"],
                "revocation_endpoint_auth_methods_supported": ["none", "client_secret_basic"],
                "introspection_endpoint_auth_methods_supported": ["client_secret_basic"],
                "code_challenge_methods_supported": ["S256"], "scopes_supported": SCOPES.split(),
                "claims_supported": ["sub", "iss", "aud", "exp", "iat", "auth_time", "nonce", "preferred_username"]}

    @app.get("/.well-known/jwks.json")
    def jwks():
        return {"keys": [service.state.signing_key.as_dict(private=False)]}

    @app.api_route("/authorize", methods=["GET", "POST"])
    async def authorize(request: Request):
        data = await parameters(request)
        with service.state.transaction():
            if request.method == "GET":
                service.rate(("authorize", request.client.host))
                service.authorize_request(data)
                return service.page("authorize", data)
            key, saved = service.csrf(request, data)
            if saved["kind"] != "authorize" or data.get("approve") != "yes":
                raise IssuerError("invalid_request")
            service.rate(("password", request.client.host), 10)
            name = data.get("username", "").strip().lower()
            if not service.auth.check_password(name, data.get("password", "")):
                # The same form again (its transaction still stands), so they can just retype.
                return pages.sign_in(data["transaction"], username=name, error="Wrong username or password.", status=401)
            user = service.bound_user(name)
            if not user:
                raise IssuerError("access_denied", 401)
            protocol = service.authorize_request(saved["data"])
            response = service.server.create_authorization_response(protocol, grant_user=user, grant=protocol.consent_grant)
            return service.finish(response, key)

    @app.post("/device_authorization")
    async def device_authorization(request: Request):
        data = await parameters(request)
        with service.state.transaction():
            client = service.authenticate(request, data)
            if not client.check_grant_type(DEVICE_GRANT):
                raise IssuerError("unauthorized_client")
            service.rate(("device_issue", request.client.host, client.client_id), 10)
            scope = client.get_allowed_scope(data.get("scope"))
            device_code = secrets.token_urlsafe(40)
            alphabet = "BCDFGHJKLMNPQRSTVWXZ"
            user_code = "".join(secrets.choice(alphabet) for _ in range(8))
            code_hash = digest(user_code)
            service.state.put("device", digest(device_code), {"client_id": client.client_id, "scope": scope, "requested_at": time.time(),
                              "exp": time.time() + DEVICE_TTL, "user_code_hash": code_hash, "interval": 5, "last_poll": 0, "used": False})
            service.state.put("user_code", code_hash, {"device": digest(device_code), "exp": time.time() + DEVICE_TTL})
            return {"device_code": device_code, "user_code": user_code[:4] + "-" + user_code[4:],
                    "verification_uri": service.issuer + "/device", "expires_in": DEVICE_TTL, "interval": 5}

    @app.api_route("/device", methods=["GET", "POST"])
    async def device(request: Request):
        data = await parameters(request)
        with service.state.transaction():
            if request.method == "GET":
                service.rate(("device_page", request.client.host))
                return service.page("device")
            key, saved = service.csrf(request, data)
            if saved["kind"] != "device" or data.get("approve") not in {"yes", "no"}:
                raise IssuerError("invalid_request")
            service.rate(("device_guess", request.client.host), 10)
            typed = data.get("user_code", "").strip()
            name = data.get("username", "").strip().lower()
            # Mistakes they can fix show the same form again (its transaction still stands).
            again = {"user_code": typed, "username": name}
            code_hash = digest(typed.upper().replace("-", "").replace(" ", ""))
            record = service.state.get("user_code", code_hash)
            if not record or record["exp"] <= time.time():
                return pages.device(data["transaction"], **again, status=400,
                                    error="That code isn't right, or it has expired. Check it, or run the login command again for a new one.")
            credential = service.state.get("device", record["device"])
            if not credential or credential["used"] or service.state.get("approval", code_hash):
                raise IssuerError("invalid_user_code")
            service.rate(("password", request.client.host), 10)
            if not service.auth.check_password(name, data.get("password", "")):
                return pages.device(data["transaction"], **again, error="Wrong username or password.", status=401)
            user = service.bound_user(name)
            if not user:
                raise IssuerError("access_denied", 401)
            approved = data["approve"] == "yes"
            service.state.put("approval", code_hash, {"user": name, "version": user.version,
                                                     "approved": approved, "exp": record["exp"]})
            return service.finish(pages.device_done(approved), key)

    @app.post("/device/console")
    async def device_from_console(request: Request):
        """A device code approved by someone already signed in to the console, without their password.

        Only the API calls this, authenticated as the introspection client, passing the person's
        console access token: the API never sees a password, and a CLI or service token can't
        approve devices. action "check" says what is asking; "approve" or "deny" decides, exactly
        as the /device form would.
        """
        data = await parameters(request)
        with service.state.transaction():
            service.authenticate(request, data, introspect=True)
            record = service.active(data.get("token", ""))
            if not record or record["client_id"] != service.settings.oidc_console_client:
                raise IssuerError("invalid_token", 401)
            user = service.bound_user(record["user"], record["version"])
            if not user:
                raise IssuerError("invalid_token", 401)
            action = data.get("action")
            if action not in {"check", "approve", "deny"}:
                raise IssuerError("invalid_request")
            # Per person, not per address: every request comes from the API.
            service.rate(("device_console", user.name), 20)
            code_hash = digest(data.get("user_code", "").strip().upper().replace("-", "").replace(" ", ""))
            code = service.state.get("user_code", code_hash)
            credential = service.state.get("device", code["device"]) if code and code["exp"] > time.time() else None
            if not credential:
                raise IssuerError("invalid_user_code")
            if credential["used"] or service.state.get("approval", code_hash):
                raise IssuerError("code_already_used", 409)
            if action == "check":
                return {"client_id": credential["client_id"], "requested_at": credential.get("requested_at"),
                        "expires_at": code["exp"], "user": user.name}
            approved = action == "approve"
            service.state.put("approval", code_hash, {"user": user.name, "version": user.version,
                                                     "approved": approved, "exp": code["exp"]})
            return {"approved": approved}

    @app.post("/token")
    async def token(request: Request):
        data = await parameters(request)
        with service.state.transaction():
            service.rate(("token", request.client.host), 120)
            # Pre-authenticate to disallow secret-in-body and mixed methods.
            service.authenticate(request, data)
            if data.get("resource") and data["resource"] != service.settings.oidc_audience:
                raise IssuerError("invalid_target")
            if data.get("audience") and data["audience"] != service.settings.oidc_audience:
                raise IssuerError("invalid_target")
            return service.server.create_token_response(ProtocolRequest(service, "POST", "/token", data, request.headers))

    @app.post("/introspect")
    async def introspect(request: Request):
        data = await parameters(request)
        with service.state.transaction():
            service.authenticate(request, data, introspect=True)
            record = service.active(data.get("token", ""))
            if not record:
                return {"active": False}
            return {"active": True, "username": record["user"], "sub": record["sub"], "iss": record["iss"],
                    "aud": record["aud"], "scope": record["scope"], "exp": record["exp"],
                    "client_id": record["client_id"], "token_type": "Bearer"}

    @app.post("/revoke")
    async def revoke(request: Request):
        data = await parameters(request)
        with service.state.transaction():
            client = service.authenticate(request, data)
            key = digest(data.get("token", ""))
            record = service.state.get("refresh", key) or service.state.get("access", key)
            if record and record["client_id"] == client.client_id:
                family = service.state.get("family", record["family"])
                family["revoked"] = True
                service.state.put("family", record["family"], family)
            return {}

    @app.api_route("/userinfo", methods=["GET", "POST"])
    async def userinfo(request: Request):
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise IssuerError("invalid_token", 401)
        with service.state.transaction():
            record = service.active(authorization[7:])
            if not record:
                raise IssuerError("invalid_token", 401)
            return {"sub": record["sub"], "preferred_username": record["user"], "username": record["user"]}

    return app


def main():
    parser = argparse.ArgumentParser(description="AI Control Mesh standalone OIDC issuer")
    subcommands = parser.add_subparsers(dest="command")
    client = subcommands.add_parser("client").add_subparsers(dest="client_command", required=True)
    add = client.add_parser("add", help="Provision a least-privilege client_credentials client")
    add.add_argument("client_id")
    add.add_argument("--user", required=True)
    add.add_argument("--scope", default="mesh-api")
    remove = client.add_parser("remove", help="Revoke a provisioned service client")
    remove.add_argument("client_id")
    args = parser.parse_args()
    settings = Settings.load()
    if args.command == "client":
        service = Issuer(settings)
        with service.state.transaction():
            if args.client_command == "add":
                try:
                    secret = service.provision(args.client_id, args.user, args.scope)
                except ValueError as error:
                    parser.error(str(error))
                print(secret)
            else:
                service.state.delete("client", args.client_id)
        return
    import uvicorn
    uvicorn.run(create_auth_app(settings), host=os.getenv("AI_PROXY_AUTH_HOST", "127.0.0.1"),
                port=int(os.getenv("AI_PROXY_AUTH_PORT", "10000")), access_log=False)


if __name__ == "__main__":
    main()
