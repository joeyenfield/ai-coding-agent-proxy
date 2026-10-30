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
)

users = Table(
    "users", api,
    Column("name", String(40), primary_key=True),
    # scrypt$n$r$p$salt$hash. With created_at it makes the account version every token is bound to (auth.account_version).
    Column("password", Text, nullable=False),
    Column("role", String(20), nullable=False),
    Column("created_at", Float),
    Column("orgs", JSON, nullable=False),
    Column("teams", JSON, nullable=False),
)

orgs = Table(
    "orgs", api,
    Column("id", String(40), primary_key=True),
    Column("name", Text, nullable=False),
    Column("created_at", Float),
    Column("teams", JSON, nullable=False),
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
)

requests = Table(
    "requests", api,
    Column("session_id", String(128), primary_key=True),
    Column("request_id", String(40), primary_key=True),
    Column("timestamp", String(40), nullable=False),
    Column("kind", String(40)),
    Column("model", Text),
    Column("backend", String(100)),
    Column("input_tokens", BigInteger, nullable=False, default=0),
    Column("output_tokens", BigInteger, nullable=False, default=0),
    Column("record", JSON, nullable=False),
    Index("requests_timestamp", "timestamp"),
)

traces = Table(
    "traces", api,
    Column("session_id", String(128), primary_key=True),
    Column("request_id", String(40), primary_key=True),
    Column("size", BigInteger, nullable=False),
    # gzipped JSON (db.pack)
    Column("body", LargeBinary, nullable=False),
)

chat_index = Table("chat_index", api, Column("session_id", String(128), primary_key=True), Column("data", JSON, nullable=False))

terminals = Table("terminals", api, Column("session_id", String(128), primary_key=True), Column("meta", JSON, nullable=False))

terminal_chunks = Table(
    "terminal_chunks", api,
    Column("session_id", String(128), primary_key=True),
    # The chunk's first byte's place in the terminal's recorded output.
    Column("offset", BigInteger, primary_key=True, autoincrement=False),
    Column("data", LargeBinary, nullable=False),
)

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
