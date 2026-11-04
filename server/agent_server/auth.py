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
# The organization above every other: only site admins are in it. Every other organization is a
# tenant under it, and everything else (users, machines, providers, sessions and their recordings)
# belongs to exactly one tenant. Nothing is shared between tenants, and the root sees none of theirs.
ROOT_TENANT = "root"


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
    """Who a request comes from: a user, their role in their organization (their tenant), and their teams there (<org>/<team>).

    A user is in exactly one organization. admin means an admin of that organization; a site admin is
    an admin of the root organization, who runs the server itself and its organizations, but sees
    nothing inside another organization.
    """

    user: str
    via: str  # cookie | token
    role: str = "member"
    teams: tuple[str, ...] = field(default_factory=tuple)
    tenant: str = ROOT_TENANT

    @property
    def admin(self) -> bool:
        return self.role == "admin"

    @property
    def site_admin(self) -> bool:
        return self.role == "admin" and self.tenant == ROOT_TENANT

    @property
    def orgs(self) -> tuple[str, ...]:
        """The organizations they're in: their tenant, only."""
        return (self.tenant,)

    def public(self) -> dict[str, Any]:
        return {
            "user": self.user, "via": self.via, "role": self.role, "tenant": self.tenant, "site_admin": self.site_admin,
            "orgs": list(self.orgs), "teams": list(self.teams),
        }


def same_tenant(identity: Identity | None, tenant: str | None) -> bool:
    """Whether something of this tenant is identity's to reach at all. Anything without a tenant is nobody's,
    except a site admin's: site admins reach every tenant, to look after the whole server."""
    if identity is not None and identity.site_admin:
        return True
    return identity is not None and bool(tenant) and identity.tenant == tenant


def administers(identity: Identity | None, tenant: str | None) -> bool:
    """Who manages an organization's users and teams: its admins, and site admins (who set organizations up)."""
    return identity is not None and identity.admin and (identity.site_admin or identity.tenant == tenant)


def tenant_of(record: dict[str, Any]) -> str:
    """A stored user's tenant: its own, else (rows from before tenants) its first organization, else the root."""
    tenant = record.get("tenant")
    if tenant:
        return str(tenant)
    orgs = sorted(str(item) for item in record.get("orgs") or [])
    return orgs[0] if orgs else ROOT_TENANT


