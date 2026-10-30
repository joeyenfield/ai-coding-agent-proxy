"""Account directory and mandatory OAuth access-token authentication for the API.

The separate issuer owns OAuth credentials. This resource server introspects them,
then reloads current roles and memberships from the existing account directory.
Browser sign-ins and launcher tokens that older versions stored in users.json are
kept but never read: they cannot authenticate.
"""

from __future__ import annotations

import base64
import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

import httpx
import anyio
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from starlette.requests import HTTPConnection

from .db import Database, kv, schema
from .remote import same_origin


COOKIE = "ai_proxy_session"
HEADER = "x-ai-proxy-token"
# scrypt cost: tens of milliseconds per check, so guessing is slow but signing in isn't.
SCRYPT = {"n": 2**14, "r": 8, "p": 1}
ROLES = ("admin", "member")
NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **SCRYPT)
    return "scrypt${n}${r}${p}${salt}${hash}".format(
        **SCRYPT, salt=base64.b64encode(salt).decode(), hash=base64.b64encode(digest).decode(),
    )


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), dklen=32, n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, base64.b64decode(expected))


def valid_name(value: str, what: str) -> str:
    value = value.strip().lower()
    if not NAME.fullmatch(value):
        raise ValueError(f"A {what} name uses lowercase letters, numbers, '.', '_' and '-' (up to 40)")
    return value


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def team_key(org: str, team: str) -> str:
    return f"{org}/{team}"


def team_org(key: str) -> str:
    return key.split("/", 1)[0]


@dataclass(frozen=True)
class Identity:
    """Who a request comes from: a user, their role, their organizations and their teams (<org>/<team>)."""

    user: str
    via: str  # cookie | token
    role: str = "member"
    teams: tuple[str, ...] = field(default_factory=tuple)
    orgs: tuple[str, ...] = field(default_factory=tuple)

    @property
    def admin(self) -> bool:
        return self.role == "admin"

    def public(self) -> dict[str, Any]:
        return {"user": self.user, "via": self.via, "role": self.role, "orgs": list(self.orgs), "teams": list(self.teams)}


def write_directory(connection: Any, users: dict[str, Any], orgs: dict[str, Any]) -> None:
    """Replace the stored users and organizations with these (UserStore's and the legacy import's)."""
    connection.execute(schema.users.delete())
    connection.execute(schema.orgs.delete())
    for name, record in users.items():
        connection.execute(schema.users.insert().values(
            name=name, password=str(record.get("password") or ""), role=record.get("role", "member"),
            created_at=record.get("created_at"), orgs=list(record.get("orgs", [])), teams=list(record.get("teams", [])),
        ))
    for org, record in orgs.items():
        connection.execute(schema.orgs.insert().values(
            id=org, name=record.get("name") or org, created_at=record.get("created_at"), teams=dict(record.get("teams") or {}),
        ))


def _migrated(data: dict[str, Any]) -> dict[str, Any]:
    """A users.json from before organizations: its one organization (the server) and teams become one organization."""
    old = data.pop("org", None)
    teams = data.pop("teams", None)
    data["orgs"] = {}
    if not data.get("users") and not teams:
        return data
    name = str((old or {}).get("name") or "Organization")
    org = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-._")[:40] or "main"
    org = org if NAME.fullmatch(org) else "main"
    data["orgs"][org] = {"name": name, "created_at": time.time(), "teams": dict(teams or {})}
    for record in (data.get("users") or {}).values():
        record["orgs"] = [org]
        record["teams"] = [team_key(org, team) for team in record.get("teams", [])]
    return data


