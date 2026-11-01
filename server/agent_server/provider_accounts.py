"""Provider accounts: people's own AI subscriptions (Claude, ChatGPT through Codex, GitHub Copilot).

A provider account is one user's subscription with an outside provider, apart from the
platform accounts (auth.py) that sign people and machines in to this server. It belongs to
one user and one tenant, and only reaches the machines its owner assigns it to.

How a subscription signs in is the provider's business, so there are two modes:

- runner: the provider's own CLI is signed in on the machine (`claude`, `codex login`,
  `copilot /login`), and stays signed in there. The server only knows that the machine says
  it is (agent-machine reports what `agent accounts detect` finds, never a token), and sends
  the account's work to those machines. Claude Code is always this: its subscription sign-in
  is never collected, stored, copied or relayed by the server.
- token: a credential the provider documents for headless use (Copilot CLI's fine-grained
  personal access token with the Copilot Requests permission, in COPILOT_GITHUB_TOKEN) is
  kept here encrypted (Vault, AES-256-GCM under a master key the database never holds) and
  handed out only to a launch the server itself started on a machine the owner assigned it
  to, once (Grants, POST /api/sessions/{id}/provider-credential). A machine holding one is
  credential-bearing: whoever controls it can use the account.

Usage: what went through this server for each account is counted by the hour
(provider_account_usage) from the recorded requests of its sessions, and 429 answers from the
provider's own hosts start a cooldown (short Retry-After) or mark the window exhausted (long
one). None of that is the provider's official meter: every figure says where it came from
(source) and how far to trust it (confidence), and a remaining balance exists only against a
limit the owner declared, as an estimate. Nothing here calls a provider's private API or reads
credentials from traffic.

Who may do what (the authorization matrix):

| Who | See it | Launch with it | Change, assign, store or revoke its token | Turn it off |
| --- | --- | --- | --- | --- |
| Its owner | yes | yes | yes | yes |
| A user its owner named (users) | yes | yes | no | no |
| An admin of its tenant | metadata only | no | no | yes |
| A site admin | metadata only | no | no | yes |
| An assigned machine | no | runs launches the server sends | gets the token once per such launch | no |
| Anyone else, or another tenant | no (404) | no | no | no |

Nobody, site admins included, can read a stored token back through the API.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select

from .auth import Identity, same_tenant
from .db import Database, kv, schema

LOGGER = logging.getLogger("agent_server.provider_accounts")

# What each kind of subscription is, how it can sign in, and where its model traffic goes (for
# telling the provider's own limits apart from a telemetry host's 429).
PROVIDERS: dict[str, dict[str, Any]] = {
    "claude": {
        "label": "Claude (Claude Code)",
        "cli": "claude",
        "modes": ["runner"],
        "plans": ["Pro", "Max 5x", "Max 20x", "Team", "Enterprise"],
        "windows": ["5h", "7d"],
        "hosts": ["api.anthropic.com", "claude.ai"],
        "sign_in": "Run `claude` on the machine and sign in with your Claude account. The sign-in stays on that machine.",
        "note": "Claude Code's subscription sign-in is never copied to or through the server: each machine signs in itself.",
    },
    "codex": {
        "label": "ChatGPT (Codex CLI)",
        "cli": "codex",
        "modes": ["runner"],
        "plans": ["Plus", "Pro", "Business", "Enterprise", "Edu"],
        "windows": ["5h", "7d"],
        "hosts": ["chatgpt.com", "api.openai.com", "auth.openai.com"],
        "sign_in": "Run `codex login` on the machine (`codex login --device-auth` without a browser) and sign in with ChatGPT.",
        "note": "Each machine keeps its own Codex sign-in. The server doesn't copy Codex's auth.json between machines.",
    },
    "copilot": {
        "label": "GitHub Copilot (Copilot CLI)",
        "cli": "copilot",
        "modes": ["runner", "token"],
        "plans": ["Free", "Pro", "Pro+", "Business", "Enterprise"],
        "windows": ["month"],
        "hosts": ["githubcopilot.com", "api.github.com", "github.com"],
        "credential_env": "COPILOT_GITHUB_TOKEN",
        "sign_in": "Run `copilot` on the machine and sign in with /login, or store a fine-grained personal access token with the Copilot Requests permission.",
        "note": "A stored token reaches only the machines you assign it to, as COPILOT_GITHUB_TOKEN, for launches from this server.",
    },
}
MODES = {
    "runner": "Signed in on its machines",
    "token": "Managed token, provisioned to its machines",
}
# Availability, worst last. Launches are refused in the BLOCKING ones; the scheduler also prefers lower ranks.
STATES = ("available", "near_limit", "unknown", "rate_limited", "quota_exhausted", "auth_expired", "offline")
BLOCKING = frozenset({"rate_limited", "quota_exhausted", "auth_expired", "offline"})
# Usage windows: rolling ones in seconds, and the calendar month (UTC), Copilot's.
WINDOWS: dict[str, int | None] = {"5h": 5 * 3600, "24h": 24 * 3600, "7d": 7 * 24 * 3600, "30d": 30 * 24 * 3600, "month": None}
LIMIT_UNITS = ("requests", "tokens")
WARNING_PERCENT = 75
STOP_PERCENT = 90
# A 429 without Retry-After cools the account down this long; one asking for at least
# QUOTA_RETRY means the subscription's window is used up, not a short throttle.
DEFAULT_COOLDOWN = 60.0
QUOTA_RETRY = 15 * 60.0
LONGEST_COOLDOWN = 8 * 24 * 3600.0
# How long a launch's one-time credential grant waits for its session.
GRANT_TTL = 120.0
# Hourly usage buckets are kept this long.
USAGE_DAYS = 62
ACCOUNT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
CREDENTIAL_LIMIT = 4096
# A fine-grained personal access token (github_pat_...), or a GitHub OAuth token from a supported login.
# Classic tokens (ghp_) don't work with Copilot CLI.
COPILOT_TOKEN = re.compile(r"(github_pat_[A-Za-z0-9_]{20,250}|gho_[A-Za-z0-9]{20,250}|ghu_[A-Za-z0-9]{20,250})")


class AccountError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


class VaultError(Exception):
    pass


class Vault:
    """Managed provider credentials, encrypted with AES-256-GCM.

    The master keys come from the environment, never the database: AI_PROXY_VAULT_KEYS (or the
    file AI_PROXY_VAULT_KEYS_FILE names, for container secrets), a comma-separated list of
    <version>:<32 bytes, base64>. The highest version encrypts; every listed one decrypts, so a
    key is rotated by adding a newer one, running `agent-server vault rotate`, then dropping the
    old one. Each ciphertext is bound to its account (associated data), so a row copied to
    another account doesn't decrypt. Without keys, managed credentials are refused (fail closed).
    """

    def __init__(self, keys: dict[int, bytes] | None = None):
        self.keys = dict(keys or {})
        for version, key in self.keys.items():
            if version < 1 or len(key) != 32:
                raise VaultError(f"Vault key {version} must be 32 bytes")

    @classmethod
    def from_environment(cls) -> "Vault":
        text = os.environ.get("AI_PROXY_VAULT_KEYS", "")
        path = os.environ.get("AI_PROXY_VAULT_KEYS_FILE", "")
        if not text and path:
            text = Path(path).read_text(encoding="utf-8")
        return cls(parse_keys(text))

    @property
    def configured(self) -> bool:
        return bool(self.keys)

    @property
    def current(self) -> int:
        return max(self.keys) if self.keys else 0

    def encrypt(self, plaintext: str, aad: bytes) -> tuple[int, bytes, bytes]:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        if not self.keys:
            raise VaultError("Managed credentials need the server's vault key: set AI_PROXY_VAULT_KEYS (agent-server vault key makes one)")
        nonce = secrets.token_bytes(12)
        return self.current, nonce, AESGCM(self.keys[self.current]).encrypt(nonce, plaintext.encode(), aad)

    def decrypt(self, version: int, nonce: bytes, ciphertext: bytes, aad: bytes) -> str:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = self.keys.get(int(version))
        if key is None:
            raise VaultError(f"The vault key {version} this credential was stored with isn't configured (AI_PROXY_VAULT_KEYS)")
        try:
            return AESGCM(key).decrypt(bytes(nonce), bytes(ciphertext), aad).decode()
        except InvalidTag as exc:
            raise VaultError("The stored credential doesn't decrypt with the configured vault key") from exc


def parse_keys(text: str) -> dict[int, bytes]:
    keys: dict[int, bytes] = {}
    for item in re.split(r"[,\s]+", text.strip()):
        if not item:
            continue
        version, _, value = item.partition(":")
        if not version.isdigit() or not value:
            raise VaultError("AI_PROXY_VAULT_KEYS is <version>:<base64 key>, comma-separated")
        try:
            keys[int(version)] = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except ValueError as exc:
            raise VaultError(f"Vault key {version} isn't base64") from exc
    return keys


def new_key(version: int = 1) -> str:
    return f"{version}:{base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip('=')}"


@dataclass
class ProviderAccount:
    id: str
    tenant: str
    owner: str
    provider: str
    display_name: str
    authentication_mode: str = "runner"
    plan_name: str = ""
    # active, or disabled (turned off by its owner or an admin: no new launches).
    status: str = "active"
    disabled_by: str | None = None
    # Lower first, when the scheduler chooses between eligible accounts.
    priority: int = 100
    # Others of its tenant its owner lets launch with it (they never see or get its credential).
    users: list[str] = field(default_factory=list)
    # A limit its owner declared ({window, unit, value}), the only base for a remaining balance.
    limit: dict[str, Any] | None = None
    warning_percent: int = WARNING_PERCENT
    stop_percent: int = STOP_PERCENT
    created_at: float = 0.0
    updated_at: float = 0.0
    last_validated_at: float | None = None
    last_success_at: float | None = None
    expires_at: float | None = None
    # What was observed: cooldowns from 429s, when the window was used up. Never a secret.
    observed: dict[str, Any] = field(default_factory=dict)
    # About the stored credential, not the credential: when it was set and which key encrypts it.
    credential: dict[str, Any] | None = None

    def stored(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if key not in {"id", "tenant", "owner", "provider"}}

    def aad(self) -> bytes:
        return f"{self.tenant}\x00{self.owner}\x00{self.id}\x00{self.provider}".encode()


@dataclass
class Assignment:
    account_id: str
    machine_id: str
    assigned_by: str
    assigned_at: float
    enabled: bool = True
    # A managed token may be handed to launches on this machine (token accounts only).
    provisioning_allowed: bool = True
    # console, or enrollment (agent accounts enroll on the machine).
    source: str = "console"

    def stored(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if key not in {"account_id", "machine_id"}}


@dataclass
class Grant:
    """One launch's permission to use an account on one machine, made when the server sends the launch."""
    launch_id: str
    account_id: str
    machine_id: str
    requested_by: str
    expires: float
    delivered: bool = False


