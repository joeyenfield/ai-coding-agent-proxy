"""Bringing a database's tables up to this version: numbered steps, run once each, in order.

Step 1 creates the tables. A later change to them is a new step appended to its list,
never an edit to an earlier one: databases already past it would never see the edit.
"""

from __future__ import annotations

from typing import Callable

from sqlalchemy import MetaData, Table, select, text
from sqlalchemy.engine import Connection

from . import API_SCHEMA, AUTH_SCHEMA, Database
from . import schema

Step = Callable[[Connection], None]


def _create(metadata: MetaData) -> Step:
    def step(connection: Connection) -> None:
        metadata.create_all(connection)
    return step


API_STEPS: list[Step] = [_create(schema.api)]
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
