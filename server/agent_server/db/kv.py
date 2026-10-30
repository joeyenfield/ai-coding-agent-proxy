"""Small helpers over the tables: upserts in either dialect, and the key-value tables (meta, settings, secrets)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Table, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Connection

from . import schema


def upsert(connection: Connection, table: Table, values: dict[str, Any]) -> None:
    """Insert the row, or replace the one with the same primary key."""
    keys = [column.name for column in table.primary_key.columns]
    dialect = sqlite if connection.dialect.name == "sqlite" else postgresql
    statement = dialect.insert(table).values(**values)
    changes = {name: statement.excluded[name] for name in values if name not in keys}
    statement = statement.on_conflict_do_update(index_elements=keys, set_=changes) if changes else statement.on_conflict_do_nothing(index_elements=keys)
    connection.execute(statement)


def get(connection: Connection, table: Table, key: str, default: Any = None) -> Any:
    column = table.c.value
    found = connection.execute(select(column).where(table.primary_key.columns[0] == key)).first()
    return found[0] if found else default


def put(connection: Connection, table: Table, key: str, value: Any) -> None:
    name = table.primary_key.columns[0].name
    upsert(connection, table, {name: key, "value": value})


def remove(connection: Connection, table: Table, key: str) -> None:
    connection.execute(table.delete().where(table.primary_key.columns[0] == key))


def get_meta(connection: Connection, key: str, default: Any = None) -> Any:
    return get(connection, schema.meta, key, default)


def put_meta(connection: Connection, key: str, value: Any) -> None:
    put(connection, schema.meta, key, value)


def secret(db: Any, name: str, create: bool = False) -> str | None:
    """A generated secret kept in the secrets table (made on first use with create)."""
    import secrets as random

    with db.write() if create else db.read() as connection:
        stored = get(connection, schema.secrets, name)
        if stored is None and create:
            stored = random.token_urlsafe(32).encode()
            put(connection, schema.secrets, name, stored)
    return bytes(stored).decode() if stored is not None else None