class UserStore:
    """Users, organizations and their teams, in the database's users and orgs tables.

    The whole directory is small, so it's kept in memory and re-read when meta's accounts_version
    says another process (`agent-server user`, a second server) changed it: once per call, on its
    outermost entry. Each change rewrites the directory in one transaction and bumps the version.
    The issuer opens it read-only (readonly=True): it never writes accounts.
    """

    def __init__(self, db: Database, readonly: bool = False):
        self.db = db
        self.readonly = readonly
        self._lock = threading.RLock()
        self._depth = 0
        self._data: dict[str, Any] | None = None
        self._version: Any = None

    # Storage

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._lock:
            if self._depth == 0:
                self._refresh()
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1

    def _refresh(self) -> None:
        try:
            self._read()
        except DBAPIError:
            if not self.readonly:
                raise
            # The issuer, started before the API ever made its tables: no accounts yet.
            self._data, self._version = {"users": {}, "orgs": {}, "sessions": {}, "tokens": {}}, None

    def _read(self) -> None:
        with self.db.read() as connection:
            current = kv.get_meta(connection, "accounts_version", 0)
            if self._data is not None and current == self._version:
                return
            users = {
                row["name"]: {
                    "password": row["password"], "role": row["role"], "orgs": list(row["orgs"] or []),
                    "teams": list(row["teams"] or []), "created_at": row["created_at"],
                }
                for row in connection.execute(select(schema.users)).mappings()
            }
            orgs = {
                row["id"]: {"name": row["name"], "created_at": row["created_at"], "teams": dict(row["teams"] or {})}
                for row in connection.execute(select(schema.orgs)).mappings()
            }
        # sessions and tokens: older versions' sign-ins, never stored here (they can't authenticate).
        self._data, self._version = {"users": users, "orgs": orgs, "sessions": {}, "tokens": {}}, current

    def _load(self) -> dict[str, Any]:
        if self._data is None:
            self._refresh()
        assert self._data is not None
        return self._data

    def _save(self) -> None:
        assert self._data is not None
        if self.readonly:
            raise PermissionError("This account directory is read-only")
        data = self._data
        with self.db.write() as connection:
            write_directory(connection, data["users"], data["orgs"])
            self._version = int(kv.get_meta(connection, "accounts_version", 0) or 0) + 1
            kv.put_meta(connection, "accounts_version", self._version)
            kv.put_meta(connection, "accounts_initialized", True)

    def initialized(self) -> bool:
        """Whether accounts were ever set up here (the first start makes an admin only once)."""
        with self.db.read() as connection:
            return bool(kv.get_meta(connection, "accounts_initialized"))

    # Organizations and their teams

    def orgs(self, viewer: Identity | None = None) -> list[dict[str, Any]]:
        """Every organization, or with a member's identity only theirs (admins see them all)."""
        with self._locked():
            data = self._load()
            mine = None if viewer is None or viewer.admin else set(viewer.orgs)
            return [self._org_public(org) for org in sorted(data["orgs"]) if mine is None or org in mine]

    def org(self, org: str) -> dict[str, Any] | None:
        with self._locked():
            return self._org_public(org) if org in self._load()["orgs"] else None

    def _org_public(self, org: str) -> dict[str, Any]:
        record = self._load()["orgs"][org]
        users = self._load()["users"]
        return {
            "id": org, "name": record.get("name") or org, "created_at": record.get("created_at"),
            "members": sorted(name for name, user in users.items() if org in user.get("orgs", [])),
            "teams": [
                {
                    "id": team, "key": team_key(org, team), "name": value.get("name") or team,
                    "members": sorted(name for name, user in users.items() if team_key(org, team) in user.get("teams", [])),
                }
                for team, value in sorted(record.get("teams", {}).items())
            ],
        }

    def add_org(self, org: str, name: str | None = None) -> str:
        org = valid_name(org, "organization")
        with self._locked():
            orgs = self._load()["orgs"]
            if org in orgs:
                raise ValueError(f"There's already an organization called {org}")
            orgs[org] = {"name": (name or org).strip()[:80] or org, "created_at": time.time(), "teams": {}}
            self._save()
        return org

    def rename_org(self, org: str, name: str) -> None:
        with self._locked():
            record = self._load()["orgs"].get(org)
            if record is None:
                raise KeyError(org)
            record["name"] = name.strip()[:80] or org
            self._save()

    def remove_org(self, org: str) -> None:
        """Delete an organization: its teams go, and its members leave it (their accounts stay)."""
        with self._locked():
            data = self._load()
            if data["orgs"].pop(org, None) is None:
                raise KeyError(org)
            for record in data["users"].values():
                record["orgs"] = [item for item in record.get("orgs", []) if item != org]
                record["teams"] = [item for item in record.get("teams", []) if team_org(item) != org]
            self._save()

    def add_team(self, org: str, team: str, name: str | None = None) -> str:
        """A team in an organization; returns its key, <org>/<team>."""
        team = valid_name(team, "team")
        with self._locked():
            record = self._load()["orgs"].get(org)
            if record is None:
                raise KeyError(org)
            teams = record.setdefault("teams", {})
            if team in teams:
                raise ValueError(f"{org} already has a team called {team}")
            teams[team] = {"name": (name or team).strip()[:80] or team, "created_at": time.time()}
            self._save()
        return team_key(org, team)

    def remove_team(self, org: str, team: str) -> None:
        with self._locked():
            data = self._load()
            if data["orgs"].get(org, {}).get("teams", {}).pop(team, None) is None:
                raise KeyError(team)
            for record in data["users"].values():
                record["teams"] = [item for item in record.get("teams", []) if item != team_key(org, team)]
            self._save()

    def rename_team(self, org: str, team: str, name: str) -> None:
        """Change a team's display name without changing its sharing key."""
        with self._locked():
            record = self._load()["orgs"].get(org, {}).get("teams", {}).get(team)
            if record is None:
                raise KeyError(team)
            record["name"] = name.strip()[:80] or team
            self._save()

    def set_membership(self, org: str, name: str, member: bool, *, team: str | None = None) -> None:
        """Change one membership under the lock, preserving memberships outside this scope."""
        with self._locked():
            data = self._load()
            organization = data["orgs"].get(org)
            if organization is None or name not in data["users"]:
                raise KeyError(org if organization is None else name)
            if team is not None and team not in organization.get("teams", {}):
                raise KeyError(team)
            record = data["users"][name]
            orgs, teams = set(record.get("orgs", [])), set(record.get("teams", []))
            if team is None:
                if member:
                    orgs.add(org)
                else:
                    orgs.discard(org)
                    teams = {key for key in teams if team_org(key) != org}
            elif member:
                orgs.add(org)
                teams.add(team_key(org, team))
            else:
                teams.discard(team_key(org, team))
            self.update_user(name, orgs=sorted(orgs), teams=sorted(teams))

    def _team_keys(self) -> set[str]:
        return {team_key(org, team) for org, record in self._load()["orgs"].items() for team in record.get("teams", {})}

    def _membership(self, orgs: list[str] | None, teams: list[str] | None, current: dict[str, Any]) -> tuple[list[str], list[str]]:
        """A user's organizations and teams, checked: joining a team joins its organization, leaving one leaves its teams."""
        data = self._load()
        wanted_orgs = set(current.get("orgs", []) if orgs is None else orgs)
        wanted_teams = set(current.get("teams", []) if teams is None else teams)
        unknown = sorted(org for org in wanted_orgs if org not in data["orgs"])
        if unknown:
            raise ValueError(f"No organization called {', '.join(unknown)}")
        unknown = sorted(wanted_teams - self._team_keys())
        if unknown:
            raise ValueError(f"No team called {', '.join(unknown)} (teams are named <organization>/<team>)")
        if teams is not None:
            wanted_orgs |= {team_org(team) for team in wanted_teams}
        wanted_teams = {team for team in wanted_teams if team_org(team) in wanted_orgs}
        return sorted(wanted_orgs), sorted(wanted_teams)

    def _user_public(self, name: str) -> dict[str, Any]:
        record = self._load()["users"][name]
        return {
            "name": name, "role": record.get("role", "member"), "orgs": list(record.get("orgs", [])),
            "teams": list(record.get("teams", [])), "created_at": record.get("created_at"),
        }

    # Users

    def users(self) -> list[str]:
        with self._locked():
            return sorted(self._load()["users"])

    def user(self, name: str) -> dict[str, Any] | None:
        with self._locked():
            return self._user_public(name) if name in self._load()["users"] else None

    def user_list(self, viewer: Identity | None = None) -> list[dict[str, Any]]:
        """Every user, or for a member those who share an organization with them (and themselves)."""
        with self._locked():
            users = [self._user_public(name) for name in sorted(self._load()["users"])]
        if viewer is None or viewer.admin:
            return users
        mine = set(viewer.orgs)
        return [user for user in users if user["name"] == viewer.user or mine & set(user["orgs"])]

    def add_user(self, name: str, password: str, role: str | None = None, teams: list[str] | None = None, orgs: list[str] | None = None) -> dict[str, Any]:
        """A new user. The first user is an admin, so someone can manage the rest."""
        name = valid_name(name, "user")
        with self._locked():
            data = self._load()
            if name in data["users"]:
                raise ValueError(f"There's already a user called {name}")
            role = role or ("admin" if not data["users"] else "member")
            if role not in ROLES:
                raise ValueError(f"Role must be one of: {', '.join(ROLES)}")
            member_of, in_teams = self._membership(orgs or [], teams or [], {})
            data["users"][name] = {
                "password": hash_password(_checked_password(password)), "role": role,
                "orgs": member_of, "teams": in_teams, "created_at": time.time(),
            }
            self._save()
            return self._user_public(name)

    def update_user(self, name: str, *, role: str | None = None, teams: list[str] | None = None, orgs: list[str] | None = None, password: str | None = None) -> dict[str, Any]:
        with self._locked():
            data = self._load()
            record = data["users"].get(name)
            if record is None:
                raise KeyError(name)
            if role is not None:
                if role not in ROLES:
                    raise ValueError(f"Role must be one of: {', '.join(ROLES)}")
                if record.get("role") == "admin" and role != "admin" and self._admins() == [name]:
                    raise ValueError("That's the last admin; make someone else an admin first")
            member_of, in_teams = self._membership(orgs, teams, record)
            encoded = hash_password(_checked_password(password)) if password is not None else None
            if role is not None:
                record["role"] = role
            record["orgs"], record["teams"] = member_of, in_teams
            if encoded is not None:
                record["password"] = encoded
                data["sessions"] = {key: value for key, value in data["sessions"].items() if value.get("user") != name}
            self._save()
            return self._user_public(name)

    def _admins(self) -> list[str]:
        return [name for name, record in self._load()["users"].items() if record.get("role") == "admin"]

    def set_password(self, name: str, password: str) -> None:
        self.update_user(name, password=password)

    def remove_user(self, name: str) -> bool:
        with self._locked():
            data = self._load()
            if name not in data["users"]:
                return False
            if data["users"][name].get("role") == "admin" and self._admins() == [name] and len(data["users"]) > 1:
                raise ValueError("That's the last admin; make someone else an admin first")
            del data["users"][name]
            for kind in ("sessions", "tokens"):
                data[kind] = {key: value for key, value in data[kind].items() if value.get("user") != name}
            self._save()
            return True

    def verify(self, name: str, password: str) -> bool:
        with self._locked():
            record = self._load()["users"].get(name)
        if record is None:
            # Same work either way, so timing doesn't reveal which users exist.
            check_password(password, hash_password("timing-equaliser"))
            return False
        return check_password(password, str(record.get("password") or ""))

    def account_version(self, name: str) -> str | None:
        with self._locked():
            record = self._load()["users"].get(name)
            if record is None:
                return None
            return _digest(json.dumps([record.get("created_at"), record.get("password")]))