class ProviderAccounts:
    """Every provider account, its assignments and its launch grants; usage and events read from the database."""

    def __init__(self, db: Database, fleet: Any, vault: Vault | None = None, clock: Callable[[], float] = time.time):
        self.db = db
        self.fleet = fleet
        self.vault = vault or Vault()
        self.clock = clock
        self.accounts: dict[str, ProviderAccount] = {}
        self.assignments: dict[tuple[str, str], Assignment] = {}
        self.grants: dict[str, Grant] = {}
        self._lock = threading.Lock()
        self._saved_success: dict[str, float] = {}
        self._load()

    # Storage

    def _load(self) -> None:
        with self.db.read() as connection:
            accounts = connection.execute(select(schema.provider_accounts)).mappings().all()
            assignments = connection.execute(select(schema.provider_account_assignments)).mappings().all()
        for row in accounts:
            data = row["data"] if isinstance(row["data"], dict) else {}
            known = {key: value for key, value in data.items() if key in ProviderAccount.__dataclass_fields__}
            self.accounts[row["id"]] = ProviderAccount(id=row["id"], tenant=row["tenant"], owner=row["owner"], provider=row["provider"], **known)
        for row in assignments:
            data = row["data"] if isinstance(row["data"], dict) else {}
            known = {key: value for key, value in data.items() if key in Assignment.__dataclass_fields__}
            self.assignments[(row["account_id"], row["machine_id"])] = Assignment(account_id=row["account_id"], machine_id=row["machine_id"], **known)

    def _save(self, account: ProviderAccount) -> None:
        values = {"id": account.id, "tenant": account.tenant, "owner": account.owner, "provider": account.provider, "data": account.stored()}
        self.db.writer.submit(lambda connection: kv.upsert(connection, schema.provider_accounts, values))

    def _save_assignment(self, assignment: Assignment) -> None:
        values = {"account_id": assignment.account_id, "machine_id": assignment.machine_id, "data": assignment.stored()}
        self.db.writer.submit(lambda connection: kv.upsert(connection, schema.provider_account_assignments, values))

    def event(self, account: ProviderAccount, kind: str, identity: Identity | None = None, **detail: Any) -> None:
        """The audit trail: what happened to an account, by whom. Callers never pass secrets."""
        values = {
            "id": secrets.token_hex(16), "account_id": account.id, "at": self.clock(),
            "data": {"kind": kind, "by": identity.user if identity else None, **detail},
        }
        LOGGER.info("provider account %s (%s, %s): %s by %s %s", account.id, account.provider, account.owner, kind, values["data"]["by"], detail or "")
        self.db.writer.submit(lambda connection: connection.execute(schema.provider_account_events.insert().values(**values)))

    def events(self, account: ProviderAccount, limit: int = 50) -> list[dict[str, Any]]:
        self.db.writer.flush()
        table = schema.provider_account_events
        with self.db.read() as connection:
            rows = connection.execute(
                select(table.c.at, table.c.data).where(table.c.account_id == account.id).order_by(table.c.at.desc()).limit(limit)
            ).all()
        return [{"at": at, **(data if isinstance(data, dict) else {})} for at, data in rows]

    # Who may do what (see the module's table)

    def may_view(self, identity: Identity | None, account: ProviderAccount) -> bool:
        if identity is None or not same_tenant(identity, account.tenant):
            return False
        return self.may_use(identity, account) or identity.admin

    def may_use(self, identity: Identity | None, account: ProviderAccount) -> bool:
        """Launch with it: its owner and the users it names. Not admins: a subscription is personal."""
        if identity is None or identity.tenant != account.tenant:
            return False
        return identity.user == account.owner or identity.user in account.users

    def may_manage(self, identity: Identity | None, account: ProviderAccount) -> bool:
        return identity is not None and identity.tenant == account.tenant and identity.user == account.owner

    def may_suspend(self, identity: Identity | None, account: ProviderAccount) -> bool:
        return self.may_manage(identity, account) or bool(identity and identity.admin and same_tenant(identity, account.tenant))

    def visible(self, account_id: str, identity: Identity | None) -> ProviderAccount:
        account = self.accounts.get(account_id)
        if account is None or not self.may_view(identity, account):
            raise AccountError(404, "No such provider account")
        return account

    def managed(self, account_id: str, identity: Identity | None) -> ProviderAccount:
        account = self.visible(account_id, identity)
        if not self.may_manage(identity, account):
            raise AccountError(403, f"Only {account.owner} can change {account.display_name}")
        return account

    def listed(self, identity: Identity | None, machine_id: str | None = None) -> list[ProviderAccount]:
        found = [account for account in self.accounts.values() if self.may_view(identity, account)]
        if machine_id:
            found = [account for account in found if (account.id, machine_id) in self.assignments]
        return sorted(found, key=lambda item: (item.owner != (identity.user if identity else None), item.priority, item.display_name.lower()))

    # Changes

    def create(self, identity: Identity, data: dict[str, Any]) -> tuple[ProviderAccount, bool]:
        """A new account for identity, or the one it already has with that provider and name (enrollment is idempotent)."""
        provider = str(data.get("provider") or "")
        if provider not in PROVIDERS:
            raise AccountError(400, f"Choose a provider: one of {', '.join(PROVIDERS)}")
        name = _text(data.get("display_name"), 80) or f"{identity.user}'s {PROVIDERS[provider]['label'].split(' (')[0]}"
        mode = str(data.get("authentication_mode") or PROVIDERS[provider]["modes"][0])
        if mode not in PROVIDERS[provider]["modes"]:
            raise AccountError(400, f"{PROVIDERS[provider]['label']} signs in as: {', '.join(PROVIDERS[provider]['modes'])}. {PROVIDERS[provider]['note']}")
        with self._lock:
            for account in self.accounts.values():
                if account.tenant == identity.tenant and account.owner == identity.user and account.provider == provider and account.display_name.lower() == name.lower():
                    return account, False
            base = re.sub(r"[^a-z0-9]+", "-", f"{identity.user}-{provider}-{name}".lower()).strip("-")[:48] or provider
            account_id = base
            while account_id in self.accounts:
                account_id = f"{base}-{secrets.token_hex(2)}"
            now = self.clock()
            account = ProviderAccount(
                id=account_id, tenant=identity.tenant, owner=identity.user, provider=provider, display_name=name,
                authentication_mode=mode, plan_name=_text(data.get("plan_name"), 40) or "", created_at=now, updated_at=now,
            )
            self._apply(account, data, identity)
            self.accounts[account_id] = account
        self._save(account)
        self.event(account, "created", identity, mode=mode)
        return account, True

    def update(self, identity: Identity, account: ProviderAccount, data: dict[str, Any]) -> ProviderAccount:
        if "status" in data:
            if not self.may_suspend(identity, account):
                raise AccountError(403, f"Only {account.owner} or an admin can turn {account.display_name} on or off")
            status = str(data["status"])
            if status not in ("active", "disabled"):
                raise AccountError(400, "status is active or disabled")
            if status != account.status:
                account.status = status
                account.disabled_by = identity.user if status == "disabled" else None
                self.event(account, "disabled" if status == "disabled" else "enabled", identity)
        rest = {key: value for key, value in data.items() if key != "status"}
        if rest:
            if not self.may_manage(identity, account):
                raise AccountError(403, f"Only {account.owner} can change {account.display_name}")
            self._apply(account, rest, identity)
            if rest.get("clear_observations"):
                account.observed = {}
                self.event(account, "observations_cleared", identity)
        account.updated_at = self.clock()
        self._save(account)
        return account

    def _apply(self, account: ProviderAccount, data: dict[str, Any], identity: Identity) -> None:
        if "display_name" in data and _text(data["display_name"], 80):
            account.display_name = _text(data["display_name"], 80) or account.display_name
        if "plan_name" in data:
            account.plan_name = _text(data["plan_name"], 40) or ""
        if "priority" in data:
            try:
                account.priority = max(0, min(10_000, int(data["priority"])))
            except (TypeError, ValueError) as exc:
                raise AccountError(400, "priority is a whole number (lower is tried first)") from exc
        if "users" in data:
            users = data["users"] if isinstance(data["users"], list) else []
            account.users = sorted({str(user)[:40] for user in users if str(user) and str(user) != account.owner})[:50]
        if "limit" in data:
            account.limit = _limit(data["limit"])
        for key in ("warning_percent", "stop_percent"):
            if key in data:
                try:
                    setattr(account, key, max(1, min(100, int(data[key]))))
                except (TypeError, ValueError) as exc:
                    raise AccountError(400, f"{key} is a percentage") from exc
        if account.warning_percent > account.stop_percent:
            raise AccountError(400, "warning_percent can't be above stop_percent")

    def delete(self, identity: Identity, account: ProviderAccount) -> None:
        with self._lock:
            self.accounts.pop(account.id, None)
            for key in [key for key in self.assignments if key[0] == account.id]:
                del self.assignments[key]
            for launch, grant in list(self.grants.items()):
                if grant.account_id == account.id:
                    del self.grants[launch]
        self.event(account, "deleted", identity)

        def write(connection: Any) -> None:
            for table in (schema.provider_accounts, schema.provider_credentials):
                key = table.c.id if table is schema.provider_accounts else table.c.account_id
                connection.execute(table.delete().where(key == account.id))
            for table in (schema.provider_account_assignments, schema.provider_account_usage):
                connection.execute(table.delete().where(table.c.account_id == account.id))

        self.db.writer.submit(write)

    def set_credential(self, identity: Identity, account: ProviderAccount, token: Any) -> None:
        """Store (or replace) a managed token, encrypted. The token is checked for shape, never logged."""
        if account.authentication_mode != "token":
            raise AccountError(409, f"{account.display_name} signs in on its machines; there's no token to store. {PROVIDERS[account.provider]['note']}")
        if not isinstance(token, str) or not token.strip() or len(token) > CREDENTIAL_LIMIT:
            raise AccountError(400, "Send the token as {\"token\": \"...\"}")
        token = token.strip()
        if account.provider == "copilot" and not COPILOT_TOKEN.fullmatch(token):
            raise AccountError(400, "That isn't a GitHub token Copilot CLI takes: use a fine-grained personal access token (github_pat_...) with the Copilot Requests permission. Classic tokens (ghp_...) don't work.")
        try:
            version, nonce, ciphertext = self.vault.encrypt(token, account.aad())
        except VaultError as exc:
            raise AccountError(409, str(exc)) from exc
        now = self.clock()
        row = {"account_id": account.id, "key_version": version, "nonce": nonce, "ciphertext": ciphertext, "created_at": now}
        with self.db.write() as connection:
            kv.upsert(connection, schema.provider_credentials, row)
        account.credential = {"set_at": now, "key_version": version, "kind": "fine-grained token" if token.startswith("github_pat_") else "OAuth token"}
        account.last_validated_at = None
        account.observed = {key: value for key, value in account.observed.items() if key != "auth_failed_at"}
        account.updated_at = now
        self._save(account)
        self.event(account, "credential_stored", identity, key_version=version)

    def revoke(self, identity: Identity, account: ProviderAccount) -> None:
        """Forget the managed token here and stop handing it out. Revoke it at the provider too: the server can't."""
        with self.db.write() as connection:
            connection.execute(schema.provider_credentials.delete().where(schema.provider_credentials.c.account_id == account.id))
        with self._lock:
            for launch, grant in list(self.grants.items()):
                if grant.account_id == account.id:
                    del self.grants[launch]
        account.credential = None
        account.updated_at = self.clock()
        self._save(account)
        self.event(account, "credential_revoked", identity)

    def _credential(self, account: ProviderAccount) -> str:
        table = schema.provider_credentials
        with self.db.read() as connection:
            row = connection.execute(select(table).where(table.c.account_id == account.id)).mappings().first()
        if row is None:
            raise AccountError(409, f"{account.display_name} has no stored token")
        try:
            return self.vault.decrypt(row["key_version"], row["nonce"], row["ciphertext"], account.aad())
        except VaultError as exc:
            raise AccountError(409, str(exc)) from exc

    def rotate(self) -> int:
        """Re-encrypt every stored credential with the newest vault key; returns how many."""
        table = schema.provider_credentials
        done = 0
        with self.db.write() as connection:
            for row in connection.execute(select(table)).mappings().all():
                account = self.accounts.get(row["account_id"])
                if account is None or row["key_version"] == self.vault.current:
                    continue
                plaintext = self.vault.decrypt(row["key_version"], row["nonce"], row["ciphertext"], account.aad())
                version, nonce, ciphertext = self.vault.encrypt(plaintext, account.aad())
                connection.execute(table.update().where(table.c.account_id == account.id).values(key_version=version, nonce=nonce, ciphertext=ciphertext))
                account.credential = {**(account.credential or {}), "key_version": version}
                self._save(account)
                done += 1
        return done

    # Assignments

    def assign(self, identity: Identity, account: ProviderAccount, machine_id: str, data: dict[str, Any] | None = None, source: str = "console") -> Assignment:
        """Let the account run on one of its owner's machines. A machine shared with others still isn't
        theirs to hand a subscription to: only machines the account's owner owns."""
        data = data or {}
        machine = self.fleet.machines.get(machine_id)
        if machine is None or not same_tenant(identity, machine.tenant) or machine.tenant != account.tenant:
            raise AccountError(404, "No such machine")
        if machine.owner != account.owner:
            raise AccountError(403, f"{account.display_name} can only be assigned to {account.owner}'s own machines; {machine.name} is {machine.owner or 'nobody'}'s.")
        key = (account.id, machine.id)
        assignment = self.assignments.get(key) or Assignment(account.id, machine.id, identity.user, self.clock(), source=source)
        if "enabled" in data:
            assignment.enabled = bool(data["enabled"])
        if "provisioning_allowed" in data:
            assignment.provisioning_allowed = bool(data["provisioning_allowed"])
        self.assignments[key] = assignment
        self._save_assignment(assignment)
        self.event(account, "assigned", identity, machine=machine.id, enabled=assignment.enabled, provisioning_allowed=assignment.provisioning_allowed, source=source)
        return assignment

    def unassign(self, identity: Identity, account: ProviderAccount, machine_id: str) -> None:
        if self.assignments.pop((account.id, machine_id), None) is None:
            raise AccountError(404, f"{account.display_name} isn't assigned to that machine")
        with self._lock:
            for launch, grant in list(self.grants.items()):
                if grant.account_id == account.id and grant.machine_id == machine_id:
                    del self.grants[launch]
        table = schema.provider_account_assignments
        self.db.writer.submit(lambda connection: connection.execute(table.delete().where((table.c.account_id == account.id) & (table.c.machine_id == machine_id))))
        self.event(account, "unassigned", identity, machine=machine_id)

    def assigned(self, account: ProviderAccount) -> list[Assignment]:
        return [item for key, item in sorted(self.assignments.items()) if key[0] == account.id]

    def find_machine(self, identity: Identity, machine_id: str | None, hostname: str | None) -> Any:
        """The machine `agent accounts enroll` runs on: named, or the owner's machine with that hostname."""
        if machine_id:
            machine = self.fleet.machines.get(machine_id)
            if machine is None or machine.owner != identity.user or machine.tenant != identity.tenant:
                raise AccountError(404, f"You have no machine {machine_id}")
            return machine
        host = (hostname or "").strip().lower()
        found = [
            machine for machine in self.fleet.machines.values()
            if machine.owner == identity.user and machine.tenant == identity.tenant and host and (machine.hostname or machine.name).lower() == host
        ]
        found.sort(key=lambda machine: (not self.fleet.connected(machine.id), -machine.last_seen))
        if not found:
            raise AccountError(404, f"No machine of yours is called {hostname or '(no hostname)'}: run agent-machine there (agent-machine login, then agent-machine start), or name it with --machine")
        return found[0]

    # Health and availability

    def health(self, account: ProviderAccount, machine_id: str) -> dict[str, Any]:
        """Whether the account can run on one machine, from what the machine last reported. Metadata only."""
        machine = self.fleet.machines.get(machine_id)
        assignment = self.assignments.get((account.id, machine_id))
        result: dict[str, Any] = {
            "machine_id": machine_id, "machine_name": machine.name if machine else None, "connected": bool(machine and self.fleet.connected(machine_id)),
            "assigned": assignment is not None, "enabled": bool(assignment and assignment.enabled),
            "installed": None, "cli_version": None, "authenticated": None, "available": False,
            "last_checked_at": None, "last_success_at": account.last_success_at, "error_code": None, "error_message": None,
        }

        def fail(code: str, message: str) -> dict[str, Any]:
            return {**result, "error_code": code, "error_message": message}

        if machine is None:
            return fail("machine_removed", "That machine is no longer in the fleet")
        reported = machine.provider_clis
        if reported is None:
            return fail("machine_too_old", f"{machine.name}'s agent-machine doesn't report provider sign-ins: run agent-machine update there")
        cli = next((item for item in reported.get("clis") or [] if item.get("provider") == account.provider), None)
        result["last_checked_at"] = reported.get("at")
        if cli is not None:
            result.update(installed=bool(cli.get("installed")), cli_version=cli.get("version"))
        if account.authentication_mode == "token":
            result["authenticated"] = bool(account.credential)
        elif cli is not None and cli.get("installed"):
            auth = cli.get("auth")
            result["authenticated"] = True if auth == "signed_in" and cli.get("subscription") is not False else (False if auth in ("signed_out", "signed_in") else None)
        if not result["connected"]:
            return fail("offline", f"{machine.name} isn't connected")
        if not (assignment and assignment.enabled):
            return fail("not_assigned", f"{account.display_name} isn't assigned to {machine.name}")
        if cli is None or not cli.get("installed"):
            return fail("cli_missing", f"{PROVIDERS[account.provider]['cli']} isn't installed on {machine.name}" if cli is not None else f"{machine.name} hasn't reported its provider CLIs yet")
        if account.authentication_mode == "token":
            if not account.credential:
                return fail("no_credential", "No token stored")
            if not assignment.provisioning_allowed:
                return fail("provisioning_off", f"Handing the token to {machine.name} is turned off")
            if not self.vault.configured:
                return fail("vault_off", "The server's vault key isn't set (AI_PROXY_VAULT_KEYS)")
        elif result["authenticated"] is False:
            if cli.get("auth") == "signed_in":
                return fail("not_subscription", f"{PROVIDERS[account.provider]['cli']} on {machine.name} is signed in with an API key, not a subscription")
            return fail("signed_out", f"{PROVIDERS[account.provider]['cli']} isn't signed in on {machine.name}. {PROVIDERS[account.provider]['sign_in']}")
        elif result["authenticated"] is None:
            return {**fail("auth_unknown", f"Can't tell whether {PROVIDERS[account.provider]['cli']} is signed in on {machine.name}"), "available": True}
        return {**result, "available": True}

    def state(self, account: ProviderAccount, machine_id: str | None = None) -> tuple[str, str]:
        """The account's availability (STATES) and why, for one machine or the best of its machines."""
        now = self.clock()
        observed = account.observed or {}
        if account.authentication_mode == "token" and not account.credential:
            return "auth_expired", "No token stored: store one, or sign in on its machines instead"
        if float(observed.get("exhausted_until") or 0) > now:
            return "quota_exhausted", f"The provider said its limit is used up until {_when(observed['exhausted_until'])}"
        if float(observed.get("cooldown_until") or 0) > now:
            return "rate_limited", f"The provider is throttling it until {_when(observed['cooldown_until'])}"
        machines = [machine_id] if machine_id else [item.machine_id for item in self.assigned(account) if item.enabled]
        if not machines:
            return "offline", "Not assigned to any machine yet"
        healths = [self.health(account, item) for item in machines]
        usage = self._estimate(account)
        if any(item["available"] and item["authenticated"] is not None for item in healths):
            if usage and usage["percent_used"] is not None and usage["percent_used"] >= account.warning_percent:
                return "near_limit", f"About {usage['percent_used']}% of the {usage['limit']['value']} {usage['limit']['unit']} you declared for {usage['limit']['window']} (estimated from traffic through this server)"
            return "available", "Ready"
        if any(item["available"] for item in healths):
            return "unknown", next(item["error_message"] for item in healths if item["available"])
        signed_out = [item for item in healths if item["error_code"] in ("signed_out", "not_subscription", "no_credential")]
        if signed_out:
            return "auth_expired", signed_out[0]["error_message"]
        connected = [item for item in healths if item["connected"]]
        return "offline", (connected or healths)[0]["error_message"] or "Not available"

    # Usage

    def observe(self, session: Any, record: dict[str, Any], http: dict[str, Any] | None) -> None:
        """One recorded request of an account's session: counted, and a 429 from the provider noted."""
        account = self.accounts.get(str(session.tags.get("provider_account") or ""))
        if account is None:
            return
        host = str(record.get("host") or "").lower()
        if host and not any(host == item or host.endswith("." + item) for item in PROVIDERS[account.provider]["hosts"]):
            return  # telemetry, package registries, web fetches: not the subscription's
        status = int(record.get("status") or 0)
        tokens = int(record.get("input_tokens") or 0) + int(record.get("output_tokens") or 0)
        model_call = bool(record.get("model")) or tokens > 0
        now = self.clock()
        amounts = {
            "requests": 1 if model_call else 0, "input_tokens": int(record.get("input_tokens") or 0), "output_tokens": int(record.get("output_tokens") or 0),
            "rate_limited": 1 if status == 429 else 0, "errors": 1 if status >= 500 else 0,
        }
        if any(amounts.values()):
            keys = {"account_id": account.id, "bucket": int(now // 3600 * 3600)}
            self.db.writer.submit(lambda connection: kv.increment(connection, schema.provider_account_usage, keys, amounts))
        if status == 429:
            retry = retry_after(http, now)
            observed = dict(account.observed or {})
            observed["last_limited_at"] = now
            observed["retry_after"] = retry
            if retry is not None and retry >= QUOTA_RETRY:
                observed["exhausted_until"] = now + min(retry, LONGEST_COOLDOWN)
                kind = "quota_exhausted"
            else:
                observed["cooldown_until"] = now + (retry if retry is not None else DEFAULT_COOLDOWN)
                kind = "rate_limited"
            account.observed = observed
            self._save(account)
            self.event(account, kind, None, session=session.session_id, retry_after=retry, host=host, source="observed")
        elif 200 <= status < 300 and model_call:
            account.last_success_at = now
            if now - self._saved_success.get(account.id, 0) > 60:
                self._saved_success[account.id] = now
                self._save(account)

    def _buckets(self, account: ProviderAccount, since: float) -> list[tuple[int, int, int, int, int, int]]:
        table = schema.provider_account_usage
        with self.db.read() as connection:
            return [tuple(row) for row in connection.execute(
                select(table.c.bucket, table.c.requests, table.c.input_tokens, table.c.output_tokens, table.c.rate_limited, table.c.errors)
                .where((table.c.account_id == account.id) & (table.c.bucket >= int(since)))
            ).all()]

    def _windows(self, account: ProviderAccount) -> dict[str, dict[str, Any]]:
        now = self.clock()
        month = datetime.fromtimestamp(now, timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
        starts = {name: (now - seconds if seconds else month) for name, seconds in WINDOWS.items()}
        buckets = self._buckets(account, min(starts.values()) - 3600)
        windows = {}
        for name, start in starts.items():
            # A bucket counts when its hour overlaps the window: rolling windows are whole hours.
            rows = [row for row in buckets if row[0] + 3600 > start]
            windows[name] = {
                "window": name, "since": start,
                "requests": sum(row[1] for row in rows), "input_tokens": sum(row[2] for row in rows), "output_tokens": sum(row[3] for row in rows),
                "rate_limited": sum(row[4] for row in rows), "errors": sum(row[5] for row in rows),
            }
        return windows

    def _estimate(self, account: ProviderAccount, windows: dict[str, dict[str, Any]] | None = None) -> dict[str, Any] | None:
        """What's left of the limit its owner declared, from traffic through this server: an estimate, never the provider's meter."""
        if not account.limit:
            return None
        windows = windows or self._windows(account)
        window = windows.get(account.limit["window"])
        if window is None:
            return None
        used = window["requests"] if account.limit["unit"] == "requests" else window["input_tokens"] + window["output_tokens"]
        value = int(account.limit["value"])
        percent = round(used * 100 / value) if value else None
        return {
            "limit": account.limit, "used": used, "remaining": max(0, value - used), "percent_used": percent,
            "warning": percent is not None and percent >= account.warning_percent,
            "stop": percent is not None and percent >= account.stop_percent,
            "source": "estimate", "confidence": "low",
            "basis": "Traffic recorded by this server against the limit you declared. Use elsewhere (other computers, the provider's apps) isn't counted.",
        }

    def usage(self, account: ProviderAccount) -> dict[str, Any]:
        self.db.writer.flush()
        windows = self._windows(account)
        observed = account.observed or {}
        now = self.clock()
        return {
            "account_id": account.id,
            "observed": [
                {**window, "source": "observed", "confidence": "server_traffic_only"} for window in windows.values()
            ],
            # The provider's own numbers, when a supported source reports them. None is wired up yet.
            "reported": None,
            "estimate": self._estimate(account, windows),
            "remaining": "unknown" if not account.limit else None,
            "rate_limits": {
                "last_limited_at": observed.get("last_limited_at"), "retry_after": observed.get("retry_after"),
                "cooldown_until": observed.get("cooldown_until") if float(observed.get("cooldown_until") or 0) > now else None,
                "exhausted_until": observed.get("exhausted_until") if float(observed.get("exhausted_until") or 0) > now else None,
                "source": "observed", "confidence": "medium",
            },
            "windows": PROVIDERS[account.provider]["windows"],
        }

    def prune(self) -> None:
        cutoff = int(self.clock() - USAGE_DAYS * 86400)
        table = schema.provider_account_usage
        self.db.writer.submit(lambda connection: connection.execute(table.delete().where(table.c.bucket < cutoff)))

    # Launches

    def for_launch(self, identity: Identity, agent: Any, machine: Any, account_id: Any) -> ProviderAccount | None:
        """The account a launch on a machine runs with, checked for whoever asked; None for the machine's own sign-in.

        Someone the machine lets start agents must not spend its owner's subscription by doing so:
        an account agent started by anyone but the machine's owner needs an account they may use.
        """
        account_id = str(account_id or "").strip()
        if not agent.subscription:
            if account_id:
                raise AccountError(400, f"{agent.name} doesn't use a provider account")
            return None
        if not account_id:
            if machine.owner == identity.user and machine.tenant == identity.tenant:
                return None  # the owner's own sign-in on their own machine, as before
            raise AccountError(403, f"{agent.name} on {machine.name} would use {machine.owner}'s own subscription. Choose a provider account you may use.")
        if account_id == "auto":
            plan = self.plan(identity, [agent], [machine], fallback=[])
            choice = plan["choice"]
            if choice is None:
                reasons = "; ".join(item["reason"] for item in plan["skipped"][:5]) or "you have no provider account for it"
                raise AccountError(409, f"No subscription can run {agent.name} on {machine.name} now: {reasons}")
            account_id = choice["account_id"]
        account = self.accounts.get(account_id)
        if account is None or not self.may_use(identity, account):
            raise AccountError(404, "No such provider account, or it isn't yours to use")
        self._usable(account, agent, machine)
        return account

    def _usable(self, account: ProviderAccount, agent: Any, machine: Any) -> None:
        if account.provider != agent.subscription:
            raise AccountError(400, f"{account.display_name} is a {PROVIDERS[account.provider]['label']} account; {agent.name} needs {PROVIDERS[agent.subscription]['label']}")
        if account.status != "active":
            raise AccountError(409, f"{account.display_name} is turned off" + (f" (by {account.disabled_by})" if account.disabled_by else ""))
        assignment = self.assignments.get((account.id, machine.id))
        if assignment is None or not assignment.enabled:
            raise AccountError(409, f"{account.display_name} isn't assigned to {machine.name}")
        state, reason = self.state(account, machine.id)
        if state in BLOCKING:
            raise AccountError(409, f"{account.display_name} can't take new work on {machine.name}: {reason}")

    def grant(self, launch_id: str, account: ProviderAccount, machine: Any, identity: Identity) -> None:
        now = self.clock()
        with self._lock:
            for launch, item in list(self.grants.items()):
                if item.expires < now:
                    del self.grants[launch]
            self.grants[launch_id] = Grant(launch_id, account.id, machine.id, identity.user, now + GRANT_TTL)

    def end_grant(self, launch_id: str) -> None:
        with self._lock:
            self.grants.pop(launch_id, None)

    def for_session(self, identity: Identity, agent: Any, account_id: Any, tags: dict[str, Any]) -> ProviderAccount | None:
        """The account a new session runs with (POST /api/agents/{id}/sessions with account), checked.

        From a launch the server sent, its grant vouches for whoever asked for it; otherwise the
        launcher's own user must be allowed to use the account, and a managed token never goes to
        a session the server didn't start on an assigned machine.
        """
        account_id = str(account_id or "").strip()
        if not account_id:
            return None
        if not agent.subscription:
            raise AccountError(400, f"{agent.name} doesn't use a provider account")
        account = self.accounts.get(account_id)
        grant = self.grants.get(str(tags.get("launch") or ""))
        vouched = (
            account is not None and grant is not None and grant.account_id == account.id and grant.expires >= self.clock()
            and grant.machine_id == str(tags.get("machine") or "") and account.owner == identity.user and account.tenant == identity.tenant
        )
        if account is None or not (vouched or self.may_use(identity, account)):
            raise AccountError(404, "No such provider account, or it isn't yours to use")
        if account.provider != agent.subscription:
            raise AccountError(400, f"{account.display_name} is a {PROVIDERS[account.provider]['label']} account; {agent.name} needs {PROVIDERS[agent.subscription]['label']}")
        if account.status != "active":
            raise AccountError(409, f"{account.display_name} is turned off")
        if account.authentication_mode == "token" and not vouched:
            raise AccountError(409, f"{account.display_name}'s token is only handed to agents started from the console on a machine it's assigned to")
        state, reason = self.state(account, grant.machine_id if vouched and grant else None)
        if state in ("rate_limited", "quota_exhausted", "auth_expired"):
            raise AccountError(409, f"{account.display_name} can't take new work: {reason}")
        return account

    def deliver(self, identity: Identity, session: Any) -> dict[str, str]:
        """The managed token for a session the server launched, once: its launcher puts it in the agent's environment."""
        account = self.accounts.get(str(session.tags.get("provider_account") or ""))
        if account is None or account.owner != identity.user or account.tenant != identity.tenant:
            raise AccountError(404, "This session has no provider account of yours")
        if account.authentication_mode != "token":
            raise AccountError(409, f"{account.display_name} signs in on its machines; there's nothing to hand over")
        with self._lock:
            grant = self.grants.get(str(session.tags.get("launch") or ""))
            assignment = self.assignments.get((account.id, grant.machine_id)) if grant else None
            if (
                grant is None or grant.account_id != account.id or grant.machine_id != str(session.tags.get("machine") or "")
                or grant.expires < self.clock() or grant.delivered
                or assignment is None or not assignment.enabled or not assignment.provisioning_allowed
                or account.status != "active"
            ):
                raise AccountError(403, "This session's launch can't receive the token (already handed over, expired, or not assigned)")
            grant.delivered = True
        token = self._credential(account)
        self.event(account, "provisioned", identity, machine=grant.machine_id, session=session.session_id, launch=grant.launch_id, requested_by=grant.requested_by)
        return {PROVIDERS[account.provider]["credential_env"]: token}

    # Scheduling

    def plan(self, identity: Identity, agents: list[Any], machines: list[Any], fallback: list[Any], allow_unknown: bool = True, project: str = "") -> dict[str, Any]:
        """Which subscription (agent, machine, account) should take a new task, best first, and why others can't.

        Prefers available accounts, then ones near a declared limit, then ones whose sign-in can't be
        told; never one throttled, used up, signed out or offline. An estimate past an account's stop
        percentage only counts here, for automatic choices. Then the fallback agents (local models
        only: a pay-as-you-go API is never chosen for anyone).
        """
        from .fleet import allowed, within_roots

        candidates: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        launchable = []
        for machine in machines:
            if not allowed(identity, machine, "launch"):
                continue
            problem = None
            if not self.fleet.connected(machine.id):
                problem = f"{machine.name} isn't connected"
            elif not machine.launches:
                problem = f"{machine.name} doesn't take launches"
            elif project and not within_roots(project, machine.roots):
                problem = f"{project} isn't inside {machine.name}'s launch folders"
            if problem:
                skipped.append({"machine_id": machine.id, "reason": problem})
            else:
                launchable.append(machine)
        for order, agent in enumerate(agents):
            if not agent.subscription:
                continue
            for machine in launchable:
                if agent.id not in machine.agents:
                    skipped.append({"agent": agent.id, "machine_id": machine.id, "reason": f"{agent.name} isn't installed on {machine.name}"})
                    continue
                for account in self.accounts.values():
                    if account.provider != agent.subscription or not self.may_use(identity, account) or (account.id, machine.id) not in self.assignments:
                        continue
                    try:
                        self._usable(account, agent, machine)
                    except AccountError as exc:
                        skipped.append({"agent": agent.id, "machine_id": machine.id, "account_id": account.id, "reason": exc.detail})
                        continue
                    state, reason = self.state(account, machine.id)
                    estimate = self._estimate(account)
                    if state == "unknown" and not allow_unknown:
                        skipped.append({"agent": agent.id, "machine_id": machine.id, "account_id": account.id, "reason": reason})
                        continue
                    if estimate and estimate["stop"]:
                        skipped.append({"agent": agent.id, "machine_id": machine.id, "account_id": account.id, "reason": f"{account.display_name} is past {account.stop_percent}% of its declared limit (estimated)"})
                        continue
                    candidates.append({
                        "kind": "subscription", "agent": agent.id, "machine_id": machine.id, "machine_name": machine.name,
                        "account_id": account.id, "account_name": account.display_name, "provider": account.provider,
                        "state": state, "reason": reason, "remaining": estimate["remaining"] if estimate else None,
                        # Better state, then the owner's priority, then the agent order asked for, then the most left.
                        "_rank": (STATES.index(state), account.priority, order, (estimate or {}).get("percent_used") or 0),
                    })
        candidates.sort(key=lambda item: item["_rank"])
        fallbacks = []
        for order, agent in enumerate(fallback):
            if agent.subscription or agent.intercept or agent.backend_type != "ollama":
                skipped.append({"agent": agent.id, "reason": f"{agent.name} isn't a local-model agent, so it can't be a fallback (a paid API is never chosen automatically)"})
                continue
            for machine in launchable:
                if agent.id in machine.agents:
                    fallbacks.append({"kind": "fallback", "agent": agent.id, "machine_id": machine.id, "machine_name": machine.name, "account_id": None, "state": "available", "reason": "Local models", "_rank": (order,)})
        ordered = [{key: value for key, value in item.items() if key != "_rank"} for item in candidates + fallbacks]
        return {"choice": ordered[0] if ordered else None, "candidates": ordered, "skipped": skipped}

    # What the console sees

    def public(self, account: ProviderAccount, identity: Identity | None, usage: bool = False) -> dict[str, Any]:
        manage = self.may_manage(identity, account)
        state, reason = self.state(account)
        value = {
            "id": account.id, "provider": account.provider, "provider_label": PROVIDERS[account.provider]["label"],
            "owner": account.owner, "tenant": account.tenant, "display_name": account.display_name, "plan_name": account.plan_name,
            "authentication_mode": account.authentication_mode, "mode_label": MODES[account.authentication_mode],
            "status": account.status, "disabled_by": account.disabled_by, "priority": account.priority,
            "users": account.users if manage else [],
            "limit": account.limit, "warning_percent": account.warning_percent, "stop_percent": account.stop_percent,
            "created_at": account.created_at, "updated_at": account.updated_at, "last_validated_at": account.last_validated_at,
            "last_success_at": account.last_success_at, "expires_at": account.expires_at,
            # Whether a token is stored, never the token.
            "credential": {"stored": bool(account.credential), **{key: value for key, value in (account.credential or {}).items() if key in ("set_at", "key_version", "kind")}},
            "state": state, "state_reason": reason,
            "assignments": [{**asdict(item), "health": self.health(account, item.machine_id)} for item in self.assigned(account)],
            "you": {"owner": manage, "use": self.may_use(identity, account), "manage": manage, "suspend": self.may_suspend(identity, account)},
        }
        if usage:
            value["usage"] = self.usage(account)
        return value


def retry_after(http: dict[str, Any] | None, now: float) -> float | None:
    """The provider's Retry-After, in seconds (a number or an HTTP date), from the recorded response headers."""
    for name, value in (http or {}).get("response_headers") or []:
        if str(name).lower() != "retry-after":
            continue
        value = str(value).strip()
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - now)
        except (TypeError, ValueError):
            return None
    return None


def _limit(value: Any) -> dict[str, Any] | None:
    if value in (None, {}, ""):
        return None
    if not isinstance(value, dict):
        raise AccountError(400, "limit is {window, unit, value}")
    window, unit = str(value.get("window") or ""), str(value.get("unit") or "requests")
    if window not in WINDOWS:
        raise AccountError(400, f"limit.window is one of {', '.join(WINDOWS)}")
    if unit not in LIMIT_UNITS:
        raise AccountError(400, f"limit.unit is one of {', '.join(LIMIT_UNITS)}")
    try:
        amount = int(value.get("value"))
    except (TypeError, ValueError) as exc:
        raise AccountError(400, "limit.value is a whole number") from exc
    if amount <= 0:
        raise AccountError(400, "limit.value must be above 0")
    return {"window": window, "unit": unit, "value": amount, "source": "user_declared"}


def _text(value: Any, size: int) -> str | None:
    text = " ".join(str(value or "").split())[:size]
    return text or None


def _when(at: Any) -> str:
    return datetime.fromtimestamp(float(at), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# What agent-machine reports about the provider CLIs on its computer (from `agent accounts detect --json`), kept to known fields.
CLI_AUTH = ("signed_in", "signed_out", "unknown")


def reported_clis(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    clis = []
    for cli in item.get("clis") or []:
        if isinstance(cli, dict) and cli.get("provider") in PROVIDERS:
            clis.append({
                "provider": cli["provider"], "installed": bool(cli.get("installed")),
                "version": _text(cli.get("version"), 60), "auth": cli.get("auth") if cli.get("auth") in CLI_AUTH else "unknown",
                "method": _text(cli.get("method"), 40),
                # False when it's signed in, but not with a subscription (an API key).
                "subscription": cli.get("subscription") if isinstance(cli.get("subscription"), bool) else None,
                "modes": [mode for mode in cli.get("modes") or [] if mode in MODES],
                "reauthenticate": bool(cli.get("reauthenticate")),
            })
    at = item.get("at")
    return {"clis": clis[: len(PROVIDERS)], "at": at if isinstance(at, (int, float)) else None, "error": _text(item.get("error"), 300)}