def write_directory(connection: Any, users: dict[str, Any], orgs: dict[str, Any]) -> None:
    """Replace the stored users and organizations with these (UserStore's and the legacy import's)."""
    connection.execute(schema.users.delete())
    connection.execute(schema.orgs.delete())
    orgs = {ROOT_TENANT: {"name": "Site administration", "created_at": time.time(), "teams": {}}, **orgs}
    for name, record in users.items():
        tenant = tenant_of(record)
        orgs.setdefault(tenant, {"name": tenant, "created_at": time.time(), "teams": {}})
        connection.execute(schema.users.insert().values(
            name=name, password=str(record.get("password") or ""), role=record.get("role", "member"),
            created_at=record.get("created_at"), tenant=tenant, orgs=[tenant],
            teams=[team for team in record.get("teams", []) if team_org(team) == tenant],
        ))
    for org, record in orgs.items():
        connection.execute(schema.orgs.insert().values(
            id=org, name=record.get("name") or org, created_at=record.get("created_at"), teams=dict(record.get("teams") or {}),
            parent=None if org == ROOT_TENANT else ROOT_TENANT,
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
            users = {}
            for row in connection.execute(select(schema.users)).mappings():
                record = {"password": row["password"], "role": row["role"], "orgs": list(row["orgs"] or []),
                          "teams": list(row["teams"] or []), "created_at": row["created_at"], "tenant": row["tenant"]}
                record["tenant"] = tenant_of(record)
                record["teams"] = [team for team in record["teams"] if team_org(team) == record["tenant"]]
                users[row["name"]] = record
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

    # Organizations (tenants) and their teams

    def orgs(self, viewer: Identity | None = None) -> list[dict[str, Any]]:
        """Every organization, or with an identity only theirs (site admins see them all, without what's inside)."""
        with self._locked():
            data = self._load()
            mine = None if viewer is None or viewer.site_admin else {viewer.tenant}
            return [self._org_public(org) for org in sorted(data["orgs"], key=lambda key: (key != ROOT_TENANT, key)) if mine is None or org in mine]

    def org(self, org: str) -> dict[str, Any] | None:
        with self._locked():
            return self._org_public(org) if org in self._load()["orgs"] else None

    def _org_public(self, org: str) -> dict[str, Any]:
        record = self._load()["orgs"][org]
        users = self._load()["users"]
        return {
            "id": org, "name": record.get("name") or org, "created_at": record.get("created_at"),
            "root": org == ROOT_TENANT, "parent": None if org == ROOT_TENANT else ROOT_TENANT,
            "members": sorted(name for name, user in users.items() if user["tenant"] == org),
            "teams": [
                {
                    "id": team, "key": team_key(org, team), "name": value.get("name") or team,
                    "members": sorted(name for name, user in users.items() if team_key(org, team) in user.get("teams", [])),
                }
                for team, value in sorted(record.get("teams", {}).items())
            ],
        }

    def add_org(self, org: str, name: str | None = None) -> str:
        """A new tenant, under the root organization."""
        org = valid_name(org, "organization")
        with self._locked():
            orgs = self._load()["orgs"]
            if org in orgs or org == ROOT_TENANT:
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
        """Delete an organization that has no users left: everything of a tenant is its users', so they go first."""
        with self._locked():
            data = self._load()
            if org == ROOT_TENANT:
                raise ValueError("The root organization can't be removed")
            if org not in data["orgs"]:
                raise KeyError(org)
            members = sorted(name for name, user in data["users"].items() if user["tenant"] == org)
            if members:
                raise ValueError(f"Remove {org}'s users first ({', '.join(members)}): users can't move to another organization")
            del data["orgs"][org]
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
        """Join or leave a team of the user's own organization. Users never join or leave an organization:
        they're made in one and stay there (everything they own belongs to it)."""
        with self._locked():
            data = self._load()
            organization = data["orgs"].get(org)
            if organization is None or name not in data["users"]:
                raise KeyError(org if organization is None else name)
            record = data["users"][name]
            if team is None:
                if member and record["tenant"] == org:
                    return
                raise ValueError("A user belongs to the organization they were made in; add them to another one as a new user")
            if team not in organization.get("teams", {}):
                raise KeyError(team)
            if record["tenant"] != org:
                raise ValueError(f"{name} isn't in {org}")
            teams = set(record.get("teams", []))
            if member:
                teams.add(team_key(org, team))
            else:
                teams.discard(team_key(org, team))
            self.update_user(name, teams=sorted(teams))

    def _teams(self, tenant: str, teams: list[str] | None, current: list[str]) -> list[str]:
        """A user's teams, checked: teams of their own organization only."""
        if teams is None:
            return sorted(current)
        wanted = {str(team) for team in teams}
        known = {team_key(tenant, team) for team in self._load()["orgs"].get(tenant, {}).get("teams", {})}
        unknown = sorted(wanted - known)
        if unknown:
            raise ValueError(f"No team called {', '.join(unknown)} in {tenant} (teams are named <organization>/<team>)")
        return sorted(wanted)

    def _user_public(self, name: str) -> dict[str, Any]:
        record = self._load()["users"][name]
        return {
            "name": name, "role": record.get("role", "member"), "tenant": record["tenant"], "orgs": [record["tenant"]],
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
        """Every user, or for an identity those of their own organization (site admins: everyone, to set organizations up)."""
        with self._locked():
            users = [self._user_public(name) for name in sorted(self._load()["users"])]
        if viewer is None or viewer.site_admin:
            return users
        return [user for user in users if user["tenant"] == viewer.tenant]

    def add_user(
        self, name: str, password: str, role: str | None = None, teams: list[str] | None = None, orgs: list[str] | None = None,
        tenant: str | None = None,
    ) -> dict[str, Any]:
        """A new user in one organization (tenant), for good. The first user is an admin of the root, so someone can run the server.

        orgs is the older way to name it: one organization at most. Without either it's the organization of
        their teams; without teams either, only an admin can be made, in the root.
        """
        name = valid_name(name, "user")
        named = sorted({str(item) for item in orgs or []} | ({tenant} if tenant else set()))
        if not named:
            named = sorted({team_org(str(team)) for team in teams or []})
        if len(named) > 1:
            raise ValueError("A user belongs to one organization; name only one")
        with self._locked():
            data = self._load()
            if name in data["users"]:
                raise ValueError(f"There's already a user called {name}")
            role = role or ("admin" if not data["users"] else "member")
            if role not in ROLES:
                raise ValueError(f"Role must be one of: {', '.join(ROLES)}")
            home = named[0] if named else ROOT_TENANT
            if home not in data["orgs"]:
                raise ValueError(f"No organization called {home}")
            if home == ROOT_TENANT and role != "admin":
                raise ValueError("The root organization is for site admins only; name the organization this user belongs to")
            data["users"][name] = {
                "password": hash_password(_checked_password(password)), "role": role, "tenant": home, "orgs": [home],
                "teams": self._teams(home, teams or [], []), "created_at": time.time(),
            }
            self._save()
            return self._user_public(name)

    def update_user(self, name: str, *, role: str | None = None, teams: list[str] | None = None, orgs: list[str] | None = None, password: str | None = None) -> dict[str, Any]:
        with self._locked():
            data = self._load()
            record = data["users"].get(name)
            if record is None:
                raise KeyError(name)
            tenant = record["tenant"]
            if orgs is not None and sorted({str(item) for item in orgs}) != [tenant]:
                raise ValueError("A user stays in the organization they were made in")
            if role is not None:
                if role not in ROLES:
                    raise ValueError(f"Role must be one of: {', '.join(ROLES)}")
                if tenant == ROOT_TENANT and role != "admin":
                    raise ValueError("The root organization is for site admins only")
                if record.get("role") == "admin" and role != "admin" and self._admins(tenant) == [name]:
                    raise ValueError("That's the last admin; make someone else an admin first")
            in_teams = self._teams(tenant, teams, record.get("teams", []))
            encoded = hash_password(_checked_password(password)) if password is not None else None
            if role is not None:
                record["role"] = role
            record["teams"] = in_teams
            if encoded is not None:
                record["password"] = encoded
                data["sessions"] = {key: value for key, value in data["sessions"].items() if value.get("user") != name}
            self._save()
            return self._user_public(name)

    def _admins(self, tenant: str) -> list[str]:
        return [name for name, record in self._load()["users"].items() if record.get("role") == "admin" and record["tenant"] == tenant]

    def set_password(self, name: str, password: str) -> None:
        self.update_user(name, password=password)

    def remove_user(self, name: str) -> bool:
        with self._locked():
            data = self._load()
            if name not in data["users"]:
                return False
            tenant = data["users"][name]["tenant"]
            others = [other for other, record in data["users"].items() if other != name and record["tenant"] == tenant]
            if data["users"][name].get("role") == "admin" and self._admins(tenant) == [name] and others:
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


# How long the issuer's answer about an access token is reused: long-lived streams (the live view,
# terminal viewers) re-check their caller on every event, which must not mean a call to the issuer each.
# A revocation or an account change reaches open connections within this time.
INTROSPECTION_CACHE_SECONDS = 10.0


# Signed access tokens (the issuer's AI_PROXY_AUTH_JWT_ACCESS_TOKENS=1) are checked here, without asking the issuer,
# only while the revocations the issuer publishes are known to be current (Revocations.fresh); else they're introspected.
JWT_SKEW = 30
JWKS_REFRESH_SECONDS = 60.0
REVOCATIONS_REFRESH_SECONDS = 5.0
REVOCATIONS_STALE_SECONDS = 30.0


class Revocations:
    """What the issuer said stopped working: token families (sign-out, a reused refresh token) and service
    clients, each with when. Kept from the shared store (cluster.py) and its messages; current or not."""

    def __init__(self, cluster: Any):
        self.cluster = cluster
        self.revoked: dict[tuple[str, str], float] = {}
        self.refreshed = 0.0
        self._lock = threading.Lock()
        cluster.on("auth.revoked", self._heard)

    def _heard(self, data: dict[str, Any], _: str) -> None:
        kind, key = str(data.get("kind") or ""), str(data.get("id") or "")
        if kind and key:
            with self._lock:
                self.revoked[(kind, key)] = float(data.get("at") or time.time())

    def refresh(self) -> None:
        if time.monotonic() - self.refreshed < REVOCATIONS_REFRESH_SECONDS:
            return
        try:
            found = {}
            for name in self.cluster.keys("revoked:"):
                parts = name.split(":", 2)
                value = self.cluster.get(name)
                if len(parts) == 3 and value:
                    found[(parts[1], parts[2])] = float(value)
        except Exception:  # noqa: BLE001 - not current any more: tokens are introspected until it is again
            return
        with self._lock:
            self.revoked = {**found, **{key: at for key, at in self.revoked.items() if key not in found and time.time() - at < 600}}
            self.refreshed = time.monotonic()

    def fresh(self) -> bool:
        self.refresh()
        return time.monotonic() - self.refreshed < REVOCATIONS_STALE_SECONDS

    def stopped(self, kind: str, key: str, issued_at: float) -> bool:
        with self._lock:
            at = self.revoked.get((kind, key))
        return at is not None and issued_at <= at + 1


class Auth:
    """Who a request comes from: OAuth access tokens introspected at the issuer (or, signed ones, checked here),
    mapped to current accounts."""

    def __init__(self, store: UserStore, settings: Any = None):
        self.store = store
        self.settings = settings
        self.client = httpx.Client(timeout=5, trust_env=False, follow_redirects=False)
        # Tokens the issuer confirmed: digest -> (when it said so, whose it is, when it expires).
        self._confirmed: dict[str, tuple[float, str, float]] = {}
        # Set with several processes (the runtime): signed tokens are then checked here (verify_jwt).
        self.revocations: Revocations | None = None
        self._jwks: tuple[float, Any] | None = None
        self._jwks_lock = threading.Lock()

    def _keys(self, kid: str | None) -> Any:
        """The issuer's signing keys (its JWKS), fetched again (at most once a minute) for a key not seen yet."""
        from joserfc.jwk import KeySet

        with self._jwks_lock:
            cached = self._jwks
            known = cached is not None and any(key.get("kid") == kid for key in cached[1].as_dict()["keys"])
            if cached is None or (not known and time.monotonic() - cached[0] > JWKS_REFRESH_SECONDS):
                response = self.client.get(self.settings.oidc_issuer + "/.well-known/jwks.json")
                response.raise_for_status()
                self._jwks = (time.monotonic(), KeySet.import_key_set(response.json()))
            return self._jwks[1]

    def verify_jwt(self, token: str) -> tuple[bool, str | None]:
        """(decided, username): a signed access token checked here. decided False: couldn't tell (introspect it)."""
        from joserfc import jwt as jose_jwt
        from joserfc.errors import JoseError

        if self.revocations is None or token.count(".") != 2 or not self.revocations.fresh():
            return False, None
        try:
            header = json.loads(base64.urlsafe_b64decode(token.split(".", 1)[0] + "=="))
        except (ValueError, UnicodeDecodeError):
            return True, None
        if not isinstance(header, dict) or header.get("alg") != "RS256" or header.get("typ") != "at+jwt":
            return True, None
        try:
            keys = self._keys(header.get("kid"))
        except (httpx.HTTPError, ValueError):
            return False, None
        try:
            claims = jose_jwt.decode(token, keys, algorithms=["RS256"]).claims
        except (JoseError, ValueError):
            return True, None
        now = time.time()
        audiences = claims.get("aud") if isinstance(claims.get("aud"), list) else [claims.get("aud")]
        user = claims.get("username")
        issued = claims.get("iat")
        if (
            claims.get("iss") != self.settings.oidc_issuer or self.settings.oidc_audience not in audiences
            or not isinstance(claims.get("exp"), (int, float)) or claims["exp"] + JWT_SKEW < now
            or (isinstance(claims.get("nbf"), (int, float)) and claims["nbf"] - JWT_SKEW > now)
            or not isinstance(issued, (int, float)) or "mesh-api" not in str(claims.get("scope", "")).split()
            or not isinstance(user, str) or not isinstance(claims.get("acct"), str)
        ):
            return True, None
        if self.revocations.stopped("family", str(claims.get("fam") or ""), issued) or self.revocations.stopped("client", str(claims.get("client_id") or ""), issued):
            return True, None
        # Bound to the account as it was: a new password (or a removed account) ends every token at once.
        current = self.account_version(user)
        if not current or not hmac.compare_digest(current, claims["acct"]):
            return True, None
        return True, user

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
            return Identity(user, via, record["role"], tuple(record["teams"]), record["tenant"])
        if environment and user == environment[0]:
            # The account from the environment manages the server: a site admin.
            return Identity(user, via, "admin", (), ROOT_TENANT)
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

    def setup_codes(self, connection: HTTPConnection, form: dict[str, str]) -> httpx.Response:
        """Create, list or revoke the signed-in person's setup codes at the issuer (its /setup-codes), as
        device_decision does: their console access token, sent as the introspection client."""
        token, _ = self.request_token(connection)
        if not self.settings or not self.settings.oidc_introspection_secret:
            raise AuthUnavailable("OIDC introspection credentials are not configured")
        try:
            return self.client.post(
                self.settings.oidc_issuer + "/setup-codes", data={"token": token, **form},
                auth=(self.settings.oidc_introspection_client, self.settings.oidc_introspection_secret),
            )
        except httpx.HTTPError as exc:
            raise AuthUnavailable("The authentication service is unavailable") from exc

    def username_of(self, token: str) -> str | None:
        """Whose a valid access token is (the console's sign-ins name their accounts by it)."""
        identity = self.identify_token(token)
        return identity.user if identity else None

    def identify_token(self, token: str, cached: bool = False) -> Identity | None:
        """The identity of an access token given on its own, as identify reads one from a request."""
        class Carrier:
            headers = {HEADER: token}
            cookies: dict[str, str] = {}

        return self.identify(Carrier(), cached=cached)  # type: ignore[arg-type]

    def expires_at(self, connection: HTTPConnection) -> float | None:
        """When the access token a request carries expires, after identify accepted it: the issuer's answer,
        else a signed token's own exp. Advisory only (clients time their renewal by it); never a check."""
        token, _ = self.request_token(connection)
        if not token:
            return None
        confirmed = self._confirmed.get(_digest(token))
        if confirmed:
            return confirmed[2]
        if token.count(".") != 2:
            return None
        try:
            claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
        except (ValueError, UnicodeDecodeError):
            return None
        expires = claims.get("exp") if isinstance(claims, dict) else None
        return float(expires) if isinstance(expires, (int, float)) and math.isfinite(expires) else None

    def recheck(self, connection: HTTPConnection) -> Identity | None:
        """identify, for a connection already open: the issuer's answer may be up to INTROSPECTION_CACHE_SECONDS old.
        A socket that renewed its token over itself (renew_socket) is checked with the newest one."""
        renewed = getattr(connection, "scope", {}).get("state", {}).get(RENEWED_TOKEN)
        if renewed:
            return self.identify_token(renewed, cached=True)
        return self.identify(connection, cached=True)

    def identify(self, connection: HTTPConnection, cached: bool = False) -> Identity | None:
        token, via = self.request_token(connection)
        if not token or len(token) > 8192:
            return None
        if not self.settings or not self.settings.oidc_introspection_secret:
            raise AuthUnavailable("OIDC introspection credentials are not configured")
        decided, user = self.verify_jwt(token)
        if decided:
            # Signed, checked here; permissions are still read fresh (identity_for), as for introspected ones.
            return self.identity_for(user, via) if user else None
        key = _digest(token)
        now = time.time()
        confirmed = self._confirmed.get(key)
        # Only rechecks of open connections reuse an answer: a new request always asks, so a sign-out applies at once.
        if cached and confirmed and now - confirmed[0] < INTROSPECTION_CACHE_SECONDS and confirmed[2] > now:
            # Permissions are read fresh every time; only the issuer's answer about the token is reused.
            return self.identity_for(confirmed[1], via)
        try:
            response = self.client.post(
                self.settings.oidc_issuer + "/introspect", data={"token": token},
                auth=(self.settings.oidc_introspection_client, self.settings.oidc_introspection_secret),
            )
            response.raise_for_status()
            claims = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logging.getLogger(__name__).warning("OIDC introspection is unavailable (%s)", type(exc).__name__)
            if confirmed and confirmed[2] > now:
                # A token the issuer confirmed, still within its own lifetime: one slow or failed answer
                # mustn't close every open connection at once. A new or expired token is still refused.
                return self.identity_for(confirmed[1], via)
            raise AuthUnavailable("The authentication service is unavailable") from exc
        if not isinstance(claims, dict) or claims.get("active") is not True:
            self._confirmed.pop(key, None)
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
            self._confirmed.pop(key, None)
            return None
        if len(self._confirmed) > 4096:
            for stale in [item for item, (_, _, expires) in list(self._confirmed.items()) if expires <= now]:
                self._confirmed.pop(stale, None)
        self._confirmed[key] = (now, claims["username"], float(expires))
        return self.identity_for(claims["username"], via)

    def check_password(self, user: str, password: str) -> bool:
        user = user.strip().lower()
        environment = self.environment_user
        if environment and hmac.compare_digest(user.encode(), environment[0].encode()):
            return hmac.compare_digest(password.encode(), environment[1].encode())
        return self.store.verify(user, password)


# Reachable without signing in: health checks, signing in itself, and the console's files
# (the app shows its sign-in page). Everything under the protected prefixes needs a credential.
# /api/auth/accounts and /api/auth/switch read and change only the browser's own sign-ins (its mesh_accounts cookie),
# which work when the active account's access has expired.
PUBLIC_PATHS = {
    "/health", "/api/auth/status", "/api/auth/login", "/api/auth/callback", "/api/auth/refresh", "/api/auth/logout",
    "/api/auth/accounts", "/api/auth/switch",
}
CREDENTIAL_CHECK_INTERVAL = 15
# Where a WebSocket's scope keeps the access token it renewed over itself (renew_socket).
RENEWED_TOKEN = "renewed_token"
PROTECTED_PREFIXES = ("/api/", "/v1/", "/anthropic/", "/session/", "/docs", "/redoc", "/openapi.json")
# Unscoped agent endpoints that may name their session in X-AI-Proxy-Session instead of the path.
AGENT_PREFIXES = ("/v1/", "/anthropic/")
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# The server itself (its settings, agents, model tuning, restarts, its computer's load) and which
# organizations exist: only site admins (admins of the root organization).
SITE_ADMIN_ONLY = [
    (re.compile(r"GET|PUT"), re.compile(r"/api/config")),
    # The stored settings themselves: agents, model tuning, retention, export and import.
    (re.compile(r"GET|POST|PUT|DELETE"), re.compile(r"/api/config/.+")),
    (re.compile(r"POST"), re.compile(r"/api/server/restart")),
    (re.compile(r"GET"), re.compile(r"/api/server/stats")),
    # Every request the server received, anyone's: addresses, users, what was refused.
    (re.compile(r"GET"), re.compile(r"/api/server/access-log(/.*)?")),
    (re.compile(r"POST"), re.compile(r"/api/orgs")),
    (re.compile(r"DELETE"), re.compile(r"/api/orgs/[^/]+")),
    # Signing the audit log's chains now.
    (re.compile(r"POST"), re.compile(r"/api/audit/checkpoint")),
]
# Changes inside an organization: its admins (and site admins, for the users and teams of any).
# The routes check which organization: these only need an admin of some organization.
ADMIN_ONLY = [
    *SITE_ADMIN_ONLY,
    (re.compile(r"POST"), re.compile(r"/api/containers")),
    (re.compile(r"PUT|DELETE"), re.compile(r"/api/containers/[^/]+")),
    (re.compile(r"POST"), re.compile(r"/api/machines/[^/]+/providers/adopt")),
    (re.compile(r"POST|PUT|DELETE"), re.compile(r"/api/orgs(/.*)?")),
    (re.compile(r"POST|PUT|DELETE"), re.compile(r"/api/users(/.*)?")),
    # Legal holds (retention.py): the routes check which organization.
    (re.compile(r"POST|DELETE"), re.compile(r"/api/holds(/.*)?")),
]


def admin_only(method: str, path: str) -> bool:
    return any(methods.fullmatch(method) and paths.fullmatch(path) for methods, paths in ADMIN_ONLY)


def site_admin_only(method: str, path: str) -> bool:
    return any(methods.fullmatch(method) and paths.fullmatch(path) for methods, paths in SITE_ADMIN_ONLY)


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
        # Known from here, refused or not: the audit log (outside this middleware) records who was refused.
        scope.setdefault("state", {})["identity"] = identity
        if scope["type"] == "http" and site_admin_only(method, path) and not identity.site_admin:
            await _deny(scope, receive, send, 403, "Only a site admin can do this.")
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
                # A socket that renewed its token over itself (renew_socket) is checked with the newest one.
                renewed = scope["state"].get(RENEWED_TOKEN)
                try:
                    if renewed:
                        current = await asyncio.to_thread(self.runtime.auth.identify_token, renewed)
                    else:
                        current = await asyncio.to_thread(self.runtime.auth.recheck, connection)
                except AuthUnavailable:
                    renewal_required = True
                    group.cancel_scope.cancel()
                    return
                if current is None or current.user != identity.user or current.tenant != identity.tenant:
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


async def renew_socket(runtime: Any, websocket: Any, token: Any) -> bool:
    """Takes a fresh access token sent over an open WebSocket, so the socket outlives the one it opened with.

    Access tokens last minutes and AuthMiddleware closes a socket (4410) once its token stops
    being valid. A long-lived socket (a machine's fleet connection) sends its next token before
    that instead, and keeps going. The new token must be valid and the same user's, in the same
    tenant; anything else is ignored, and the old token's expiry closes the socket as before.
    """
    current = request_identity(websocket)
    if not isinstance(token, str) or not token or len(token) > 8192 or current is None:
        return False
    try:
        identity = await asyncio.to_thread(runtime.auth.identify_token, token)
    except AuthUnavailable:
        return False
    if identity is None or identity.user != current.user or identity.tenant != current.tenant:
        return False
    state = websocket.scope.setdefault("state", {})
    state[RENEWED_TOKEN] = token
    state["identity"] = identity
    return True


def request_identity(request: Any) -> Identity | None:
    """The identity attached by middleware; never None inside a protected handler."""
    return request.scope.get("state", {}).get("identity")


class AuthUnavailable(RuntimeError):
    """Authentication infrastructure failed; never interpret this as local access."""
