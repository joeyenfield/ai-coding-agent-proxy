"""agent-server config ... and agent-server db ...: the stored settings and the database, from the server's shell."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config_io
from .config import Settings
from .db import migrate, redacted
from .db import legacy


def main(kind: str, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"agent-server {kind}")
    commands = parser.add_subparsers(dest="action", required=True)
    if kind == "config":
        parser.description = "The server's settings (network, backends, model tuning, agents) as one YAML document."
        export = commands.add_parser("export", help="Write every setting as YAML (to a file, or to standard output)")
        export.add_argument("file", nargs="?")
        load = commands.add_parser("import", help="Store a YAML (or JSON) settings document: only the parts it has, unless --replace")
        load.add_argument("file", help="The document, or - for standard input")
        load.add_argument("--replace", action="store_true", help="Every part: what the document leaves out goes back to empty")
    else:
        parser.description = "The database everything is kept in (AI_PROXY_DATABASE_URL; without it a SQLite file in logs/)."
        commands.add_parser("info", help="Which database, its version, size and what's in it")
        commands.add_parser("migrate", help="Bring the tables up to this version (agent-server does it on start too)")
        load = commands.add_parser("import", help="Import an older install's files (config/, users.json, logs/sessions, ...)")
        load.add_argument("--from", dest="root", help="The old install's folder (holding config/, certs/ and logs/); without it, this install's")
    args = parser.parse_args(argv)
    try:
        if kind == "config" and args.action == "export":
            text = config_io.dump(config_io.export(Settings.load()))
            if args.file:
                Path(args.file).write_text(text, encoding="utf-8")
                print(f"Wrote the settings to {args.file}.", file=sys.stderr)
            else:
                sys.stdout.write(text)
        elif kind == "config":
            text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
            changed = config_io.apply(Settings.load(), config_io.parse(text), replace=args.replace)
            print(f"Stored {', '.join(changed) or 'nothing'}. A running agent-server picks up the listen address on its next restart.")
        elif args.action == "info":
            settings = Settings.load()
            db = settings.open_database()
            sessions = len(db_sessions(db))
            print(f"Database: {redacted(db.url)}")
            print(f"Version: {migrate.version(db)} (this agent-server's: {migrate.latest()})")
            size = db.size()
            print(f"Size: {size / 1e6:.1f} MB" if size is not None else "Size: unknown")
            print(f"Sessions: {sessions}")
            print(f"Recordings kept: {f'{settings.retention_days} days' if settings.retention_days else 'forever'}")
        elif args.action == "migrate":
            settings = Settings.load(database=False)
            from .db import Database

            db = Database(settings.database_address)
            print(f"{redacted(db.url)} is at version {migrate.migrate(db)}.")
        else:
            settings = Settings.load()
            db = settings.open_database()
            root = Path(args.root).resolve() if args.root else None
            with db.write() as connection:
                done = legacy.import_settings(connection, settings, root)
            sessions = legacy.import_sessions(db, (root / "logs" / "sessions") if root else settings.log_dir / "sessions")
            print(f"Imported {', '.join(done) or 'no settings'} and {sessions} sessions. Restart agent-server to use them.")
    except (OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def db_sessions(db: object) -> list[str]:
    from sqlalchemy import select

    from .db import schema

    with db.read() as connection:  # type: ignore[attr-defined]
        return list(connection.execute(select(schema.sessions.c.id)).scalars())