def _checked_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("Use a password of at least 8 characters")
    return password


class Auth:
    """Who a request comes from: OAuth access tokens introspected at the issuer, mapped to current accounts."""

    def __init__(self, store: UserStore, settings: Any = None):
        self.store = store
        self.settings = settings
        self.client = httpx.Client(timeout=5, trust_env=False, follow_redirects=False)

    @property
    def environment_user(self) -> tuple[str, str] | None:
        user, password = os.getenv("AI_PROXY_USERNAME"), os.getenv("AI_PROXY_PASSWORD")
        return (user.strip().lower(), password) if user and password else None

    def account_version(self, user: str) -> str | None:
        version = self.store.account_version(user)
        if version:
            return version
        environment = self.environment_user
        return _digest(environment[1]) if environment and user == environment[0] else None

    def identity_for(self, user: str, via: str) -> Identity | None:
        environment = self.environment_user
        record = self.store.user(user)
        if record:
            return Identity(user, via, record["role"], tuple(record["teams"]), tuple(record["orgs"]))
        if environment and user == environment[0]:
            # The account from the environment manages the server.
            return Identity(user, via, "admin", ())
        return None

    @staticmethod
    def request_token(connection: HTTPConnection) -> tuple[str, str]:
        """The access token a request carries and how: X-AI-Proxy-Token, then Bearer ("token"), then the cookie."""
        token = connection.headers.get(HEADER, "")
        authorization = connection.headers.get("authorization", "")
        if not token and authorization:
            scheme, _, value = authorization.partition(" ")
            if scheme.lower() == "bearer":
                token = value
        if token:
            return token, "token"
        return connection.cookies.get(COOKIE, ""), "cookie"

    def device_decision(self, connection: HTTPConnection, user_code: str, action: str) -> httpx.Response:
        """Ask the issuer to check, approve or deny a device code for whoever is signed in here.

        The person's own access token goes to the issuer, which accepts only console sign-ins;
        the API authenticates as the introspection client, so nothing else can make this call.
        """
        token, _ = self.request_token(connection)
        if not self.settings or not self.settings.oidc_introspection_secret:
            raise AuthUnavailable("OIDC introspection credentials are not configured")
        try:
            return self.client.post(
                self.settings.oidc_issuer + "/device/console", data={"token": token, "user_code": user_code, "action": action},
                auth=(self.settings.oidc_introspection_client, self.settings.oidc_introspection_secret),
            )
        except httpx.HTTPError as exc:
            raise AuthUnavailable("The authentication service is unavailable") from exc

    def identify(self, connection: HTTPConnection) -> Identity | None:
        token, via = self.request_token(connection)
        if not token or len(token) > 8192:
            return None
        if not self.settings or not self.settings.oidc_introspection_secret:
            raise AuthUnavailable("OIDC introspection credentials are not configured")
        try:
            response = self.client.post(
                self.settings.oidc_issuer + "/introspect", data={"token": token},
                auth=(self.settings.oidc_introspection_client, self.settings.oidc_introspection_secret),
            )
            response.raise_for_status()
            claims = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logging.getLogger(__name__).warning("OIDC introspection is unavailable (%s)", type(exc).__name__)
            raise AuthUnavailable("The authentication service is unavailable") from exc
        if not isinstance(claims, dict) or claims.get("active") is not True:
            return None
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        expires = claims.get("exp")
        if (
            claims.get("iss") != self.settings.oidc_issuer
            or self.settings.oidc_audience not in audiences
            or str(claims.get("token_type", "")).lower() != "bearer"
            or "mesh-api" not in str(claims.get("scope", "")).split()
            or not isinstance(expires, (int, float)) or not math.isfinite(expires) or expires <= time.time()
            or not isinstance(claims.get("username"), str)
        ):
            return None
        return self.identity_for(claims["username"], via)

    def check_password(self, user: str, password: str) -> bool:
        user = user.strip().lower()
        environment = self.environment_user
        if environment and hmac.compare_digest(user.encode(), environment[0].encode()):
            return hmac.compare_digest(password.encode(), environment[1].encode())
        return self.store.verify(user, password)


