"""Keep tests away from the real database, logs and secrets.

Every test gets its own log folder, account-state folder and CA folder, so Settings
(built in code or loaded) opens a SQLite file under tmp_path. With
AI_PROXY_TEST_DATABASE_URL set to a PostgreSQL server (an admin role), each test gets a
database of its own there instead, dropped afterwards. This also fails any test that
touches the real database or creates secrets in certs/.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from agent_server.config import ROOT


REAL_DATABASE = [ROOT / "logs" / name for name in ("agent-server.sqlite3", "agent-server.sqlite3-wal")]
SECRETS = [ROOT / "certs" / name for name in ("remote-access-key", "container-keys.json", "users.json", "first-user-password")]


@pytest.fixture(autouse=True)
def isolated_storage(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("server-home")
    monkeypatch.setenv("AI_PROXY_LOG_DIR", str(home / "logs"))
    monkeypatch.setenv("AI_PROXY_REMOTE_KEY_FILE", str(home / "certs" / "remote-access-key"))
    monkeypatch.setenv("AI_PROXY_CA_DIR", str(home / "ca"))
    for name in ("AI_PROXY_DATABASE_URL", "AI_PROXY_AUTH_DATABASE_URL", "AI_PROXY_BACKENDS", "AI_PROXY_MODELS", "AI_PROXY_AGENTS"):
        monkeypatch.delenv(name, raising=False)
    database = _postgres_database()
    if database:
        monkeypatch.setenv("AI_PROXY_DATABASE_URL", database[1])
    before = {path: _stamp(path) for path in REAL_DATABASE}
    secrets = {path: path.exists() for path in SECRETS}
    try:
        yield
    finally:
        if database:
            _drop(*database)
    touched = [path for path, stamp in before.items() if _stamp(path) != stamp]
    created = [path for path, existed in secrets.items() if not existed and path.exists()]
    for path in created:
        path.unlink()
    assert not touched, f"The test used the real database ({', '.join(str(Path(item).relative_to(ROOT)) for item in touched)})"
    assert not created, f"The test created {', '.join(str(Path(item).relative_to(ROOT)) for item in created)}"


def _stamp(path: Path) -> tuple[float, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime, stat.st_size


def _postgres_database() -> tuple[str, str] | None:
    """A new, empty database on the test PostgreSQL server: its name and URL."""
    url = os.getenv("AI_PROXY_TEST_DATABASE_URL")
    if not url:
        return None
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    name = "agent_test_" + uuid.uuid4().hex[:12]
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine.dispose()
    return name, make_url(url).set(database=name).render_as_string(hide_password=False)


def _drop(name: str, _: str) -> None:
    from sqlalchemy import create_engine, text

    engine = create_engine(os.environ["AI_PROXY_TEST_DATABASE_URL"], isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    engine.dispose()
