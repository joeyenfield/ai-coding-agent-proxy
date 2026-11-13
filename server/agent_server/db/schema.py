"""The tables. API tables are in schema "mesh", the issuer's in "mesh_auth" (PostgreSQL; SQLite has neither).

Documents whose shape the code already owns (a session, a machine, an agent definition,
a request record) are kept as JSON beside the columns queries filter or sort on.
"""

from __future__ import annotations

from sqlalchemy import JSON, Boolean, Column, Float, Index, Integer, LargeBinary, MetaData, String, Table, Text, BigInteger

from . import API_SCHEMA, AUTH_SCHEMA

api = MetaData(schema=API_SCHEMA)
auth = MetaData(schema=AUTH_SCHEMA)


def _versions(metadata: MetaData) -> Table:
    return Table("schema_version", metadata, Column("component", String(40), primary_key=True), Column("version", Integer, nullable=False))


api_versions = _versions(api)

# Small server-wide values: accounts_initialized, imported, accounts_version.
meta = Table("meta", api, Column("key", String(100), primary_key=True), Column("value", JSON))

# The server's settings: proxy (listen address, url, public_url, tls), default_backend,
# intercept, models (defaults, profiles, aliases), retention_days.
settings = Table("settings", api, Column("key", String(100), primary_key=True), Column("value", JSON))

backends = Table(
    "backends", api,
    Column("name", String(100), primary_key=True),
    Column("position", Integer, nullable=False, default=0),
    Column("type", String(40), nullable=False),
    Column("url", Text, nullable=False),
    Column("machine", String(200)),
    Column("container", JSON),
    # An api backend's options (api_providers.py; API step 8). Its key is never here.
    Column("options", JSON),
)

agents = Table(
    "agents", api,
    Column("id", String(100), primary_key=True),
    Column("position", Integer, nullable=False, default=0),
    Column("definition", JSON, nullable=False),
    # Shipped with the server (defaults.AGENTS); an admin's edit sets edited, and upgrades leave it alone.
    Column("builtin", Boolean, nullable=False, default=False),
    Column("edited", Boolean, nullable=False, default=False),
    Column("updated_at", Float),
    # The organization whose definition it is (API step 9); None for the server's own (shipped ones, site admins').
    Column("org", String(40)),
)

users = Table(
    "users", api,
    Column("name", String(40), primary_key=True),
    # scrypt$n$r$p$salt$hash. With created_at it makes the account version every token is bound to (auth.account_version).
    Column("password", Text, nullable=False),
    Column("role", String(20), nullable=False),
    Column("created_at", Float),
    # The one organization (tenant) the user belongs to, for good; "root" for site admins.
    # orgs is kept as [tenant] for older readers.
    Column("tenant", String(40)),
    Column("orgs", JSON, nullable=False),
    Column("teams", JSON, nullable=False),
)

orgs = Table(
    "orgs", api,
    Column("id", String(40), primary_key=True),
    Column("name", Text, nullable=False),
    Column("created_at", Float),
    Column("teams", JSON, nullable=False),
    # Every organization is a tenant under the root one ("root", whose parent is None).
    Column("parent", String(40)),
)

# Generated secrets: the browser state key, the intercept CA, Strata containers' API keys.
secrets = Table("secrets", api, Column("name", String(200), primary_key=True), Column("value", LargeBinary, nullable=False))

machines = Table("machines", api, Column("id", String(200), primary_key=True), Column("data", JSON, nullable=False))
machines_removed = Table("machines_removed", api, Column("id", String(200), primary_key=True), Column("data", JSON, nullable=False))

sessions = Table(
    "sessions", api,
    Column("id", String(128), primary_key=True),
    Column("started_at", String(40), nullable=False),
    Column("data", JSON, nullable=False),
    # Counters kept apart from the document (API step 6), so processes counting requests never
    # write back a session's tags or sharing as they last saw them: each only ever raises these.
    Column("request_sequence", BigInteger),
    Column("request_count", BigInteger),
    Column("last_activity_at", String(40)),
    Column("input_tokens", BigInteger),
    Column("output_tokens", BigInteger),
    # When retention next has something of this session's to delete (retention.py); None: not looked at yet.
    Column("retention_next", String(40)),
    Index("sessions_last_activity", "last_activity_at"),
    Index("sessions_retention_next", "retention_next"),
)