# Reachable without signing in: health checks, signing in itself, and the console's files
# (the app shows its sign-in page). Everything under the protected prefixes needs a credential.
PUBLIC_PATHS = {"/health", "/api/auth/status", "/api/auth/login", "/api/auth/callback", "/api/auth/refresh", "/api/auth/logout"}
CREDENTIAL_CHECK_INTERVAL = 15
PROTECTED_PREFIXES = ("/api/", "/v1/", "/anthropic/", "/session/", "/docs", "/redoc", "/openapi.json")
# Unscoped agent endpoints that may name their session in X-AI-Proxy-Session instead of the path.
AGENT_PREFIXES = ("/v1/", "/anthropic/")
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Changes to the server itself: only admins make them. Members use the server.
ADMIN_ONLY = [
    (re.compile(r"PUT"), re.compile(r"/api/config")),
    # The stored settings themselves: agents, model tuning, retention, export and import.
    (re.compile(r"GET|POST|PUT|DELETE"), re.compile(r"/api/config/.+")),
    (re.compile(r"POST"), re.compile(r"/api/containers")),
    (re.compile(r"PUT|DELETE"), re.compile(r"/api/containers/[^/]+")),
    (re.compile(r"POST"), re.compile(r"/api/server/restart")),
    (re.compile(r"GET"), re.compile(r"/api/server/stats")),
    (re.compile(r"POST"), re.compile(r"/api/machines/[^/]+/providers/adopt")),
    (re.compile(r"POST|PUT|DELETE"), re.compile(r"/api/orgs(/.*)?")),
    (re.compile(r"POST|PUT|DELETE"), re.compile(r"/api/users(/.*)?")),
]


