"""Accounts, for running the proxy as a server that other machines and people use.

With no account, the proxy works as before: a tool on your own computer, where
this computer is trusted and phones pair with the access key. Once an account
exists (`agent-server user add <name>`, or AI_PROXY_USERNAME and AI_PROXY_PASSWORD),
everyone signs in:

  users          the people who sign in; a user can be in several organizations
  organizations  groups of users, each with its own teams
  teams          groups of an organization's users, named <org>/<team>; a user can
                 be in several, and only in teams of their organizations
  roles          admin (manages the server, organizations and users) or member (uses it)
  sessions       owned by whoever started them, shared privately, with a team or
                 with one of the owner's organizations (see access.py)

How requests prove who they are:

  console and management API   a browser sign-in (cookie) or a launcher token
                               (X-AI-Proxy-Token, from `agent login`)
  /session/<id>/...            the open session's id is the credential, so agents
                               keep their own Authorization headers for hosted APIs

Users, organizations, browser sign-ins and launcher tokens live in users.json next
to the access key, with only hashes of passwords and tokens. A file from before
organizations (one organization, the server, with its teams) loads as a single
organization holding those teams and every user.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from starlette.requests import HTTPConnection

from .remote import same_origin


COOKIE = "ai_proxy_session"
HEADER = "x-ai-proxy-token"
SESSION_DAYS = 30
# scrypt cost: tens of milliseconds per check, so guessing is slow but signing in isn't.
SCRYPT = {"n": 2**14, "r": 8, "p": 1}
FAILURE_WINDOW = 600
FAILURE_LIMIT = 10
TOKEN_PREFIX = "aip_"
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
    """users.json: users, organizations and their teams, browser sign-ins and launcher tokens."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] | None = None
        self._loaded_mtime = 0.0

    # Storage

    def _load(self) -> dict[str, Any]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = 0.0
        # Reload when `agent-server user` changed the file while the proxy runs.
        if self._data is None or mtime != self._loaded_mtime:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            data = data if isinstance(data, dict) else {}
            if "orgs" not in data:
                data = _migrated(data)
            for key in ("users", "orgs", "sessions", "tokens"):
                data.setdefault(key, {})
            self._data, self._loaded_mtime = data, mtime
        return self._data

    def _save(self) -> None:
        assert self._data is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(self._data, handle, indent=2)
        temporary.replace(self.path)
        self._loaded_mtime = self.path.stat().st_mtime

    # Organizations and their teams

    def orgs(self, viewer: Identity | None = None) -> list[dict[str, Any]]:
        """Every organization, or with a member's identity only theirs (admins see them all)."""
        with self._lock:
            data = self._load()
            mine = None if viewer is None or viewer.admin else set(viewer.orgs)
            return [self._org_public(org) for org in sorted(data["orgs"]) if mine is None or org in mine]

    def org(self, org: str) -> dict[str, Any] | None:
        with self._lock:
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
        with self._lock:
            orgs = self._load()["orgs"]
            if org in orgs:
                raise ValueError(f"There's already an organization called {org}")
            orgs[org] = {"name": (name or org).strip()[:80] or org, "created_at": time.time(), "teams": {}}
            self._save()
        return org

    def rename_org(self, org: str, name: str) -> None:
        with self._lock:
            record = self._load()["orgs"].get(org)
            if record is None:
                raise KeyError(org)
            record["name"] = name.strip()[:80] or org
            self._save()

    def remove_org(self, org: str) -> None:
        """Delete an organization: its teams go, and its members leave it (their accounts stay)."""
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            data = self._load()
            if data["orgs"].get(org, {}).get("teams", {}).pop(team, None) is None:
                raise KeyError(team)
            for record in data["users"].values():
                record["teams"] = [item for item in record.get("teams", []) if item != team_key(org, team)]
            self._save()

    def rename_team(self, org: str, team: str, name: str) -> None:
        """Change a team's display name without changing its sharing key."""
        with self._lock:
            record = self._load()["orgs"].get(org, {}).get("teams", {}).get(team)
            if record is None:
                raise KeyError(team)
            record["name"] = name.strip()[:80] or team
            self._save()

    def set_membership(self, org: str, name: str, member: bool, *, team: str | None = None) -> None:
        """Change one membership under the lock, preserving memberships outside this scope."""
        with self._lock:
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
        with self._lock:
            return sorted(self._load()["users"])

    def user(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            return self._user_public(name) if name in self._load()["users"] else None

    def user_list(self, viewer: Identity | None = None) -> list[dict[str, Any]]:
        """Every user, or for a member those who share an organization with them (and themselves)."""
        with self._lock:
            users = [self._user_public(name) for name in sorted(self._load()["users"])]
        if viewer is None or viewer.admin:
            return users
        mine = set(viewer.orgs)
        return [user for user in users if user["name"] == viewer.user or mine & set(user["orgs"])]

    def add_user(self, name: str, password: str, role: str | None = None, teams: list[str] | None = None, orgs: list[str] | None = None) -> dict[str, Any]:
        """A new user. The first user is an admin, so someone can manage the rest."""
        name = valid_name(name, "user")
        with self._lock:
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
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            record = self._load()["users"].get(name)
        if record is None:
            # Same work either way, so timing doesn't reveal which users exist.
            check_password(password, hash_password("timing-equaliser"))
            return False
        return check_password(password, str(record.get("password") or ""))

    # Browser sign-ins

    def start_session(self, name: str, agent: str) -> str:
        secret = secrets.token_urlsafe(32)
        with self._lock:
            data = self._load()
            now = time.time()
            data["sessions"] = {key: value for key, value in data["sessions"].items() if value.get("expires_at", 0) > now}
            data["sessions"][_digest(secret)] = {"user": name, "created_at": now, "expires_at": now + SESSION_DAYS * 86400, "agent": agent[:200]}
            self._save()
        return secret

    def end_session(self, secret: str) -> None:
        with self._lock:
            if self._load()["sessions"].pop(_digest(secret), None) is not None:
                self._save()

    def session_user(self, secret: str) -> str | None:
        with self._lock:
            record = self._load()["sessions"].get(_digest(secret))
            if not record or record.get("expires_at", 0) < time.time():
                return None
            return str(record["user"])

    # Launcher tokens

    def create_token(self, name: str, label: str) -> tuple[str, str]:
        """A launcher token: returns (its id, the token itself, shown once)."""
        token_id = secrets.token_hex(4)
        secret = TOKEN_PREFIX + secrets.token_urlsafe(32)
        with self._lock:
            self._load()["tokens"][token_id] = {
                "user": name, "label": label.strip()[:80] or "launcher", "hash": _digest(secret),
                "created_at": time.time(), "last_used_at": None,
            }
            self._save()
        return token_id, secret

    def tokens(self, name: str) -> list[dict[str, Any]]:
        with self._lock:
            tokens = self._load()["tokens"]
            return [
                {"id": token_id, "label": value["label"], "created_at": value["created_at"], "last_used_at": value.get("last_used_at")}
                for token_id, value in sorted(tokens.items(), key=lambda item: item[1]["created_at"]) if value.get("user") == name
            ]

    def revoke_token(self, name: str, token_id: str) -> bool:
        with self._lock:
            tokens = self._load()["tokens"]
            if tokens.get(token_id, {}).get("user") != name:
                return False
            del tokens[token_id]
            self._save()
            return True

    def token_user(self, secret: str) -> str | None:
        if not secret.startswith(TOKEN_PREFIX):
            return None
        digest = _digest(secret)
        with self._lock:
            for value in self._load()["tokens"].values():
                if hmac.compare_digest(value.get("hash", ""), digest):
                    # Recorded at most once a minute, to keep writes rare.
                    if not value.get("last_used_at") or time.time() - value["last_used_at"] > 60:
                        value["last_used_at"] = time.time()
                        self._save()
                    return str(value["user"])
        return None


def _checked_password(password: str) -> str:
    if len(password) < 8:
        raise ValueError("Use a password of at least 8 characters")
    return password


class Auth:
    """Whether accounts are on, who a request comes from, and the sign-in attempt limit."""

    def __init__(self, store: UserStore, access_key: Any = None):
        self.store = store
        self.access_key = access_key
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    @property
    def environment_user(self) -> tuple[str, str] | None:
        user, password = os.getenv("AI_PROXY_USERNAME"), os.getenv("AI_PROXY_PASSWORD")
        return (user.strip().lower(), password) if user and password else None

    @property
    def enabled(self) -> bool:
        return self.environment_user is not None or bool(self.store.users())

    def identity_for(self, user: str, via: str) -> Identity | None:
        environment = self.environment_user
        record = self.store.user(user)
        if record:
            return Identity(user, via, record["role"], tuple(record["teams"]), tuple(record["orgs"]))
        if environment and user == environment[0]:
            # The account from the environment manages the server.
            return Identity(user, via, "admin", ())
        return None

    def identify(self, connection: HTTPConnection) -> Identity | None:
        cookie = connection.cookies.get(COOKIE)
        if cookie:
            user = self.store.session_user(cookie)
            identity = self.identity_for(user, "cookie") if user else None
            if identity:
                return identity
        token = connection.headers.get(HEADER, "")
        if token:
            user = self.store.token_user(token)
            if user:
                return self.identity_for(user, "token")
        return None

    def check_password(self, user: str, password: str) -> bool:
        user = user.strip().lower()
        environment = self.environment_user
        if environment and hmac.compare_digest(user.encode(), environment[0].encode()):
            return hmac.compare_digest(password.encode(), environment[1].encode())
        return self.store.verify(user, password)

    def allowed_attempt(self, client: str) -> bool:
        attempts = self._failures[client]
        now = time.time()
        while attempts and now - attempts[0] > FAILURE_WINDOW:
            attempts.popleft()
        return len(attempts) < FAILURE_LIMIT

    def failed(self, client: str) -> None:
        self._failures[client].append(time.time())

    def succeeded(self, client: str) -> None:
        self._failures.pop(client, None)


# Reachable without signing in: health checks, signing in itself, and the console's files
# (the app shows its sign-in page). Everything under the protected prefixes needs a credential.
PUBLIC_PATHS = {"/health", "/api/auth/status", "/api/auth/login", "/api/auth/token", "/api/auth/logout"}
PROTECTED_PREFIXES = ("/api/", "/v1/", "/anthropic/", "/session/", "/docs", "/redoc", "/openapi.json")
# Unscoped agent endpoints that may name their session in X-AI-Proxy-Session instead of the path.
AGENT_PREFIXES = ("/v1/", "/anthropic/")
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Changes to the server itself: only admins make them. Members use the server.
ADMIN_ONLY = [
    (re.compile(r"PUT"), re.compile(r"/api/config")),
    (re.compile(r"POST"), re.compile(r"/api/containers")),
    (re.compile(r"PUT|DELETE"), re.compile(r"/api/containers/[^/]+")),
    (re.compile(r"POST"), re.compile(r"/api/remote/rotate")),
    (re.compile(r"POST"), re.compile(r"/api/server/restart")),
    (re.compile(r"POST"), re.compile(r"/api/machines/[^/]+/providers/adopt")),
    (re.compile(r"POST|PUT|DELETE"), re.compile(r"/api/orgs(/.*)?")),
    (re.compile(r"POST|PUT|DELETE"), re.compile(r"/api/users(/.*)?")),
]


def admin_only(method: str, path: str) -> bool:
    return any(methods.fullmatch(method) and paths.fullmatch(path) for methods, paths in ADMIN_ONLY)


class AuthMiddleware:
    """Once accounts exist: a signed-in user for the console and API, an open session for agents."""

    def __init__(self, app: Any, runtime: Any):
        self.app = app
        self.runtime = runtime

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket") or not self.runtime.auth.enabled:
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        connection = HTTPConnection(scope)
        if not path.startswith(PROTECTED_PREFIXES) or self._session_capability(path, connection):
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET")
        if scope["type"] == "http" and method in UNSAFE_METHODS and not same_origin(connection):
            await _deny(scope, receive, send, 403, "Requests from other sites aren't allowed.")
            return
        if path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        identity = self.runtime.auth.identify(connection)
        if identity is None:
            await _deny(scope, receive, send, 401, "Sign in first.")
            return
        if scope["type"] == "http" and admin_only(method, path) and not identity.admin:
            await _deny(scope, receive, send, 403, "Only an admin can change this.")
            return
        scope.setdefault("state", {})["identity"] = identity
        await self.app(scope, receive, send)

    def _session_capability(self, path: str, connection: HTTPConnection) -> bool:
        """An agent's request in an open session: the session id, from the launcher, is its credential."""
        session_id = None
        if path.startswith("/session/"):
            session_id = path.split("/", 3)[2]
        elif path.startswith(AGENT_PREFIXES):
            session_id = connection.headers.get("x-ai-proxy-session")
        if not session_id:
            return False
        session = self.runtime.sessions.get(session_id)
        return session is not None and session.ended_at is None


async def _deny(scope: dict[str, Any], receive: Any, send: Any, status: int, message: str) -> None:
    if scope["type"] == "websocket":
        await receive()  # websocket.connect
        await send({"type": "websocket.close", "code": 4400 + status % 100, "reason": message})
        return
    body = json.dumps({"detail": message}).encode()
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def request_identity(request: Any) -> Identity | None:
    """The identity the middleware attached; None when accounts are off."""
    return request.scope.get("state", {}).get("identity")