requests = Table(
    "requests", api,
    Column("session_id", String(128), primary_key=True),
    Column("request_id", String(40), primary_key=True),
    Column("timestamp", String(40), nullable=False),
    Column("kind", String(40)),
    Column("model", Text),
    # A backend's name, or a remote provider's full name (routing.py), which is longer (widened in API step 9).
    Column("backend", String(255)),
    Column("input_tokens", BigInteger, nullable=False, default=0),
    Column("output_tokens", BigInteger, nullable=False, default=0),
    Column("record", JSON, nullable=False),
    # The record's cost estimate (pricing.cost_columns), apart from the document so a session's cost is a sum
    # in the database (API step 10): dollars, and "api" (money spent) or "subscription" (at API prices).
    Column("cost_usd", Float),
    Column("cost_basis", String(16)),
    Index("requests_timestamp", "timestamp"),
)

traces = Table(
    "traces", api,
    Column("session_id", String(128), primary_key=True),
    Column("request_id", String(40), primary_key=True),
    Column("size", BigInteger, nullable=False),
    # gzipped JSON (db.pack); empty when the trace is in blob storage (location).
    Column("body", LargeBinary, nullable=False),
    # Its key in blob storage (blobs.py, AI_PROXY_BLOB_URL), or None when it's in body (API step 6).
    Column("location", Text),
)

chat_index = Table("chat_index", api, Column("session_id", String(128), primary_key=True), Column("data", JSON, nullable=False))

terminals = Table("terminals", api, Column("session_id", String(128), primary_key=True), Column("meta", JSON, nullable=False))

terminal_chunks = Table(
    "terminal_chunks", api,
    Column("session_id", String(128), primary_key=True),
    # The chunk's first byte's place in the terminal's recorded output.
    Column("offset", BigInteger, primary_key=True, autoincrement=False),
    Column("data", LargeBinary, nullable=False),
    # As traces': the chunk's key in blob storage, with data empty (API step 6).
    Column("location", Text),
)

# Provider accounts (provider_accounts.py): people's own AI subscriptions (Claude, ChatGPT/Codex,
# GitHub Copilot), apart from the platform accounts above. Each belongs to one user and tenant.
provider_accounts = Table(
    "provider_accounts", api,
    Column("id", String(64), primary_key=True),
    Column("tenant", String(40), nullable=False),
    Column("owner", String(40), nullable=False),
    Column("provider", String(40), nullable=False),
    # Metadata only (names, plan, mode, priority, declared limits, cooldowns): never a credential.
    Column("data", JSON, nullable=False),
    Index("provider_accounts_owner", "tenant", "owner"),
)

# Which of its owner's machines an account may run on (and, for a managed token, be provisioned to).
provider_account_assignments = Table(
    "provider_account_assignments", api,
    Column("account_id", String(64), primary_key=True),
    Column("machine_id", String(200), primary_key=True),
    Column("data", JSON, nullable=False),
)

# What went through the server for each account, by hour (bucket: the hour's start, seconds since the epoch).
provider_account_usage = Table(
    "provider_account_usage", api,
    Column("account_id", String(64), primary_key=True),
    Column("bucket", BigInteger, primary_key=True, autoincrement=False),
    Column("requests", BigInteger, nullable=False, default=0),
    Column("input_tokens", BigInteger, nullable=False, default=0),
    Column("output_tokens", BigInteger, nullable=False, default=0),
    Column("rate_limited", BigInteger, nullable=False, default=0),
    Column("errors", BigInteger, nullable=False, default=0),
)

# The audit trail: enrollments, assignments, provisioning, revocations, observed limits. Never a secret.
provider_account_events = Table(
    "provider_account_events", api,
    Column("id", String(32), primary_key=True),
    Column("account_id", String(64), nullable=False),
    Column("at", Float, nullable=False),
    Column("data", JSON, nullable=False),
    Index("provider_account_events_account", "account_id", "at"),
)

# The vault: managed provider credentials, encrypted (AES-256-GCM) with a master key the database
# never holds (AI_PROXY_VAULT_KEYS). Not the secrets table: those are the server's own generated keys.
provider_credentials = Table(
    "provider_credentials", api,
    Column("account_id", String(64), primary_key=True),
    Column("key_version", Integer, nullable=False),
    Column("nonce", LargeBinary, nullable=False),
    Column("ciphertext", LargeBinary, nullable=False),
    Column("created_at", Float, nullable=False),
)