def admin_only(method: str, path: str) -> bool:
    return any(methods.fullmatch(method) and paths.fullmatch(path) for methods, paths in ADMIN_ONLY)


class AuthMiddleware:
    """Require an active access token and preserve session, role and sharing checks."""

    def __init__(self, app: Any, runtime: Any):
        self.app = app
        self.runtime = runtime

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        connection = HTTPConnection(scope)
        if not path.startswith(PROTECTED_PREFIXES):
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET")
        if (scope["type"] == "websocket" or method in UNSAFE_METHODS) and not same_origin(connection):
            await _deny(scope, receive, send, 403, "Requests from other sites aren't allowed.")
            return
        if path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        try:
            identity = await asyncio.to_thread(self.runtime.auth.identify, connection)
        except AuthUnavailable as exc:
            await _deny(scope, receive, send, 503, str(exc))
            return
        if identity is None:
            await _deny(scope, receive, send, 401, "Sign in first.")
            return
        if scope["type"] == "http" and admin_only(method, path) and not identity.admin:
            await _deny(scope, receive, send, 403, "Only an admin can change this.")
            return
        session_id = path.split("/", 3)[2] if path.startswith("/session/") else connection.headers.get("x-ai-proxy-session") if path.startswith(AGENT_PREFIXES) else None
        if session_id:
            from .access import can_control

            session = self.runtime.sessions.get(session_id)
            if session is None or session.ended_at is not None or not can_control(identity, session):
                await _deny(scope, receive, send, 404, "No open session with that id")
                return
        scope.setdefault("state", {})["identity"] = identity
        if scope["type"] != "websocket":
            await self.app(scope, receive, send)
            return

        renewal_required = False

        async def credential_valid() -> None:
            nonlocal renewal_required
            while True:
                await asyncio.sleep(CREDENTIAL_CHECK_INTERVAL)
                try:
                    current = await asyncio.to_thread(self.runtime.auth.identify, connection)
                except AuthUnavailable:
                    renewal_required = True
                    group.cancel_scope.cancel()
                    return
                if current is None or current.user != identity.user:
                    renewal_required = True
                    group.cancel_scope.cancel()
                    return
                scope["state"]["identity"] = current

        async with anyio.create_task_group() as group:
            group.start_soon(credential_valid)
            try:
                await self.app(scope, receive, send)
            finally:
                group.cancel_scope.cancel()
        if renewal_required:
            await send({"type": "websocket.close", "code": 4410, "reason": "Renew your OAuth access token and reconnect"})


async def _deny(scope: dict[str, Any], receive: Any, send: Any, status: int, message: str) -> None:
    if scope["type"] == "websocket":
        await receive()  # websocket.connect
        await send({"type": "websocket.close", "code": 4400 + status % 100, "reason": message})
        return
    body = json.dumps({"detail": message}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store")]
    if status == 401:
        headers.append((b"www-authenticate", b'Bearer realm="ai-control-mesh"'))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def request_identity(request: Any) -> Identity | None:
    """The identity attached by middleware; never None inside a protected handler."""
    return request.scope.get("state", {}).get("identity")


class AuthUnavailable(RuntimeError):
    """Authentication infrastructure failed; never interpret this as local access."""
