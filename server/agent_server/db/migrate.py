"""Bringing a database's tables up to this version: numbered steps, run once each, in order.

Step 1 creates the tables. A later change to them is a new step appended to its list,
never an edit to an earlier one: databases already past it would never see the edit.
"""

from __future__ import annotations

from typing import Callable

from sqlalchemy import MetaData, Table, inspect, select, text
from sqlalchemy.engine import Connection

from . import API_SCHEMA, AUTH_SCHEMA, Database
from . import schema

Step = Callable[[Connection], None]


def _create(metadata: MetaData) -> Step:
    def step(connection: Connection) -> None:
        metadata.create_all(connection)
    return step


def _add_column(connection: Connection, table: Table, column: str, kind: str) -> None:
    """ALTER TABLE ... ADD COLUMN, unless step 1 already made it (a new database gets today's schema.py)."""
    sqlite = connection.dialect.name == "sqlite"
    existing = {item["name"] for item in inspect(connection).get_columns(table.name, schema=None if sqlite else API_SCHEMA)}
    if column not in existing:
        qualified = f'"{table.name}"' if sqlite else f'"{API_SCHEMA}"."{table.name}"'
        connection.execute(text(f'ALTER TABLE {qualified} ADD COLUMN "{column}" {kind}'))


ROOT_TENANT = "root"
# Members from before tenants who were in no organization: the root is for site admins only.
UNASSIGNED_TENANT = "main"


def _tenants(connection: Connection) -> None:
    """Step 2: every organization becomes a tenant under the root one, and everything gets one tenant.

    A user's tenant is their first organization (by id); an admin in none goes to the root (a site
    admin), a member in none to "main". Teams outside their tenant are dropped. Sessions and machines
    belong to their owner's tenant; those without an owner to the root, which only site admins see.
    Frozen here, as migrations are: later changes to auth.py don't change what this step did.
    """
    _add_column(connection, schema.users, "tenant", "VARCHAR(40)")
    _add_column(connection, schema.orgs, "parent", "VARCHAR(40)")
    orgs = {row["id"]: row for row in connection.execute(select(schema.orgs)).mappings()}
    users = list(connection.execute(select(schema.users)).mappings())
    tenants: dict[str, str] = {}
    for user in users:
        named = sorted(str(item) for item in user["orgs"] or [] if str(item) in orgs and str(item) != ROOT_TENANT)
        tenant = user["tenant"] or (named[0] if named else ROOT_TENANT if user["role"] == "admin" else UNASSIGNED_TENANT)
        tenants[user["name"]] = tenant
    for tenant in {ROOT_TENANT, *tenants.values()} - set(orgs):
        name = "Site administration" if tenant == ROOT_TENANT else "Members from before organizations"
        connection.execute(schema.orgs.insert().values(id=tenant, name=name, created_at=None, teams={}, parent=None))
    connection.execute(schema.orgs.update().where(schema.orgs.c.id != ROOT_TENANT).values(parent=ROOT_TENANT))
    connection.execute(schema.orgs.update().where(schema.orgs.c.id == ROOT_TENANT).values(parent=None))
    for user in users:
        tenant = tenants[user["name"]]
        teams = [team for team in user["teams"] or [] if str(team).split("/", 1)[0] == tenant]
        connection.execute(schema.users.update().where(schema.users.c.name == user["name"]).values(tenant=tenant, orgs=[tenant], teams=teams))
    for table in (schema.sessions, schema.machines, schema.machines_removed):
        key = table.c.id
        for row_id, data in connection.execute(select(key, table.c.data)).all():
            if not isinstance(data, dict):
                continue
            if table is schema.sessions:
                tags = dict(data.get("tags") or {})
                if tags.get("tenant"):
                    continue
                tags["tenant"] = tenants.get(str(tags.get("owner") or ""), ROOT_TENANT)
                data = {**data, "tags": tags}
            else:
                if data.get("tenant"):
                    continue
                data = {**data, "tenant": tenants.get(str(data.get("owner") or ""), ROOT_TENANT)}
            connection.execute(table.update().where(key == row_id).values(data=data))
    # Every process re-reads the accounts.
    from . import kv
    kv.put_meta(connection, "accounts_version", int(kv.get_meta(connection, "accounts_version", 0) or 0) + 1)


def _provider_accounts(connection: Connection) -> None:
    """Provider accounts and their vault (step 1 already made them in a database created since)."""
    for table in schema.PROVIDER_ACCOUNT_TABLES:
        table.create(connection, checkfirst=True)


def _access_log(connection: Connection) -> None:
    """The access log's saved failures and fleet connections (step 1 already made it in a database created since)."""
    schema.access_log.create(connection, checkfirst=True)


API_STEPS: list[Step] = [_create(schema.api), _tenants, _provider_accounts, _access_log]
AUTH_STEPS: list[Step] = [_create(schema.auth)]

# One lock id per component, so two processes starting at once don't both migrate.
LOCKS = {"api": 7_406_001, "auth": 7_406_002}


def migrate(db: Database, component: str = "api") -> int:
    """Run the steps this database hasn't had; returns its version."""
    steps, versions, schema_name = (
        (API_STEPS, schema.api_versions, API_SCHEMA) if component == "api" else (AUTH_STEPS, schema.auth_versions, AUTH_SCHEMA)
    )
    with db.write() as connection:
        if not db.sqlite:
            connection.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": LOCKS[component]})
            # Made beforehand by an administrator (server/docker/postgres-init.sh), or here by a role that may.
            exists = connection.execute(text("SELECT 1 FROM pg_namespace WHERE nspname = :name"), {"name": schema_name}).first()
            if not exists:
                connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        versions.create(connection, checkfirst=True)
        current = connection.execute(select(versions.c.version).where(versions.c.component == component)).scalar()
        current = int(current or 0)
        for number, step in enumerate(steps[current:], start=current + 1):
            step(connection)
            _set(connection, versions, component, number)
        if component == "api" and not db.sqlite:
            grant_issuer(connection)
        return max(current, len(steps))


# What agent-auth reads of the API's tables: accounts and the counter that says they changed.
ISSUER_READS = ("users", "orgs", "meta")


def grant_issuer(connection: Connection, role: str = "mesh_auth") -> None:
    """Let the issuer's role read the accounts, and nothing else of mesh (when that role exists)."""
    if connection.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}).first() is None:
        return
    connection.execute(text(f'GRANT USAGE ON SCHEMA "{API_SCHEMA}" TO "{role}"'))
    tables = ", ".join(f'"{API_SCHEMA}"."{name}"' for name in ISSUER_READS)
    connection.execute(text(f'GRANT SELECT ON {tables} TO "{role}"'))


def _set(connection: Connection, versions: Table, component: str, number: int) -> None:
    updated = connection.execute(versions.update().where(versions.c.component == component).values(version=number))
    if not updated.rowcount:
        connection.execute(versions.insert().values(component=component, version=number))


def version(db: Database, component: str = "api") -> int:
    versions = schema.api_versions if component == "api" else schema.auth_versions
    try:
        with db.read() as connection:
            return int(connection.execute(select(versions.c.version).where(versions.c.component == component)).scalar() or 0)
    except Exception:  # noqa: BLE001 - no table yet
        return 0


def latest(component: str = "api") -> int:
    return len(API_STEPS if component == "api" else AUTH_STEPS)