# The access log's saved part (access_log.py): failed requests, dropped WebSockets and every fleet
# connection, kept for the retention setting (30 days without one). The rest stays in memory.
access_log = Table(
    "access_log", api,
    Column("id", String(32), primary_key=True),
    # When it arrived (seconds since the epoch) and its UTC day (YYYY-MM-DD), for counting by day in either dialect.
    Column("time", Float, nullable=False),
    Column("day", String(10), nullable=False),
    Column("outcome", String(10), nullable=False),
    Column("path", Text, nullable=False),
    # Its words, lowercased, for searching with LIKE in either dialect.
    Column("search", Text, nullable=False),
    Column("entry", JSON, nullable=False),
    Index("access_log_time", "time"),
    Index("access_log_day", "day"),
)

# The request log (request_log.py): every HTTP request and WebSocket, kept for the request_log setting's days.
request_log = Table(
    "request_log", api,
    Column("id", String(32), primary_key=True),
    Column("time", Float, nullable=False),
    Column("method", String(10), nullable=False),
    Column("path", Text, nullable=False),
    # The HTTP status, or the WebSocket handshake's (101 when accepted); null if none was sent.
    Column("status", Integer),
    Column("outcome", String(10), nullable=False),
    Column("user", String(200)),
    Column("search", Text, nullable=False),
    Column("entry", JSON, nullable=False),
    # Whether request_log_payloads has its bodies (debug mode was on).
    Column("payload", Boolean, nullable=False, default=False),
    Index("request_log_time", "time"),
)

# Bodies and headers of logged requests, only while the request log's debug mode is on: gzipped JSON,
# kept for payload_days (usually far fewer than the request log's own days).
request_log_payloads = Table(
    "request_log_payloads", api,
    Column("id", String(32), primary_key=True),
    Column("time", Float, nullable=False),
    Column("data", LargeBinary, nullable=False),
    Index("request_log_payloads_time", "time"),
)

# Usage by person and provider (usage_limits.py), by hour: provider is backend:<name> or subscription:<kind>.
provider_usage = Table(
    "provider_usage", api,
    Column("tenant", String(40), primary_key=True),
    Column("user", String(100), primary_key=True),
    Column("provider", String(200), primary_key=True),
    Column("bucket", BigInteger, primary_key=True, autoincrement=False),
    Column("requests", BigInteger, nullable=False, default=0),
    Column("input_tokens", BigInteger, nullable=False, default=0),
    Column("output_tokens", BigInteger, nullable=False, default=0),
    Index("provider_usage_tenant", "tenant", "bucket"),
)

# The limits an organization's admins set: tokens per window, for one user or everyone in it ("*").
usage_limits = Table(
    "usage_limits", api,
    Column("tenant", String(40), primary_key=True),
    Column("user", String(100), primary_key=True),
    Column("provider", String(200), primary_key=True),
    Column("window", String(8), primary_key=True),
    Column("tokens", BigInteger, nullable=False),
    Column("set_by", String(100)),
    Column("set_at", Float),
)

# How long an organization keeps what's recorded, within the server's bounds (retention.py), API step 6.
retention_policies = Table(
    "retention_policies", api,
    Column("tenant", String(40), primary_key=True),
    Column("policy", JSON, nullable=False),
    Column("set_by", String(100)),
    Column("set_at", Float),
)

# Legal holds: an organization, a person or a session whose recordings no retention deletes (retention.py).
legal_holds = Table(
    "legal_holds", api,
    Column("id", String(32), primary_key=True),
    Column("tenant", String(40), nullable=False),
    # org, user or session; target is the organization, the user's name or the session id.
    Column("scope", String(10), nullable=False),
    Column("target", String(200), nullable=False),
    Column("reason", Text),
    Column("set_by", String(100)),
    Column("set_at", Float, nullable=False),
    Index("legal_holds_target", "scope", "target"),
)

# The audit log (audit.py): append only, a hash chain per tenant. audit_chains holds each chain's
# head, which a new event locks to take the next seq; audit_checkpoints the signed hourly heads.
audit_events = Table(
    "audit_events", api,
    Column("tenant", String(40), primary_key=True),
    Column("seq", BigInteger, primary_key=True, autoincrement=False),
    Column("id", String(36), nullable=False, unique=True),
    Column("time", Float, nullable=False),
    Column("type", String(80), nullable=False),
    Column("actor", String(100)),
    Column("outcome", String(20), nullable=False),
    Column("event", JSON, nullable=False),
    Column("prev", String(64), nullable=False),
    Column("hash", String(64), nullable=False),
    Index("audit_events_time", "time"),
)
audit_chains = Table(
    "audit_chains", api,
    Column("tenant", String(40), primary_key=True),
    Column("seq", BigInteger, nullable=False),
    Column("head", String(64), nullable=False),
)
audit_checkpoints = Table(
    "audit_checkpoints", api,
    Column("id", String(32), primary_key=True),
    Column("time", Float, nullable=False),
    # {tenant: [seq, head]} at the time, and the server's signature over them (Ed25519).
    Column("heads", JSON, nullable=False),
    Column("signature", Text, nullable=False),
    Column("key_id", String(64), nullable=False),
    Column("location", Text),
    Index("audit_checkpoints_time", "time"),
)
# One row: whether this transaction may delete audit events (the database's triggers check it; retention sets it).
audit_guard = Table("audit_guard", api, Column("id", Integer, primary_key=True), Column("allow", Integer, nullable=False, default=0))

# Peering (links.py, bridges.py; API step 9). A link joins this server to another; a bridge joins one of
# this server's organizations to one of the other's, over a link. data holds what the code owns: the
# peer's public keys, who presented, the catalogue received. No private key is ever here (that's a secret).
links = Table(
    "links", api,
    Column("id", String(64), primary_key=True),
    Column("peer", String(40), nullable=False),
    Column("url", Text, nullable=False),
    Column("state", String(16), nullable=False),
    Column("data", JSON, nullable=False),
    Column("created_at", Float),
    Column("updated_at", Float),
)
# One-use codes a site admin (link) or an organization admin (bridge) hands the other side: only their digest.
peer_codes = Table(
    "peer_codes", api,
    Column("digest", String(64), primary_key=True),
    Column("kind", String(8), nullable=False),
    Column("org", String(40)),
    Column("link", String(64)),
    Column("exp", Float, nullable=False),
    Column("created_by", String(100)),
    Column("used_at", Float),
)
bridges = Table(
    "bridges", api,
    Column("id", String(64), primary_key=True),
    Column("link", String(64), nullable=False),
    Column("org", String(40), nullable=False),
    Column("peer_org", String(40), nullable=False),
    Column("state", String(16), nullable=False),
    Column("data", JSON, nullable=False),
    Column("created_at", Float),
    Column("updated_at", Float),
    Index("bridges_link", "link"),
)
# Each side's signed policies, every version: scope link or bridge, side local (ours) or peer (theirs).
peer_policies = Table(
    "peer_policies", api,
    Column("scope", String(8), primary_key=True),
    Column("scope_id", String(64), primary_key=True),
    Column("side", String(8), primary_key=True),
    Column("version", Integer, primary_key=True, autoincrement=False),
    Column("document", JSON, nullable=False),
    Column("digest", String(64), nullable=False),
    Column("signature", Text, nullable=False),
    Column("kid", String(40), nullable=False),
    Column("created_at", Float),
)

PEERING_TABLES = (links, peer_codes, bridges, peer_policies)

SCALING_TABLES = (retention_policies, legal_holds, audit_events, audit_chains, audit_checkpoints, audit_guard)

PROVIDER_ACCOUNT_TABLES = (provider_accounts, provider_account_assignments, provider_account_usage, provider_account_events, provider_credentials)

auth_versions = _versions(auth)

# The issuer's OAuth state: clients, codes, tokens (hashed), device grants, transactions.
records = Table(
    "records", auth,
    Column("kind", String(40), primary_key=True),
    Column("key", String(300), primary_key=True),
    Column("data", Text, nullable=False),
    Column("exp", Float),
    Index("records_exp", "exp"),
)

auth_secrets = Table("secrets", auth, Column("name", String(200), primary_key=True), Column("value", LargeBinary, nullable=False))
